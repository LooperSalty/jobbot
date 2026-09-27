"""Time budget of a night run: until ``settings.night.until`` (Paris time) or ``--budget`` minutes.

Every step checks :meth:`Deadline.expired` between two units of work (a page, a company, a document) and stops
cleanly; a long step gets a :meth:`Deadline.share` of what is left so the next steps still run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from jobbot import clock
from jobbot.config import parse_clock

MAX_DEFAULT_HOURS = 8.0  # a manual daytime run without --budget still ends the same day


def _now() -> datetime:  # indirection so tests can drive the clock
    return clock.now()


def now() -> datetime:
    return _now()


def night_end(until: str, *, start: datetime, budget_minutes: int | None = None) -> datetime:
    """``start + budget_minutes``, else the next ``until`` (local time) after ``start``, at most 8 hours later."""
    if budget_minutes is not None:
        return start + timedelta(minutes=max(0, budget_minutes))
    at = parse_clock(until)
    local = clock.to_paris(start)
    end = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return min(end, local + timedelta(hours=MAX_DEFAULT_HOURS))


@dataclass(frozen=True)
class Deadline:
    end: datetime

    def expired(self) -> bool:
        return _now() >= self.end

    def remaining_s(self) -> float:
        return max(0.0, (self.end - _now()).total_seconds())

    def share(self, fraction: float) -> Deadline:
        """Sub-deadline: now + ``fraction`` of the remaining time (never after ``end``)."""
        return Deadline(min(self.end, _now() + timedelta(seconds=self.remaining_s() * max(0.0, fraction))))
