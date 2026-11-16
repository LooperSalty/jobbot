"""Apply orchestration: pick the jobs, route each one to its channel, record the outcome.

- Mode = ``preferences.effective_mode`` (review during the warm-up, then ``settings.mode``).
- One applier per channel, built lazily; the Playwright browser is opened at most once per run and closed.
- Quotas (``Pacer``) per channel, human-like pause between two submissions, dedup (same job, same company within
  60 days, blacklist), channel-level problems (quota, login, SMTP/LBA down, network lost) leave the job untouched
  for a retry.
- APPLIED → ``Application`` + ``applied`` event + ``next_followup_at``; EXTERNAL → NEEDS_MANUAL (``jobbot assist``).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jobbot import clock
from jobbot.apply.base import Applier, ApplyResult
from jobbot.apply.board import board_view  # relayed offers (Classroom) reach the appliers as their board's offer
from jobbot.context import AppContext
from jobbot.models import Application, ApplicationEvent, ApplyMethod, JobOffer, JobStatus, TailoredDocuments
from jobbot.pacing import Pacer
from jobbot.textutil import normalize_company, truncate

log = logging.getLogger(__name__)

RECENT_COMPANY_DAYS = 60
QUEUE_STATUSES = (JobStatus.DOCS_READY, JobStatus.APPROVED)
EXPLICIT_STATUSES = frozenset({JobStatus.SHORTLISTED, JobStatus.DOCS_READY, JobStatus.PENDING_REVIEW,
                               JobStatus.APPROVED, JobStatus.FAILED})
CHANNEL_BY_METHOD = {
    ApplyMethod.LINKEDIN_EASY_APPLY: "linkedin", ApplyMethod.HELLOWORK: "hellowork", ApplyMethod.LBA: "lba",
    ApplyMethod.EMAIL: "email", ApplyMethod.EXTERNAL: "external", ApplyMethod.MANUAL: "external",
}
EXTERNAL_DETAIL = ("Candidature sur le site de l'employeur : documents prêts, lien dans le tableau de bord "
                   "(lance « jobbot assist » pour le pré-remplissage).")
MAX_NOTE_CHARS = 500
_QUOTA_RE = re.compile(r"quota", re.I)
_LOGIN_RE = re.compile(r"jobbot login|limite quotidienne", re.I)  # also LinkedIn's own daily cap: retry tomorrow
# The machine lost its network (DNS / connection down): the offer is fine, try it again at the next run.
_NETWORK_RE = re.compile(
    r"net::ERR_(?:NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|NETWORK_CHANGED|NETWORK_ACCESS_DENIED|ADDRESS_UNREACHABLE"
    r"|CONNECTION_(?:REFUSED|RESET|CLOSED|TIMED_OUT|FAILED))|temporary failure in name resolution"
    r"|name or service not known|getaddrinfo failed|network is unreachable",
    re.I,
)
MAX_NETWORK_RETRIES = 3  # runs in a row lost to the network on one offer before it is set aside (FAILED)
NETWORK_RETRIES_KEY = "network_retries"


class ApplyReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    applied: int = 0
    needs_manual: int = 0
    failed: int = 0
    skipped: int = 0
    pending_review: int = 0
    details: list[str] = Field(default_factory=list)


def channel_of(job: JobOffer) -> str:
    return CHANNEL_BY_METHOD.get(job.apply_method, "external")


def _label(job: JobOffer) -> str:
    return f"{job.company or 'Entreprise inconnue'} — {job.title}"


# --------------------------------------------------------------------------- selection


def dedup_reason(ctx: AppContext, job: JobOffer) -> tuple[str, JobStatus] | None:
    """Why this job must not be applied to (and the status to give it), or None."""
    existing = ctx.repo.application_for_job(job.id)
    if existing is not None:
        return f"déjà postulé (candidature n°{existing.id})", JobStatus.APPLIED
    email = (job.contact_email or "").lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    key = normalize_company(job.company)
    if ctx.repo.is_blacklisted(job.company, key, email, domain, job.company_domain or ""):
        return "entreprise ou adresse en liste noire", JobStatus.SKIPPED
    if key and key in {normalize_company(c) for c in ctx.profile.target.exclude_companies}:
        return "entreprise exclue dans le profil", JobStatus.SKIPPED
    if ctx.repo.recently_applied_to_company(job.company, RECENT_COMPANY_DAYS):
        return (f"candidature déjà envoyée à {job.company} il y a moins de {RECENT_COMPANY_DAYS} jours",
                JobStatus.SKIPPED)
    return None


def _candidates(ctx: AppContext, job_ids: list[str] | None, details: list[str]) -> list[JobOffer]:
    if job_ids is None:
        from jobbot.apply.external import supported_url

        ready = ctx.repo.list_jobs(QUEUE_STATUSES)
        # Only reconsider the old generic manual handoff, never a form already attempted or uncertain.
        waiting = [job for job in ctx.repo.list_jobs(JobStatus.NEEDS_MANUAL)
                   if job.raw.get("apply_detail") == EXTERNAL_DETAIL and supported_url(job.apply_url or job.url)]
        return [*ready, *waiting]
    jobs: list[JobOffer] = []
    for job_id in dict.fromkeys(job_ids):
        job = ctx.repo.get_job(job_id)
        if job is None:
            details.append(f"Offre {job_id} introuvable.")
        elif job.status not in EXPLICIT_STATUSES:
            details.append(f"{_label(job)} : statut « {job.status.value} », candidature impossible.")
        else:
            jobs.append(job)
    return jobs


def truth_warnings(ctx: AppContext, job: JobOffer) -> list[str]:
    """Claims of the job's latest CV that the profile does not back (never sent without the user's approval)."""
    docs = ctx.repo.latest_documents(job_id=job.id)
    return list(docs.truth_warnings) if docs is not None else []


def _partition(ctx: AppContext, jobs: list[JobOffer], channels: list[str] | None,
               explicit: bool) -> tuple[list[JobOffer], list[JobOffer]]:
    """(ready to apply, to review) according to the effective mode. In auto mode a CV with truth warnings
    always goes to review; an approved (or explicitly requested) job is the user's decision."""
    from jobbot.preferences import effective_mode

    auto = effective_mode(ctx.settings) == "auto"
    threshold = ctx.settings.thresholds.auto_apply_min_score
    wanted = {c.strip().lower() for c in channels} if channels else None
    ready: list[JobOffer] = []
    review: list[JobOffer] = []
    for job in (j for j in jobs if wanted is None or channel_of(j) in wanted):
        automatic = auto and (job.match_score or 0) >= threshold and not truth_warnings(ctx, job)
        (ready if explicit or job.status == JobStatus.APPROVED or automatic else review).append(job)
    return ready, review


