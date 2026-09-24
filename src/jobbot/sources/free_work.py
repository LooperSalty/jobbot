"""Free-Work (IT jobs board): public API Platform JSON API — junior CDI track + Luxembourg track.

Search listings already carry the full description. Apply is EXTERNAL: ``applicationType == "url"`` gives
the company ATS link; ``"turnover"`` means applying on Free-Work with an account (human, offer page).
Senior/expert offers are dropped at the source (junior track). A foreign search location (the profile's
Luxembourg area, CDI only) is searched country-wide with the ``<iso>~~~`` location key (``lu~~~``).
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
from jobbot.sources.countries import is_foreign, location_country
from jobbot.textutil import extract_emails, html_to_text, slugify, stable_id

log = logging.getLogger(__name__)

API_URL = "https://api.free-work.com/job_postings"
SITE_URL = "https://www.free-work.com"
MIN_INTERVAL_S = 1.0
MAX_PER_PAGE = 100
MAX_PAGES = 5
LUXEMBOURG_KEY = "lu~~~"
_HEADERS = {"Accept": "application/ld+json", "X-Varnish-Public": "1"}
_SENIOR_LEVELS = frozenset({"senior", "expert"})
_REMOTE_MAP = {"full": True, "partial": False, "none": False}
_CONTRACT_PARAM = {
    ContractType.ALTERNANCE: "apprenticeship", ContractType.CDI: "permanent", ContractType.CDD: "fixed-term",
    ContractType.FREELANCE: "contractor", ContractType.STAGE: "internship",
}
_CONTRACT_PRIORITY = (
    ("apprenticeship", ContractType.ALTERNANCE), ("permanent", ContractType.CDI), ("fixed-term", ContractType.CDD),
    ("internship", ContractType.STAGE), ("contractor", ContractType.FREELANCE),
)
_CONTRACT_WORDS = re.compile(
    r"\b(alternance|alternant(e|s|es)?|apprentissage|apprenti(e|s|es)?|cdi|cdd|stage|stagiaire|freelance)\b", re.I
)
_PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError)

# Free-Work location keys look like "fr~grand-est~moselle~metz" (region / department / city slugs).
_REGIONS = {
    "Auvergne-Rhône-Alpes": "01 Ain|03 Allier|07 Ardèche|15 Cantal|26 Drôme|38 Isère|42 Loire|43 Haute-Loire|"
                            "63 Puy-de-Dôme|69 Rhône|73 Savoie|74 Haute-Savoie",
    "Bourgogne-Franche-Comté": "21 Côte-d'Or|25 Doubs|39 Jura|58 Nièvre|70 Haute-Saône|71 Saône-et-Loire|89 Yonne|"
                               "90 Territoire de Belfort",
    "Bretagne": "22 Côtes-d'Armor|29 Finistère|35 Ille-et-Vilaine|56 Morbihan",
    "Centre-Val de Loire": "18 Cher|28 Eure-et-Loir|36 Indre|37 Indre-et-Loire|41 Loir-et-Cher|45 Loiret",
    "Corse": "2A Corse-du-Sud|2B Haute-Corse",
    "Grand Est": "08 Ardennes|10 Aube|51 Marne|52 Haute-Marne|54 Meurthe-et-Moselle|55 Meuse|57 Moselle|"
                 "67 Bas-Rhin|68 Haut-Rhin|88 Vosges",
    "Hauts-de-France": "02 Aisne|59 Nord|60 Oise|62 Pas-de-Calais|80 Somme",
    "Île-de-France": "75 Paris|77 Seine-et-Marne|78 Yvelines|91 Essonne|92 Hauts-de-Seine|93 Seine-Saint-Denis|"
                     "94 Val-de-Marne|95 Val-d'Oise",
    "Normandie": "14 Calvados|27 Eure|50 Manche|61 Orne|76 Seine-Maritime",
    "Nouvelle-Aquitaine": "16 Charente|17 Charente-Maritime|19 Corrèze|23 Creuse|24 Dordogne|33 Gironde|40 Landes|"
                          "47 Lot-et-Garonne|64 Pyrénées-Atlantiques|79 Deux-Sèvres|86 Vienne|87 Haute-Vienne",
    "Occitanie": "09 Ariège|11 Aude|12 Aveyron|30 Gard|31 Haute-Garonne|32 Gers|34 Hérault|46 Lot|48 Lozère|"
                 "65 Hautes-Pyrénées|66 Pyrénées-Orientales|81 Tarn|82 Tarn-et-Garonne",
    "Pays de la Loire": "44 Loire-Atlantique|49 Maine-et-Loire|53 Mayenne|72 Sarthe|85 Vendée",
    "Provence-Alpes-Côte d'Azur": "04 Alpes-de-Haute-Provence|05 Hautes-Alpes|06 Alpes-Maritimes|"
                                  "13 Bouches-du-Rhône|83 Var|84 Vaucluse",
}
_DEPARTMENTS: dict[str, tuple[str, str]] = {
    entry.split(" ", 1)[0]: (region, entry.split(" ", 1)[1])
    for region, entries in _REGIONS.items()
    for entry in entries.split("|")
}


class FreeWorkSource:
    """Read-only Free-Work source."""

    name = "free_work"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client
        self._throttle = HostThrottle(MIN_INTERVAL_S)

    def available(self) -> tuple[bool, str]:
        return True, ""

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        if query.contract_types and not any(c in _CONTRACT_PARAM for c in query.contract_types):
            log.info("Free-Work : aucun type de contrat compatible (%s), requête ignorée",
                     ", ".join(c.value for c in query.contract_types))
            return
        found_at, seen, yielded = clock.now(), set(), 0
        key, radius = _location_param(query)
        for member in self._iter_members(query, key, radius):
            job = _member_or_none(member, query, found_at)
            if job is None or job.external_id in seen:
                continue
            seen.add(job.external_id)
            yield job
            yielded += 1
            if yielded >= query.max_results:
                return

    def fetch_details(self, job: JobOffer) -> JobOffer:
        slug = job.raw.get("slug") or job.external_id
        params = {"jobSlug": job.raw["job_slug"]} if job.raw.get("job_slug") else None
        try:
            resp = request_with_retry(self._http(), "GET", f"{API_URL}/{slug}", params=params,
                                      headers={"Accept": "application/json"}, throttle=self._throttle, retries=1)
            resp.raise_for_status()
            fresh = _member_to_job(resp.json(), wanted=(job.contract_type,), found_at=job.found_at)
        except (httpx.HTTPError, *_PARSE_ERRORS) as exc:
            log.info("Free-Work : détail indisponible pour %s (%s)", job.url, exc)
            return job
        return _merge_fresh(job, fresh)

    # ------------------------------------------------------------------ HTTP

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client()
        return self._client

    def _iter_members(self, query: SearchQuery, key: str | None, radius: int | None) -> Iterator[dict]:
        per_page = max(1, min(query.max_results, MAX_PER_PAGE))
        for page in range(1, MAX_PAGES + 1):
            try:
                data = self._get_page(_build_params(query, page, per_page, key, radius))
            except SourceUnavailable:
                if page == 1:
                    raise
                log.warning("Free-Work : page %d (%s) indisponible, on continue", page, key or "France", exc_info=True)
                return
            members = [m for m in data.get("hydra:member") or [] if isinstance(m, dict)]
            yield from members
            if not (data.get("hydra:view") or {}).get("hydra:next") or len(members) < per_page:
                return

    def _get_page(self, params: dict) -> dict:
        try:
            resp = request_with_retry(self._http(), "GET", API_URL, params=params, headers=_HEADERS,
                                      throttle=self._throttle)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"Free-Work injoignable : {exc}") from exc
        if resp.status_code != 200:
            raise SourceUnavailable(f"Free-Work : erreur HTTP {resp.status_code} sur la recherche.")
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceUnavailable("Free-Work : réponse JSON illisible.") from exc
        if not isinstance(data, dict):
            raise SourceUnavailable("Free-Work : format de réponse inattendu.")
        return data


# ---------------------------------------------------------------------- query building


def _location_param(query: SearchQuery) -> tuple[str | None, int | None]:
    """(``locationKeys``, ``locationRadius``) — no implicit Luxembourg pass: the profile's LU area has its queries."""
    loc = query.location
    if query.remote or loc is None:
        return None, None
    key = _location_key(loc)
    if key is None:
        log.warning("Free-Work : lieu « %s » non reconnu (code postal manquant ?), recherche sans filtre de lieu",
                    loc.city)
        return None, None
    return key, (None if key.endswith("~~~") else loc.radius_km)  # a country key is searched country-wide


