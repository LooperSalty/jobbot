"""Which company career boards to read this run: found in the database, never guessed.

- companies (spontaneous-application targets): ``careers_url`` / ``website`` / URL hints in ``raw`` that point to a
  public ATS, else ``careers_url`` read as a careers page (JSON-LD);
- stored offers whose ``apply_url`` / ``url`` points to a public ATS (that employer's other offers).
A board slug is never guessed from a domain (another company can own the same slug on Lever / Greenhouse…).

Bookkeeping in the repository kv: ``company_ats:last:<board key>`` = ISO time of the last attempt (success, 404 or
failure) and ``company_ats:fail:<board key>`` = consecutive failures. A board is due again ``refresh_hours`` after
its last attempt, doubled per consecutive failure (a site that keeps failing never starves the others); due
boards are read never-read first then oldest, half of the per-run cap being kept for re-reads.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from jobbot import clock
from jobbot.sources.ats_detect import AtsBoard, ats_board, page_key
from jobbot.sources.public_page import is_safe_url

log = logging.getLogger(__name__)

KV_PREFIX = "company_ats:last:"
FAIL_PREFIX = "company_ats:fail:"
MAX_BACKOFF_DOUBLINGS = 4  # 20 h → 40 h → … → 320 h (≈ 13 days) between attempts on a failing board
MAX_STORED_JOBS = 5000
SKIPPED_COMPANY_STATUSES = frozenset({"blacklisted", "irrelevant", "rejected"})
# Pages that are job boards, not a company's own careers page (their offers come from their own source).
_BOARD_HOSTS = ("linkedin.com", "indeed.", "hellowork.com", "welcometothejungle.com", "francetravail.fr",
                "pole-emploi.fr", "apec.fr", "free-work.com", "apprentissage.beta.gouv.fr", "jobteaser.com",
                "meteojob.com", "glassdoor.")
_HINT_KEY = re.compile(r"career|carri|jobs?|recrut|ats|emploi", re.I)


@dataclass(frozen=True)
class BoardTarget:
    """One board (ATS) or careers page (``board is None``) to read, with the employer's name as a hint."""

    board: AtsBoard | None
    page_url: str | None
    company: str
    origin: str  # "company" | "job"

    @property
    def key(self) -> str:
        return self.board.key if self.board else f"page:{page_key(self.page_url or '')}"


def discover_targets(repo: Any) -> list[BoardTarget]:
    """Every distinct board / careers page known from the companies and the stored offers."""
    targets: dict[str, BoardTarget] = {}
    for target in [*_company_targets(repo), *_job_targets(repo)]:
        targets.setdefault(target.key, target)
    return list(targets.values())


def select_targets(repo: Any, targets: list[BoardTarget], *, limit: int, refresh_hours: float,
                   now: datetime | None = None) -> list[BoardTarget]:
    """Due targets (see module doc), at most ``limit``: never-read ones first, but at least half of the cap is
    kept for the oldest re-reads when there are some."""
    now = now or clock.now()
    fresh: list[BoardTarget] = []
    old: list[tuple[datetime, int, BoardTarget]] = []
    for index, target in enumerate(targets):
        last = last_fetch(repo, target.key)
        if last is None:
            fresh.append(target)
        elif now - last >= refresh_delay(repo, target.key, refresh_hours):
            old.append((last, index, target))
    limit = max(0, limit)
    rereads = [target for _, _, target in sorted(old, key=lambda item: (item[0], item[1]))]
    first = fresh[:limit - min(len(rereads), limit // 2)]
    chosen = first + rereads[:limit - len(first)]
    if len(fresh) + len(rereads) > limit:
        log.info("Pages carrières : %d lues sur %d à lire (limite par exécution)", limit, len(fresh) + len(rereads))
    return chosen


def last_fetch(repo: Any, key: str) -> datetime | None:
    value = repo.kv_get(KV_PREFIX + key)
    try:
        return clock.parse_dt(value) if value else None
    except ValueError:
        return None


def refresh_delay(repo: Any, key: str, refresh_hours: float) -> timedelta:
    """``refresh_hours`` after a success, doubled for each consecutive failure (capped)."""
    return timedelta(hours=refresh_hours * 2 ** min(failures(repo, key), MAX_BACKOFF_DOUBLINGS))


def failures(repo: Any, key: str) -> int:
    value = repo.kv_get(FAIL_PREFIX + key) or "0"
    return int(value) if value.isdigit() else 0


def mark_fetched(repo: Any, key: str, when: datetime | None = None) -> None:
    """A completed read (offers or 404): due again after ``refresh_hours``."""
    repo.kv_set(KV_PREFIX + key, clock.iso(when or clock.now()) or "")
    if failures(repo, key):
        repo.kv_set(FAIL_PREFIX + key, "0")


def mark_failed(repo: Any, key: str, when: datetime | None = None) -> int:
    """A failed read (network, 5xx, unreadable): retried later with a doubling delay. Returns the failure count."""
    count = failures(repo, key) + 1
    repo.kv_set(KV_PREFIX + key, clock.iso(when or clock.now()) or "")
    repo.kv_set(FAIL_PREFIX + key, str(count))
    return count


# --------------------------------------------------------------------------- discovery


def _company_targets(repo: Any) -> list[BoardTarget]:
    found: list[BoardTarget] = []
    for company in repo.list_companies():
        if company.status in SKIPPED_COMPANY_STATUSES:
            continue
        urls = [company.careers_url, company.website, *_raw_urls(company.raw)]
        boards = [b for b in (ats_board(u) for u in urls if u) if b is not None]
        if boards:
            found.extend(BoardTarget(b, None, company.name, "company") for b in dict.fromkeys(boards))
        elif is_public_page(company.careers_url):
            found.append(BoardTarget(None, company.careers_url, company.name, "company"))
    return found


def _job_targets(repo: Any) -> list[BoardTarget]:
    """Boards behind stored offers (WTTJ excluded: the wttj source already searches that index)."""
    found: list[BoardTarget] = []
    for job in repo.list_jobs(order="found_at DESC", limit=MAX_STORED_JOBS):
        if job.source == "company_ats":
            continue
        for url in (job.apply_url, job.url):
            board = ats_board(url)
            if board is not None and board.provider != "wttj":
                found.append(BoardTarget(board, None, job.company, "job"))
                break
    return found


def _raw_urls(raw: dict) -> list[str]:
    """URL-looking values stored under career / jobs / ATS keys of ``Company.raw``."""
    return [str(v) for k, v in (raw or {}).items()
            if isinstance(v, str) and v.startswith(("http://", "https://")) and _HINT_KEY.search(str(k))]


def is_public_page(url: str | None) -> bool:
    """A company page worth reading: safe URL (``public_page.is_safe_url``) and not a job board."""
    if not is_safe_url(url):
        return False
    host = (urlsplit(url or "").hostname or "").lower()
    return not any(board in host for board in _BOARD_HOSTS)
