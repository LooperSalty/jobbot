"""La Bonne Alternance: keyless website JSON API (path B) + official API Apprentissage (path A, key).

docs/research/la-bonne-alternance.md: path B ``/api/v1/search`` (strict params, ``admin_area``, 0-based pages)
and ``/api/_private/jobs/{source}/{id}`` (detail: ``contact.hasEmail``, ``recipient_id``, per-offer token);
path A ``/job/v1/search`` (Bearer key, ROME filter, no pagination). LBA never exposes recruiter e-mails.
JSON → model mapping lives in ``lba_mapping`` (re-exported here).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from itertools import chain
from typing import Any

import httpx
from pydantic import ValidationError

from jobbot.config import Secrets
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import ApplyMethod, Company, ContractType, JobOffer, JobStatus
from jobbot.sources.base import SearchQuery
from jobbot.sources.france_travail_mapping import department_from_postal
from jobbot.sources.lba_mapping import (  # noqa: F401  (re-exported public helpers)
    LBA_SITE,
    RECRUITER_SOURCE,
    SOURCE,
    SUB_LBA,
    SUB_PARTNER,
    SUB_RECRUITER,
    api_job_to_job,
    api_recruiter_to_company,
    apply_detail,
    detail_description,
    geo_point,
    hit_to_company,
    hit_to_job,
    merge_company,
    page_url,
    safe_map,
    search_text,
    split_address,
)

log = logging.getLogger(__name__)

SITE_SEARCH_URL = f"{LBA_SITE}/api/v1/search"
SITE_DETAIL_URL = f"{LBA_SITE}/api/_private/jobs/{{sub_type}}/{{id}}"
API_SEARCH_URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"
HITS_PER_PAGE = 100  # server max
MAX_PAGES = 10
MAX_DETAILS_PER_SEARCH = 25  # partner offers enriched through the detail endpoint (5 req/s server side)
SITE_INTERVAL_S = 0.5
API_INTERVAL_S = 1.1  # API Apprentissage search: 60 req/min per key
RATE_LIMIT_STATUSES = (419, 429)
DEFAULT_ADMIN_AREAS = ("departement:57", "departement:54")
DEFAULT_ROMES = ("M1855", "M1805", "M1827", "M1818", "M1836", "M1852", "M1853")
# Path B ignores ROME codes in `q` (live: q=M1855 → 0 hit): search their labels instead.
ROME_SEARCH_TERMS = {
    "M1855": "développeur web", "M1805": "développeur informatique", "M1861": "développeur logiciel",
    "M1827": "ingénieur devops", "M1818": "ingénieur études informatique",
    "M1836": "ingénieur concepteur informatique", "M1852": "analyste informatique",
    "M1853": "chef de projet développement informatique", "M1811": "data engineer",
}
_sleep = time.sleep


# --------------------------------------------------------------------------- source


class LaBonneAlternanceSource:
    name = SOURCE

    def __init__(self, secrets: Secrets, client: httpx.Client | None = None):
        self._api_token = secrets.lba_api_token or None
        self._client = client
        self._site_throttle = HostThrottle(SITE_INTERVAL_S)
        self._api_throttle = HostThrottle(API_INTERVAL_S)
        self._details: dict[tuple[str, str], dict[str, Any]] = {}
        self._api_cache: dict[tuple, dict[str, Any]] = {}
        self._api_disabled = False

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(headers={"Accept": "application/json"})
        return self._client

    def available(self) -> tuple[bool, str]:
        return True, ""  # path B needs no key; path A is added when LBA_API_TOKEN is set

    # ------------------------------------------------------------------ offers

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        if query.max_results <= 0 or (query.contract_types and ContractType.ALTERNANCE not in query.contract_types):
            return  # LBA only lists apprenticeship / professionalisation contracts
        seen: set[str] = set()
        for job in chain(self._api_jobs(query), self._site_jobs(query)):
            if job.id in seen or (query.remote and job.remote is not True):
                continue
            seen.add(job.id)
            yield job
            if len(seen) >= query.max_results:
                return

    def _site_jobs(self, query: SearchQuery) -> Iterator[JobOffer]:
        budget = MAX_DETAILS_PER_SEARCH
        for params in self._site_param_sets(query):
            for hit in self._site_hits(params, query.max_results):
                job = safe_map(hit_to_job, hit)
                if job is None:
                    continue
                if job.raw["source"] == SUB_PARTNER and job.apply_method != ApplyMethod.LBA and budget > 0:
                    budget -= 1
                    job = self.fetch_details(job)  # partner offers: apply URL lives in the detail
                yield job

    def _site_param_sets(self, query: SearchQuery) -> list[list[tuple[str, str]]]:
        per_page = str(min(HITS_PER_PAGE, max(1, query.max_results)))
        base = [("mode", "emplois"), ("is_algo_company", "false"), ("sort", "date"), ("hitsPerPage", per_page)]
        text = search_text(query.keywords)
        if text:
            base.append(("q", text))
        loc = query.location
        if query.remote:
            return [base]
        if loc is not None and loc.latitude is not None and loc.longitude is not None:
            radius = str(min(200, max(1, loc.radius_km)))
            return [base + [("latitude", str(loc.latitude)), ("longitude", str(loc.longitude)), ("radius", radius)]]
        dept = department_from_postal(loc.postal_code) if loc else None
        areas = [f"departement:{dept}"] if dept else list(DEFAULT_ADMIN_AREAS)
        return [base + [("admin_area", area)] for area in areas]

    def _site_hits(self, params: list[tuple[str, str]], limit: int) -> Iterator[dict[str, Any]]:
        received = 0
        for page in range(MAX_PAGES):
            resp = request_with_retry(self.client, "GET", SITE_SEARCH_URL, params=params + [("page", str(page))],
                                      throttle=self._site_throttle)
            if resp.status_code != 200:
                raise RuntimeError(f"La Bonne Alternance : HTTP {resp.status_code} sur la recherche ({resp.text[:150]})")
            data = resp.json()
            hits = data.get("hits") or []
            yield from hits
            received += len(hits)
            if not hits or page + 1 >= int(data.get("nbPages") or 0) or received >= limit:
                return

    def _api_jobs(self, query: SearchQuery) -> Iterator[JobOffer]:
        if not self._api_token or self._api_disabled:
            return
        params: list[tuple[str, str]] = [("romes", ",".join(DEFAULT_ROMES))]
        loc = query.location
        if query.remote:
            pass  # whole France, remote filtered locally
        elif loc is not None and loc.latitude is not None and loc.longitude is not None:
            params += [("latitude", str(loc.latitude)), ("longitude", str(loc.longitude)),
                       ("radius", str(min(200, max(0, loc.radius_km))))]
        else:
            dept = department_from_postal(loc.postal_code) if loc else None
            params += [("departements", d) for d in ([dept] if dept else [a.split(":")[1] for a in DEFAULT_ADMIN_AREAS])]
        for item in self._api_search(params).get("jobs") or []:
            job = safe_map(api_job_to_job, item)
            if job is not None:
                yield job

    def _api_search(self, params: list[tuple[str, str]]) -> dict[str, Any]:
        """Official API call, cached per parameter set (no keyword filter → same answer for every title)."""
        key = tuple(params)
        if key in self._api_cache or self._api_disabled:
            return self._api_cache.get(key, {})
        headers = {"Authorization": f"Bearer {self._api_token}", "Accept": "application/json"}
        try:
            resp = self._api_request(params, headers)
            body = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("API Apprentissage indisponible : %s", exc)
            return {}
        if resp.status_code in (401, 403):
            self._api_disabled = True
            log.warning("Clé LBA_API_TOKEN refusée (HTTP %s) : API Apprentissage ignorée, site LBA seul", resp.status_code)
            return {}
        if resp.status_code != 200:
            log.warning("API Apprentissage : HTTP %s", resp.status_code)
            return {}
        for warning in body.get("warnings") or []:
            log.info("API Apprentissage : %s", (warning or {}).get("message"))
        self._api_cache[key] = body
        return body

    def _api_request(self, params: list[tuple[str, str]], headers: dict[str, str]) -> httpx.Response:
        resp = request_with_retry(self.client, "GET", API_SEARCH_URL, params=params, headers=headers,
                                  throttle=self._api_throttle)
        if resp.status_code in RATE_LIMIT_STATUSES:  # still rate limited after request_with_retry's retries
            wait = resp.headers.get("retry-after", "")
            _sleep(min(float(wait) if wait.isdigit() else 5.0, 60.0))
            resp = request_with_retry(self.client, "GET", API_SEARCH_URL, params=params, headers=headers,
                                      throttle=self._api_throttle)
        return resp

    # ------------------------------------------------------------------ recruiters

    def search_recruiters(self, latitude: float, longitude: float, radius_km: int, romes: list[str]) -> list[Company]:
        """Companies likely to hire apprentices (spontaneous applications), deduplicated by SIRET."""
        codes = list(dict.fromkeys(romes or DEFAULT_ROMES))
        geo = [("latitude", str(latitude)), ("longitude", str(longitude))]
        companies = self._site_recruiters(codes, geo + [("radius", str(min(200, max(1, radius_km))))])
        if self._api_token and not self._api_disabled:
            api_params = geo + [("radius", str(min(200, max(0, radius_km)))), ("romes", ",".join(codes))]
            companies = self._with_api_recruiters(companies, api_params)
        return list(companies.values())

    def _site_recruiters(self, codes: list[str], geo: list[tuple[str, str]]) -> dict[str, Company]:
        terms = list(dict.fromkeys(ROME_SEARCH_TERMS[c] for c in codes if c in ROME_SEARCH_TERMS))
        if not terms:
            log.info("Aucun libellé de recherche LBA connu pour les codes ROME %s", ", ".join(codes))
        companies: dict[str, Company] = {}
        for term in terms:
            params = [("mode", "emplois"), ("is_algo_company", "true"), ("hitsPerPage", str(HITS_PER_PAGE)),
                      ("q", term)] + geo
            try:
                for hit in self._site_hits(params, HITS_PER_PAGE * MAX_PAGES):
                    company = safe_map(hit_to_company, hit)
                    if company is not None:
                        companies.setdefault(company.raw.get("siret") or company.id, company)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                log.warning("Recherche des recruteurs LBA « %s » en échec : %s", term, exc)
        return companies

    def _with_api_recruiters(self, companies: dict[str, Company], params: list[tuple[str, str]]) -> dict[str, Company]:
        merged = dict(companies)
        for item in self._api_search(params).get("recruiters") or []:
            company = safe_map(api_recruiter_to_company, item)
            if company is None:
                continue
            key = company.raw.get("siret") or company.id
            merged[key] = merge_company(merged[key], company) if key in merged else company
        return merged

    # ------------------------------------------------------------------ details

    def fetch_details(self, job: JobOffer) -> JobOffer:
        sub_type, url_id = job.raw.get("source"), job.raw.get("id")
        if job.source != SOURCE or sub_type not in (SUB_LBA, SUB_PARTNER) or not url_id:
            return job
        key = (str(sub_type), str(url_id))
        try:
            if key not in self._details:
                resp = request_with_retry(self.client, "GET", SITE_DETAIL_URL.format(sub_type=key[0], id=key[1]),
                                          throttle=self._site_throttle)
                if resp.status_code == 404:
                    log.info("Offre LBA %s introuvable : marquée expirée", url_id)
                    return job if job.status == JobStatus.APPLIED else job.model_copy(update={"status": JobStatus.EXPIRED})
                if resp.status_code != 200:
                    log.warning("Détail LBA %s : HTTP %s", url_id, resp.status_code)
                    return job
                self._details[key] = resp.json()
            return apply_detail(job, self._details[key])
        except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as exc:
            log.warning("Détail LBA %s indisponible : %s", url_id, exc)
            return job
