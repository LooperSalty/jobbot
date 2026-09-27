"""Adzuna (official API, free developer key — https://developer.adzuna.com): job ads aggregated from many boards.

``GET https://api.adzuna.com/v1/api/jobs/<country>/search/<page>`` with ``app_id``, ``app_key``, ``what``,
``where``, ``distance`` (km), ``max_days_old``, ``results_per_page`` (≤ 50), ``sort_by=date`` and ``permanent=1`` /
``contract=1``. Result: ``{"count", "results": [{id, title, description (snippet ≈ 500 chars), redirect_url, created,
company.display_name, location.display_name / area[], latitude, longitude, contract_type (permanent|contract),
contract_time, salary_min/max, salary_is_predicted, category}]}``; titles and snippets carry ``<strong>`` highlights.

Countries: gb us at au be br ca ch de es fr in it mx nl nz pl sg za — **no Luxembourg**: Luxembourg queries are
skipped. Only a snippet and the tracked ``redirect_url`` are published: the description stays short
(``raw["description_truncated"]``) and the redirect is never followed automatically (every hit counts as a click
for the advertiser). Apply is EXTERNAL on the offer page. Free plan ≈ 250 requests a day: ≤ 2 pages per query,
2.5 s between requests, and a per-run query budget in the registry.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from jobbot import clock
from jobbot.config import Secrets
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import ApplyMethod, ContractType, JobOffer
from jobbot.sources import key_redaction
from jobbot.sources.base import SearchQuery, SourceUnavailable
from jobbot.sources.countries import is_foreign, location_country
from jobbot.sources.offer_utils import (
    QueryFailed,
    as_int,
    detect_contract,
    detect_remote,
    dict_items,
    float_or_none,
    format_salary,
    is_not_dev_title,
    parse_date,
    secret_value,
    strip_tags,
    with_contract_word,
)
from jobbot.textutil import stable_id

log = logging.getLogger(__name__)
key_redaction.install()  # app_id / app_key travel as query parameters: masked in the httpx request logs

API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
COUNTRIES = frozenset({"GB", "US", "AT", "AU", "BE", "BR", "CA", "CH", "DE", "ES", "FR", "IN", "IT", "MX", "NL",
                       "NZ", "PL", "SG", "ZA"})
MIN_INTERVAL_S = 2.5
PER_PAGE = 50
MAX_PAGES = 2
REMOTE_WORDS = "télétravail remote"
_CONTRACT_PARAMS = {ContractType.CDI: "permanent", ContractType.CDD: "contract"}
_CONTRACT_HINTS = {"permanent": "CDI"}  # « contract » covers CDD, interim and freelance: the text decides
CURRENCIES = {"GB": "GBP", "US": "USD", "AU": "AUD", "BR": "BRL", "CA": "CAD", "CH": "CHF", "IN": "INR", "MX": "MXN",
              "NZ": "NZD", "PL": "PLN", "SG": "SGD", "ZA": "ZAR"}  # every other Adzuna country: EUR
_PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError)
_MISSING_KEYS = ("Clés ADZUNA_APP_ID / ADZUNA_APP_KEY absentes du .env (gratuites sur https://developer.adzuna.com "
                 "→ Register → Dashboard).")


class AdzunaSource:
    """Read-only Adzuna source (official API)."""

    name = "adzuna"

    def __init__(self, secrets: Secrets, client: httpx.Client | None = None):
        self._app_id = secret_value(secrets.adzuna_app_id)
        self._app_key = secret_value(secrets.adzuna_app_key)
        self._client = client
        self._throttle = HostThrottle(MIN_INTERVAL_S)
        self._refused: str | None = None

    def available(self) -> tuple[bool, str]:
        if not (self._app_id and self._app_key):
            return False, _MISSING_KEYS
        return (False, self._refused) if self._refused else (True, "")

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        ok, reason = self.available()
        if not ok:
            raise SourceUnavailable(reason)
        country = _country(query)
        if country is None:
            log.info("Adzuna : pays %s non couvert, requête « %s » ignorée", location_country(query.location),
                     query.keywords)
            return
        found_at, seen = clock.now(), set()
        for result in self._iter_results(query, country):
            job = _result_or_none(result, country, found_at)
            if job is None or job.external_id in seen:
                continue
            seen.add(job.external_id)
            yield job
            if len(seen) >= query.max_results:
                return

    def fetch_details(self, job: JobOffer) -> JobOffer:
        return job  # only the snippet is public; the redirect is a tracked click, never followed by a robot

    # ------------------------------------------------------------------ HTTP

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client()
        return self._client

    def _iter_results(self, query: SearchQuery, country: str) -> Iterator[dict]:
        per_page = max(1, min(query.max_results, PER_PAGE))
        for page in range(1, MAX_PAGES + 1):
            data = self._get_page(country, page, build_params(query, per_page))
            if data is None:
                return
            results = dict_items(data.get("results"))
            yield from results
            if len(results) < per_page or page * per_page >= as_int(data.get("count")):
                return

    def _get_page(self, country: str, page: int, params: dict[str, Any]) -> dict | None:
        url = API_URL.format(country=country.lower(), page=page)
        auth = {"app_id": self._app_id, "app_key": self._app_key}
        try:
            resp = request_with_retry(self._http(), "GET", url, params={**auth, **params}, retries=1,
                                      headers={"Accept": "application/json"}, throttle=self._throttle)
        except httpx.HTTPError as exc:
            return _fail(f"Adzuna injoignable : {type(exc).__name__}", page)
        if resp.status_code in (401, 403):
            self._refused = (f"Adzuna a refusé les identifiants (HTTP {resp.status_code}) : vérifie ADZUNA_APP_ID / "
                             "ADZUNA_APP_KEY dans .env.")
            raise SourceUnavailable(self._refused)
        if resp.status_code == 429:
            raise SourceUnavailable("Adzuna : quota de requêtes atteint (HTTP 429), réessaie demain.")
        if resp.status_code != 200:
            return _fail(f"Adzuna : erreur HTTP {resp.status_code} sur la recherche.", page)
        try:
            data = resp.json()
        except ValueError:
            return _fail("Adzuna : réponse JSON illisible.", page)
        return data if isinstance(data, dict) else _fail("Adzuna : format de réponse inattendu.", page)


# ---------------------------------------------------------------------- query building


def _country(query: SearchQuery) -> str | None:
    """ISO code of the Adzuna site to search (the query's country), None when Adzuna does not cover it."""
    if query.remote or not is_foreign(query.location):
        return "FR"
    code = location_country(query.location)
    return code if code in COUNTRIES else None


def build_params(query: SearchQuery, per_page: int) -> dict[str, Any]:
    params: dict[str, Any] = {
        "what": with_contract_word(query.keywords, query.contract_types), "results_per_page": per_page,
        "max_days_old": query.max_age_days, "sort_by": "date", "content-type": "application/json",
    }
    loc = query.location
    if query.remote:
        params["what_or"] = REMOTE_WORDS
    elif loc is not None and loc.city:
        params["where"] = loc.city
        params["distance"] = loc.radius_km
    flags = {_CONTRACT_PARAMS[c] for c in query.contract_types if c in _CONTRACT_PARAMS}
    if len(flags) == 1 and len(query.contract_types) == 1:
        params[flags.pop()] = 1
    return params


def _fail(message: str, page: int) -> None:
    """Page 1: this query fails (the next ones still run); later pages: the pagination stops."""
    if page == 1:
        raise QueryFailed(message)
    log.warning("%s Pagination interrompue.", message)
    return None


# ---------------------------------------------------------------------- mapping


def _result_or_none(result: dict, country: str, found_at: datetime) -> JobOffer | None:
    try:
        job = result_to_job(result, country=country, found_at=found_at)
    except _PARSE_ERRORS as exc:
        log.warning("Adzuna : offre ignorée (format inattendu : %s)", exc)
        return None
    if is_not_dev_title(job.title):
        log.debug("Adzuna : offre hors informatique ignorée (%s)", job.title)
        return None
    return job


def result_to_job(result: dict, *, country: str = "FR", found_at: datetime | None = None) -> JobOffer:
    ad_id = str(result["id"])
    title = strip_tags(result["title"])
    if not title:
        raise ValueError("titre Adzuna manquant")
    description = strip_tags(result.get("description"))
    location = result.get("location") or {}
    contract_type = str(result.get("contract_type") or "")
    predicted = str(result.get("salary_is_predicted") or "0") == "1"
    return JobOffer(
        id=stable_id("adzuna", ad_id), source="adzuna", external_id=ad_id, title=title,
        company=strip_tags((result.get("company") or {}).get("display_name")),
        location=_location_label(location, country), latitude=float_or_none(result.get("latitude")),
        longitude=float_or_none(result.get("longitude")), description=description,
        url=result.get("redirect_url") or "", apply_method=ApplyMethod.EXTERNAL,
        contract_type=detect_contract(title, description, _CONTRACT_HINTS.get(contract_type)),
        remote=detect_remote(title, description),
        salary=None if predicted else format_salary(result.get("salary_min"), result.get("salary_max"),
                                                    CURRENCIES.get(country, "EUR"), "year"),
        published_at=parse_date(result.get("created")), found_at=found_at or clock.now(),
        raw={
            "adzuna_id": ad_id, "country_code": country, "contract_type": contract_type or None,
            "contract_time": result.get("contract_time"), "area": location.get("area") or [],
            "category": (result.get("category") or {}).get("label"), "salary_predicted": predicted,
            "description_truncated": True,
        },
    )


def _location_label(location: dict, country: str) -> str:
    """« Metz, Moselle » as published (department name kept for the matcher); abroad « Place, BE »."""
    label = strip_tags(location.get("display_name"))
    return label if country == "FR" or not label else f"{label}, {country}"