def select_jobs_to_apply(ctx: AppContext, limit: int | None = None) -> list[JobOffer]:
    """DOCS_READY/APPROVED jobs that would be applied to now (no DB change), best score first."""
    ready, _ = _partition(ctx, ctx.repo.list_jobs(QUEUE_STATUSES), None, explicit=False)
    eligible = [job for job in ready if dedup_reason(ctx, job) is None]
    return eligible[:limit] if limit is not None else eligible


# --------------------------------------------------------------------------- status & application records


def set_job_status(ctx: AppContext, job: JobOffer, status: JobStatus, detail: str) -> None:
    """New status + the reason in ``raw['apply_detail']`` (shown in the dashboard)."""
    raw = {**job.raw, "apply_detail": truncate(detail, MAX_NOTE_CHARS), "apply_at": clock.iso(clock.now())}
    ctx.repo.save_job(job.model_copy(update={"status": status, "raw": raw}))


def _next_followup(ctx: AppContext, app: Application) -> datetime | None:
    try:
        from jobbot.followup import compute_next_followup

        return compute_next_followup(app, ctx.settings.followup)
    except Exception as exc:  # the follow-up module must never block the record
        log.warning("Date de relance non calculée (%s) : délai par défaut", exc)
        return app.applied_at + timedelta(days=ctx.settings.followup.first_after_days)


