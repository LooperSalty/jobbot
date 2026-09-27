"""« jobbot night »: use the idle server to find companies and contacts — nothing is ever sent at night.

Steps, each isolated (an exception is recorded, the next step runs), within the time budget (until
``settings.night.until``, 06:30 Paris time, or ``--budget`` minutes; a step never starts once it is reached):

1. ``purge`` — GDPR retention of named contacts (:mod:`jobbot.night.purge`);
2. ``harvest`` — registry around every area, LBA recruiters, employers of the stored offers (≤ 35 % of the time);
3. ``enrich`` — parallel website / contact search (≤ 75 % of what is left);
4. ``prioritize`` — priority of every open company, ready queue;
5. ``prepare`` — documents for the next day's first targets (none in dry-run);

then one French report (notification « important » + kv for status / doctor / dashboard).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from jobbot.context import AppContext
from jobbot.night import budget
from jobbot.night.budget import Deadline, night_end
from jobbot.night.models import NightReport
from jobbot.textutil import truncate

log = logging.getLogger(__name__)

LOCK_FILE = "night.lock"
LOCK_MARGIN_S = 2 * 3600  # running items finish after the deadline
LOCK_STALE_S = budget.MAX_DEFAULT_HOURS * 3600 + LOCK_MARGIN_S  # a running night's lock is never taken over
HARVEST_SHARE = 0.35
ENRICH_SHARE = 0.75  # the rest: priority + documents
MAX_ERROR_CHARS = 300
STEP_LABELS = {"purge": "Rétention RGPD", "harvest": "Collecte des entreprises", "enrich": "Recherche des contacts",
               "prioritize": "Priorités", "prepare": "Dossiers du lendemain"}


@dataclass(frozen=True)
class StepOutcome:
    fields: dict = field(default_factory=dict)  # NightReport fields
    stopped: bool = False  # cut short by the time budget
    errors: tuple[str, ...] = ()
    details: tuple[str, ...] = ()


# --------------------------------------------------------------------------- steps


def _purge(ctx: AppContext, deadline: Deadline) -> StepOutcome:
    from jobbot.night.purge import purge_personal_data

    return StepOutcome({"purged": purge_personal_data(ctx)})


def _harvest(ctx: AppContext, deadline: Deadline) -> StepOutcome:
    from jobbot.night.harvest import harvest

    result = harvest(ctx, deadline.share(HARVEST_SHARE), store_until=deadline)
    return StepOutcome({"companies_found": result.found, "companies_new": result.new, "relevant": result.relevant,
                        "irrelevant": result.irrelevant}, result.stopped, tuple(result.errors), tuple(result.details))


def _enrich(ctx: AppContext, deadline: Deadline) -> StepOutcome:
    from jobbot.night.enrich import run_enrichment

    result = run_enrichment(ctx, deadline.share(ENRICH_SHARE))
    details = (f"{result.not_started} entreprise(s) à enrichir la nuit prochaine",) if result.not_started else ()
    return StepOutcome({"enriched": result.processed, "with_email": result.with_email,
                        "generic_emails": result.generic_emails, "nominative_emails": result.nominative_emails},
                       result.not_started > 0, details=details)


def _prioritize(ctx: AppContext, deadline: Deadline) -> StepOutcome:
    from jobbot.night.prioritize import prioritize

    _, queue = prioritize(ctx, deadline)
    return StepOutcome({"queue_ready": queue}, deadline.expired())


def _prepare(ctx: AppContext, deadline: Deadline) -> StepOutcome:
    from jobbot.night.prepare import prepare_ahead

    cap = _daily_cap(ctx)
    result = prepare_ahead(ctx, deadline, daily_cap=cap)
    details = [*result.details]
    if result.planned:
        details.append(f"[simulation] {result.planned} dossier(s) auraient été préparés")
    return StepOutcome({"docs_prepared": result.prepared, "daily_cap": cap}, result.stopped, details=tuple(details))


STEPS: dict[str, Callable[[AppContext, Deadline], StepOutcome]] = {
    "purge": _purge, "harvest": _harvest, "enrich": _enrich, "prioritize": _prioritize, "prepare": _prepare,
}


def _daily_cap(ctx: AppContext) -> int:
    from jobbot.ramp import spontaneous_cap

    return spontaneous_cap(ctx.repo, ctx.settings.limits)


def _final_counts(ctx: AppContext, fields: dict) -> dict:
    """Queue size and daily cap even when their steps did not run (never raises)."""
    out = dict(fields)
    try:
        if "daily_cap" not in out:
            out["daily_cap"] = _daily_cap(ctx)
        if "queue_ready" not in out:
            from jobbot.outreach.priority import ready_queue

            out["queue_ready"] = len(ready_queue(ctx))
    except Exception:
        log.exception("File d'attente de nuit non calculée")
    return out


# --------------------------------------------------------------------------- run


def _run_step(ctx: AppContext, name: str, deadline: Deadline) -> StepOutcome:
    log.info("Nuit — étape « %s »…", name)
    try:
        return STEPS[name](ctx, deadline)
    except Exception as exc:  # one step never stops the night
        log.exception("Nuit — étape « %s » en échec", name)
        return StepOutcome(errors=(truncate(f"{STEP_LABELS[name]} : {exc}", MAX_ERROR_CHARS),))


def run_steps(ctx: AppContext, deadline: Deadline) -> tuple[dict, bool, list[str], list[str]]:
    """(report fields, stopped early, errors, details) of every step, in order, within the deadline."""
    fields: dict = {}
    errors: list[str] = []
    details: list[str] = []
    stopped = False
    for name in STEPS:
        if deadline.expired():
            stopped = True
            details.append(f"{STEP_LABELS[name]} : non lancée (heure de fin atteinte)")
            continue
        outcome = _run_step(ctx, name, deadline)
        fields.update(outcome.fields)
        stopped = stopped or outcome.stopped
        errors.extend(outcome.errors)
        details.extend(outcome.details)
    return fields, stopped, errors, details


def run_night(ctx: AppContext, *, budget_minutes: int | None = None) -> NightReport:
    """Every night step within the time budget, then the report (notified). Nothing is sent. Raises
    ``pipeline.PipelineBusy`` when another night run holds the lock."""
    from jobbot.night.report import publish
    from jobbot.pipeline import run_lock

    start = budget.now()
    deadline = Deadline(night_end(ctx.settings.night.until, start=start, budget_minutes=budget_minutes))
    stale_s = max(LOCK_STALE_S, (deadline.end - start).total_seconds() + LOCK_MARGIN_S)  # --budget may be longer
    with run_lock(ctx, LOCK_FILE, stale_s=stale_s):
        fields, stopped, errors, details = run_steps(ctx, deadline)
        fields = _final_counts(ctx, fields)
    duration = round((budget.now() - start).total_seconds() / 60, 1)
    report = NightReport(**fields, duration_min=duration, stopped_early=stopped, dry_run=ctx.dry_run,
                         errors=errors, details=details)
    publish(ctx, report)
    log.info("%s", report.summary())
    return report
