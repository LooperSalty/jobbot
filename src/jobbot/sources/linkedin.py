"""LinkedIn job source — logged-out guest endpoints only (search cards + job detail).

No cookie, no login, no ``li_at``: the user's account is never involved. Guest search honours
``keywords``, ``location``/``geoId``, ``distance`` (miles), ``f_TPR``, ``f_AL`` (Easy Apply) and ``start``;
job type / level / remote filters are ignored by LinkedIn and must be done client-side.
~10 requests in 3 s → HTTP 429, so we keep ≥ 3 s (+ jitter) between requests. See docs/research/linkedin.md.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

from jobbot import clock
from jobbot.config import LocationPref
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import ApplyMethod, ContractType, JobOffer, JobStatus
from jobbot.sources.base import JobSource, SearchQuery, SourceUnavailable
from jobbot.sources.countries import country_name, is_foreign, location_country
from jobbot.textutil import html_to_text, normalize, stable_id, strip_accents

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
HEADERS = {"Accept-Language": "fr-FR,fr;q=0.9"}  # pinned: the parser expects French labels
PAGE_SIZE = 10
MAX_PAGES = 3  # research: ≤ 3 pages per query, one run per day
MIN_INTERVAL_S = 3.0
JITTER_S = 2.0
BACKOFFS_429_S = (30.0,)  # one pause, then the source is suspended for the run
DISTANCES_MI = (0, 5, 10, 25, 50)
FRANCE_GEO_ID = "105015875"
GEO_IDS = {  # live-verified 2026-09-23
    "metz": "103167155",
    "nancy": "106742268",
    "thionville": "103333173",
    "strasbourg": "105580607",
    "moselle": "105424345",
    "meurthe-et-moselle": "104346793",
    "grand est": "101735443",
    "luxembourg": "104042105",
    "france": FRANCE_GEO_ID,
}
CITY_GEOS = frozenset({"metz", "nancy", "thionville", "strasbourg"})  # "distance" only makes sense for a city
BLOCK_MARKERS = ("/authwall", "/checkpoint/", "/uas/login")
_JOB_ID_RE = re.compile(r"urn:li:jobPosting:(\d+)")
_COMPANY_ID_RE = re.compile(r"facetCurrentCompany(?:%3D|=)(\d+)")
OPEN_STATUSES = frozenset(
    {JobStatus.NEW, JobStatus.SHORTLISTED, JobStatus.FILTERED_OUT, JobStatus.DOCS_READY,
     JobStatus.PENDING_REVIEW, JobStatus.APPROVED}
)

# Client-side classification (guest filters are ignored). Applied on accent-folded lowercase text.
_ALTERNANCE_RE = re.compile(
    r"\b(alternan(?:ce|t|te|ts|tes)|apprenti(?:e|s|es)?|apprentissage|contrat\s+pro(?:fessionnalisation)?|"
    r"professionnalisation|work[\s-]?study|apprenticeship|ausbildung|duales?\s+studium)\b"
)
_ALTERNANCE_DESC_RE = re.compile(  # stricter: "3 alternants dans l'équipe" must not make a CDI an alternance
    r"\b(alternance|apprentissage|professionnalisation|apprenticeship|work[\s-]?study|ausbildung|duales?\s+studium)\b"
)
_INTERNSHIP_RE = re.compile(r"\b(stage|stagiaire|internship|intern|pfe)\b")
_EXPLICIT_CONTRACTS = (
    (re.compile(r"\bcdi\b"), ContractType.CDI),
    (re.compile(r"\bcdd\b"), ContractType.CDD),
    (re.compile(r"\b(freelance|independant|portage salarial)\b"), ContractType.FREELANCE),
    (re.compile(r"\binterim(aire)?\b"), ContractType.INTERIM),
)
_FULL_REMOTE_RE = re.compile(
    r"(full[\s-]?remote|100\s*%\s*(?:remote|teletravail|a distance)|teletravail\s+(?:complet|total|integral)|"
    r"fully\s+remote|totalement\s+a\s+distance)"
)


def _fold(text: str | None) -> str:
    return strip_accents(text or "").lower().replace("’", "'")


def guess_contract_type(title: str, description: str = "", employment_type: str | None = None,
                        seniority: str | None = None) -> ContractType:
    """Contract type from free text (title first, then description, then LinkedIn criteria)."""
    title_f, desc_f = _fold(title), _fold(description)
    if _ALTERNANCE_RE.search(title_f):
        return ContractType.ALTERNANCE
    if _INTERNSHIP_RE.search(title_f):
        return ContractType.STAGE
    by_title = _explicit_contract(title_f)
    if by_title is not None:
        return by_title
    if _ALTERNANCE_DESC_RE.search(desc_f):
        return ContractType.ALTERNANCE
    if _fold(employment_type) == "stage / alternance" or _fold(seniority) == "stagiaire / alternant":
        return ContractType.STAGE if _INTERNSHIP_RE.search(desc_f) else ContractType.ALTERNANCE
    return _explicit_contract(desc_f) or ContractType.OTHER


def _explicit_contract(folded: str) -> ContractType | None:
    return next((contract for pattern, contract in _EXPLICIT_CONTRACTS if pattern.search(folded)), None)


def detect_remote(text: str) -> bool | None:
    """True for full-remote offers, None when unknown or partial (hybrid)."""
    return True if _FULL_REMOTE_RE.search(_fold(text)) else None


def job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def distance_param(radius_km: int) -> int:
    miles = radius_km / 1.609
    return next((d for d in DISTANCES_MI if d >= miles), DISTANCES_MI[-1])


def _foreign_location_params(loc: LocationPref) -> dict[str, str]:
    """Luxembourg track: the whole country (geoId wins over ``location``; matching filters by radius)."""
    name = country_name(location_country(loc))
    geo_id = GEO_IDS.get(normalize(name))
    if geo_id:
        return {"geoId": geo_id, "location": name}
    city = normalize(loc.city)
    return {"location": f"{loc.city}, {name}" if city and city != normalize(name) else name}


def _location_params(loc: LocationPref | None) -> dict[str, str]:
    if loc is not None and is_foreign(loc):
        return _foreign_location_params(loc)
    city = normalize(loc.city) if loc is not None and loc.city else ""
    if city in GEO_IDS:
        params = {"geoId": GEO_IDS[city]}
        if city in CITY_GEOS:
            params["distance"] = str(distance_param(loc.radius_km))
        return params
    return {"location": loc.city} if city else {"geoId": FRANCE_GEO_ID}


def build_search_params(query: SearchQuery, easy_apply_only: bool) -> dict[str, str]:
    keywords = query.keywords
    params: dict[str, str] = {"f_TPR": f"r{max(query.max_age_days, 1) * 86_400}"}
    params.update(_location_params(query.location))
    if query.remote and "teletravail" not in normalize(keywords):
        keywords = f"{keywords} télétravail"  # f_WT is ignored by the guest endpoint
    params["keywords"] = keywords
    if easy_apply_only:
        params["f_AL"] = "true"
    return params


# --------------------------------------------------------------------------- parsing


def node_text(node: Tag | None) -> str:
    """Whitespace-normalised text of a node ('' for None). Shared with hellowork.py."""
    return " ".join(node.get_text(" ", strip=True).split()) if node is not None else ""


def _href(node: Tag | None) -> str | None:
    href = node.get("href") if node is not None else None
    return href.split("?")[0] if href else None


def parse_search_cards(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "html.parser")
    cards: dict[str, dict[str, Any]] = {}
    for card in soup.select("[data-entity-urn]"):
        m = _JOB_ID_RE.search(card.get("data-entity-urn") or "")
        if not m or m.group(1) in cards:
            continue
        stamp = card.select_one("time")
        cards[m.group(1)] = {
            "job_id": m.group(1),
            "title": node_text(card.select_one("h3.base-search-card__title")),
            "company": node_text(card.select_one("h4.base-search-card__subtitle")),
            "company_url": _href(card.select_one("h4.base-search-card__subtitle a")),
            "location": node_text(card.select_one("span.job-search-card__location")),
            "posted_date": stamp.get("datetime") if stamp is not None else None,
            "insight": node_text(card.select_one("span.job-posting-benefits__text")) or None,
            "salary": node_text(card.select_one("span.job-search-card__salary-info")) or None,
        }
    return list(cards.values())


def _criteria(soup: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in soup.select("li.description__job-criteria-item"):
        label, value = item.select_one("h3"), item.select_one("span")
        if label is not None and value is not None:
            out[node_text(label).replace("’", "'")] = node_text(value)
    return out


def _poster(soup: BeautifulSoup) -> dict[str, str | None] | None:
    block = soup.select_one(".message-the-recruiter")
    if block is None:
        return None
    return {
        "name": node_text(block.select_one(".base-main-card__title")) or None,
        "headline": node_text(block.select_one(".base-main-card__subtitle")) or None,
        "profile_url": _href(block.select_one("a.base-card__full-link")),
    }


def _apply_mode(soup: BeautifulSoup) -> str:
    if soup.select_one('[data-tracking-control-name="public_jobs_apply-link-onsite"]') is not None:
        return "easy_apply"
    if soup.select_one('[data-tracking-control-name^="public_jobs_apply-link-offsite"]') is not None:
        return "external"
    return "unknown"


def parse_job_detail(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    who = soup.select_one("a.see-who-was-hired")
    company_id = _COMPANY_ID_RE.search(who.get("href") or "") if who is not None else None
    desc = soup.select_one("div.show-more-less-html__markup")
    org = soup.select_one("a.topcard__org-name-link")
    criteria = _criteria(soup)
    return {
        "title": node_text(soup.select_one("h2.top-card-layout__title")),
        "company": node_text(org),
        "company_url": _href(org),
        "company_id": company_id.group(1) if company_id else None,
        "location": node_text(soup.select_one("span.topcard__flavor--bullet")),
        "posted_relative": node_text(soup.select_one("span.posted-time-ago__text")) or None,
        "applicants": node_text(soup.select_one(".num-applicants__caption")) or None,
        "closed": soup.select_one(".closed-job__flavor--closed") is not None,
        "description": html_to_text(str(desc)) if desc is not None else "",
        "criteria": criteria,
        "seniority": criteria.get("Niveau hiérarchique"),
        "employment_type": criteria.get("Type d'emploi"),
        "apply_mode": _apply_mode(soup),
        "poster": _poster(soup),
    }


def parse_date(value: Any) -> datetime | None:
    """ISO date/datetime string → aware datetime; None for anything unparsable."""
    try:
        return clock.parse_dt(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


def card_to_job(card: dict[str, Any], now: datetime, easy_apply: bool) -> JobOffer:
    return JobOffer(
        id=stable_id("linkedin", card["job_id"]),
        source="linkedin",
        external_id=card["job_id"],
        title=card["title"],
        company=card["company"],
        location=card["location"],
        url=job_url(card["job_id"]),
        apply_method=ApplyMethod.LINKEDIN_EASY_APPLY if easy_apply else ApplyMethod.EXTERNAL,
        contract_type=guess_contract_type(card["title"]),
        remote=detect_remote(card["title"]),
        salary=card["salary"],
        published_at=parse_date(card["posted_date"]),
        found_at=now,
        raw={k: v for k, v in {
            "company_url": card["company_url"], "insight": card["insight"],
            "easy_apply": True if easy_apply else None, "needs_details": True,
        }.items() if v is not None},
    )


_APPLY_METHODS = {"easy_apply": ApplyMethod.LINKEDIN_EASY_APPLY, "external": ApplyMethod.EXTERNAL}


def merge_detail(job: JobOffer, detail: dict[str, Any]) -> JobOffer:
    """Copy of ``job`` enriched with a parsed guest detail page."""
    title = detail["title"] or job.title
    description = detail["description"] or job.description
    update: dict[str, Any] = {
        "title": title,
        "company": detail["company"] or job.company,
        "location": detail["location"] or job.location,
        "description": description,
        "apply_method": _APPLY_METHODS.get(detail["apply_mode"], job.apply_method),
        "contract_type": guess_contract_type(title, description, detail["employment_type"], detail["seniority"]),
        "remote": detect_remote(f"{title}\n{detail['location']}\n{description}") or job.remote,
    }
    extra = {k: detail[k] for k in ("company_url", "company_id", "posted_relative", "applicants", "criteria",
                                    "apply_mode", "poster") if detail[k]}
    update["raw"] = {**job.raw, **extra, "easy_apply": detail["apply_mode"] == "easy_apply",
                     "closed": detail["closed"], "needs_details": False}
    return job.model_copy(update=update)


def mark_expired(job: JobOffer) -> JobOffer:
    """Offer gone/closed: flag it and move not-yet-applied jobs to EXPIRED. Shared with hellowork.py."""
    update: dict[str, Any] = {"raw": {**job.raw, "expired": True, "needs_details": False}}
    if job.status in OPEN_STATUSES:
        update["status"] = JobStatus.EXPIRED
    return job.model_copy(update=update)


# --------------------------------------------------------------------------- source


class LinkedInSource(JobSource):
    """Guest (logged-out) LinkedIn job search. Apply happens elsewhere (apply/linkedin.py)."""

    name = "linkedin"

    def __init__(self, client: httpx.Client | None = None, easy_apply_only: bool = True):
        self._client = client or make_client(headers=HEADERS)
        self.easy_apply_only = easy_apply_only
        self._throttle = HostThrottle(MIN_INTERVAL_S)
        self._sleep: Callable[[float], None] = time.sleep
        self._rng = random.Random()
        self._blocked: str | None = None

    def available(self) -> tuple[bool, str]:
        return (False, self._blocked) if self._blocked else (True, "")

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        params, seen, now = build_search_params(query, self.easy_apply_only), set(), clock.now()
        pages = min(MAX_PAGES, max(1, -(-query.max_results // PAGE_SIZE)))
        for page in range(pages):
            cards = self._search_page(params, page * PAGE_SIZE, first=page == 0)
            fresh = [c for c in cards if c["job_id"] not in seen]
            for card in fresh:
                if len(seen) >= query.max_results:
                    return
                seen.add(card["job_id"])
                yield card_to_job(card, now, self.easy_apply_only)
            if len(cards) < PAGE_SIZE or not fresh:
                return

    def fetch_details(self, job: JobOffer) -> JobOffer:
        if job.source != self.name or not job.external_id.isdigit():
            return job
        resp = self._request(DETAIL_URL.format(job_id=job.external_id))
        if resp.status_code in (404, 410):
            return mark_expired(job)
        if resp.status_code >= 400:
            raise SourceUnavailable(f"LinkedIn : offre {job.external_id} illisible (HTTP {resp.status_code})")
        detail = parse_job_detail(resp.text)
        if not detail["title"] and not detail["description"]:
            raise SourceUnavailable(f"LinkedIn : page de l'offre {job.external_id} méconnaissable (site modifié ?)")
        merged = merge_detail(job, detail)
        return mark_expired(merged) if detail["closed"] else merged

    def _search_page(self, params: dict[str, str], start: int, *, first: bool) -> list[dict[str, Any]]:
        resp = self._request(SEARCH_URL, {**params, "start": str(start)})
        if resp.status_code == 400:  # start ≥ 1000 or end of results
            return []
        if resp.status_code >= 400:
            raise SourceUnavailable(f"LinkedIn : recherche impossible (HTTP {resp.status_code})")
        cards = parse_search_cards(resp.text)
        if first and not cards and "<li" in resp.text:
            raise SourceUnavailable("LinkedIn : résultats méconnaissables (site modifié ?)")
        return cards

    def _request(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        for backoff in (0.0, *BACKOFFS_429_S):
            if backoff:
                log.warning("LinkedIn limite le débit (HTTP 429) : pause de %.0f s", backoff)
                self._sleep(backoff)
            resp = self._send(url, params)
            if resp.status_code != 429:
                self._check_blocked(resp)
                return resp
        raise self._block("LinkedIn limite le débit (HTTP 429 répétés)")

    def _send(self, url: str, params: dict[str, str] | None) -> httpx.Response:
        if self._blocked:
            raise SourceUnavailable(self._blocked)
        self._sleep(self._rng.uniform(0, JITTER_S))
        try:
            return request_with_retry(self._client, "GET", url, params=params, headers=HEADERS,
                                      retries=0, throttle=self._throttle)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"LinkedIn injoignable : {exc}") from exc

    def _check_blocked(self, resp: httpx.Response) -> None:
        target = f"{resp.url} {resp.headers.get('location', '')}"  # followed or not-followed redirect
        if resp.status_code in (403, 999) or any(m in target for m in BLOCK_MARKERS):
            raise self._block(f"LinkedIn refuse l'accès invité (HTTP {resp.status_code})")
        if len(resp.text) < 3000 and "linkedin.com/authwall" in resp.text:
            raise self._block("LinkedIn redirige vers la page de connexion (authwall)")

    def _block(self, reason: str) -> SourceUnavailable:
        """Suspend the source for the rest of the run and return the exception to raise."""
        self._blocked = f"{reason} : source LinkedIn suspendue pour cette exécution"
        log.warning(self._blocked)
        return SourceUnavailable(self._blocked)
