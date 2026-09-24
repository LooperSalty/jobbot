"""Welcome to the Jungle: Algolia search (public front-end key) + public REST job detail.

Apply is always EXTERNAL: WTTJ in-site forms need a candidate account and the ToS (§10) forbid robots, so
jobbot only prepares the documents; a human submits. ``apply_url`` is the company ATS when WTTJ exposes it
(``ats == "external"``); for in-site "Welcome Kit" forms (``ats == "wkit"``) it stays ``None`` → offer page.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from jobbot import clock
from jobbot.config import LocationPref
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import ApplyMethod, ContractType, JobOffer
from jobbot.sources.base import SearchQuery, SourceUnavailable
from jobbot.sources.countries import country_name, is_foreign, location_country
from jobbot.textutil import html_to_text, normalize, stable_id

log = logging.getLogger(__name__)

ENV_URL = "https://www.welcometothejungle.com/api/env"
SITE_URL = "https://www.welcometothejungle.com"
API_URL = "https://api.welcometothejungle.com/api/v1/organizations"
REFERER = "https://www.welcometothejungle.com/"  # mandatory: the Algolia key is referer-restricted
DEFAULT_APP_ID = "CSEKHVMS53"
DEFAULT_API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
JOBS_INDEX = "wttj_jobs_production_fr"
MIN_INTERVAL_S = 1.0  # politeness between two requests to the same host
MAX_HITS_PER_PAGE = 100
MAX_PAGES = 10
MAX_DETAILS_PER_SEARCH = 40  # detail calls (apply_url + full description) per search() call

_CONTRACT_FILTER = {
    ContractType.ALTERNANCE: "apprenticeship", ContractType.CDI: "full_time", ContractType.CDD: "temporary",
    ContractType.STAGE: "internship", ContractType.FREELANCE: "freelance", ContractType.INTERIM: "temporary",
    ContractType.OTHER: "other",
}
_CONTRACT_MAP = {
    "apprenticeship": ContractType.ALTERNANCE, "full_time": ContractType.CDI, "graduate_program": ContractType.CDI,
    "temporary": ContractType.CDD, "internship": ContractType.STAGE, "freelance": ContractType.FREELANCE,
}
_REMOTE_MAP = {"fulltime": True, "partial": False, "punctual": False, "no": False}
_PERIODS = {"yearly": "an", "monthly": "mois", "weekly": "semaine", "daily": "jour", "hourly": "heure"}
_ALTERNANCE_TITLE = re.compile(r"\b(alternance|alternant(e|s|es)?|apprenti(e|s|es)?|en apprentissage)\b", re.I)
_CONTRACT_WORDS = re.compile(
    r"\b(alternance|alternant(e|s|es)?|apprentissage|apprenti(e|s|es)?|cdi|cdd|stage|stagiaire|freelance)\b", re.I
)
_PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError)


class WelcomeToTheJungleSource:
    """Read-only WTTJ source (search + detail). Never applies."""

    name = "wttj"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client
        self._credentials: tuple[str, str] | None = None
        self._throttle = HostThrottle(MIN_INTERVAL_S)
        self._details: dict[str, dict | None] = {}

    def available(self) -> tuple[bool, str]:
        return True, ""

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        found_at, yielded = clock.now(), 0
        for hit in self._iter_hits(query):
            job = _hit_or_none(hit, query.location, found_at)
            if job is None:
                continue
            yield self._enrich(job) if yielded < MAX_DETAILS_PER_SEARCH else job
            yielded += 1
            if yielded >= query.max_results:
                return

    def fetch_details(self, job: JobOffer) -> JobOffer:
        return self._enrich(job)

    # ------------------------------------------------------------------ HTTP

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client()
        return self._client

    def _iter_hits(self, query: SearchQuery) -> Iterator[dict]:
        """Hits page by page; the next page is only requested if the consumer asks for more."""
        per_page = max(1, min(query.max_results, MAX_HITS_PER_PAGE))
        for page in range(MAX_PAGES):
            data = self._search_page(query, page, per_page)
            if data is None:
                return
            yield from (hit for hit in data.get("hits") or [] if isinstance(hit, dict))
            if page + 1 >= int(data.get("nbPages") or 0):
                return

    def _search_page(self, query: SearchQuery, page: int, per_page: int) -> dict | None:
        try:
            return self._post_algolia(_build_search_body(query, page, per_page))
        except SourceUnavailable:
            if page == 0:
                raise
            log.warning("WTTJ : page %d des résultats indisponible, arrêt de la pagination", page + 1, exc_info=True)
            return None

    def _post_algolia(self, body: dict) -> dict:
        app_id, api_key = self._algolia_credentials()
        url = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/{JOBS_INDEX}/query"
        headers = {"X-Algolia-Application-Id": app_id, "X-Algolia-API-Key": api_key, "Referer": REFERER}
        try:
            resp = request_with_retry(self._http(), "POST", url, json=body, headers=headers, throttle=self._throttle)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"WTTJ (Algolia) injoignable : {exc}") from exc
        if resp.status_code != 200:
            raise SourceUnavailable(
                f"WTTJ (Algolia) a refusé la recherche (HTTP {resp.status_code}) : clé publique peut-être changée."
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceUnavailable("WTTJ (Algolia) : réponse JSON illisible.") from exc
        if not isinstance(data, dict):
            raise SourceUnavailable("WTTJ (Algolia) : format de réponse inattendu.")
        return data

    def _algolia_credentials(self) -> tuple[str, str]:
        if self._credentials is None:
            self._credentials = _load_credentials(self._http())
        return self._credentials

    def _enrich(self, job: JobOffer) -> JobOffer:
        detail = self._detail(job)
        if detail is None:
            return job
        try:
            return _merge_detail(job, detail)
        except _PARSE_ERRORS as exc:
            log.warning("WTTJ : détail illisible pour %s (%s)", job.url, exc)
            return job

    def _detail(self, job: JobOffer) -> dict | None:
        org_slug, slug = _slugs(job)
        if not org_slug or not slug:
            return None
        key = f"{org_slug}/{slug}"
        if key not in self._details:
            self._details[key] = self._fetch_detail(org_slug, slug)
        return self._details[key]

    def _fetch_detail(self, org_slug: str, slug: str) -> dict | None:
        url = f"{API_URL}/{org_slug}/jobs/{slug}"
        try:
            resp = request_with_retry(self._http(), "GET", url, headers={"Accept": "application/json"},
                                      throttle=self._throttle, retries=1)
            resp.raise_for_status()
            job = resp.json().get("job")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.info("WTTJ : détail indisponible pour %s (%s)", url, exc)
            return None
        return job if isinstance(job, dict) else None


# ---------------------------------------------------------------------- credentials & query


def _load_credentials(client: httpx.Client) -> tuple[str, str]:
    """Algolia app id + public search key from the site's runtime env (fallback: known constants)."""
    try:
        resp = client.get(ENV_URL, headers={"Accept": "*/*"})
        resp.raise_for_status()
        match = re.search(r"window\.env\s*=\s*(\{.*\})", resp.text, re.S)
        env = json.loads(match.group(1)) if match else {}
    except (httpx.HTTPError, ValueError) as exc:
        log.info("WTTJ : configuration publique indisponible (%s), clés Algolia par défaut utilisées", exc)
        env = {}
    return (env.get("PUBLIC_ALGOLIA_APPLICATION_ID") or DEFAULT_APP_ID,
            env.get("PUBLIC_ALGOLIA_API_KEY_CLIENT") or DEFAULT_API_KEY)


