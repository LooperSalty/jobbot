"""Sending ramp of spontaneous e-mails (``settings.limits.spontaneous_ramp``) — the real bottleneck is deliverability.

Effective daily cap = ``start + step_per_week × full weeks`` since the first real spontaneous e-mail (or since the
last reset), at most ``max``. When the bounce rate of the spontaneous e-mails of the last 7 days exceeds
``max_bounce_rate``, the cap falls back to ``start`` at once; :func:`check_reset` (called by the campaign) records
the reset (kv ``ramp:spontaneous:reset_at``: the weeks count again from there) and notifies the user once.
Disabled ramp → ``limits.spontaneous_per_day``.

Pure reads everywhere (``Pacer.quota``, status, dashboard): only :func:`check_reset` writes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from jobbot import clock
from jobbot.models import Application, ApplicationStatus, ApplyMethod

if TYPE_CHECKING:
    from jobbot.config import Limits
    from jobbot.context import AppContext
    from jobbot.db import Repository

log = logging.getLogger(__name__)

RESET_KEY = "ramp:spontaneous:reset_at"
WINDOW_DAYS = 7
WEEK_S = 7 * 24 * 3600


@dataclass(frozen=True)
class RampState:
    cap: int  # spontaneous e-mails allowed today
    enabled: bool
    weeks: int = 0  # full weeks since the reference (first e-mail / last reset)
    since: datetime | None = None
    sent: int = 0  # spontaneous e-mails of the last 7 days…
    bounced: int = 0  # …and how many bounced
    reset_due: bool = False  # bounce rate too high while the ramp was above its start


def is_spontaneous(app: Application) -> bool:
    """A spontaneous e-mail application (no offer, a company): only real sends create one."""
    return app.method == ApplyMethod.EMAIL and app.job_id is None and app.company_id is not None


def first_spontaneous_at(apps: list[Application]) -> datetime | None:
    dates = [clock.parse_dt(a.applied_at) for a in apps if is_spontaneous(a)]
    return min((d for d in dates if d is not None), default=None)


def _reset_at(repo: Any) -> datetime | None:
    try:
        return clock.parse_dt(repo.kv_get(RESET_KEY))
    except ValueError:
        return None


def _recent(apps: list[Application], now: datetime) -> tuple[int, int]:
    since = now - timedelta(days=WINDOW_DAYS)
    recent = [a for a in apps if is_spontaneous(a) and clock.parse_dt(a.applied_at) >= since]
    return len(recent), sum(a.status == ApplicationStatus.BOUNCED for a in recent)


def ramp_state(repo: Repository, limits: Limits, *, now: datetime | None = None,
               apps: list[Application] | None = None) -> RampState:
    """Today's cap and why (no write)."""
    cfg = limits.spontaneous_ramp
    if not cfg.enabled:
        return RampState(cap=int(limits.spontaneous_per_day), enabled=False)
    at = now or clock.now()
    history = apps if apps is not None else repo.list_applications()
    anchors = [d for d in (first_spontaneous_at(history), _reset_at(repo)) if d is not None]
    since = max(anchors) if anchors else None
    weeks = max(0, int((at - since).total_seconds() // WEEK_S)) if since else 0
    ramped = min(cfg.max, cfg.start + cfg.step_per_week * weeks)
    sent, bounced = _recent(history, at)
    too_many = sent > 0 and bounced / sent > cfg.max_bounce_rate
    return RampState(cap=cfg.start if too_many else ramped, enabled=True, weeks=weeks, since=since, sent=sent,
                     bounced=bounced, reset_due=too_many and ramped > cfg.start)


def spontaneous_cap(repo: Repository, limits: Limits, *, now: datetime | None = None) -> int:
    return ramp_state(repo, limits, now=now).cap


def describe(state: RampState, limits: Limits) -> str:
    """French one-liner for status / dashboard."""
    if not state.enabled:
        return f"{state.cap}/jour (montée progressive désactivée)"
    cfg = limits.spontaneous_ramp
    if state.sent and state.bounced / state.sent > cfg.max_bounce_rate:
        return (f"{state.cap}/jour — ramené au départ : {state.bounced} rebond(s) sur {state.sent} envoi(s) en "
                f"7 jours (> {cfg.max_bounce_rate:.0%})")
    if state.since is None:
        return f"{state.cap}/jour (montée progressive : démarre au premier envoi, +{cfg.step_per_week} par semaine)"
    return (f"{state.cap}/jour (montée progressive : semaine {state.weeks}, +{cfg.step_per_week} par semaine, "
            f"{cfg.max} max)")


def check_reset(ctx: AppContext, *, now: datetime | None = None) -> RampState:
    """Record a reset (weeks count again from now) and notify once when the bounce rate is too high."""
    state = ramp_state(ctx.repo, ctx.settings.limits, now=now)
    if not state.reset_due:
        return state
    cfg = ctx.settings.limits.spontaneous_ramp
    message = (f"Plafond des candidatures spontanées ramené à {cfg.start}/jour : {state.bounced} rebond(s) sur "
               f"{state.sent} envoi(s) ces 7 derniers jours (> {cfg.max_bounce_rate:.0%}). La montée reprend "
               f"dans une semaine.")
    log.warning(message)
    if ctx.dry_run:
        return state
    ctx.repo.kv_set(RESET_KEY, clock.iso(now or clock.now()) or "")
    from jobbot.notify import notify

    notify(ctx, "Envois spontanés : montée progressive réinitialisée", message, level="important")
    return ramp_state(ctx.repo, ctx.settings.limits, now=now)
