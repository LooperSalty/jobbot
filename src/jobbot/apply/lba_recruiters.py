"""Spontaneous applications relayed by La Bonne Alternance to « recruteurs LBA » flagged ``hasEmail``.

Research (docs/research/la-bonne-alternance.md §1.2-1.3, §3.3): for companies likely to hire apprentices, LBA
relays a « candidature spontanée » by e-mail without ever exposing the recruiter address — no e-mail hunting,
no cold e-mail from Gmail. One application per SIRET, ever (``LbaApplier`` dedup), quota channel ``lba``,
review mode → documents prepared and company ``pending_review`` (validated in the dashboard / « approve »).
Recruiters outside the target (``outreach.relevance``: public works, HVAC… for a developer) are set aside
(status ``irrelevant``) before any document is written: LBA allows 3 applications per company, ever.
LBA does not relay follow-ups: they become manual follow-ups (listed in the dashboard).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from jobbot import clock
from jobbot.apply.base import ApplyResult
from jobbot.apply.service import ApplyReport, build_pacer
from jobbot.context import AppContext
from jobbot.models import Application, ApplicationEvent, ApplyMethod, Company, JobStatus, TailoredDocuments
from jobbot.outreach.relevance import OFF_TARGET, report_lines, set_aside, split_relevant, sweep_review_queue
from jobbot.sources.lba_mapping import RECRUITER_SOURCE
from jobbot.textutil import normalize_company, truncate

log = logging.getLogger(__name__)

CHANNEL = "lba"
CANDIDATE_STATUSES = ("approved", "new", "enriched", "no_email")
RECENT_DAYS = 60
DONE_FLAG = "lba_relay_done"
REVIEW_QUEUE_MAX = 8  # review mode: at most this many LBA companies waiting for validation


class RelayReport(ApplyReport):
    irrelevant: int = 0  # recruiters set aside as outside the target (never applied to)


def candidates(ctx: AppContext) -> list[Company]:
    """LBA recruiters with an LBA relay, never tried, validated ones first then best scored."""
    usable = [
        c for c in ctx.repo.list_companies(CANDIDATE_STATUSES)
        if c.source == RECRUITER_SOURCE and c.raw.get("hasEmail") and (c.raw.get("siret") or c.raw.get("id"))
        and not c.raw.get(DONE_FLAG)
    ]
    return sorted(usable, key=lambda c: (c.status != "approved", -_score(c)))


def _score(company: Company) -> float:
    try:
        return float(company.raw.get("outreach_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _blocked(ctx: AppContext, company: Company) -> str | None:
    key = normalize_company(company.name)
    if ctx.repo.is_blacklisted(company.name, key, company.siren or "", company.domain or ""):
        return "liste noire"
    if key in {normalize_company(x) for x in ctx.profile.target.exclude_companies}:
        return "entreprise exclue"
    if ctx.repo.recently_applied_to_company(company.name, RECENT_DAYS):
        return f"candidature déjà envoyée il y a moins de {RECENT_DAYS} jours"
    return None


def _documents(ctx: AppContext, company: Company) -> TailoredDocuments:
    docs = ctx.repo.latest_documents(company_id=company.id)
    if docs is not None:
        return docs
    from jobbot.documents.service import prepare_documents

    return prepare_documents(ctx, company=company)


def _mark(ctx: AppContext, company: Company, status: str, **raw: Any) -> None:
    ctx.repo.upsert_company(company.model_copy(update={"status": status, "raw": {**company.raw, **raw}}))


def _record(ctx: AppContext, company: Company, docs: TailoredDocuments, result: ApplyResult) -> Application:
    now = clock.now()
    title = "Candidature spontanée" + (" (alternance)" if ctx.profile.alternance else "")
    app = ctx.repo.create_application(Application(
        company_id=company.id, method=ApplyMethod.LBA, applied_at=now, company_name=company.name, job_title=title,
        cv_path=docs.cv_pdf, letter_path=docs.letter_pdf, last_contact_at=now, notes=truncate(result.detail, 500),
    ))
    try:
        from jobbot.followup import compute_next_followup

        nxt = compute_next_followup(app, ctx.settings.followup)
    except Exception as exc:  # never lose the record because of the follow-up date
        log.warning("Date de relance non calculée (%s)", exc)
        nxt = now + timedelta(days=ctx.settings.followup.first_after_days)
    app = app.model_copy(update={"next_followup_at": nxt})
    ctx.repo.update_application(app)
    ctx.repo.add_event(ApplicationEvent(application_id=app.id, at=now, kind="applied",
                                        detail="Candidature spontanée relayée par La Bonne Alternance"))
    _mark(ctx, company, "contacted", contacted_at=clock.iso(now), contacted_via="lba", **{DONE_FLAG: True})
    return app


def _one(ctx: AppContext, company: Company, applier: Any, pacer: Any, review: bool,
         pause: Callable[[], None]) -> tuple[str, str]:
    """(outcome kind, French detail) for one recruiter."""
    reason = _blocked(ctx, company)
    if reason:
        return "skipped", reason
    docs = _documents(ctx, company)
    if (review or docs.truth_warnings) and company.status != "approved":  # unbacked CV claims: user checks first
        _mark(ctx, company, "pending_review", review_contact="via La Bonne Alternance", review_channel="lba",
              review_cv=docs.cv_pdf)
        check = f" — CV à vérifier : {' ; '.join(docs.truth_warnings[:3])}" if docs.truth_warnings else ""
        return "pending_review", f"documents prêts, en attente de validation (relais La Bonne Alternance){check}"
    pause()
    result = applier.apply_to_recruiter(company, docs)
    if result.status == JobStatus.APPLIED:
        pacer.record(CHANNEL, True, company.name)
        _record(ctx, company, docs, result)
        return "applied", result.detail
    if result.ok:  # dry-run
        return "skipped", result.detail
    if result.status == JobStatus.FAILED:
        pacer.record(CHANNEL, False, truncate(f"{company.name} {result.detail}", 200))
        return "failed", result.detail
    if OFF_TARGET in result.detail:  # the detail revealed the real sector
        set_aside(ctx.repo, company, result.detail)
        return "irrelevant", result.detail
    if not applier.fatal:  # already applied, no relay, company gone: never retry the relay
        _mark(ctx, company, company.status, **{DONE_FLAG: True})
    return "skipped", result.detail


def _budget(ctx: AppContext, pacer: Any, review: bool, limit: int | None) -> int:
    budget = limit if limit is not None else pacer.remaining(CHANNEL)
    if review:  # documents cost LLM time: keep the validation queue short
        waiting = sum(1 for c in ctx.repo.list_companies("pending_review") if c.raw.get("review_channel") == "lba")
        budget = min(budget, max(0, REVIEW_QUEUE_MAX - waiting))
    return budget


def run_lba_recruiters(ctx: AppContext, *, limit: int | None = None) -> RelayReport:
    """Relay spontaneous applications through LBA (review mode: prepare + queue). One failure never stops it."""
    from jobbot.apply.lba import LbaApplier
    from jobbot.preferences import effective_mode

    review = effective_mode(ctx.settings) == "review"
    pacer, applier = build_pacer(ctx), LbaApplier(ctx)
    queued_off = sweep_review_queue(ctx.repo, ctx.profile)  # before the queue is counted
    budget = _budget(ctx, pacer, review, limit)
    relevant, off = split_relevant(ctx.repo, ctx.profile, candidates(ctx))
    off = queued_off + off
    counts: Counter[str] = Counter()
    details: list[str] = report_lines(off)
    attempts = [0]

    def pause() -> None:
        if attempts[0] and not ctx.dry_run:
            pacer.between_applications()
        attempts[0] += 1

    for company in relevant:
        if counts["applied"] + counts["failed"] + counts["pending_review"] >= budget or applier.fatal:
            break
        try:
            kind, detail = _one(ctx, company, applier, pacer, review, pause)
        except Exception as exc:
            log.exception("Candidature LBA à %s impossible", company.name)
            kind, detail = "failed", truncate(f"erreur inattendue ({exc})", 240)
        counts[kind] += 1
        details.append(f"{company.name} : {detail}")
    return RelayReport(applied=counts["applied"], failed=counts["failed"], skipped=counts["skipped"],
                       pending_review=counts["pending_review"], irrelevant=len(off) + counts["irrelevant"],
                       details=details)
