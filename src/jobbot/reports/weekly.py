"""Weekly report — ``output/rapports/semaine-YYYY-WW.md`` (ISO week), notification, optional e-mail to oneself.

Window: from the Monday of ``week_end``'s ISO week to the end of ``week_end`` (capped at now). Default
``week_end`` = today (current week so far). The scheduled report (pipeline) runs on
``settings.reports.weekly_report_day``: on Monday (default) it covers the previous full week, on any other day the
current week up to that day; a missed run is caught up during the next ``CATCH_UP_DAYS`` days, once per week
(kv ``reports:weekly:done:<YYYY-Www>``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from jobbot import clock
from jobbot.notify import notify
from jobbot.reports._weekly_render import last_day, render_weekly
from jobbot.reports._weekly_stats import compute_week_stats

if TYPE_CHECKING:
    from jobbot.context import AppContext

log = logging.getLogger(__name__)

WEEKLY_DIR = "rapports"
DONE_KV_PREFIX = "reports:weekly:done:"
CATCH_UP_DAYS = 2

__all__ = [
    "WeeklyReport", "build_weekly_report", "parse_day", "previous_week_end", "report_path_name",
    "scheduled_weekly_report", "week_bounds", "week_label", "weekly_report_due", "wants_email",
]


class WeeklyReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    week: str  # ISO week, "2026-W39"
    start: datetime
    end: datetime
    path: str
    offers: int = 0
    offers_per_source: dict[str, int] = Field(default_factory=dict)
    applications: int = 0
    applications_per_channel: dict[str, int] = Field(default_factory=dict)
    responses: int = 0
    interviews: int = 0
    pending_manual: int = 0
    upcoming_followups: int = 0
    emailed: bool = False
    details: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        """'Semaine 39 : 42 offre(s), 12 candidature(s), 2 réponse(s), 1 entretien(s)'."""
        number = int(self.week.rsplit("W", 1)[-1])
        return (f"Semaine {number} : {self.offers} offre(s), {self.applications} candidature(s), "
                f"{self.responses} réponse(s), {self.interviews} entretien(s)")


# --------------------------------------------------------------------------- dates


def _today() -> date:
    return clock.to_paris(clock.now()).date()


def _as_date(value: date | datetime) -> date:
    return clock.to_paris(value).date() if isinstance(value, datetime) else value


def parse_day(text: str) -> date:
    """'2026-09-27' → date; French ValueError otherwise."""
    try:
        return date.fromisoformat(text.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"Date invalide : {text!r} (format attendu AAAA-MM-JJ)") from exc


def previous_week_end(today: date | None = None) -> date:
    """Sunday of the last complete week."""
    day = today if today is not None else _today()
    return day - timedelta(days=day.weekday() + 1)


def week_label(day: date | datetime) -> str:
    iso = _as_date(day).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def report_path_name(day: date | datetime) -> str:
    iso = _as_date(day).isocalendar()
    return f"semaine-{iso.year}-{iso.week:02d}.md"


def week_bounds(week_end: date | datetime | None = None, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """UTC [start, end): Monday 00:00 (Paris) of ``week_end``'s week → end of ``week_end`` (capped at ``now``)."""
    current = now or clock.now()
    day = _as_date(week_end) if week_end is not None else clock.to_paris(current).date()
    monday = day - timedelta(days=day.weekday())
    start = datetime.combine(monday, time(0), clock.PARIS).astimezone(UTC)
    stop = datetime.combine(day + timedelta(days=1), time(0), clock.PARIS).astimezone(UTC)
    if start > current:
        raise ValueError(f"Semaine future ({monday:%d/%m/%Y}) : aucun rapport possible.")
    return start, min(stop, current)


# --------------------------------------------------------------------------- report


def wants_email(ctx: AppContext) -> bool:
    """``reports.weekly_email`` (off by default)."""
    return bool(ctx.settings.reports.weekly_email)


def _write(ctx: AppContext, day: date, text: str) -> Path:
    folder = ctx.output_dir / WEEKLY_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / report_path_name(day)
    path.write_text(text, encoding="utf-8")
    return path


