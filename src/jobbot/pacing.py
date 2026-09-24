"""Daily quotas and human-like pauses, shared by every channel that acts on the user's behalf."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from jobbot.config import Limits
from jobbot.db import Repository

log = logging.getLogger(__name__)

# channel -> Limits attribute holding its daily quota
CHANNEL_QUOTA = {
    "linkedin": "linkedin_per_day",
    "hellowork": "hellowork_per_day",
    "lba": "lba_per_day",
    "email": "email_applications_per_day",
    "spontaneous": "spontaneous_per_day",
    "followup": "followups_per_day",
}
APPLY_ACTION = "apply"


class QuotaExceeded(RuntimeError):
    pass


class Pacer:
    """Tracks per-channel daily quotas (persisted in action_log) and sleeps like a human."""

    def __init__(
        self,
        repo: Repository,
        limits: Limits,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.repo = repo
        self.limits = limits
        self._sleep = sleep
        self._rng = rng or random.Random()

    def quota(self, channel: str) -> int:
        attr = CHANNEL_QUOTA.get(channel)
        if attr is None:
            raise KeyError(f"canal inconnu: {channel}")
        return int(getattr(self.limits, attr))

    def used_today(self, channel: str) -> int:
        return self.repo.count_actions_today(channel, APPLY_ACTION)

    def remaining(self, channel: str) -> int:
        return max(0, self.quota(channel) - self.used_today(channel))

    def ensure_available(self, channel: str) -> None:
        if self.remaining(channel) <= 0:
            raise QuotaExceeded(f"Quota journalier atteint pour {channel} ({self.quota(channel)}/jour)")

    def record(self, channel: str, ok: bool, detail: str = "") -> None:
        """Count one attempt. Only successful ones consume quota."""
        self.repo.log_action(channel, APPLY_ACTION, ok, detail)

    def between_applications(self) -> float:
        """Long pause between two applications/emails."""
        delay = self._rng.uniform(self.limits.min_delay_s, self.limits.max_delay_s)
        log.info("Pause de %.0fs avant la prochaine action", delay)
        self._sleep(delay)
        return delay

    def between_actions(self) -> float:
        """Short pause between two UI actions (click, type...)."""
        delay = self._rng.uniform(self.limits.action_min_ms, self.limits.action_max_ms) / 1000
        self._sleep(delay)
        return delay