def record_application(
    ctx: AppContext, job: JobOffer, result: ApplyResult, docs: TailoredDocuments | None
) -> Application:
    """Persist a successful application: Application (+ follow-up date), ``applied`` event, job APPLIED."""
    now = clock.now()
    mid = result.email_message_id
    app = ctx.repo.create_application(Application(
        job_id=job.id, method=result.method, applied_at=now, company_name=job.company, job_title=job.title,
        email_to=result.email_to, email_message_id=mid, email_thread_ids=[mid] if mid else [],
        cv_path=docs.cv_pdf if docs else None, letter_path=docs.letter_pdf if docs else None,
        last_contact_at=now, notes=truncate(result.detail, MAX_NOTE_CHARS),
    ))
    nxt = _next_followup(ctx, app)
    if nxt is not None:
        app = app.model_copy(update={"next_followup_at": nxt})
        ctx.repo.update_application(app)
    detail = f"Candidature ({result.method.value}) : {result.detail}"
    ctx.repo.add_event(ApplicationEvent(application_id=app.id, at=now, kind="applied",
                                        detail=truncate(detail, MAX_NOTE_CHARS)))
    set_job_status(ctx, job, JobStatus.APPLIED, result.detail)
    return app


# --------------------------------------------------------------------------- appliers & shared resources


