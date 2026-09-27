"""Jooble (official REST API, free key on request — https://fr.jooble.org/api/about): aggregated job ads.

``POST https://<country host>/api/<key>`` with JSON ``{keywords, location, radius, page, ResultOnPage,
companysearch}``; ``radius`` (km) is one of 0, 4, 8, 16, 26, 40, 80. Response ``{"totalCount", "jobs": [{id, title,
location, snippet, salary, source, type, link, company, updated}]}`` (Jooble help centre, 2026-09). **Each country has
its own host and key** (jooble.org = United States, fr.jooble.org = France…): ``JOOBLE_API_KEY`` is the French key,
so only French queries are searched (Luxembourg skipped). The snippet is short (``raw["description_truncated"]``)
and ``link`` goes through Jooble to the offer: apply is EXTERNAL there. The request quota is set per key by Jooble:
one page per query and a small per-run query budget in the registry.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from jobbot import clock
from jobbot.config import Secrets
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import ApplyMethod, JobOffer
from jobbot.sources import key_redaction
from jobbot.sources.base import SearchQuery, SourceUnavailable
from jobbot.sources.countries import is_foreign
from jobbot.sources.offer_utils import (
    QueryFailed,
    as_int,
    detect_contract,
    detect_remote,
    dict_items,
    is_not_dev_title,
    parse_date,
    secret_value,
    strip_tags,
    with_contract_word,
)
from jobbot.textutil import html_to_text, stable_id

log = logging.getLogger(__name__)
key_redaction.install()  # the key is part of the URL path: masked in the httpx request logs

API_URL = "https://{host}/api/{key}"
HOST = "fr.jooble.org"  # the French key only works on the French host
RADII_KM = (0, 4, 8, 16, 26, 40, 80)
PER_PAGE = 50
MAX_PAGES = 1
MIN_INTERVAL_S = 2.0
REMOTE_WORDS = "télétravail"
_BLOCK_MARKERS = ("just a moment", "cf-chl", "challenge-platform")
_PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError)
_MISSING_KEY = ("Clé JOOBLE_API_KEY absente du .env (gratuite sur demande : https://fr.jooble.org/api/about, "
                "formulaire « Get API key »).")


class JoobleSource:
    """Read-only Jooble source (official REST API, France)."""

    name = "jooble"

    def __init__(self, secrets: Secrets, client: httpx.Client | None = None):
        self._key = secret_value(secrets.jooble_api_key)
        self._client = client
        self._throttle = HostThrottle(MIN_INTERVAL_S)
        self._refused: str | None = None

    def available(self) -> tuple[bool, str]:
        if not self._key:
            return False, _MISSING_KEY
        return (False, self._refused) if self._refused else (True, "")

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        ok, reason = self.available()
        if not ok:
            raise SourceUnavailable(reason)
        if not query.remote and is_foreign(query.location):
            log.info("Jooble : clé française uniquement, requête à l'étranger « %s » ignorée", query.keywords)
            return
        found_at, seen = clock.now(), set()
        cutoff = found_at - timedelta(days=query.max_age_days)
        for item in self._iter_jobs(query):
            job = _item_or_none(item, found_at)
            if job is None or job.external_id in seen or (job.published_at and job.published_at < cutoff):
                continue
            seen.add(job.external_id)
            yield job
            if len(seen) >= query.max_results:
                return

    def fetch_details(self, job: JobOffer) -> JobOffer:
        return job  # the API only publishes a snippet; the offer page is behind Jooble's redirect

    # ------------------------------------------------------------------ HTTP

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client()
        return self._client

    def _iter_jobs(self, query: SearchQuery) -> Iterator[dict]:
        per_page = max(1, min(query.max_results, PER_PAGE))
        for page in range(1, MAX_PAGES + 1):
            data = self._post(build_body(query, page, per_page), page)
            if data is None:
                return
            jobs = dict_items(data.get("jobs"))
            yield from jobs
            if len(jobs) < per_page or page * per_page >= as_int(data.get("totalCount")):
                return

    def _post(self, body: dict[str, Any], page: int) -> dict | None:
        url = API_URL.format(host=HOST, key=quote(self._key, safe=""))
        try:
            resp = request_with_retry(self._http(), "POST", url, json=body, throttle=self._throttle, retries=1,
                                      headers={"Accept": "application/json", "Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            return _fail(f"Jooble injoignable : {type(exc).__name__}", page)
        if resp.status_code == 403 and _is_challenge(resp):
            raise SourceUnavailable("Jooble bloque la requête (protection anti-robot) : réessaie plus tard.")
        if resp.status_code in (401, 403, 404):
            self._refused = (f"Jooble a refusé la clé (HTTP {resp.status_code}) : vérifie JOOBLE_API_KEY (clé "
                             "française, créée sur fr.jooble.org/api/about).")
            raise SourceUnavailable(self._refused)
        if resp.status_code != 200:
            return _fail(f"Jooble : erreur HTTP {resp.status_code} sur la recherche.", page)
        try:
            data = resp.json()
        except ValueError:
            return _fail("Jooble : réponse JSON illisible.", page)
        return data if isinstance(data, dict) else _fail("Jooble : format de réponse inattendu.", page)


# ---------------------------------------------------------------------- query building


def radius_for(km: int) -> int:
    """Smallest radius Jooble accepts that covers ``km`` (80 at most)."""
    return next((r for r in RADII_KM if r >= km), RADII_KM[-1])


def build_body(query: SearchQuery, page: int, per_page: int) -> dict[str, Any]:
    keywords = with_contract_word(query.keywords, query.contract_types)
    loc = query.location
    body: dict[str, Any] = {"keywords": keywords, "location": "France", "page": page, "ResultOnPage": per_page,
                            "companysearch": False}
    if query.remote:
        body["keywords"] = f"{keywords} {REMOTE_WORDS}"
    elif loc is not None and loc.city:
        body["location"] = loc.city
        body["radius"] = str(radius_for(loc.radius_km))
    return body


def _is_challenge(resp: httpx.Response) -> bool:
    if resp.headers.get("cf-mitigated"):
        return True
    return any(marker in resp.text[:4000].lower() for marker in _BLOCK_MARKERS)


def _fail(message: str, page: int) -> None:
    """Page 1: this query fails (the next ones still run); later pages: the pagination stops."""
    if page == 1:
        raise QueryFailed(message)
    log.warning("%s Pagination interrompue.", message)
    return None


# ---------------------------------------------------------------------- mapping


def _item_or_none(item: dict, found_at: datetime) -> JobOffer | None:
    try:
        job = item_to_job(item, found_at=found_at)
    except _PARSE_ERRORS as exc:
        log.warning("Jooble : offre ignorée (format inattendu : %s)", exc)
        return None
    if is_not_dev_title(job.title):
        log.debug("Jooble : offre hors informatique ignorée (%s)", job.title)
        return None
    return job


def item_to_job(item: dict, *, found_at: datetime | None = None) -> JobOffer:
    job_id = str(item["id"])
    title = strip_tags(item["title"])
    link = str(item.get("link") or "")
    if not title or not link:
        raise ValueError("titre ou lien Jooble manquant")
    snippet = html_to_text(str(item.get("snippet") or "")).replace("\xa0", " ").strip()
    kind = strip_tags(item.get("type"))
    return JobOffer(
        id=stable_id("jooble", job_id), source="jooble", external_id=job_id, title=title,
        company=strip_tags(item.get("company")), location=strip_tags(item.get("location")), description=snippet,
        url=link, apply_method=ApplyMethod.EXTERNAL, contract_type=detect_contract(title, snippet, kind),
        remote=detect_remote(title, snippet, kind), salary=strip_tags(item.get("salary")) or None,
        published_at=parse_date(item.get("updated")), found_at=found_at or clock.now(),
        raw={"jooble_id": job_id, "board": item.get("source"), "type": kind or None, "country_code": "FR",
             "description_truncated": True},
    )