def _build_search_body(query: SearchQuery, page: int, hits_per_page: int, *, now: datetime | None = None) -> dict:
    since = int(((now or clock.now()) - timedelta(days=query.max_age_days)).timestamp())
    body: dict[str, Any] = {
        "query": _clean_keywords(query), "page": page, "hitsPerPage": hits_per_page,
        "filters": " AND ".join(_filter_clauses(query)),
        "numericFilters": [f"published_at_timestamp>={since}"],
        "attributesToHighlight": [], "attributesToSnippet": [],
    }
    loc = query.location
    if not query.remote and loc and loc.latitude is not None and loc.longitude is not None:
        body["aroundLatLng"] = f"{loc.latitude},{loc.longitude}"
        body["aroundRadius"] = int(loc.radius_km * 1000)  # metres
    return body


def _filter_clauses(query: SearchQuery) -> list[str]:
    contracts = sorted({_CONTRACT_FILTER[c] for c in query.contract_types})
    clauses = ["(" + " OR ".join(f"contract_type:{c}" for c in contracts) + ")"] if contracts else []
    loc = query.location
    if query.remote:
        return clauses + ["remote:fulltime", "offices.country_code:FR"]
    no_coords = loc is not None and (loc.latitude is None or loc.longitude is None)
    return clauses + _place_clauses(loc) if no_coords else clauses


