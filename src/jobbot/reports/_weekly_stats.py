"""Numbers of the weekly report, computed from the repository only (no network, no LLM).

Window = [start, end). Offers and applications are counted in the window; response rates are cumulative
(an application sent this week rarely has an answer yet), bounced addresses excluded.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from jobbot.models import Application, ApplicationEvent, ApplicationStatus, Company, JobOffer, JobStatus

if TYPE_CHECKING:
    from jobbot.context import AppContext

KEPT_STATUSES = frozenset({JobStatus.SHORTLISTED, JobStatus.DOCS_READY, JobStatus.PENDING_REVIEW, JobStatus.APPROVED,
                           JobStatus.APPLIED, JobStatus.NEEDS_MANUAL})
ANSWERED_STATUSES = frozenset({ApplicationStatus.RESPONSE, ApplicationStatus.INTERVIEW, ApplicationStatus.OFFER,
                               ApplicationStatus.REJECTED})
OPEN_STATUSES = frozenset({ApplicationStatus.APPLIED, ApplicationStatus.FOLLOWED_UP})
RESPONSE_EVENTS = frozenset({"reply", "interview", "rejected"})
SENT_KINDS = ("application", "spontaneous", "followup")
UPCOMING_DAYS = 7
SPONTANEOUS = "candidature spontanée"


@dataclass(frozen=True)
class Rate:
    name: str
    total: int
    answered: int
    week: int = 0

    @property
    def percent(self) -> str:
        return f"{round(100 * self.answered / self.total)} %" if self.total else "—"


@dataclass(frozen=True)
class WeekStats:
    start: datetime
    end: datetime
    jobs: tuple[JobOffer, ...]
    per_source: dict[str, int]
    kept_per_source: dict[str, int]
    applications: tuple[Application, ...]  # sent in the window, oldest first
    per_channel: dict[str, int]
    responses: tuple[tuple[Application, ApplicationEvent | None], ...]
    interviews: int
    channel_rates: tuple[Rate, ...]
    source_rates: tuple[Rate, ...]
    needs_manual: tuple[JobOffer, ...]
    pending_jobs: int
    pending_companies: int
    manual_followups: tuple[tuple[Application, ApplicationEvent], ...]
    upcoming: tuple[Application, ...]
    emails_sent: int
    jobs_by_id: dict[str, JobOffer]
    companies_by_id: dict[str, Company]  # companies of the week's spontaneous applications

    @property
    def pending_manual(self) -> int:
        return len(self.needs_manual) + self.pending_jobs + self.pending_companies + len(self.manual_followups)


def channel_name(app: Application) -> str:
    from jobbot.cli_support import METHOD_FR, label

    return label(METHOD_FR, app.method)


def source_name(app: Application, jobs_by_id: dict[str, JobOffer]) -> str:
    job = jobs_by_id.get(app.job_id or "")
    if job is not None:
        return job.source
    return SPONTANEOUS if app.job_id is None else "offre supprimée"


def _in(value: datetime | None, start: datetime, end: datetime) -> bool:
    return value is not None and start <= value < end


def _answered(app: Application) -> bool:
    return app.last_response_at is not None or app.status in ANSWERED_STATUSES


def _rates(apps: Iterable[Application], week: set[int | None], key: Callable[[Application], str]) -> tuple[Rate, ...]:
    totals: Counter[str] = Counter()
    answered: Counter[str] = Counter()
    recent: Counter[str] = Counter()
    for app in apps:
        if app.status == ApplicationStatus.BOUNCED:
            continue
        name = key(app)
        totals[name] += 1
        answered[name] += int(_answered(app))
        recent[name] += int(app.id in week)
    return tuple(Rate(n, totals[n], answered[n], recent[n]) for n in sorted(totals, key=lambda n: (-totals[n], n)))


def _responses(apps: list[Application], events: dict[int, list[ApplicationEvent]], start: datetime,
               end: datetime) -> list[tuple[Application, ApplicationEvent | None]]:
    out: list[tuple[Application, ApplicationEvent | None]] = []
    for app in apps:
        hits = [e for e in events.get(app.id or 0, []) if e.kind in RESPONSE_EVENTS and _in(e.at, start, end)]
        if hits:
            out.append((app, hits[-1]))
        elif _in(app.last_response_at, start, end):
            out.append((app, None))
    return sorted(out, key=lambda pair: pair[1].at if pair[1] else pair[0].last_response_at)


def _interviews(apps: list[Application], events: dict[int, list[ApplicationEvent]], start: datetime,
                end: datetime) -> int:
    """Applications with an interview invitation received in the window."""
    return sum(1 for app in apps
               if any(e.kind == "interview" and _in(e.at, start, end) for e in events.get(app.id or 0, [])))


def _emails_sent(ctx: AppContext, start: datetime, end: datetime) -> int:
    repo = ctx.repo
    return sum(repo.count_sent_emails_since(start, kind) - repo.count_sent_emails_since(end, kind)
               for kind in SENT_KINDS)


def _pending(ctx: AppContext, apps: list[Application], events: dict[int, list[ApplicationEvent]],
             start: datetime, end: datetime) -> tuple[tuple[JobOffer, ...], int, int, tuple]:
    manual = tuple(ctx.repo.list_jobs(JobStatus.NEEDS_MANUAL))
    followups = tuple((app, e) for app in apps if app.status in OPEN_STATUSES
                      for e in events.get(app.id or 0, []) if e.kind == "manual_followup" and _in(e.at, start, end))
    return (manual, len(ctx.repo.list_jobs(JobStatus.PENDING_REVIEW)), len(ctx.repo.list_companies("pending_review")),
            followups)


def compute_week_stats(ctx: AppContext, start: datetime, end: datetime, *, now: datetime) -> WeekStats:
    all_jobs = ctx.repo.list_jobs(order="found_at DESC")
    jobs_by_id = {job.id: job for job in all_jobs}
    week_jobs = tuple(j for j in all_jobs if _in(j.found_at, start, end))
    apps = ctx.repo.list_applications()
    events = {app.id: ctx.repo.list_events(app.id) for app in apps if app.id is not None}
    week_apps = tuple(sorted((a for a in apps if _in(a.applied_at, start, end)), key=lambda a: a.applied_at))
    week_ids = {a.id for a in week_apps}
    responses = _responses(apps, events, start, end)
    manual, pending_jobs, pending_companies, followups = _pending(ctx, apps, events, start, end)
    horizon = now + timedelta(days=UPCOMING_DAYS)
    upcoming = tuple(sorted((a for a in apps if a.status in OPEN_STATUSES and _in(a.next_followup_at, now, horizon)),
                            key=lambda a: a.next_followup_at))
    return WeekStats(
        start=start, end=end, jobs=week_jobs, per_source=dict(Counter(j.source for j in week_jobs)),
        kept_per_source=dict(Counter(j.source for j in week_jobs if j.status in KEPT_STATUSES)),
        applications=week_apps, per_channel=dict(Counter(channel_name(a) for a in week_apps)),
        responses=tuple(responses),
        interviews=_interviews(apps, events, start, end),
        channel_rates=_rates(apps, week_ids, channel_name),
        source_rates=_rates(apps, week_ids, lambda a: source_name(a, jobs_by_id)),
        needs_manual=manual, pending_jobs=pending_jobs, pending_companies=pending_companies,
        manual_followups=followups, upcoming=upcoming, emails_sent=_emails_sent(ctx, start, end), jobs_by_id=jobs_by_id,
        companies_by_id=_companies(ctx, week_apps),
    )


def _companies(ctx: AppContext, apps: Iterable[Application]) -> dict[str, Company]:
    found = {cid: ctx.repo.get_company(cid) for cid in {a.company_id for a in apps if a.company_id}}
    return {cid: company for cid, company in found.items() if company is not None}