def _location_key(loc: LocationPref) -> str | None:
    postal = (loc.postal_code or "").strip()
    if is_foreign(loc):
        return f"{location_country(loc).lower()}~~~"
    if re.fullmatch(r"\d{4}", postal):
        return LUXEMBOURG_KEY  # Luxembourg postal code (older profiles without ``country``)
    info = _DEPARTMENTS.get(_department_code(postal) or "")
    if info is None:
        return None
    region, department = info
    return f"fr~{slugify(region)}~{slugify(department)}~{slugify(loc.city)}"


def _department_code(postal: str) -> str | None:
    if not re.fullmatch(r"\d{5}", postal):
        return None
    if postal.startswith("20"):
        return "2A" if int(postal) < 20200 else "2B"
    return postal[:2]


def _build_params(query: SearchQuery, page: int, per_page: int, key: str | None, radius: int | None) -> dict:
    params: dict = {"page": page, "itemsPerPage": per_page}
    text = _CONTRACT_WORDS.sub(" ", query.keywords) if query.contract_types else query.keywords
    keywords = " ".join(text.split())
    if keywords:
        params["searchKeywords"] = keywords
    contracts = sorted({_CONTRACT_PARAM[c] for c in query.contract_types if c in _CONTRACT_PARAM})
    if contracts:
        params["contracts"] = ",".join(contracts)
    if key:
        params["locationKeys"] = key
        if radius:
            params["locationRadius"] = radius
    return params


# ---------------------------------------------------------------------- mapping


