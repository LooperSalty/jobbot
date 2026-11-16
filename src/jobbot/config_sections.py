"""Settings sections split from ``config`` (file size): outreach, reports, Classroom watcher (+ active hours),
night scraper (``night``) and the spontaneous sending ramp (``limits.spontaneous_ramp``).

Everything here is re-exported by ``jobbot.config``: import from there.
"""

from __future__ import annotations

import re
from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CLOCK_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def parse_clock(value: str) -> time:
    """'06:30' → time(6, 30). ValueError (French) when malformed."""
    match = _CLOCK_RE.match((value or "").strip())
    if not match:
        raise ValueError(f"Heure invalide « {value} » (format HH:MM, ex. 06:30)")
    return time(int(match[1]), int(match[2]))


class SpontaneousRamp(Frozen):
    """Sending ramp of spontaneous e-mails (deliverability): ``start`` per day, ``+step_per_week`` per full week
    since the first real spontaneous e-mail, at most ``max``; back to ``start`` when the bounce rate of the last
    7 days exceeds ``max_bounce_rate``. Disabled → ``limits.spontaneous_per_day``."""

    enabled: bool = True
    start: int = Field(20, ge=1)
    step_per_week: int = Field(5, ge=0)
    max: int = Field(40, ge=1)
    max_bounce_rate: float = Field(0.05, ge=0, le=1)

    @model_validator(mode="after")
    def _start_below_max(self) -> SpontaneousRamp:
        if self.start > self.max:
            raise ValueError(f"Montée progressive : start ({self.start}) supérieur à max ({self.max})")
        return self


class NightSettings(Frozen):
    """« jobbot night »: harvest companies + contacts while the server is idle (nothing is ever sent)."""

    enabled: bool = True  # scheduled at 00:30 by « jobbot schedule install »
    until: str = "06:30"  # local (Paris) time when the run stops cleanly
    extra_radius_km: int = Field(20, ge=0, le=50)  # added to each location's radius (API maximum: 50 km)
    workers: int = Field(4, ge=1, le=16)  # parallel enrichment (one worker per domain at a time)
    max_llm_summaries: int = Field(80, ge=0)  # website summaries (fast model) per night
    prepare_margin: int = Field(5, ge=0)  # documents prepared for the next day: daily cap + this margin
    retention_days: int = Field(365, ge=30)  # named contacts of companies never contacted are purged after this
    search_min_interval_s: float = Field(6.0, ge=0)  # between two SearXNG queries (jittered), day and night
    max_search_queries: int = Field(300, ge=0)  # SearXNG queries per day
    search_backoff_hours: float = Field(6.0, ge=0)  # SearXNG left alone after a CAPTCHA / block
    max_siren_lookups: int = Field(200, ge=0)  # registry queries per night to identify the offers' employers
    max_brave_queries: int = Field(900, ge=0)  # Brave Search API queries per month (billed beyond the plan's credit)

    @field_validator("until")
    @classmethod
    def _valid_until(cls, value: str) -> str:
        parse_clock(value)
        return value.strip()


_EMPLOYER_TRANCHES = ("00", "01", "02", "03", "11", "12", "21", "22", "31", "32", "41", "42", "51", "52", "53")
_DIVISION_RE = re.compile(r"^\d\d$")


class LargeCompanies(Frozen):
    """« Grandes entreprises » spontaneous targets: ETI / GE of any sector (field work, car dealers, stores, hotels,
    care homes and personal services aside) whose local site has ≥ ``min_site_tranche`` employees (INSEE code: 21 = 50-99),
    within ``max_km`` of the first French search area; the campaign writes ring by ring (``rings_km``), nearest
    first (:mod:`jobbot.night.large`, :func:`jobbot.outreach.priority.focus_ring`)."""

    enabled: bool = False
    min_site_tranche: str = "21"
    max_km: int = Field(75, ge=1)
    rings_km: list[int] = Field(default_factory=lambda: [15, 30, 50, 75])
    # car dealers / garages, stores, hotels, care homes, social work, personal services, household employers
    skip_divisions: list[str] = Field(default_factory=lambda: ["45", "47", "55", "87", "88", "96", "97"])

    @field_validator("min_site_tranche")
    @classmethod
    def _valid_tranche(cls, value: str) -> str:
        if value not in _EMPLOYER_TRANCHES:
            raise ValueError(f"Code de tranche d'effectif invalide « {value} » (codes INSEE : "
                             f"{', '.join(_EMPLOYER_TRANCHES)} ; ex. 21 = 50 à 99 salariés)")
        return value

    @field_validator("skip_divisions")
    @classmethod
    def _valid_divisions(cls, value: list[str]) -> list[str]:
        bad = [d for d in value if not _DIVISION_RE.match(str(d))]
        if bad:
            raise ValueError(f"Division NAF invalide : {', '.join(map(str, bad))} (deux chiffres, ex. 47)")
        return value

    @field_validator("rings_km")
    @classmethod
    def _sorted_rings(cls, value: list[int]) -> list[int]:
        if not value or any(r <= 0 for r in value):
            raise ValueError("Cercles d'envoi : au moins un rayon positif (ex. [15, 30, 50, 75])")
        return sorted(value)


class OutreachSettings(Frozen):
    """Spontaneous applications: who may be contacted and how aggressively."""

    country: str | None = None  # restrict spontaneous campaigns to explicitly identified country codes
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
    large_companies: LargeCompanies = Field(default_factory=LargeCompanies)


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
