"""APEC (cadres / jeunes diplômés, CDI track): open search endpoint only.

The offer detail endpoint is protected by DataDome: jobbot never calls it and never tries to get around it
(CNIL guidance on scraping). Offers keep the ≈250-char search snippet (``raw["description_truncated"]``)
and link to the public offer page, where a human applies (ApplyMethod.EXTERNAL).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime, timedelta

import httpx

from jobbot import clock
from jobbot.config import LocationPref
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import ApplyMethod, ContractType, JobOffer
from jobbot.sources.base import SearchQuery, SourceUnavailable
from jobbot.textutil import normalize, stable_id

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.apec.fr/cms/webservices/rechercheOffre"
LIEU_URL = "https://www.apec.fr/cms/webservices/autocompletion/lieuautocomplete"
OFFER_URL = "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{}"
MIN_INTERVAL_S = 2.0
PAGE_SIZE = 20  # front-end default
MAX_PAGES = 5
_CONTRACT_CODES: dict[ContractType, tuple[str, ...]] = {
    ContractType.ALTERNANCE: ("20053", "597137", "597139", "597138", "597140"),
    ContractType.CDI: ("101888",),
    ContractType.CDD: ("101887",),
    ContractType.STAGE: ("597171",),
    ContractType.INTERIM: ("101930", "101889", "597141"),
}
_CODE_TO_CONTRACT = {code: ct for ct, codes in _CONTRACT_CODES.items() for code in codes}
_CONVENTIONS = ("143684", "143685", "143686", "143687")  # entreprise, cabinet, agence, ESN
_EMPTY_LISTS = ("fonctions", "statutPoste", "niveauxExperience", "idsEtablissement", "secteursActivite",
                "typesTeletravail", "idNomZonesDeplacement", "positionNumbersExcluded")
_HEADERS = {"Accept": "application/json", "Origin": "https://www.apec.fr",
            "Referer": "https://www.apec.fr/candidat/recherche-emploi.html/emploi"}
_BLOCK_MARKERS = ("captcha-delivery", "datadome", "just a moment", "cf-mitigated")
# "développeur" also matches real-estate / sales jobs on APEC
_NOT_IT_TITLE = re.compile(r"\b(foncier|fonci[èe]re|immobili\w*|commercial\w*|business dev\w*|sales)\b", re.I)
_FULL_REMOTE = re.compile(
    r"100\s?%\s?(en\s)?t[ée]l[ée]travail|full remote|t[ée]l[ée]travail (complet|total|int[ée]gral)", re.I
)
_CONTRACT_WORDS = re.compile(
    r"\b(alternance|alternant(e|s|es)?|apprentissage|apprenti(e|s|es)?|cdi|cdd|stage|stagiaire|freelance)\b", re.I
)
_PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError)


class ApecSource:
    """Read-only APEC source (search endpoint only)."""

    name = "apec"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client
        self._throttle = HostThrottle(MIN_INTERVAL_S)
        self._places: dict[tuple[str, str], str | None] = {}
        self._blocked: str | None = None

    def available(self) -> tuple[bool, str]:
        return (False, self._blocked) if self._blocked else (True, "")

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        if not _searchable(query):
            return
        if self._blocked:
            raise SourceUnavailable(self._blocked)
        lieux, geo = self._place(query.location)
        found_at = clock.now()
        cutoff = found_at - timedelta(days=query.max_age_days)
        for count, job in enumerate(self._iter_jobs(query, lieux, geo, found_at), start=1):
            if job.published_at and job.published_at < cutoff:
                return  # results are sorted by date, newest first
            yield job
            if count >= query.max_results:
                return

    def fetch_details(self, job: JobOffer) -> JobOffer:
        log.debug("APEC : détail protégé (DataDome), description tronquée conservée pour %s", job.url)
        return job

    def _iter_jobs(self, query: SearchQuery, lieux: list[str], geo: tuple[float, float, int] | None,
                   found_at: datetime) -> Iterator[JobOffer]:
        """Offers page by page; the next page is only requested if the consumer asks for more."""
        for page in range(MAX_PAGES):
            data = self._post(_build_body(query, lieux, geo, page * PAGE_SIZE, PAGE_SIZE), first=page == 0)
            if data is None:
                return
            results = [r for r in data.get("resultats") or [] if isinstance(r, dict)]
            yield from (job for r in results if (job := _result_or_none(r, found_at)) is not None)
            if len(results) < PAGE_SIZE or (page + 1) * PAGE_SIZE >= int(data.get("totalCount") or 0):
                return

    # ------------------------------------------------------------------ HTTP

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client()
        return self._client

    def _post(self, body: dict, *, first: bool) -> dict | None:
        try:
            resp = request_with_retry(self._http(), "POST", SEARCH_URL, json=body, headers=_HEADERS,
                                      throttle=self._throttle)
        except httpx.HTTPError as exc:
            return self._fail(f"APEC injoignable : {exc}", first)
        if _is_blocked(resp):
            self._blocked = ("APEC bloque les requêtes automatiques (DataDome) : source suspendue pour cette "
                             "exécution, réessaie plus tard.")
            raise SourceUnavailable(self._blocked)
        if resp.status_code != 200:
            return self._fail(f"APEC : erreur HTTP {resp.status_code} sur la recherche.", first)
        try:
            data = resp.json()
        except ValueError:
            return self._fail("APEC : réponse JSON illisible.", first)
        return data if isinstance(data, dict) else self._fail("APEC : format de réponse inattendu.", first)

    @staticmethod
    def _fail(message: str, first: bool) -> None:
        if first:
            raise SourceUnavailable(message)
        log.warning("%s Pagination interrompue.", message)
        return None

    def _place(self, loc: LocationPref | None) -> tuple[list[str], tuple[float, float, int] | None]:
        """Commune id + radius when coordinates are known (a radius needs lat/lon), else the department."""
        if loc is None:
            return [], None
        dept = _department(loc.postal_code or "")
        if loc.latitude is not None and loc.longitude is not None and loc.city:
            lieu_id = self._commune_id(loc.city, dept or "")
            if lieu_id:
                return [lieu_id], (loc.latitude, loc.longitude, loc.radius_km)
        if not dept:
            log.warning("APEC : lieu « %s » sans code postal, recherche France entière", loc.city)
        return ([dept] if dept else []), None

    def _commune_id(self, city: str, dept: str) -> str | None:
        key = (normalize(city), dept)
        if key not in self._places:
            try:
                resp = request_with_retry(self._http(), "GET", LIEU_URL, headers={"Accept": "application/json"},
                                          params={"q": city, "max": 5, "byLeftAndRight": "false"},
                                          throttle=self._throttle, retries=1)
                resp.raise_for_status()
                candidates = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.info("APEC : lieu « %s » introuvable (%s), recherche par département", city, exc)
                candidates = []
            self._places[key] = _pick_commune(candidates, city, dept)
        return self._places[key]


# ---------------------------------------------------------------------- helpers


def _build_body(query: SearchQuery, lieux: list[str], geo: tuple[float, float, int] | None,
                start: int, size: int) -> dict:
    text = _CONTRACT_WORDS.sub(" ", query.keywords) if query.contract_types else query.keywords
    body: dict = {
        "motsCles": " ".join(text.split()), "lieux": list(lieux), "typesContrat": _contract_codes(query),
        "typesConvention": list(_CONVENTIONS), **{name: [] for name in _EMPTY_LISTS},
        "typeClient": "CADRE", "sorts": [{"type": "DATE", "direction": "DESCENDING"}],
        "pagination": {"range": size, "startIndex": start}, "activeFiltre": True,
        "pointGeolocDeReference": {"distance": 0},
    }
    if geo:  # a radius without lat/lon silently returns the whole of France
        lat, lon, km = geo
        body["pointGeolocDeReference"] = {"distance": km, "latitude": lat, "longitude": lon}
    return body


def _searchable(query: SearchQuery) -> bool:
    if query.remote:
        log.info("APEC : pas de filtre télétravail fiable, requête télétravail ignorée")
        return False
    if query.contract_types and not _contract_codes(query):
        log.info("APEC : aucun type de contrat compatible, requête ignorée")
        return False
    return True


def _contract_codes(query: SearchQuery) -> list[str]:
    codes = (code for ct in query.contract_types for code in _CONTRACT_CODES.get(ct, ()))
    return list(dict.fromkeys(codes))


def _department(postal: str) -> str | None:
    postal = postal.strip()
    if not re.fullmatch(r"\d{5}", postal):
        return None
    if postal.startswith("20"):
        return "2A" if int(postal) < 20200 else "2B"
    return postal[:3] if postal.startswith("97") else postal[:2]


def _pick_commune(candidates: object, city: str, dept: str) -> str | None:
    for cand in candidates if isinstance(candidates, list) else []:
        if not isinstance(cand, dict) or cand.get("lieuType") != "FR_COMMUNE" or cand.get("lieuId") is None:
            continue
        name = str(cand.get("lieuDisplay") or "").rsplit(" - ", 1)[0]
        if normalize(name) == normalize(city) and (not dept or dept in (cand.get("lieuxParentsIds") or [])):
            return str(cand["lieuId"])
    return None


def _is_blocked(resp: httpx.Response) -> bool:
    if resp.status_code not in (403, 429):
        return False
    return bool(resp.headers.get("x-datadome")) or any(m in resp.text.lower() for m in _BLOCK_MARKERS)


def _result_or_none(result: dict, found_at: datetime) -> JobOffer | None:
    title = str(result.get("intitule") or "")
    if _NOT_IT_TITLE.search(title):
        log.debug("APEC : offre hors informatique ignorée (%s)", title)
        return None
    try:
        return _result_to_job(result, found_at=found_at)
    except _PARSE_ERRORS as exc:
        log.warning("APEC : offre ignorée (format inattendu : %s)", exc)
        return None


def _result_to_job(result: dict, *, found_at: datetime | None = None) -> JobOffer:
    numero = str(result["numeroOffre"])
    text = " ".join(str(result.get("texteOffre") or "").split())
    return JobOffer(
        id=stable_id("apec", numero), source="apec", external_id=numero,
        title=" ".join(str(result["intitule"]).split()), company=result.get("nomCommercial") or "",
        location=_location_label(result.get("lieuTexte") or ""),
        latitude=_float(result.get("latitude")), longitude=_float(result.get("longitude")),
        description=text, url=OFFER_URL.format(numero), apply_method=ApplyMethod.EXTERNAL,
        contract_type=_CODE_TO_CONTRACT.get(str(result.get("typeContrat")), ContractType.OTHER),
        remote=True if _FULL_REMOTE.search(text) else None, salary=result.get("salaireTexte") or None,
        published_at=_parse(result.get("datePublication")), found_at=found_at or clock.now(),
        raw={
            "numero_offre": numero, "apec_id": result.get("id"), "type_contrat": result.get("typeContrat"),
            "secteur_activite": result.get("secteurActivite"), "confidentielle": result.get("offreConfidentielle"),
            "description_truncated": True,
        },
    )


def _location_label(text: str) -> str:
    """Turn "Metz - 57" into "Metz (57)" (same style as the other sources)."""
    city, sep, dept = text.strip().rpartition(" - ")
    dept = dept.strip()
    if sep and city.strip() and 2 <= len(dept) <= 3 and dept.isalnum():
        return f"{city.strip()} ({dept})"
    return text.strip()


def _float(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse(value: object) -> datetime | None:
    try:
        return clock.parse_dt(value) if isinstance(value, str) else None
    except ValueError:
        return None
