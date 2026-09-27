"""Daily run: classroom → collect → score → prepare → apply → discover → enrich → spontaneous → inbox → followup.

``classroom`` (first, the school's offers are the most valuable) runs one check of the Google Classroom
stream whatever the time (``classroom.active_hours`` only limits the frequent scheduled watcher).

Every step is isolated (an exception is recorded as a French error string and the next step runs); the run
ends with a notification summary, then the weekly report when it is due (``reports.weekly_report_day``).
Mode decisions use ``preferences.effective_mode`` (warm-up → review).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jobbot import clock
from jobbot.apply.service import ApplyReport, dedup_reason, set_job_status
from jobbot.classroom.watch import ClassroomReport
from jobbot.context import AppContext
from jobbot.followup import FollowupReport
from jobbot.mail.messages import InboxReport
from jobbot.matching.match import MatchReport
from jobbot.models import JobOffer, JobStatus
from jobbot.outreach.companies import DiscoverReport
from jobbot.outreach.emails import EnrichReport
from jobbot.outreach.spontaneous import CampaignReport
from jobbot.reports.weekly import WeeklyReport
from jobbot.sources.registry import CollectReport
from jobbot.textutil import truncate

log = logging.getLogger(__name__)

STEPS: tuple[str, ...] = (
    "classroom", "collect", "score", "prepare", "apply", "discover", "enrich", "spontaneous", "inbox", "followup",
)
PREPARE_LIMIT = 10  # tailored CV + letter per run (LLM time), best match scores first
PREPARE_MAX_FAILURES = 3  # a job whose documents keep failing leaves the SHORTLISTED queue (status FAILED)
ENRICH_LIMIT = 50
LOCK_FILE = "jobbot.lock"
LOCK_STALE_S = 6 * 3600
MAX_ERROR_CHARS = 300
LOGIN_ALERT_KV = "notify:login_alert:"  # + day: one « session à renouveler » notification per day
LOGIN_HELP = ("reconnecte-toi avec « jobbot login … » ; sur un serveur sans écran : login sur le PC, "
              "« jobbot session export all » puis « python scripts/deploy_server.py --only private ».")


class PipelineBusy(RuntimeError):
    """Another jobbot run holds the lock (scheduled task + manual run)."""


class PrepareReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prepared: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[str] = Field(default_factory=list)


class DailyReport(BaseModel):
    """One field per step: that step's report, an error string, or None when the step did not run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classroom: ClassroomReport | str | None = None
    collect: CollectReport | str | None = None
    score: MatchReport | str | None = None
    prepare: PrepareReport | str | None = None
    apply: ApplyReport | str | None = None
    discover: DiscoverReport | str | None = None
    enrich: EnrichReport | str | None = None
    spontaneous: CampaignReport | str | None = None
    inbox: InboxReport | str | None = None
    followup: FollowupReport | str | None = None
    weekly: WeeklyReport | str | None = None  # not a step: written after the run on reports.weekly_report_day

    def errors(self) -> dict[str, str]:
        return {step: value for step in STEPS if isinstance(value := getattr(self, step), str)}

    def summary(self) -> str:
        """Short French summary (notification / CLI)."""
        parts = [text for step in STEPS if (text := _describe(step, getattr(self, step)))]
        if self.errors():
            parts.append(f"{len(self.errors())} étape(s) en erreur : {', '.join(self.errors())}")
        return " · ".join(parts) or "Rien à signaler."


def _describe(step: str, report: Any) -> str:
    if report is None or isinstance(report, str):
        return ""
    texts: dict[str, Callable[[Any], str]] = {
        "classroom": lambda r: (f"école : {r.offers} offre(s), {r.applied + r.simulated} candidature(s)"
                                if r.offers or r.applied or r.simulated else ""),
        "collect": lambda r: f"{r.new} nouvelle(s) offre(s)",
        "score": lambda r: f"{r.shortlisted} retenue(s)",
        "prepare": lambda r: f"{r.prepared} dossier(s) prêt(s)",
        "apply": lambda r: (f"{r.applied} candidature(s) envoyée(s)"
                            + (f", {r.needs_manual} à finir à la main" if r.needs_manual else "")
                            + (f", {r.pending_review} à valider" if r.pending_review else "")),
        "spontaneous": lambda r: (f"{r.sent} spontanée(s)"
                                  + (f" ({r.irrelevant} entreprise(s) hors cible écartée(s))" if r.irrelevant else "")),
        "inbox": lambda r: f"{r.matched} réponse(s)" if r.matched else "",
        "followup": lambda r: f"{r.sent} relance(s)" if r.sent or r.manual else "",
    }
    describe = texts.get(step)
    return describe(report) if describe else ""


