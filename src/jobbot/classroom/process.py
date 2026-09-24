"""One new school-relayed offer: score → documents → auto-apply decision → application → notification.

Decision (``review_reason``): ``classroom.auto_apply`` applies even during the warm-up review period (explicit
wish of the user), but never when ``mode: review`` is set, never under the score threshold
(``classroom.min_score``, default ``thresholds.auto_apply_min_score``) and never with a CV carrying truth
warnings. An offer cleared for auto-apply becomes APPROVED (the standing rule is the user's decision) and is
sent at once through ``apply.service.apply_to_jobs`` (quota, blacklist, company dedup, dry-run); a channel
problem (quota, browser busy…) leaves it APPROVED and the next check retries it (``MAX_ATTEMPTS``).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from jobbot.apply.service import (
    ApplyReport,
    apply_to_jobs,
    build_pacer,
    channel_of,
    dedup_reason,
    set_job_status,
    truth_warnings,
)
from jobbot.context import AppContext
from jobbot.models import JobOffer, JobStatus
from jobbot.textutil import truncate

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
AUTO_DETAIL = "Validée automatiquement : offre relayée par l'école (classroom.auto_apply)."
MAX_REASON_CHARS = 160
TITLES = {
    "applied": "Classroom : candidature envoyée", "simulated": "Classroom : candidature simulée (--dry-run)",
    "pending_review": "Classroom : offre à valider", "needs_manual": "Classroom : candidature à faire toi-même",
    "filtered_out": "Classroom : offre écartée", "skipped": "Classroom : offre ignorée",
    "deferred": "Classroom : candidature reportée", "failed": "Classroom : échec de la candidature",
}


KIND_FR = {
    "applied": "candidature envoyée", "simulated": "candidature simulée", "pending_review": "à valider",
    "needs_manual": "à faire à la main", "filtered_out": "écartée", "skipped": "ignorée",
    "deferred": "reportée", "failed": "échec",
}


class Outcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str  # applied | simulated | pending_review | needs_manual | filtered_out | skipped | deferred | failed
    detail: str = ""

    def kind_fr(self) -> str:
        return KIND_FR.get(self.kind, self.kind)


def label(job: JobOffer) -> str:
    return f"{job.company or 'Entreprise inconnue'} — {job.title}"


# --------------------------------------------------------------------------- decision


def auto_threshold(ctx: AppContext) -> float:
    cfg = ctx.settings.classroom
    return cfg.min_score if cfg.min_score is not None else ctx.settings.thresholds.auto_apply_min_score


def review_reason(ctx: AppContext, job: JobOffer, *, check_truth: bool = True) -> str | None:
    """Why this offer must wait for the user's validation (None = apply now)."""
    if not ctx.settings.classroom.auto_apply:
        return "candidature automatique désactivée (classroom.auto_apply)"
    if ctx.settings.mode == "review":
        return "mode review : tu valides chaque candidature"
    threshold = auto_threshold(ctx)
    if (job.match_score or 0) < threshold:
        return f"score {job.match_score or 0:.0f} sous le seuil de candidature automatique ({threshold:.0f})"
    warnings = truth_warnings(ctx, job) if check_truth else []
    return f"CV à vérifier ({' ; '.join(warnings[:3])})" if warnings else None


# --------------------------------------------------------------------------- processing


def process_offer(ctx: AppContext, job: JobOffer) -> Outcome:
    """Score, prepare and (maybe) apply to a NEW relayed offer. Exceptions propagate to the caller."""
    from jobbot.documents.service import prepare_documents
    from jobbot.matching.match import score_one
    from jobbot.sources.registry import ensure_details

    job = score_one(ctx, ensure_details(ctx, job))
    reason = job.match_reasons[0] if job.match_reasons else f"score {job.match_score or 0:.0f}"
    if job.status == JobStatus.SKIPPED:
        return Outcome(kind="skipped", detail=reason)
    if job.status != JobStatus.SHORTLISTED:
        detail = reason if (job.match_score or 0) == 0 else f"score {job.match_score or 0:.0f} trop bas"
        return Outcome(kind="filtered_out", detail=detail)
    dup = dedup_reason(ctx, job)
    if dup is not None:
        set_job_status(ctx, job, dup[1], dup[0].capitalize())
        return Outcome(kind="skipped", detail=dup[0])
    prepare_documents(ctx, job=job)
    return submit(ctx, ctx.repo.get_job(job.id) or job)