def _place_clauses(loc: LocationPref) -> list[str]:
    """Place without coordinates (else ``aroundLatLng``): abroad, the country + the city unless it is its name."""
    code = location_country(loc)
    country = [f"offices.country_code:{code}"] if is_foreign(loc) else []
    city_is_country = bool(country) and normalize(loc.city) == normalize(country_name(code))
    return country + ([f'offices.city:"{loc.city}"'] if loc.city and not city_is_country else [])


def _clean_keywords(query: SearchQuery) -> str:
    """Contract words are handled by filters; keeping them would require them in the offer text."""
    text = _CONTRACT_WORDS.sub(" ", query.keywords) if query.contract_types else query.keywords
    return " ".join(text.split())


# ---------------------------------------------------------------------- mapping


def _hit_or_none(hit: dict, near: LocationPref | None, found_at: datetime) -> JobOffer | None:
    try:
        return _hit_to_job(hit, near=near, found_at=found_at)
    except _PARSE_ERRORS as exc:
        log.warning("WTTJ : offre ignorée (format inattendu : %s)", exc)
        return None


def _hit_to_job(hit: dict, *, near: LocationPref | None = None, found_at: datetime | None = None) -> JobOffer:
    org = hit.get("organization") or {}
    org_slug, slug = org["slug"], hit["slug"]
    external_id = f"{org_slug}/{slug}"
    office, geo = _pick_office(hit, near)
    return JobOffer(
        id=stable_id("wttj", external_id), source="wttj", external_id=external_id,
        title=" ".join(str(hit["name"]).split()), company=org.get("name") or "",
        location=_office_label(office), latitude=geo.get("lat"), longitude=geo.get("lng"),
        description=_hit_description(hit), url=f"{SITE_URL}/fr/companies/{org_slug}/jobs/{slug}",
        apply_method=ApplyMethod.EXTERNAL, contract_type=_map_contract(hit.get("contract_type"), hit["name"]),
        remote=_REMOTE_MAP.get(hit.get("remote") or ""),
        salary=_format_salary(hit.get("salary_minimum"), hit.get("salary_maximum"),
                              hit.get("salary_currency"), hit.get("salary_period")),
        published_at=_published(hit), found_at=found_at or clock.now(), raw=_hit_raw(hit, office),
    )


def _map_contract(value: str | None, title: str = "") -> ContractType:
    mapped = _CONTRACT_MAP.get(value or "", ContractType.OTHER)
    if mapped == ContractType.OTHER and _ALTERNANCE_TITLE.search(title or ""):
        return ContractType.ALTERNANCE
    return mapped


def _pick_office(hit: dict, near: LocationPref | None) -> tuple[dict, dict]:
    """The office (and its coordinates) closest to the searched location — multi-office jobs are common."""
    offices = hit.get("offices") or []
    geos = hit.get("_geoloc") or []
    geos = [geos] if isinstance(geos, dict) else geos
    idx = 0
    if near and near.latitude is not None and near.longitude is not None and len(geos) > 1:
        idx = min(range(len(geos)), key=lambda i: _sq_dist(geos[i], near))
    office: dict = (offices[idx] if idx < len(offices) else offices[0]) if offices else {}
    return office, (geos[idx] if idx < len(geos) else {})


def _sq_dist(geo: dict, near: LocationPref) -> float:
    dlat = float(geo.get("lat", 0.0)) - float(near.latitude or 0.0)
    dlon = (float(geo.get("lng", 0.0)) - float(near.longitude or 0.0)) * math.cos(math.radians(near.latitude or 0.0))
    return dlat * dlat + dlon * dlon


def _office_label(office: dict) -> str:
    city = office.get("city") or ""
    country_code = office.get("country_code")
    if country_code and country_code != "FR":
        return ", ".join(p for p in (city, office.get("country") or country_code) if p)
    zip_code = office.get("zip_code") or ""
    return f"{city} ({zip_code[:2]})" if city and len(zip_code) == 5 else city


def _hit_description(hit: dict) -> str:
    parts = [hit.get("summary") or ""]
    missions = [m for m in hit.get("key_missions") or [] if m]
    if missions:
        parts.append("Missions :\n" + "\n".join(f"- {m}" for m in missions))
    if hit.get("profile"):
        parts.append(html_to_text(hit["profile"]))
    return "\n\n".join(p for p in parts if p).strip()