def _member_or_none(member: dict, query: SearchQuery, found_at: datetime) -> JobOffer | None:
    if (member.get("experienceLevel") or "") in _SENIOR_LEVELS:
        log.debug("Free-Work : offre %s ignorée (niveau %s)", member.get("slug"), member.get("experienceLevel"))
        return None
    if query.remote and member.get("remoteMode") != "full":
        return None
    try:
        job = _member_to_job(member, wanted=tuple(query.contract_types), found_at=found_at)
    except _PARSE_ERRORS as exc:
        log.warning("Free-Work : offre ignorée (format inattendu : %s)", exc)
        return None
    if job.published_at and job.published_at < found_at - timedelta(days=query.max_age_days):
        return None
    return job


def _member_to_job(member: dict, *, wanted: tuple[ContractType, ...] = (),
                   found_at: datetime | None = None) -> JobOffer:
    slug = str(member["slug"])
    job_slug = (member.get("job") or {}).get("slug") or ""
    loc = member.get("location") or {}
    return JobOffer(
        id=stable_id("free_work", slug), source="free_work", external_id=slug,
        title=" ".join(str(member["title"]).split()), company=(member.get("company") or {}).get("name") or "",
        location=_location_label(loc), postal_code=loc.get("postalCode") or None,
        latitude=_float(loc.get("latitude")), longitude=_float(loc.get("longitude")),
        description=_description(member), url=_offer_url(slug, job_slug), apply_url=_apply_url(member),
        apply_method=ApplyMethod.EXTERNAL, contract_type=_pick_contract(member.get("contracts") or [], wanted),
        remote=_REMOTE_MAP.get(member.get("remoteMode") or ""), salary=_salary(member),
        contact_email=_contact_email(member.get("applicationContact")), published_at=_parse(member.get("publishedAt")),
        found_at=found_at or clock.now(), raw=_raw(member, job_slug),
    )


def _merge_fresh(job: JobOffer, fresh: JobOffer) -> JobOffer:
    """Refresh from the detail call; alert-mail offers (title/company unknown) get every field filled."""
    fill = ("title", "company", "location", "postal_code", "latitude", "longitude", "published_at", "salary",
            "contact_email", "remote")
    update = {name: getattr(job, name) or getattr(fresh, name) for name in fill}
    update["apply_url"] = fresh.apply_url or job.apply_url
    update["description"] = fresh.description or job.description
    if job.contract_type == ContractType.OTHER:
        update["contract_type"] = fresh.contract_type
    return job.model_copy(update={**update, "raw": {**job.raw, **fresh.raw}})


def _pick_contract(contracts: list[str], wanted: tuple[ContractType, ...]) -> ContractType:
    """Offers often list several contracts (["contractor", "permanent"]): prefer the one searched for."""
    offered = [ct for value, ct in _CONTRACT_PRIORITY if value in contracts]
    return next((ct for ct in offered if ct in wanted), offered[0] if offered else ContractType.OTHER)


def _offer_url(slug: str, job_slug: str) -> str:
    return f"{SITE_URL}/fr/tech-it/job-mission/{job_slug}/{slug}" if job_slug else f"{SITE_URL}/fr/tech-it/{slug}"


def _location_label(loc: dict) -> str:
    if loc.get("countryCode") in (None, "FR"):
        return loc.get("shortLabel") or loc.get("label") or loc.get("locality") or ""
    return ", ".join(p for p in (loc.get("locality") or loc.get("label"), loc.get("country")) if p)


def _description(member: dict) -> str:
    parts = [html_to_text(member.get("description") or "")]
    if member.get("candidateProfile"):
        parts.append("Profil recherché :\n" + html_to_text(member["candidateProfile"]))
    if member.get("companyDescription"):
        parts.append("À propos de l'entreprise :\n" + html_to_text(member["companyDescription"]))
    return "\n\n".join(p for p in parts if p).strip()


def _apply_url(member: dict) -> str | None:
    if member.get("applicationType") != "url":
        return None
    return member.get("applicationUrl") or None


def _contact_email(contact: object) -> str | None:
    if isinstance(contact, dict):
        contact = contact.get("email") or ""
    emails = extract_emails(contact) if isinstance(contact, str) else []
    return emails[0] if emails else None


def _salary(member: dict) -> str | None:
    parts = []
    if member.get("annualSalary"):
        parts.append(f"{member['annualSalary']} / an")
    if member.get("dailySalary"):
        parts.append(f"{member['dailySalary']} / jour")
    return " ; ".join(parts) or None


def _raw(member: dict, job_slug: str) -> dict:
    return {
        "id": member.get("id"), "slug": member.get("slug"), "job_slug": job_slug,
        "contracts": member.get("contracts") or [], "experience_level": member.get("experienceLevel"),
        "remote_mode": member.get("remoteMode"), "application_type": member.get("applicationType"),
        "expired_at": member.get("expiredAt"), "starts_at": member.get("startsAt"),
        "duration": {"value": member.get("durationValue") or member.get("duration"),
                     "period": member.get("durationPeriod")},
        "company_slug": (member.get("company") or {}).get("slug"),
    }


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
