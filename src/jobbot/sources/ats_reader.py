"""Reads the company career boards due this run (``ats_targets.select_targets``) with their bookkeeping:
a completed read (offers or 404) → ``mark_fetched``; a failure → ``mark_failed`` (doubling delay, so a site that
keeps failing never starves the others); a time budget per run; a database error never loses what was read.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

from jobbot import clock
from jobbot.models import JobOffer
from jobbot.sources.ats_detect import AtsBoard
from jobbot.sources.ats_fetch import FETCH_ERRORS, AtsFetcher, BoardNotFound
from jobbot.sources.ats_targets import BoardTarget, last_fetch, mark_failed, mark_fetched, refresh_delay

log = logging.getLogger(__name__)

MAX_RUN_SECONDS = 900.0  # all boards of one run (≈ 80 boards at ≤ 1 request/s per provider)
MAX_LINKED_BOARDS = 2  # ATS boards followed from one careers page


class BoardReader:
    """Reads targets one by one (``read_all`` stops at the time budget)."""

    def __init__(self, repo: Any, fetcher: Callable[[], AtsFetcher], *, refresh_hours: float,
                 time_budget_s: float = MAX_RUN_SECONDS):
        self._repo = repo
        self._fetcher = fetcher
        self._refresh_hours = refresh_hours
        self._budget_s = time_budget_s

    def read_all(self, targets: list[BoardTarget],
                 max_age_days: int) -> Iterator[tuple[BoardTarget, list[JobOffer]]]:
        started = time.monotonic()
        for done, target in enumerate(targets):
            if time.monotonic() - started > self._budget_s:
                log.warning("Pages carrières : temps imparti dépassé, %d page(s) reportée(s) à la prochaine exécution",
                            len(targets) - done)
                return
            yield target, self.read(target, max_age_days)

    def read(self, target: BoardTarget, max_age_days: int) -> list[JobOffer]:
        try:
            jobs = self._fetch(target, max_age_days)
        except BoardNotFound as exc:
            log.info("Page carrières %s introuvable (%s) : ignorée", target.key, exc)
            jobs = []
        except FETCH_ERRORS as exc:
            self._mark(target.key, ok=False)
            log.warning("Page carrières %s illisible, nouvel essai plus tard : %s", target.key, exc)
            return []
        except Exception as exc:  # one odd board must never stop the others (logged with its traceback)
            self._mark(target.key, ok=False)
            log.warning("Page carrières %s : erreur inattendue (%s), nouvel essai plus tard", target.key, exc,
                        exc_info=True)
            return []
        self._mark(target.key, ok=True)
        return jobs

    def _fetch(self, target: BoardTarget, max_age_days: int) -> list[JobOffer]:
        fetcher = self._fetcher()
        if target.board is not None:
            return fetcher.fetch_board(target.board, target.company, max_age_days=max_age_days)
        result = fetcher.fetch_page(target.page_url or "", target.company)
        jobs = list(result.offers)
        for board in result.linked_boards[:MAX_LINKED_BOARDS]:
            jobs += self._linked_board(board, target.company, max_age_days)
        return jobs

    def _linked_board(self, board: AtsBoard, company: str, max_age_days: int) -> list[JobOffer]:
        """An ATS found on a careers page: read now unless it is not due under its own key."""
        last = self._safe(last_fetch, board.key)
        delay = self._safe(refresh_delay, board.key, self._refresh_hours) or timedelta(hours=self._refresh_hours)
        if last is not None and clock.now() - last < delay:
            return []
        target = BoardTarget(board, None, company, "page")
        return self.read(target, max_age_days)

    def _mark(self, key: str, *, ok: bool) -> None:
        self._safe(mark_fetched if ok else mark_failed, key)

    def _safe(self, fn: Callable[..., Any], key: str, *args: Any) -> Any:
        """kv access that never raises (a locked database must not lose the offers already read)."""
        try:
            return fn(self._repo, key, *args)
        except Exception as exc:
            log.warning("Pages carrières : suivi « %s » non enregistré (%s)", key, exc)
            return None