def _hit_raw(hit: dict, office: dict) -> dict:
    org = hit.get("organization") or {}
    profession = hit.get("new_profession") or {}
    return {
        "object_id": hit.get("objectID"), "reference": hit.get("wk_reference") or hit.get("reference"),
        "org_slug": org.get("slug"), "slug": hit.get("slug"), "contract_type": hit.get("contract_type"),
        "remote": hit.get("remote"), "experience_level_minimum": hit.get("experience_level_minimum"),
        "education_level": hit.get("education_level"), "profession": profession.get("pivot_name"),
        "sub_category": profession.get("sub_category_reference"), "language": hit.get("language"),
        "contract_duration_min": hit.get("contract_duration_minimum"),
        "contract_duration_max": hit.get("contract_duration_maximum"),
        "office_city": office.get("city"), "office_country": office.get("country_code"),
    }


def _published(hit: dict) -> datetime | None:
    try:
        parsed = clock.parse_dt(hit.get("published_at"))
    except (TypeError, ValueError):
        parsed = None
    stamp = hit.get("published_at_timestamp")
    if parsed is None and isinstance(stamp, int | float):
        parsed = datetime.fromtimestamp(stamp, UTC)
    return parsed


def _slugs(job: JobOffer) -> tuple[str | None, str | None]:
    """(organization slug, job slug) from a search offer, an alert-mail offer or the external id."""
    org_slug = job.raw.get("org_slug") or job.raw.get("organization_slug")
    slug = job.raw.get("slug") or job.raw.get("job_slug")
    if not (org_slug and slug) and "/" in job.external_id:
        org_slug, slug = job.external_id.split("/", 1)
    return org_slug, slug


def _merge_detail(job: JobOffer, detail: dict) -> JobOffer:
    office = _detail_office(detail, job.raw.get("office_city"))
    org = detail.get("organization") or {}
    update = {
        "title": job.title or " ".join(str(detail.get("name") or "").split()),
        "company": job.company or org.get("name") or "",
        "published_at": job.published_at or _published(detail),
        "description": _detail_description(detail) or job.description,
        "apply_url": detail.get("apply_url") or None,
        "location": _office_label(office) or job.location,
        "postal_code": office.get("zip_code") or job.postal_code,
        "latitude": office.get("latitude") if office.get("latitude") is not None else job.latitude,
        "longitude": office.get("longitude") if office.get("longitude") is not None else job.longitude,
        "salary": _format_salary(detail.get("salary_min"), detail.get("salary_max"),
                                 detail.get("salary_currency"), detail.get("salary_period")) or job.salary,
        "company_domain": _domain(org.get("media_website_url")) or job.company_domain,
        "raw": {**job.raw, **_detail_raw(detail)},
    }
    if detail.get("contract_type"):
        update["contract_type"] = _map_contract(detail["contract_type"], job.title)
    if detail.get("remote") in _REMOTE_MAP:
        update["remote"] = _REMOTE_MAP[detail["remote"]]
    return job.model_copy(update=update)


def _detail_office(detail: dict, city: str | None) -> dict:
    offices = [o for o in detail.get("offices") or [] if isinstance(o, dict)]
    same_city = next((o for o in offices if city and o.get("city") == city), None)
    return same_city or detail.get("office") or (offices[0] if offices else {})


def _detail_description(detail: dict) -> str:
    parts = [html_to_text(detail.get("description") or ""), html_to_text(detail.get("profile") or "")]
    tools = [t.get("name") for t in detail.get("tools") or [] if isinstance(t, dict) and t.get("name")]
    if tools:
        parts.append("Outils : " + ", ".join(tools))
    return "\n\n".join(p for p in parts if p).strip()


def _detail_raw(detail: dict) -> dict:
    fields = [f"{f.get('name')}:{f.get('mode')}" for f in detail.get("application_fields") or [] if isinstance(f, dict)]
    return {
        "ats": detail.get("ats"), "application_fields": fields, "ats_questions": detail.get("ats_questions") or [],
        "experience_level": detail.get("experience_level"), "start_date": detail.get("start_date"),
        "tools": [t.get("name") for t in detail.get("tools") or [] if isinstance(t, dict)],
        "status": detail.get("status"), "detail_fetched": True,
    }


def _format_salary(low: Any, high: Any, currency: str | None, period: str | None) -> str | None:
    values = [float(v) for v in (low, high) if isinstance(v, int | float) and v > 0]
    if not values:
        return None
    amounts = " – ".join(f"{round(v):,}".replace(",", " ") for v in dict.fromkeys(values))
    symbol = "€" if (currency or "EUR").upper() == "EUR" else currency
    unit = _PERIODS.get(period or "")
    return f"{amounts} {symbol}" + (f" / {unit}" if unit else "")


def _domain(url: str | None) -> str | None:
    host = urlparse(url or "").netloc.lower().split(":")[0]
    return host.removeprefix("www.") or None
