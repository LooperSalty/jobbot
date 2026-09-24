"""Settings sections split from ``config`` (file size): outreach, reports, Classroom watcher (+ active hours).

Everything here is re-exported by ``jobbot.config``: import from there.
"""

from __future__ import annotations

import re
from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OutreachSettings(Frozen):
    """Spontaneous applications: who may be contacted and how aggressively."""

    allow_nominative: bool = True  # named people (leaders, employees) and not only rh@/contact@
    allow_employee_search: bool = True  # look for HR / tech managers (team pages, Hunter, search engines)
    allow_unverified_guesses: bool = True  # send to a best-guess pattern (prenom.nom@) when it can't be verified
    max_unverified_per_day: int = Field(8, ge=0)
    max_bounce_rate: float = Field(0.08, ge=0, le=1)  # pause guessed sends when recent bounce rate exceeds this
    max_attempts_per_company: int = Field(2, ge=1)  # after a bounce, try the next best address
    preferred_roles: list[str] = Field(
        default_factory=lambda: ["rh", "recrutement", "talent", "cto", "directeur technique", "lead", "gérant",
                                 "président"]
    )


class ReportSettings(Frozen):
    weekly_report: bool = True
    weekly_report_day: int = Field(0, ge=0, le=6)  # 0 = Monday … 6 = Sunday
    interview_prep: bool = True  # generate a prep sheet when an interview invitation arrives
    weekly_email: bool = False  # also e-mail the weekly report to the candidate himself (SMTP required)


_HOURS_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def parse_active_hours(spec: str) -> tuple[time, time]:
    """'07:30-21:00' → (07:30, 21:00). ValueError (French) when malformed."""
    match = _HOURS_RE.match(spec or "")
    if not match:
        raise ValueError(f"Plage horaire invalide « {spec} » (format HH:MM-HH:MM, ex. 07:30-21:00)")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    return time(h1, m1), time(h2, m2)


class ClassroomSettings(Frozen):
    """Offers relayed by the school in Google Classroom: watched often, applied to immediately."""

    enabled: bool = True
    backend: Literal["browser", "api"] = "browser"  # browser: logged-in profile (no setup); api: OAuth (optional)
    poll_minutes: int = Field(15, ge=1, le=1439)  # period of the scheduled « classroom watch --once » (≤ 1 day)
    active_hours: str = "07:30-21:00"  # outside this window the watcher exits at once
    auto_apply: bool = True  # apply right away, even during the warm-up review period
    min_score: float | None = Field(None, ge=0, le=100)  # None → thresholds.auto_apply_min_score
    headless: bool = True  # headful retry once when Google asks to log in
    browser_profile: str = "data/browser-profile-classroom"  # never shared with the LinkedIn/HelloWork profile
    max_posts: int = Field(30, ge=1, le=200)
    school_bonus: float = Field(5.0, ge=0, le=30)  # added to the match score of a school-relayed offer

    @field_validator("active_hours")
    @classmethod
    def _valid_hours(cls, value: str) -> str:
        parse_active_hours(value)
        return value.strip()