def submit(ctx: AppContext, job: JobOffer) -> Outcome:
    """Auto-apply decision then immediate application of one prepared (or approved) offer."""
    hold = review_reason(ctx, job)
    if hold is not None:
        set_job_status(ctx, job, JobStatus.PENDING_REVIEW, f"En attente de validation : {hold}.")
        return Outcome(kind="pending_review", detail=hold)
    channel = channel_of(job)
    attempts = int(job.raw.get("classroom_attempts") or 0)
    if channel != "external" and build_pacer(ctx).remaining(channel) <= 0:
        _approve(ctx, job, attempts)
        return Outcome(kind="deferred", detail=f"quota {channel} du jour atteint : nouvel essai au prochain passage")
    _approve(ctx, job, attempts + 1)
    report = apply_to_jobs(ctx, job_ids=[job.id])
    return _from_report(ctx, job, report)


def _approve(ctx: AppContext, job: JobOffer, attempts: int) -> None:
    raw = {**job.raw, "classroom_attempts": attempts, "classroom_auto": True}
    ctx.repo.save_job(job.model_copy(update={"raw": raw}))
    if job.status != JobStatus.APPROVED:
        set_job_status(ctx, ctx.repo.get_job(job.id) or job, JobStatus.APPROVED, AUTO_DETAIL)


def _from_report(ctx: AppContext, job: JobOffer, report: ApplyReport) -> Outcome:
    detail = report.details[-1] if report.details else ""
    detail = detail.removeprefix(f"{label(job)} : ")
    if report.applied:
        return Outcome(kind="applied", detail=detail)
    if report.needs_manual:
        return Outcome(kind="needs_manual", detail=detail)
    if report.failed:
        return Outcome(kind="failed", detail=detail)
    if ctx.dry_run and "[dry-run]" in detail:
        return Outcome(kind="simulated", detail=detail)
    stored = ctx.repo.get_job(job.id)
    final = stored is not None and stored.status in (JobStatus.SKIPPED, JobStatus.APPLIED, JobStatus.EXPIRED)
    return Outcome(kind="skipped" if final else "deferred", detail=detail)


def retry_approved(ctx: AppContext) -> list[tuple[JobOffer, Outcome]]:
    """Auto-approved relayed offers whose application did not go through yet (quota, busy channel…)."""
    done: list[tuple[JobOffer, Outcome]] = []
    for job in ctx.repo.list_jobs(JobStatus.APPROVED, source="classroom"):
        if not job.raw.get("classroom_auto") or int(job.raw.get("classroom_attempts") or 0) >= MAX_ATTEMPTS:
            continue
        try:
            done.append((job, submit(ctx, job)))
        except Exception as exc:  # one offer never blocks the check
            log.exception("Nouvel essai de candidature Classroom impossible (%s)", job.id)
            done.append((job, Outcome(kind="failed", detail=truncate(str(exc), MAX_REASON_CHARS))))
    return done


# --------------------------------------------------------------------------- notification


def notify_outcome(ctx: AppContext, job: JobOffer, outcome: Outcome) -> None:
    """One notification per new offer: applied / to validate / to do by hand (link) / filtered (reason)."""
    from jobbot.notify import notify

    reason = truncate(outcome.detail, MAX_REASON_CHARS)
    messages = {
        "applied": f"{label(job)} : {reason}",
        "simulated": f"{label(job)} : {reason}",
        "pending_review": f"{label(job)} — {reason}. Valide avec « jobbot approve {job.id} ».",
        "needs_manual": f"{label(job)} — postule ici : {job.apply_url or job.url}",
        "deferred": f"{label(job)} — {reason}",
    }
    message = messages.get(outcome.kind, f"{label(job)} — {reason}")
    notify(ctx, TITLES.get(outcome.kind, "Classroom"), message)
