"""Review queue and manual actions shared by the CLI (approve / reject / assist) and the dashboard."""

from __future__ import annotations

from jobbot.context import AppContext
from jobbot.models import Application, ApplyMethod, JobOffer, JobStatus


def _label(job: JobOffer) -> str:
    return f"{job.company or 'Entreprise inconnue'} — {job.title}"


def record_manual_application(ctx: AppContext, job: JobOffer, *, detail: str = "",
                              email_to: str | None = None) -> Application:
    """The human submitted the application himself (external site, ``jobbot assist``, dashboard)."""
    from jobbot.apply.base import ApplyResult
    from jobbot.apply.service import record_application

    method = job.apply_method if job.apply_method != ApplyMethod.MANUAL else ApplyMethod.EXTERNAL
    result = ApplyResult(ok=True, status=JobStatus.APPLIED, method=method, email_to=email_to,
                         detail=detail or "Candidature envoyée manuellement.")
    return record_application(ctx, job, result, ctx.repo.latest_documents(job_id=job.id))


def approve_job(ctx: AppContext, job_id: str) -> str:
    """Review queue → APPROVED (applied by the next « apply », whatever the score)."""
    from jobbot.apply.service import set_job_status

    job = ctx.repo.get_job(job_id)
    if job is None:
        return f"Offre {job_id} introuvable."
    if job.status in (JobStatus.APPLIED, JobStatus.EXPIRED):
        return f"{_label(job)} : déjà {job.status.value}, rien à valider."
    set_job_status(ctx, job, JobStatus.APPROVED, "Validée par l'utilisateur.")
    return f"{_label(job)} : validée."


def reject_job(ctx: AppContext, job_id: str, reason: str = "Refusée par l'utilisateur.") -> str:
    from jobbot.apply.service import set_job_status

    job = ctx.repo.get_job(job_id)
    if job is None:
        return f"Offre {job_id} introuvable."
    if job.status == JobStatus.APPLIED:
        return f"{_label(job)} : candidature déjà envoyée, impossible de la refuser."
    set_job_status(ctx, job, JobStatus.SKIPPED, reason)
    return f"{_label(job)} : écartée."


def review_company(ctx: AppContext, company_id: str, approve: bool) -> str:
    """Spontaneous-application queue: ``approved`` (sent by the next campaign) or ``rejected``."""
    company = ctx.repo.get_company(company_id)
    if company is None:
        return f"Entreprise {company_id} introuvable."
    if company.status == "contacted":
        return f"{company.name} : déjà contactée."
    status = "approved" if approve else "rejected"
    ctx.repo.upsert_company(company.model_copy(update={"status": status}))
    return f"{company.name} : {'validée' if approve else 'écartée'}."