def _email(ctx: AppContext, day: date, text: str, mailer: Any) -> tuple[bool, str]:
    """Send the report to the candidate himself. (sent?, French detail line)."""
    from jobbot.mail.mailer import MailError, OutgoingEmail, build_mailer

    mailer = mailer if mailer is not None else build_mailer(ctx)
    if mailer is None:
        return False, "E-mail non envoyé : SMTP non configuré (SMTP_USER / SMTP_PASSWORD dans .env)."
    week = _as_date(day).isocalendar().week
    email = OutgoingEmail(to=ctx.profile.identity.email, subject=f"jobbot — rapport de la semaine {week}",
                          body=text, kind="report")
    try:
        sent = mailer.send(email)
    except MailError as exc:
        log.warning("Rapport hebdomadaire non envoyé par e-mail : %s", exc)
        return False, f"E-mail non envoyé : {exc}"
    prefix = "[simulation] " if sent.dry_run else ""
    return True, f"{prefix}Rapport envoyé par e-mail à {email.to}"


def build_weekly_report(ctx: AppContext, week_end: date | datetime | None = None, *,
                        send_email: bool | None = None, mailer: Any = None) -> WeeklyReport:
    """Write the report of ``week_end``'s week, notify its summary and (optionally) e-mail it to the candidate.

    ``send_email`` None → ``reports.weekly_email``. Raises ValueError for a future week.
    """
    now = clock.now()
    day = _as_date(week_end) if week_end is not None else clock.to_paris(now).date()
    start, end = week_bounds(day, now=now)
    stats = compute_week_stats(ctx, start, end, now=now)
    text = render_weekly(ctx.profile, stats, generated_at=now)
    path = _write(ctx, day, text)
    details = [f"Rapport écrit : {path}"]
    emailed = False
    if wants_email(ctx) if send_email is None else send_email:
        emailed, line = _email(ctx, day, text, mailer)
        details.append(line)
    report = WeeklyReport(
        week=week_label(day), start=start, end=end, path=str(path), offers=len(stats.jobs),
        offers_per_source=stats.per_source, applications=len(stats.applications),
        applications_per_channel=stats.per_channel, responses=len(stats.responses), interviews=stats.interviews,
        pending_manual=stats.pending_manual, upcoming_followups=len(stats.upcoming), emailed=emailed, details=details,
    )
    notify(ctx, f"Rapport hebdomadaire — semaine {day.isocalendar().week}", report.summary())
    log.info("Rapport hebdomadaire écrit : %s (%s → %s)", path, start, last_day(stats))
    return report


# --------------------------------------------------------------------------- schedule


def weekly_report_due(ctx: AppContext, today: date | None = None) -> date | None:
    """``week_end`` of the report due today, or None (disabled, not the day, too late, already written)."""
    cfg = ctx.settings.reports
    if not cfg.weekly_report:
        return None
    if not 0 <= cfg.weekly_report_day <= 6:
        log.warning("reports.weekly_report_day invalide (%s) : 0 = lundi … 6 = dimanche.", cfg.weekly_report_day)
        return None
    day = today if today is not None else _today()
    report_day = day - timedelta(days=(day.weekday() - cfg.weekly_report_day) % 7)
    if (day - report_day).days > CATCH_UP_DAYS:
        return None
    week_end = report_day - timedelta(days=1) if cfg.weekly_report_day == 0 else report_day
    if ctx.repo.kv_get(DONE_KV_PREFIX + week_label(week_end)):
        return None
    return week_end


def scheduled_weekly_report(ctx: AppContext, *, mailer_factory: Callable[[], Any] | None = None,
                            today: date | None = None) -> WeeklyReport | None:
    """Pipeline hook: the due weekly report (once per week), None when nothing is due."""
    week_end = weekly_report_due(ctx, today)
    if week_end is None:
        return None
    send = wants_email(ctx)
    mailer = mailer_factory() if send and mailer_factory is not None else None
    report = build_weekly_report(ctx, week_end, send_email=send, mailer=mailer)
    ctx.repo.kv_set(DONE_KV_PREFIX + report.week, clock.iso(clock.now()) or "1")
    return report
