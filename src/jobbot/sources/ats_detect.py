"""Which public ATS job board a URL belongs to: ``detect_ats(url) -> (provider, slug) | None``.

Providers with a public, key-less postings API (live-checked 2026-09-27): Lever (``jobs.lever.co/<slug>``, EU
instance ``jobs.eu.lever.co``), Greenhouse (``boards.greenhouse.io/<slug>``, ``job-boards.greenhouse.io``,
``…/embed/job_board?for=<slug>``), Recruitee (``<slug>.recruitee.com``), SmartRecruiters
(``jobs.smartrecruiters.com/<CompanyId>/…``, ``careers.smartrecruiters.com``, ``smartr.me/oneclick-ui/company/…``),
Workable (``apply.workable.com/<slug>``), Teamtailor (``<slug>.teamtailor.com``) and Welcome to the Jungle company
pages (``welcometothejungle.com/<lang>/companies/<slug>``). Reusable by any module (apply, outreach…).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

PROVIDERS = ("lever", "greenhouse", "recruitee", "smartrecruiters", "workable", "teamtailor", "wttj")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
_RESERVED_SUBDOMAINS = frozenset({
    "www", "app", "api", "help", "support", "blog", "docs", "status", "cdn", "assets", "static", "go", "info",
    "mail", "marketplace", "partners", "integrations", "academy", "careers-page", "jobs", "apply", "resources",
})
_RESERVED_SEGMENTS = frozenset({"api", "embed", "j", "oneclick-ui", "ni", "sr-jobs", "v0", "v1", "v2", "v3"})
_GREENHOUSE_HOSTS = frozenset({
    "boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
    "boards-api.greenhouse.io", "api.greenhouse.io",
})


@dataclass(frozen=True)
class AtsBoard:
    provider: str
    slug: str
    region: str = ""  # "eu" for the EU Lever instance

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.region + '/' if self.region else ''}{self.slug}"


def detect_ats(url: str | None) -> tuple[str, str] | None:
    """``(provider, board slug)`` of a public ATS URL, else None (``"lever", "acme"`` for
    ``https://jobs.lever.co/acme/ee6d…``)."""
    board = ats_board(url)
    return (board.provider, board.slug) if board else None


def ats_board(url: str | None) -> AtsBoard | None:
    """Same as ``detect_ats`` with the instance region (EU Lever)."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    segments = [unquote(s) for s in parts.path.split("/") if s]
    query = parse_qs(parts.query)
    for matcher in (_lever, _greenhouse, _smartrecruiters, _workable, _subdomain_boards, _wttj):
        board = matcher(host, segments, query)
        if board is not None:
            return board if _SLUG.fullmatch(board.slug) else None  # fullmatch: no trailing "\n" (%0A)
    return None


def board_url(board: AtsBoard) -> str:
    """Human page of the board (careers site)."""
    slug = board.slug
    return {
        "lever": f"https://jobs.{'eu.' if board.region == 'eu' else ''}lever.co/{slug}",
        "greenhouse": f"https://job-boards.greenhouse.io/{slug}",
        "recruitee": f"https://{slug}.recruitee.com/",
        "smartrecruiters": f"https://careers.smartrecruiters.com/{slug}",
        "workable": f"https://apply.workable.com/{slug}/",
        "teamtailor": f"https://{slug}.teamtailor.com/jobs",
        "wttj": f"https://www.welcometothejungle.com/fr/companies/{slug}/jobs",
    }.get(board.provider, "")


def page_key(url: str) -> str:
    """Short stable id of a careers page URL (scheme and ``www.`` dropped)."""
    return re.sub(r"^https?://(www\.)?", "", (url or "").strip()).rstrip("/")[:200] or "page"


# --------------------------------------------------------------------------- matchers


def _first_free(segments: list[str], start: int = 0) -> str | None:
    rest = segments[start:]
    return rest[0] if rest and rest[0].lower() not in _RESERVED_SEGMENTS else None


def _after(segments: list[str], marker: str) -> str | None:
    """The path segment right after ``marker`` (``…/companies/<id>/…`` → ``<id>``)."""
    if marker not in segments:
        return None
    idx = segments.index(marker) + 1
    return segments[idx] if idx < len(segments) else None


def _subdomain(host: str, domain: str) -> str | None:
    sub = host.removesuffix(domain) if host.endswith(domain) else ""
    return sub if sub and "." not in sub and sub not in _RESERVED_SUBDOMAINS else None


def _board(provider: str, slug: str | None, region: str = "") -> AtsBoard | None:
    return AtsBoard(provider, slug, region) if slug else None


def _lever(host: str, segments: list[str], query: dict) -> AtsBoard | None:
    region = "eu" if host.endswith(".eu.lever.co") else ""
    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        return _board("lever", _first_free(segments), region)
    if host in ("api.lever.co", "api.eu.lever.co"):
        return _board("lever", _after(segments, "postings"), region)
    return None


def _greenhouse(host: str, segments: list[str], query: dict) -> AtsBoard | None:
    if host not in _GREENHOUSE_HOSTS:
        return None
    if host in ("boards-api.greenhouse.io", "api.greenhouse.io"):
        return _board("greenhouse", _after(segments, "boards"))
    if segments[:1] == ["embed"]:
        return _board("greenhouse", (query.get("for") or [None])[0])
    return _board("greenhouse", _first_free(segments))


def _smartrecruiters(host: str, segments: list[str], query: dict) -> AtsBoard | None:
    if host.endswith("smartr.me") or (host.endswith("smartrecruiters.com") and "oneclick-ui" in segments):
        return _board("smartrecruiters", _after(segments, "company"))
    if host == "api.smartrecruiters.com":
        return _board("smartrecruiters", _after(segments, "companies"))
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com", "www.smartrecruiters.com"):
        return _board("smartrecruiters", _first_free(segments))
    return None


def _workable(host: str, segments: list[str], query: dict) -> AtsBoard | None:
    if host == "apply.workable.com":
        slug = _after(segments, "accounts") if segments[:1] == ["api"] else _first_free(segments)
        return _board("workable", slug)
    return _board("workable", _subdomain(host, ".workable.com"))


def _subdomain_boards(host: str, segments: list[str], query: dict) -> AtsBoard | None:
    """``<slug>.recruitee.com`` and ``<slug>.teamtailor.com``."""
    return (_board("recruitee", _subdomain(host, ".recruitee.com"))
            or _board("teamtailor", _subdomain(host, ".teamtailor.com")))


def _wttj(host: str, segments: list[str], query: dict) -> AtsBoard | None:
    if not host.endswith("welcometothejungle.com"):
        return None
    return _board("wttj", _after(segments, "companies"))
