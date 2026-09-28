"""Brave Search API under a monthly budget: paid plans bill every query beyond their credit, so jobbot never sends
more than ``night.max_brave_queries`` a month (kv ``brave:queries:YYYY-MM``, Paris month, shared by the day and
night runs and every enrichment worker). Without a key or with the budget spent, the free providers are used.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from jobbot import clock

if TYPE_CHECKING:
    from jobbot.context import AppContext
    from jobbot.db import Repository

log = logging.getLogger(__name__)

KV_PREFIX = "brave:queries:"
_LOCK = threading.Lock()  # parallel enrichment workers never overspend the budget


def month_key(now: datetime | None = None) -> str:
    return f"{KV_PREFIX}{clock.to_paris(now or clock.now()):%Y-%m}"


def used_this_month(repo: Repository) -> int:
    return int(repo.kv_get(month_key(), "0") or 0)


@dataclass(frozen=True)
class BraveEngine:
    """Brave Search key + the monthly budget it draws on (``brave_for(ctx)``)."""

    api_key: str
    repo: Repository
    monthly_cap: int

    def take(self) -> bool:
        """Reserve one query in this month's budget; False when it is spent (nothing is sent then)."""
        key = month_key()
        with _LOCK:
            used = int(self.repo.kv_get(key, "0") or 0)
            if used >= self.monthly_cap:
                log.debug("Budget Brave du mois atteint : %d/%d", used, self.monthly_cap)
                return False
            self.repo.kv_set(key, str(used + 1))
        return True


def brave_for(ctx: AppContext) -> BraveEngine | None:
    """The run's Brave engine, None without ``BRAVE_API_KEY``."""
    key = ctx.secrets.brave_api_key
    return BraveEngine(api_key=key, repo=ctx.repo, monthly_cap=ctx.settings.night.max_brave_queries) if key else None