# --------------------------------------------------------------------------- helpers


def with_effective_mode(ctx: AppContext) -> AppContext:
    """Context whose ``settings.mode`` is the effective one (review during the warm-up)."""
    from jobbot.preferences import effective_mode

    mode = effective_mode(ctx.settings)
    if mode == ctx.settings.mode:
        return ctx
    return dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={"mode": mode}))


def _label(job: JobOffer) -> str:
    return f"{job.company or 'Entreprise inconnue'} — {job.title}"


def _rescore(ctx: AppContext, before: JobOffer, job: JobOffer) -> str | None:
    """Score a shortlisted offer (search card, older rules) or a just completed one again before its documents;
    the French reason when it no longer qualifies (it then leaves the queue: FILTERED_OUT)."""
    completed = (job.description or "") != (before.description or "")
    if not completed and job.status != JobStatus.SHORTLISTED:
        return None
    from jobbot.matching.match import match_job

    (score, reasons), threshold = match_job(job, ctx.profile), ctx.settings.thresholds.min_match_score
    keep = score >= threshold
    ctx.repo.set_job_match(job.id, score, reasons, job.status if keep else JobStatus.FILTERED_OUT)
    if keep:
        return None
    why = reasons[0] if score <= 0 and reasons else f"score {score:.0f} < {threshold:.0f}"
    when = "après lecture de l'annonce complète" if completed else "à la nouvelle notation"
    return f"écartée {when} ({why})"


def _prepare_one(ctx: AppContext, job: JobOffer) -> tuple[str, str]:
    from jobbot.documents.service import prepare_documents
    from jobbot.sources.registry import ensure_details

    dup = dedup_reason(ctx, job)
    if dup is not None:  # leave the SHORTLISTED queue, or it would block the best-first batch forever
        set_job_status(ctx, job, dup[1], dup[0].capitalize())
        return "skipped", dup[0]
    before, job = job, ensure_details(ctx, job)
    if job.status == JobStatus.EXPIRED:
        return "skipped", "offre expirée"
    dropped = _rescore(ctx, before, job)
    if dropped is not None:
        return "skipped", dropped
    docs = prepare_documents(ctx, job=job)
    ats = f"CV ATS {docs.cv_ats.score:.0f}/100" if docs.cv_ats else "CV prêt"
    return "prepared", ats


def prepare_shortlisted(ctx: AppContext, *, limit: int = PREPARE_LIMIT,
                        job_ids: list[str] | None = None) -> PrepareReport:
    """Tailored documents for the best SHORTLISTED jobs (or the given ids). One failure never stops the batch."""
    if job_ids is not None:
        jobs = [job for job in (ctx.repo.get_job(i) for i in job_ids) if job is not None]
    else:
        jobs = ctx.repo.list_jobs(JobStatus.SHORTLISTED, limit=limit)
    counts = {"prepared": 0, "failed": 0, "skipped": 0}
    details: list[str] = []
    for job in jobs:
        try:
            kind, detail = _prepare_one(ctx, job)
        except Exception as exc:
            log.exception("Documents impossibles pour %s", job.id)
            kind, detail = "failed", truncate(f"erreur : {exc}", MAX_ERROR_CHARS) + _note_failure(ctx, job, exc)
        counts[kind] += 1
        details.append(f"{_label(job)} : {detail}")
    return PrepareReport(**counts, details=details)


