"""HTTP side of the company career boards: one public, key-less call (or a few pages) per board.

Endpoints live-checked 2026-09-27:
- Lever ``GET api[.eu].lever.co/v0/postings/<slug>?mode=json`` → list (unknown board: 404);
- Greenhouse ``GET boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true`` → ``{"jobs", "meta"}`` (404);
- Recruitee ``GET <slug>.recruitee.com/api/offers/`` → ``{"offers"}`` (404);
- SmartRecruiters ``GET api.smartrecruiters.com/v1/companies/<id>/postings?country=fr&limit=100`` →
  ``{"totalFound", "content"}`` (unknown company: 200 + ``totalFound: 0``), detail ``…/postings/<id>``;
- Workable ``POST apply.workable.com/api/v3/accounts/<slug>/jobs`` → ``{"total", "results", "nextPage"}``, detail
  ``GET …/api/v2/accounts/<slug>/jobs/<shortcode>``;
- Teamtailor ``GET <slug>.teamtailor.com/jobs.rss`` (RSS 2.0 + ``tt:`` locations);
- Welcome to the Jungle: no per-company endpoint (``api/v1/organizations/<slug>/jobs`` → 404), the wttj source's
  Algolia index filtered on ``organization.slug``;
- any careers page: schema.org ``JobPosting`` JSON-LD, and links to one of the ATS above.
Politeness: ≤ 1 request per second per provider (``HostThrottle`` keyed by provider).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import httpx

from jobbot import clock
from jobbot.http import HostThrottle, request_with_retry
from jobbot.models import JobOffer
from jobbot.sources import ats_mapping as mapping
from jobbot.sources.ats_detect import AtsBoard, ats_board, page_key
from jobbot.sources.base import SourceUnavailable
from jobbot.sources.jsonld import job_postings, posting_fields
from jobbot.sources.public_page import PageGone, fetch_public_page

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 1.0
SMARTRECRUITERS_PAGE = 100
MAX_PAGES = 3
_HREF = re.compile(r"""(?:href|src|data-url)\s*=\s*["']([^"'<>\s]+)["']""", re.I)
_PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError, ET.ParseError)
# What a board fetch may raise besides ``BoardNotFound`` (network, HTTP status, unreadable JSON / RSS, WTTJ down).
FETCH_ERRORS = (httpx.HTTPError, httpx.InvalidURL, SourceUnavailable, RecursionError, *_PARSE_ERRORS)


class BoardNotFound(RuntimeError):
    """The ATS says this board does not exist (404): remembered like a successful fetch."""


@dataclass(frozen=True)
class BoardResult:
    offers: list[JobOffer] = field(default_factory=list)
    linked_boards: list[AtsBoard] = field(default_factory=list)  # ATS found on a careers page


class AtsFetcher:
    """Fetch one board / careers page and map its postings (errors → ``httpx.HTTPError`` / ``BoardNotFound``)."""

    def __init__(self, client: httpx.Client, *, wttj_source: Any = None, countries: frozenset[str] = frozenset({"FR"})):
        self._client = client
        self._throttle = HostThrottle(MIN_INTERVAL_S)
        self._wttj = wttj_source
        self._countries = countries

    # ------------------------------------------------------------------ boards

    def fetch_board(self, board: AtsBoard, company_hint: str = "", *, max_age_days: int = 60) -> list[JobOffer]:
        now = clock.now()
        handlers = {
            "lever": self._lever, "greenhouse": self._greenhouse, "recruitee": self._recruitee,
            "smartrecruiters": self._smartrecruiters, "workable": self._workable, "teamtailor": self._teamtailor,
        }
        if board.provider == "wttj":
            return self._wttj_jobs(board.slug, max_age_days)
        handler = handlers.get(board.provider)
        if handler is None:
            return []
        offers: list[JobOffer] = []
        for posting, needs_details in handler(board):
            try:
                offers.append(mapping.to_offer(board, posting, company_hint=company_hint, found_at=now,
                                               needs_details=needs_details))
            except ValueError as exc:  # pydantic ValidationError
                log.info("Offre %s (%s) ignorée : %s", posting.posting_id, board.key, exc)
        return offers

    def fetch_page(self, url: str, company_hint: str = "") -> BoardResult:
        """JSON-LD postings of a company careers page + the ATS boards it links to (public addresses only)."""
        final_url, page = self._page(url)
        board = AtsBoard("jsonld", page_key(url))
        now, offers = clock.now(), []
        for raw in job_postings(page):
            try:
                fields = posting_fields(raw)
                if fields is not None:
                    posting = mapping.jsonld_posting(fields, final_url)
                    offers.append(mapping.to_offer(board, posting, company_hint=company_hint, found_at=now,
                                                   needs_details=mapping.jsonld_needs_details(posting, final_url)))
            except _PARSE_ERRORS as exc:
                log.info("Offre JSON-LD ignorée sur %s : %s", url, exc)
        return BoardResult(offers, linked_ats(page))

    # ------------------------------------------------------------------ details

    def smartrecruiters_detail(self, slug: str, posting_id: str) -> dict[str, Any] | None:
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
        return self._detail("smartrecruiters", url)

    def workable_detail(self, slug: str, shortcode: str) -> dict[str, Any] | None:
        url = f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}"
        return self._detail("workable", url)

    def page_html(self, url: str) -> str:
        return self._page(url)[1]

    def _page(self, url: str) -> tuple[str, str]:
        """(final URL, HTML) of a third-party page: see ``public_page`` (404/410 → ``BoardNotFound``)."""
        try:
            return fetch_public_page(self._client, url, throttle=_ProviderThrottle(self._throttle, "page"),
                                     headers={"Accept": "text/html,application/xhtml+xml"})
        except PageGone as exc:
            raise BoardNotFound(str(exc)) from exc

    def _detail(self, provider: str, url: str) -> dict[str, Any] | None:
        """Posting JSON, None when the posting is gone (404/410)."""
        try:
            data = self._get(provider, url).json()
        except BoardNotFound:
            return None
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ providers

    def _lever(self, board: AtsBoard) -> list[tuple[mapping.Posting, bool]]:
        host = "api.eu.lever.co" if board.region == "eu" else "api.lever.co"
        items = self._get("lever", f"https://{host}/v0/postings/{board.slug}", params={"mode": "json"}).json()
        return _mapped(mapping.lever_posting, _dicts(items))

    def _greenhouse(self, board: AtsBoard) -> list[tuple[mapping.Posting, bool]]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs"
        data = self._get("greenhouse", url, params={"content": "true"}).json()
        return _mapped(mapping.greenhouse_posting, _dicts(_field(data, "jobs")))

    def _recruitee(self, board: AtsBoard) -> list[tuple[mapping.Posting, bool]]:
        data = self._get("recruitee", f"https://{board.slug}.recruitee.com/api/offers/").json()
        return _mapped(mapping.recruitee_posting, _dicts(_field(data, "offers")))

    def _smartrecruiters(self, board: AtsBoard) -> list[tuple[mapping.Posting, bool]]:
        """One pass per searched country (``country=fr``, ``lu``…): big groups list thousands of postings."""
        url = f"https://api.smartrecruiters.com/v1/companies/{board.slug}/postings"
        items: list[dict] = []
        for country in sorted(self._countries):
            for page in range(MAX_PAGES):
                params = {"country": country.lower(), "limit": SMARTRECRUITERS_PAGE,
                          "offset": page * SMARTRECRUITERS_PAGE}
                data = self._get("smartrecruiters", url, params=params).json()
                items += _dicts(_field(data, "content"))
                if (page + 1) * SMARTRECRUITERS_PAGE >= int(_field(data, "totalFound") or 0):
                    break
        return _mapped(mapping.smartrecruiters_posting, items, board.slug, needs_details=True)

    def _workable(self, board: AtsBoard) -> list[tuple[mapping.Posting, bool]]:
        url = f"https://apply.workable.com/api/v3/accounts/{board.slug}/jobs"
        body: dict[str, Any] = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        items: list[dict] = []
        for _ in range(MAX_PAGES):
            data = self._get("workable", url, method="POST", json=body).json()
            items += _dicts(_field(data, "results"))
            if not _field(data, "nextPage"):
                break
            body = {**body, "token": data["nextPage"]}
        return _mapped(mapping.workable_posting, items, board.slug, needs_details=True)

    def _teamtailor(self, board: AtsBoard) -> list[tuple[mapping.Posting, bool]]:
        rss = self._get("teamtailor", f"https://{board.slug}.teamtailor.com/jobs.rss").content
        company, postings = mapping.teamtailor_postings(rss)
        return [(p if p.company else replace(p, company=company), False) for p in postings]

    def _wttj_jobs(self, slug: str, max_age_days: int) -> list[JobOffer]:
        """WTTJ company page: the wttj source's Algolia client (credentials, referer, errors) filtered on one company.
        Offers keep ``source="wttj"`` so that they merge with the WTTJ search and its details."""
        if self._wttj is None:
            return []
        from jobbot.sources import wttj

        since = int((clock.now() - timedelta(days=max_age_days)).timestamp())
        body = {"query": "", "page": 0, "hitsPerPage": wttj.MAX_HITS_PER_PAGE,
                "filters": f'organization.slug:"{slug}"', "numericFilters": [f"published_at_timestamp>={since}"],
                "attributesToHighlight": [], "attributesToSnippet": []}
        self._throttle.wait("https://wttj.ats/")
        data = self._wttj._post_algolia(body)  # noqa: SLF001 — same index and client as the wttj source
        found_at = clock.now()
        hits = [wttj._hit_or_none(h, None, found_at) for h in _dicts(data.get("hits"))]  # noqa: SLF001
        return [job for job in hits if job is not None]

    # ------------------------------------------------------------------ HTTP

    def _get(self, provider: str, url: str, *, method: str = "GET", **kwargs: Any) -> httpx.Response:
        self._throttle.wait(f"https://{provider}.ats/")
        headers = {"Accept": "application/json", **kwargs.pop("headers", {})}
        resp = request_with_retry(self._client, method, url, headers=headers, retries=1, **kwargs)
        if resp.status_code in (404, 410):
            raise BoardNotFound(f"{url} : HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp


# --------------------------------------------------------------------------- helpers


class _ProviderThrottle:
    """``HostThrottle`` keyed by provider (one request per second per ATS, not per company sub-domain)."""

    def __init__(self, throttle: HostThrottle, provider: str):
        self._throttle, self._key = throttle, f"https://{provider}.ats/"

    def wait(self, _url: str) -> None:
        self._throttle.wait(self._key)


def linked_ats(page: str) -> list[AtsBoard]:
    """ATS boards linked (href / iframe src) from a careers page, first occurrence order."""
    boards: dict[str, AtsBoard] = {}
    for url in _HREF.findall(page or ""):
        board = ats_board(url.replace("&amp;", "&"))
        if board is not None and board.provider != "wttj":
            boards.setdefault(board.key, board)
    return list(boards.values())


def _dicts(value: object) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _field(data: object, key: str) -> Any:
    return data.get(key) if isinstance(data, dict) else None


def _mapped(fn: Callable[..., mapping.Posting | None], items: list[dict], *args: Any,
            needs_details: bool = False) -> list[tuple[mapping.Posting, bool]]:
    """Map every item; one malformed posting is logged and skipped, never the whole board."""
    found = []
    for item in items:
        try:
            posting = fn(item, *args)
        except _PARSE_ERRORS as exc:
            log.info("Offre ATS ignorée (format inattendu : %s)", exc)
            continue
        if posting is not None and posting.title:
            found.append((posting, needs_details))
    return found
