"""France Travail « Offres d'emploi v2 » (francetravail.io, OAuth2 client_credentials).

Field names follow the official OpenAPI spec (docs/research/specs/francetravail-offres-emploi-v2.openapi.json).
Key rules (docs/research/france-travail.md): alternance = ``natureContrat=E2,FS`` and never combined with
``typeContrat`` (the API ORs them); 206 = more pages (Content-Range ``offres p-d/total``); 204 = nothing;
``contact.courriel`` is a sentence + link, never trusted as an e-mail unless it really is one.
A foreign search location (Luxembourg track) is searched with ``paysContinent``, whose code is resolved at
runtime from ``/referentiel/pays`` (by libellé) and cached for the source's lifetime.
Pure helpers and the offer mapping live in ``france_travail_mapping`` (re-exported here).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any, NamedTuple

import httpx
from pydantic import ValidationError

from jobbot.config import LocationPref, Secrets
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import JobOffer
from jobbot.sources.base import SearchQuery, SourceUnavailable
from jobbot.sources.countries import country_name, is_foreign, location_country
from jobbot.sources.france_travail_mapping import (  # noqa: F401  (re-exported public helpers)
    CONTRACT_WORDS,
    NATURE_ALTERNANCE,
    OFFER_PAGE_URL,
    SOURCE,
    apply_route,
    build_description,
    contract_filters,
    department_from_postal,
    detect_remote,
    domain_of,
    expire_job,
    is_alternance,
    keywords_param,
    method_for_url,
    offer_to_job,
    parse_content_range,
    pick_commune,
    published_since,
    usable_email,
)
from jobbot.textutil import normalize

log = logging.getLogger(__name__)

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
TOKEN_PARAMS = {"realm": "/partenaire"}  # must be in the query string
SCOPE = "api_offresdemploiv2 o2dsoffre"
API_BASE = "https://api.francetravail.io/partenaire/offresdemploi/v2"
SEARCH_URL = f"{API_BASE}/offres/search"
DETAIL_URL = f"{API_BASE}/offres/{{id}}"
COUNTRIES_URL = f"{API_BASE}/referentiel/pays"
GEO_COMMUNES_URL = "https://geo.api.gouv.fr/communes"

PAGE_SIZE = 150  # max per call
MAX_START = 3000  # range start <= 3000
MAX_END = 3149  # range end <= 3149
TOKEN_SAFETY_S = 60  # token lives ~1499 s; refresh a minute early
REQUEST_INTERVAL_S = 0.15  # official quota: 10 calls/s (bronze tier)
# ROME 4.0 (v61, June 2026) developer family — keep <= 20 codes per request.
ROME_DEV_CODES = (
    "M1805", "M1855", "M1861", "M1818", "M1827", "M1841",
    "M1848", "M1852", "M1836", "M1813", "M1821", "M1837",
)
DEFAULT_DEPARTMENTS = ("57", "54")  # Moselle, Meurthe-et-Moselle (+ inclureLimitrophes)


class _Plan(NamedTuple):
    params: dict[str, str]
    exclude_alternance: bool  # typeContrat queries: drop alternance offers locally
    fallback: dict[str, str] | None  # same query by department when the commune search fails


class _BadRequest(RuntimeError):
    pass


class FranceTravailSource:
    name = SOURCE

    def __init__(self, secrets: Secrets, client: httpx.Client | None = None):
        self._client_id = secrets.france_travail_client_id or ""
        self._client_secret = secrets.france_travail_client_secret or ""
        self._client = client
        self._throttle = HostThrottle(REQUEST_INTERVAL_S)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._communes: dict[tuple[str, str], str | None] = {}
        self._countries: list[dict] | None = None  # « référentiel pays » ({code, libelle})
        self._country_codes: dict[str, str | None] = {}  # ISO code → paysContinent code

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(headers={"Accept": "application/json"})
        return self._client

    def available(self) -> tuple[bool, str]:
        if self._client_id and self._client_secret:
            return True, ""
        return False, (
            "Identifiants France Travail absents (FRANCE_TRAVAIL_CLIENT_ID / FRANCE_TRAVAIL_CLIENT_SECRET) — "
            "créer une application gratuite sur francetravail.io et l'abonner à « Offres d'emploi v2 »"
        )

    # ------------------------------------------------------------------ auth & HTTP

    def _fetch_token(self) -> None:
        data = {"grant_type": "client_credentials", "client_id": self._client_id,
                "client_secret": self._client_secret, "scope": SCOPE}
        resp = request_with_retry(self.client, "POST", TOKEN_URL, params=TOKEN_PARAMS, data=data, retries=2)
        body = _json(resp)
        token = body.get("access_token") if resp.status_code == 200 else None
        if not token:
            error = body.get("error") or f"HTTP {resp.status_code}"
            raise SourceUnavailable(
                f"Authentification France Travail refusée ({error}) — vérifier FRANCE_TRAVAIL_CLIENT_ID / SECRET"
            )
        self._token = str(token)
        self._token_expires_at = time.monotonic() + int(body.get("expires_in") or 1499) - TOKEN_SAFETY_S

    def _auth_headers(self, force: bool = False) -> dict[str, str]:
        if force or self._token is None or time.monotonic() >= self._token_expires_at:
            self._fetch_token()
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _api_get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        """GET with the bearer token; on 401 refresh the token and retry once (official guidance)."""
        resp = request_with_retry(self.client, "GET", url, params=params, headers=self._auth_headers(),
                                  throttle=self._throttle)
        if resp.status_code == 401:
            resp = request_with_retry(self.client, "GET", url, params=params,
                                      headers=self._auth_headers(force=True), throttle=self._throttle)
        if resp.status_code in (401, 403):
            raise SourceUnavailable(
                f"France Travail refuse l'accès (HTTP {resp.status_code}) — "
                "vérifier l'abonnement à l'API Offres d'emploi"
            )
        return resp

    # ------------------------------------------------------------------ search

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        ok, reason = self.available()
        if not ok:
            raise SourceUnavailable(reason)
        if query.max_results <= 0:
            return
        seen: set[str] = set()
        count = 0
        for plan in self._plans(query):
            for offre in self._run_plan(plan):
                job = self._accept(offre, plan, query, seen)
                if job is None:
                    continue
                yield job
                count += 1
                if count >= query.max_results:
                    return

    def _accept(self, offre: dict, plan: _Plan, query: SearchQuery, seen: set[str]) -> JobOffer | None:
        ext_id = str(offre.get("id") or "")
        if not ext_id or ext_id in seen or (plan.exclude_alternance and is_alternance(offre)):
            return None
        seen.add(ext_id)
        try:
            job = offer_to_job(offre)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            log.warning("Offre France Travail %s ignorée (format inattendu) : %s", ext_id, exc)
            return None
        if query.remote and job.remote is not True:
            return None
        return job

    def _plans(self, query: SearchQuery) -> list[_Plan]:
        base = {"sort": "1", "codeROME": ",".join(ROME_DEV_CODES),
                "publieeDepuis": str(published_since(query.max_age_days))}
        words = keywords_param(query.keywords)
        if words:
            base["motsCles"] = words
        located = self._geo_params(query)
        if located is None:
            return []
        geo, fallback = located
        plans = [
            _Plan({**base, **geo, **contract}, exclude, {**base, **fallback, **contract} if fallback else None)
            for contract, exclude in contract_filters(query.contract_types)
        ]
        if not plans:
            log.info("France Travail : aucun type de contrat interrogeable pour « %s »", query.keywords)
        return plans

    def _geo_params(self, query: SearchQuery) -> tuple[dict[str, str], dict[str, str] | None] | None:
        """(params, fallback params); None when the place cannot be searched (unknown foreign country)."""
        default = {"departement": ",".join(DEFAULT_DEPARTMENTS), "inclureLimitrophes": "true"}
        if query.remote:
            return {}, None  # national search, remote detected locally
        loc = query.location
        if loc is None:
            return default, None
        if is_foreign(loc):
            code = self._country_code(location_country(loc))
            return ({"paysContinent": code}, None) if code else None
        dept = department_from_postal(loc.postal_code)
        by_dept = {"departement": dept} if dept else default
        code = self._commune_code(loc)
        if code is None:
            return by_dept, None
        return {"commune": code, "distance": str(max(0, loc.radius_km))}, by_dept

    def _commune_code(self, loc: LocationPref) -> str | None:
        key = (normalize(loc.city), loc.postal_code or "")
        if key not in self._communes:
            self._communes[key] = self._lookup_commune(loc)
        return self._communes[key]

    def _lookup_commune(self, loc: LocationPref) -> str | None:
        """INSEE code via geo.api.gouv.fr (keyless); None → department search."""
        params = {"fields": "code,nom,population", "format": "json"}
        if loc.postal_code:
            params["codePostal"] = loc.postal_code
        else:
            params.update({"nom": loc.city, "boost": "population", "limit": "5"})
        try:
            resp = request_with_retry(self.client, "GET", GEO_COMMUNES_URL, params=params, retries=1)
            communes = resp.json() if resp.status_code == 200 else []
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Code INSEE introuvable pour %s (%s) : recherche par département", loc.city, exc)
            return None
        return pick_commune(communes if isinstance(communes, list) else [], loc.city)

    def _country_code(self, iso: str) -> str | None:
        """``paysContinent`` code of an ISO country; referential fetched once, answer cached per country."""
        if iso not in self._country_codes:
            if self._countries is None:
                self._countries = self._fetch_countries()
            name = country_name(iso)
            self._country_codes[iso] = pick_country(self._countries, name)
            if self._country_codes[iso] is None:
                log.warning("France Travail : pays « %s » introuvable dans le référentiel, recherche ignorée", name)
        return self._country_codes[iso]

    def _fetch_countries(self) -> list[dict]:
        try:
            resp = self._api_get(COUNTRIES_URL)
            items = resp.json() if resp.status_code in (200, 206) else None
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("France Travail : référentiel des pays indisponible (%s)", exc)
            return []
        if not isinstance(items, list):
            log.warning("France Travail : référentiel des pays illisible (HTTP %s)", resp.status_code)
            return []
        return [item for item in items if isinstance(item, dict)]

    def _run_plan(self, plan: _Plan) -> Iterator[dict]:
        try:
            yield from self._paginate(plan.params)
        except _BadRequest as exc:
            if plan.fallback is None:
                log.warning("Recherche France Travail rejetée : %s", exc)
                return
            log.warning("Recherche par commune rejetée (%s) : nouvel essai par département", exc)
            try:
                yield from self._paginate(plan.fallback)
            except _BadRequest as exc2:
                log.warning("Recherche France Travail rejetée : %s", exc2)

    def _paginate(self, params: dict[str, str]) -> Iterator[dict]:
        start = 0
        while start <= MAX_START:
            end = min(start + PAGE_SIZE - 1, MAX_END)
            resp = self._api_get(SEARCH_URL, {**params, "range": f"{start}-{end}"})
            if resp.status_code == 204:
                return
            if resp.status_code == 400:
                raise _BadRequest(str(_json(resp).get("message") or resp.text[:200]))
            if resp.status_code not in (200, 206):
                raise RuntimeError(f"France Travail : HTTP {resp.status_code} sur la recherche d'offres")
            yield from _json(resp).get("resultats") or []
            bounds = parse_content_range(resp.headers.get("Content-Range"))
            if resp.status_code == 200 or bounds is None or bounds[1] + 1 >= bounds[2]:
                return
            start = bounds[1] + 1

    # ------------------------------------------------------------------ details

    def fetch_details(self, job: JobOffer) -> JobOffer:
        if job.source != SOURCE or not job.external_id or not self.available()[0]:
            return job
        try:
            resp = self._api_get(DETAIL_URL.format(id=job.external_id))
            if resp.status_code in (204, 404):
                log.info("Offre France Travail %s retirée : marquée expirée", job.external_id)
                return expire_job(job)
            if resp.status_code != 200:
                log.warning("Détail France Travail %s : HTTP %s", job.external_id, resp.status_code)
                return job
            fresh = offer_to_job(resp.json())
        except (httpx.HTTPError, SourceUnavailable, KeyError, TypeError, ValueError, ValidationError) as exc:
            log.warning("Détail France Travail %s indisponible : %s", job.external_id, exc)
            return job
        return fresh.model_copy(update={
            "id": job.id, "found_at": job.found_at, "status": job.status,
            "match_score": job.match_score, "match_reasons": job.match_reasons,
        })


def pick_country(items: list[Any], name: str) -> str | None:
    """``paysContinent`` code whose libellé is ``name`` (exact, else starting with it), None if absent."""
    wanted = normalize(name)
    entries = [(normalize(str(i.get("libelle") or "")), str(i.get("code") or ""))
               for i in items if isinstance(i, dict)]
    exact = next((code for label, code in entries if label == wanted and code), None)
    return exact or next((code for label, code in entries if label.startswith(wanted) and code), None)


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