def _note_failure(ctx: AppContext, job: JobOffer, exc: Exception) -> str:
    """Count failed preparations; after PREPARE_MAX_FAILURES the job leaves the queue (FAILED)."""
    try:
        fresh = ctx.repo.get_job(job.id) or job
        failures = int(fresh.raw.get("prepare_failures") or 0) + 1
        give_up = failures >= PREPARE_MAX_FAILURES and fresh.status == JobStatus.SHORTLISTED
        raw = {**fresh.raw, "prepare_failures": failures, "apply_detail": truncate(f"Documents : {exc}", 300)}
        ctx.repo.save_job(fresh.model_copy(update={"raw": raw, **({"status": JobStatus.FAILED} if give_up else {})}))
    except Exception:  # bookkeeping only
        log.debug("Échec de préparation non enregistré pour %s", job.id, exc_info=True)
        return ""
    return f" ({failures}e échec : offre mise de côté)" if give_up else ""


class _Shared:
    """Mailer built once per run, only when a step needs it."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._mailer: Any = None
        self._built = False

    def mailer(self) -> Any:
        if not self._built:
            from jobbot.mail.mailer import build_mailer

            self._mailer, self._built = build_mailer(self.ctx), True
        return self._mailer


# --------------------------------------------------------------------------- steps


def _classroom(ctx: AppContext, shared: _Shared) -> ClassroomReport:
    from jobbot.classroom.watch import run_classroom

    return run_classroom(ctx, respect_hours=False)


def _collect(ctx: AppContext, shared: _Shared) -> CollectReport:
    from jobbot.sources.registry import collect_jobs

    return collect_jobs(ctx)


def score_new_offers(ctx: AppContext) -> MatchReport:
    """« score » step: short search cards read in full first (capped per run, never blocking), then scoring."""
    from jobbot.matching.match import score_new_jobs
    from jobbot.sources.registry import complete_new_offers

    try:
        completed = complete_new_offers(ctx)
    except Exception:
        log.exception("Lecture des annonces complètes impossible avant la notation")
        completed = 0
    return score_new_jobs(ctx).model_copy(update={"completed": completed})


def _score(ctx: AppContext, shared: _Shared) -> MatchReport:
    return score_new_offers(ctx)


def _prepare(ctx: AppContext, shared: _Shared) -> PrepareReport:
    return prepare_shortlisted(ctx)


def _apply(ctx: AppContext, shared: _Shared) -> ApplyReport:
    from jobbot.apply.service import apply_to_jobs

    report = apply_to_jobs(ctx)
    _alert_expired_session(ctx, report)
    return report


def _alert_expired_session(ctx: AppContext, report: ApplyReport) -> None:
    """A channel stopped for « jobbot login » (LinkedIn / HelloWork session lost): urgent notice, once a day."""
    lines = [d for d in report.details if "jobbot login" in d]
    if not lines:
        return
    key = f"{LOGIN_ALERT_KV}{clock.to_paris(clock.now()).date().isoformat()}"
    if ctx.repo.kv_get(key):
        return
    ctx.repo.kv_set(key, "1")
    from jobbot.notify import notify

    notify(ctx, "Session à renouveler", f"{truncate(lines[0], 160)} — {LOGIN_HELP}", level="urgent")


def _discover(ctx: AppContext, shared: _Shared) -> DiscoverReport:
    from jobbot.outreach.companies import discover_companies

    return discover_companies(ctx)


def _enrich(ctx: AppContext, shared: _Shared) -> EnrichReport:
    from jobbot.outreach.emails import enrich_companies

    return enrich_companies(ctx, limit=ENRICH_LIMIT)


def spontaneous_applications(ctx: AppContext, mailer: Any, *, limit: int | None = None) -> CampaignReport:
    """LBA-relayed applications to « recruteurs LBA » first, then the e-mail campaign (effective mode)."""
    from jobbot.apply.lba_recruiters import run_lba_recruiters
    from jobbot.outreach.spontaneous import run_spontaneous_campaign

    effective = with_effective_mode(ctx)
    try:
        relay = run_lba_recruiters(effective, limit=limit)
    except Exception as exc:  # the e-mail campaign still runs
        log.exception("Relais La Bonne Alternance en échec")
        relay = ApplyReport(failed=1, details=[truncate(f"erreur : {exc}", MAX_ERROR_CHARS)])
    email = run_spontaneous_campaign(effective, mailer, limit=limit)
    return CampaignReport(
        sent=email.sent + relay.applied, skipped=email.skipped + relay.skipped + relay.pending_review,
        failed=email.failed + relay.failed, irrelevant=email.irrelevant + getattr(relay, "irrelevant", 0),
        details=[*(f"LBA — {d}" for d in relay.details), *email.details],
    )


def _spontaneous(ctx: AppContext, shared: _Shared) -> CampaignReport:
    return spontaneous_applications(ctx, shared.mailer())


def _inbox(ctx: AppContext, shared: _Shared) -> InboxReport:
    from jobbot.mail.inbox import process_inbox

    return process_inbox(ctx)


def _followup(ctx: AppContext, shared: _Shared) -> FollowupReport:
    from jobbot.followup import run_followups

    return run_followups(ctx, shared.mailer())


STEP_FUNCTIONS: dict[str, Callable[[AppContext, _Shared], Any]] = {
    "classroom": _classroom, "collect": _collect, "score": _score, "prepare": _prepare, "apply": _apply, "discover": _discover,
    "enrich": _enrich, "spontaneous": _spontaneous, "inbox": _inbox, "followup": _followup,
}


def validate_steps(steps: list[str] | None) -> list[str]:
    """Requested steps in pipeline order; ValueError (French) on an unknown name."""
    if steps is None:
        return list(STEPS)
    wanted = [s.strip().lower() for s in steps if s and s.strip()]
    unknown = [s for s in wanted if s not in STEPS]
    if unknown:
        raise ValueError(f"Étape(s) inconnue(s) : {', '.join(unknown)} (possibles : {', '.join(STEPS)})")
    return [s for s in STEPS if s in wanted]


@contextmanager
def run_lock(ctx: AppContext, lock_file: str = LOCK_FILE, stale_s: float = LOCK_STALE_S) -> Iterator[None]:
    """Exclusive lock file in the data folder (a stale lock older than ``stale_s``, 6 h, is taken over)."""
    path = ctx.path(f"{ctx.settings.paths.data_dir}/{lock_file}")
    if path.exists() and time.time() - path.stat().st_mtime < stale_s:
        raise PipelineBusy(f"Une exécution de jobbot est déjà en cours (verrou {path}). Réessaie plus tard.")
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Verrou %s non supprimé : %s", path, exc)


def _run_step(ctx: AppContext, name: str, shared: _Shared) -> Any:
    log.info("Étape « %s »…", name)
    try:
        return STEP_FUNCTIONS[name](ctx, shared)
    except Exception as exc:  # one step never stops the run
        log.exception("Étape « %s » en échec", name)
        return truncate(f"Erreur : {exc}", MAX_ERROR_CHARS)


def run_daily(ctx: AppContext, *, steps: list[str] | None = None) -> DailyReport:
    """Run the requested steps (default: all) in order, notify a summary, then write the weekly report if due."""
    selected = validate_steps(steps)
    shared = _Shared(ctx)
    results: dict[str, Any] = {}
    with run_lock(ctx):
        for name in selected:
            results[name] = _run_step(ctx, name, shared)
    report = DailyReport(**results)
    _notify(ctx, report)
    weekly = _weekly(ctx, shared)
    return report if weekly is None else report.model_copy(update={"weekly": weekly})


def _weekly(ctx: AppContext, shared: _Shared) -> WeeklyReport | str | None:
    """Weekly report on ``reports.weekly_report_day`` (once per week, catch-up included). Never raises."""
    from jobbot.reports import weekly

    try:
        return weekly.scheduled_weekly_report(ctx, mailer_factory=shared.mailer)
    except Exception as exc:  # a report must never fail the daily run
        log.exception("Rapport hebdomadaire en échec")
        return truncate(f"Erreur : {exc}", MAX_ERROR_CHARS)


def _notify(ctx: AppContext, report: DailyReport) -> None:
    try:
        from jobbot.notify import notify

        title = "jobbot — bilan" + (" (simulation)" if ctx.dry_run else "")
        notify(ctx, title, report.summary())
    except Exception:  # notification is best effort
        log.debug("Notification du bilan impossible", exc_info=True)