class _Resources:
    """Per-run lazy resources: browser (opened once), mailer, pacer, appliers, blocked channels."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.pacer = build_pacer(ctx)
        self.blocked: dict[str, str] = {}
        self.attempts = 0
        self._stack = ExitStack()
        self._browser: Any = None
        self._mailer: Any = None
        self._mailer_built = False
        self._appliers: dict[str, Applier | None] = {}

    def browser(self) -> Any:
        if self._browser is None:
            from jobbot.apply.browser import open_browser

            self._browser = self._stack.enter_context(open_browser(self.ctx))
        return self._browser

    def mailer(self) -> Any:
        if not self._mailer_built:
            from jobbot.mail.mailer import build_mailer

            self._mailer, self._mailer_built = build_mailer(self.ctx), True
        return self._mailer

    def answerer_factory(self) -> Callable[[JobOffer], Any]:
        from jobbot.apply.answers import FormAnswerer

        return lambda job: FormAnswerer(self.ctx.profile, self.ctx.llm, self.ctx.repo, job)

    def applier(self, channel: str) -> Applier | None:
        if channel not in self._appliers:
            self._appliers[channel] = make_applier(self.ctx, channel, self)
        return self._appliers[channel]

    def pause(self) -> None:
        if self.attempts and not self.ctx.dry_run:
            self.pacer.between_applications()
        self.attempts += 1

    def close(self) -> None:
        try:
            self._stack.close()
        except Exception as exc:  # closing the browser must not hide the report
            log.warning("Fermeture du navigateur : %s", exc)


def build_pacer(ctx: AppContext) -> Pacer:
    return Pacer(ctx.repo, ctx.settings.limits)


def make_applier(ctx: AppContext, channel: str, res: _Resources) -> Applier | None:
    """Applier for ``channel`` (None when the channel cannot work, e.g. SMTP not configured)."""
    if channel == "linkedin":
        from jobbot.apply.linkedin import LinkedInApplier

        return LinkedInApplier(ctx, res.browser(), res.pacer, res.answerer_factory())
    if channel == "hellowork":
        from jobbot.apply.hellowork import HelloWorkApplier

        return HelloWorkApplier(ctx, res.browser(), res.pacer, res.answerer_factory())
    if channel == "lba":
        from jobbot.apply.lba import LbaApplier

        return LbaApplier(ctx)
    if channel == "email":
        from jobbot.apply.email_apply import EmailApplier

        mailer = res.mailer()
        return EmailApplier(ctx, mailer) if mailer is not None else None
    if channel == "external":
        from jobbot.apply.external import ExternalApplier

        return ExternalApplier(ctx, res.browser(), res.answerer_factory())
    return None


# --------------------------------------------------------------------------- processing


def _documents(ctx: AppContext, job: JobOffer) -> TailoredDocuments | None:
    docs = ctx.repo.latest_documents(job_id=job.id)
    if docs is not None:
        return docs
    try:
        from jobbot.documents.service import prepare_documents

        return prepare_documents(ctx, job=job)
    except Exception:
        log.exception("Documents impossibles à préparer pour %s", job.id)
        return None


def _get_applier(ctx: AppContext, job: JobOffer, channel: str,
                 res: _Resources) -> tuple[Applier | None, str, str]:
    """(applier, '', '') or (None, outcome kind, detail) after recording why the channel cannot be used."""
    try:
        applier = res.applier(channel)
    except Exception as exc:  # browser missing, Playwright error...
        log.exception("Canal %s indisponible", channel)
        res.blocked[channel] = f"canal {channel} indisponible ({truncate(str(exc), 160)})"
        return None, "skipped", f"{res.blocked[channel]} (sera retentée)"
    if applier is None:
        detail = (f"Envoi d'e-mails non configuré (SMTP) : écris toi-même à {job.contact_email}."
                  if channel == "email" else f"Canal {channel} indisponible : candidature à faire à la main.")
    elif not applier.can_apply(board_view(job)):
        detail = f"Candidature automatique ({channel}) impossible pour cette offre : à faire à la main."
    else:
        return applier, "", ""
    set_job_status(ctx, job, JobStatus.NEEDS_MANUAL, detail)
    return None, "needs_manual", detail


def _attempt(applier: Applier, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
    try:
        return applier.apply(board_view(job), docs)
    except Exception as exc:  # appliers should not raise; stay safe anyway
        log.exception("Candidature %s en échec", job.id)
        return ApplyResult(ok=False, status=JobStatus.FAILED, method=job.apply_method,
                           detail=truncate(f"Erreur inattendue : {exc}", 240))


def _channel_problem(applier: Applier, result: ApplyResult, fatal_before: str | None) -> str | None:
    fatal = getattr(applier, "fatal", None)
    if fatal and fatal != fatal_before:
        return fatal
    if result.status == JobStatus.SKIPPED and not result.ok and _QUOTA_RE.search(result.detail):
        return result.detail
    if result.status == JobStatus.NEEDS_MANUAL and _LOGIN_RE.search(result.detail):
        return result.detail
    if _network_outage(result):
        return f"réseau indisponible ({truncate(result.detail, 160)})"
    return None


def _network_outage(result: ApplyResult) -> bool:
    return result.status == JobStatus.FAILED and bool(_NETWORK_RE.search(result.detail))


def _note_network_retry(ctx: AppContext, job: JobOffer, detail: str) -> bool:
    """Count one run lost to the network on this offer; after ``MAX_NETWORK_RETRIES`` it is set aside (FAILED,
    visible in the dashboard) instead of waiting forever. True when set aside."""
    fresh = ctx.repo.get_job(job.id) or job
    retries = int(fresh.raw.get(NETWORK_RETRIES_KEY) or 0) + 1
    counted = fresh.model_copy(update={"raw": {**fresh.raw, NETWORK_RETRIES_KEY: retries}})
    if retries < MAX_NETWORK_RETRIES:
        ctx.repo.save_job(counted)
        return False
    set_job_status(ctx, counted, JobStatus.FAILED, f"{detail} (réseau indisponible {retries} fois de suite)")
    return True


def _outcome(ctx: AppContext, job: JobOffer, result: ApplyResult, docs: TailoredDocuments) -> str:
    if result.status == JobStatus.APPLIED:
        record_application(ctx, job, result, docs)
        return "applied"
    if result.status == JobStatus.SKIPPED and result.ok:  # dry-run: nothing happened
        return "skipped"
    status = {JobStatus.NEEDS_MANUAL: JobStatus.NEEDS_MANUAL, JobStatus.EXPIRED: JobStatus.EXPIRED,
              JobStatus.SKIPPED: JobStatus.SKIPPED}.get(result.status, JobStatus.FAILED)
    set_job_status(ctx, job, status, result.detail)
    return {JobStatus.NEEDS_MANUAL: "needs_manual", JobStatus.FAILED: "failed"}.get(status, "skipped")


def _submit(ctx: AppContext, job: JobOffer, channel: str, res: _Resources) -> tuple[str, str]:
    applier, kind, detail = _get_applier(ctx, job, channel, res)
    if applier is None:
        return kind, detail
    if res.pacer.remaining(channel) <= 0:
        res.blocked[channel] = f"quota journalier {channel} atteint ({res.pacer.quota(channel)}/jour)"
        return "skipped", res.blocked[channel]
    docs = _documents(ctx, job)
    if docs is None:
        return "skipped", "documents introuvables (sera retentée)"
    res.pause()
    fatal_before = getattr(applier, "fatal", None)
    result = _attempt(applier, job, docs)
    problem = _channel_problem(applier, result, fatal_before)
    if problem:
        res.blocked[channel] = problem
        if _network_outage(result) and _note_network_retry(ctx, job, result.detail):
            return "failed", f"{problem} : offre mise de côté après {MAX_NETWORK_RETRIES} essais"
        return "skipped", f"{problem} (sera retentée)"
    if result.status in (JobStatus.APPLIED, JobStatus.FAILED):
        res.pacer.record(channel, result.status == JobStatus.APPLIED, truncate(f"{job.id} {result.detail}", 200))
    return _outcome(ctx, job, result, docs), result.detail


def _process(ctx: AppContext, job: JobOffer, res: _Resources) -> tuple[str, str]:
    """(outcome kind, French detail) for one ready job."""
    dup = dedup_reason(ctx, job)
    if dup is not None:
        set_job_status(ctx, job, dup[1], dup[0].capitalize())
        return "skipped", dup[0]
    channel = channel_of(job)
    if channel == "external":
        from jobbot.apply.external import supported_url

        if not supported_url(job.apply_url or job.url):
            set_job_status(ctx, job, JobStatus.NEEDS_MANUAL, EXTERNAL_DETAIL)
            return "needs_manual", "candidature externe, documents prêts"
    if channel in res.blocked:
        return "skipped", f"{res.blocked[channel]} (sera retentée)"
    return _submit(ctx, job, channel, res)


def _queue_for_review(ctx: AppContext, jobs: list[JobOffer], counts: Counter[str], details: list[str]) -> None:
    for job in jobs:
        try:
            dup = dedup_reason(ctx, job)
            if dup is not None:
                set_job_status(ctx, job, dup[1], dup[0].capitalize())
                counts["skipped"] += 1
                details.append(f"{_label(job)} : ignorée ({dup[0]})")
            elif job.status != JobStatus.PENDING_REVIEW:
                warnings = truth_warnings(ctx, job)
                check = f" : CV à vérifier ({' ; '.join(warnings[:3])})" if warnings else ""
                set_job_status(ctx, job, JobStatus.PENDING_REVIEW, f"En attente de validation{check}.")
                counts["pending_review"] += 1
        except Exception as exc:
            log.exception("Mise en attente de %s impossible", job.id)
            details.append(f"{_label(job)} : erreur ({exc})")


def apply_to_jobs(
    ctx: AppContext, *, channels: list[str] | None = None, limit: int | None = None,
    job_ids: list[str] | None = None,
) -> ApplyReport:
    """Apply to the ready jobs (see module doc). One failing job never stops the others."""
    details: list[str] = []
    counts: Counter[str] = Counter()
    ready, review = _partition(ctx, _candidates(ctx, job_ids, details), channels, explicit=job_ids is not None)
    _queue_for_review(ctx, review, counts, details)
    res = _Resources(ctx)
    try:
        for job in ready[:max(0, limit)] if limit is not None else ready:
            try:
                kind, detail = _process(ctx, job, res)
            except Exception as exc:
                log.exception("Candidature à %s impossible", job.id)
                kind, detail = "failed", f"erreur inattendue ({exc})"
            counts[kind] += 1
            details.append(f"{_label(job)} : {detail}")
    finally:
        res.close()
    log.info("Candidatures : %s", dict(counts))
    fields = ("applied", "needs_manual", "failed", "skipped", "pending_review")
    return ApplyReport(**{name: counts[name] for name in fields}, details=details)
