"""Spontaneous applications ("candidatures spontanées") by e-mail to enriched companies.

Safeguards: only employers inside the target (:mod:`jobbot.outreach.relevance`: companies outside it are set aside
before any document is written, and counted in the report), one address per company and per attempt, chosen under ``settings.outreach`` (see
:mod:`jobbot.outreach.selection`: recruitment address > named HR > tech manager > leader of a small company >
contact@; unverified guesses capped per day and paused on bounces), company-level dedup (no second application
within 60 days — except a retry with the next best address after a bounce, up to ``max_attempts_per_company``),
blacklist (company, SIREN, domain, address), daily quota on the ``spontaneous`` channel, human-like pauses, a
personalised greeting ("Bonjour Prénom Nom," for a person, else "Madame, Monsieur,"), an opt-out + address-source
line in every body (CNIL / GDPR art. 14), review mode (documents prepared, company → ``pending_review``, nothing
sent) and dry-run.

Targets are taken by priority (:mod:`jobbot.outreach.priority`), documents prepared the night before (« jobbot
night ») are reused for a week, and the daily quota follows the sending ramp (:mod:`jobbot.ramp`, checked for a
reset after too many bounces at the start of every campaign).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jobbot import clock, ramp
from jobbot.context import AppContext
from jobbot.models import Application, ApplicationEvent, ApplyMethod, Company, ContactEmail, TailoredDocuments
from jobbot.outreach import relevance
from jobbot.outreach import selection as sel
from jobbot.outreach._compose import OPT_OUT_SENTENCE, build_email, ensure_opt_out, is_alternance
from jobbot.outreach.holds import hold_raw
from jobbot.outreach.priority import company_priority, focus_ring
from jobbot.pacing import Pacer, QuotaExceeded
from jobbot.textutil import normalize_company

if TYPE_CHECKING:  # written concurrently by the mail module owner: imported lazily at runtime
    from jobbot.mail.mailer import Mailer

__all__ = ["OPT_OUT_SENTENCE", "CampaignReport", "ensure_opt_out", "fresh_documents", "run_spontaneous_campaign"]

log = logging.getLogger(__name__)

CHANNEL = sel.CHANNEL
RECENT_DAYS = sel.RECENT_DAYS
DOCS_FRESH_DAYS = 7  # documents prepared ahead (night) are reused for a week
# Fallback when the mailer does not raise MailFatalError: SMTP answers that must stop the whole channel
# (bad App Password, policy/spam block, sending limit, server unreachable)…
_STOP_ERRORS = re.compile(
    r"\b(?:535|534|454|421)\b|5\.7\.\d|authenti|login|password|mot de passe|identifiants|limit|injoignable|"
    r"unreachable|connexion smtp",
    re.I,
)
# …and those meaning "this address is unusable" (never retry it).
_RECIPIENT_ERRORS = re.compile(
    r"5\.1\.\d|\b55[0-3]\b|recipient|destinataire|mailbox|user unknown|adresse (?:e-mail )?invalide|adresse refusée",
    re.I,
)


class CampaignReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sent: int
    skipped: int
    failed: int
    irrelevant: int = 0  # companies set aside as outside the target (never contacted)
    details: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _Outcome:
    # stop = end the campaign (quota); abort = failed AND end the campaign (SMTP account refused)
    kind: Literal["sent", "simulated", "pending", "skipped", "failed", "stop", "abort"]
    detail: str
    guess: bool = False  # the address was an unverified guess


@dataclass(frozen=True)
class _Run:
    """What every company of one campaign run shares."""

    mailer: Any
    pacer: Pacer
    review: bool
    apps: tuple[Application, ...]  # snapshot of every application (dedup, bounce retry, bounce rate)
    paused: str | None  # why unverified guesses are paused, if they are


# --------------------------------------------------------------------------- selection


def _score(company: Company) -> int:
    try:
        return int(company.raw.get("outreach_score", 0))
    except (TypeError, ValueError):
        return 0


def _priority(ctx: AppContext, company: Company) -> float:
    """Priority of the target now (:mod:`jobbot.outreach.priority`; the night stores it for the dashboard)."""
    return company_priority(company, ctx.profile, ctx.repo, cfg=ctx.settings.outreach)[0]


def _candidates(ctx: AppContext, apps: list[Application]) -> list[Company]:
    """Retries after a bounce first (already chosen, never reached), then approved, then enriched ring by ring
    around the first search area (« grandes entreprises » focus), by priority (then discovery score)."""
    cfg = ctx.settings.outreach
    fresh = sorted(ctx.repo.list_companies(status=["approved", "enriched"]),
                   key=lambda c: (c.status != "approved", focus_ring(c, ctx.profile, cfg), -_priority(ctx, c),
                                  -_score(c)))
    seen = {c.id for c in fresh}
    retries = [c for c in sel.bounced_companies(ctx, apps) if c.id not in seen]
    return [*retries, *fresh]


def _targets(ctx: AppContext, run: _Run) -> tuple[list[Company], list[tuple[Company, str]]]:
    """Candidates inside the target, and the companies set aside (review queue included) with their reason —
    before any document is written."""
    queued_off = relevance.sweep_review_queue(ctx.repo, ctx.profile)
    relevant, off = relevance.split_relevant(ctx.repo, ctx.profile, _candidates(ctx, list(run.apps)))
    country = ctx.settings.outreach.country
    if country:
        relevant = [c for c in relevant if str(c.raw.get("country", "")).upper() == country.upper()]
    return relevant, queued_off + off


def _skip_reason(ctx: AppContext, company: Company, history: sel.History) -> str | None:
    if ctx.repo.is_blacklisted(company.name, company.siren or "", company.domain or ""):
        return "liste noire"
    if normalize_company(company.name) in {normalize_company(x) for x in ctx.profile.target.exclude_companies}:
        return "entreprise exclue"
    return history.reason if history.state == "blocked" else None


# --------------------------------------------------------------------------- collaborators (lazy imports)


def _build_pacer(ctx: AppContext) -> Pacer:
    return Pacer(ctx.repo, ctx.settings.limits)


def _prepare(ctx: AppContext, company: Company) -> TailoredDocuments:
    from jobbot.documents.service import prepare_documents

    return prepare_documents(ctx, company=company)


def fresh_documents(repo: Any, company_id: str, *, days: int = DOCS_FRESH_DAYS) -> TailoredDocuments | None:
    """Documents prepared for this company less than ``days`` ago whose CV file still exists (night preparation)."""
    docs = repo.latest_documents(company_id=company_id)
    if docs is None or not docs.cv_pdf or not Path(docs.cv_pdf).is_file():
        return None
    created = clock.parse_dt(docs.created_at)
    changed = repo.kv_get("documents:target_changed_at")
    if changed:
        try:
            cutoff = clock.parse_dt(changed)
            if created is None or cutoff is None or created < cutoff:
                return None
        except ValueError:
            return None
    return docs if created is not None and clock.now() - created < timedelta(days=days) else None


def _docs_for(ctx: AppContext, company: Company, history: sel.History) -> TailoredDocuments:
    """Documents already prepared are reused (greeting set at send time): any age for an approved company or a
    retry, less than ``DOCS_FRESH_DAYS`` old otherwise (prepared the night before by « jobbot night »)."""
    if company.status == "approved" or history.state == "retry":
        docs = ctx.repo.latest_documents(company_id=company.id)
        if docs is not None:
            return docs
    return fresh_documents(ctx.repo, company.id) or _prepare(ctx, company)


def _next_followup(ctx: AppContext, app: Application) -> datetime:
    try:
        from jobbot.followup import compute_next_followup

        return compute_next_followup(app, ctx.settings.followup) or _default_followup(ctx, app)
    except Exception as exc:  # module missing or failing → deterministic fallback
        log.debug("Calcul de relance indisponible (%s), délai par défaut", exc)
        return _default_followup(ctx, app)


def _default_followup(ctx: AppContext, app: Application) -> datetime:
    return app.applied_at + timedelta(days=ctx.settings.followup.first_after_days)


# --------------------------------------------------------------------------- actions


def _describe(contact: ContactEmail) -> str:
    parts = [contact.kind, contact.source]
    if sel.is_guess(contact):
        parts.append("devinée, non vérifiée")
    elif contact.verified:
        parts.append("vérifiée")
    if contact.person_name:
        parts.append(contact.person_name)
    return f"Adresse {parts[0]} ({', '.join(parts[1:])})"


def _record_sent(ctx: AppContext, company: Company, contact: ContactEmail, docs: TailoredDocuments,
                 message_id: str, history: sel.History) -> Application:
    now = clock.now()
    title = "Candidature spontanée" + (" (alternance)" if is_alternance(ctx) else "")
    app = Application(
        company_id=company.id, method=ApplyMethod.EMAIL, applied_at=now, company_name=company.name,
        job_title=title, email_to=contact.email, email_message_id=message_id, email_thread_ids=[message_id],
        cv_path=docs.cv_pdf, letter_path=docs.letter_pdf, last_contact_at=now, notes=_describe(contact),
    )
    app = ctx.repo.create_application(app.model_copy(update={"next_followup_at": _next_followup(ctx, app)}))
    ctx.repo.add_event(ApplicationEvent(
        application_id=app.id, at=now, kind="applied", detail=f"Candidature spontanée envoyée à {contact.email}",
    ))
    if history.state == "retry":
        previous = history.bounced.email_to if history.bounced is not None else None
        ctx.repo.add_event(ApplicationEvent(
            application_id=app.id, at=now, kind="retry_after_bounce",
            detail=f"Nouvel essai après l'échec de livraison à {previous or 'une adresse invalide'} "
                   f"(tentative {history.attempts + 1}/{ctx.settings.outreach.max_attempts_per_company})",
        ))
    _mark_contacted(ctx, company, contact, now)
    return app


def _mark_contacted(ctx: AppContext, company: Company, contact: ContactEmail, when: datetime) -> None:
    raw = {**company.raw, "contacted_at": clock.iso(when), "contacted_email": contact.email}
    ctx.repo.upsert_company(company.model_copy(update={"status": "contacted", "raw": raw}))


def _prepare_for_review(ctx: AppContext, company: Company, contact: ContactEmail, history: sel.History) -> _Outcome:
    try:
        docs = _docs_for(ctx, company, history)
    except Exception as exc:
        log.exception("Documents non générés pour %s", company.name)
        return _Outcome("failed", f"{company.name} : documents non générés ({exc})")
    return _hold_for_review(ctx, company, contact, docs, history)


def _hold_for_review(ctx: AppContext, company: Company, contact: ContactEmail, docs: TailoredDocuments,
                     history: sel.History) -> _Outcome:
    """Company → ``pending_review`` with its documents (review mode, or a CV whose claims need checking)."""
    raw = hold_raw(company, review_contact=contact.email, review_cv=docs.cv_pdf)
    ctx.repo.upsert_company(company.model_copy(update={"status": "pending_review", "raw": raw}))
    again = " (nouvel essai après un rebond)" if history.state == "retry" else ""
    check = f" — CV à vérifier : {' ; '.join(docs.truth_warnings[:3])}" if docs.truth_warnings else ""
    return _Outcome("pending",
                    f"{company.name} : documents prêts, en attente de validation ({contact.email}){again}{check}")


def _is_fatal(exc: Exception) -> bool:
    try:
        from jobbot.mail.mailer import MailFatalError
    except ImportError:
        return False
    return isinstance(exc, MailFatalError)


def _send_failure(ctx: AppContext, company: Company, contact: ContactEmail, pacer: Pacer,
                  exc: Exception) -> _Outcome:
    """Auth/policy errors stop the campaign; a rejected recipient is blacklisted (never retried)."""
    message = str(exc)
    log.warning("Envoi à %s (%s) échoué : %s", contact.email, company.name, message)
    if not ctx.dry_run:
        pacer.record(CHANNEL, False, f"{company.name}: {message[:200]}")
    if _is_fatal(exc) or _STOP_ERRORS.search(message):
        return _Outcome("abort", f"Campagne interrompue (compte d'envoi refusé ou limité) : {message[:200]}")
    if _RECIPIENT_ERRORS.search(message) and not ctx.dry_run:
        ctx.repo.blacklist_add(contact.email, f"adresse rejetée à l'envoi : {message[:120]}")
    return _Outcome("failed", f"{company.name} : envoi échoué ({message[:200]})")


def _after_send(ctx: AppContext, company: Company, contact: ContactEmail, docs: TailoredDocuments,
                sent: Any, run: _Run, history: sel.History) -> _Outcome:
    guess = sel.is_guess(contact)
    if ctx.dry_run or sent is None or getattr(sent, "dry_run", False):
        return _Outcome("simulated", f"[simulation] {company.name} : e-mail préparé pour {contact.email}", guess)
    run.pacer.record(CHANNEL, True, company.name)
    if guess:
        sel.record_unverified(ctx.repo, contact.email)
    try:
        _record_sent(ctx, company, contact, docs, sent.message_id, history)
    except Exception as exc:  # the mail left: never let the company be selected again
        log.exception("Candidature envoyée à %s mais suivi non enregistré", company.name)
        _mark_contacted(ctx, company, contact, clock.now())
        return _Outcome("sent", f"{company.name} : envoyée à {contact.email} (suivi non enregistré : {exc})", guess)
    again = " (nouvel essai après un rebond)" if history.state == "retry" else ""
    return _Outcome("sent", f"{company.name} : candidature envoyée à {contact.email}{again}", guess)


def _send(ctx: AppContext, company: Company, contact: ContactEmail, run: _Run, pause: bool,
          history: sel.History) -> _Outcome:
    try:
        docs = _docs_for(ctx, company, history)
        from jobbot.outreach._compose import documents_match_target

        if not documents_match_target(ctx, docs):
            return _Outcome("failed", f"{company.name} : documents mentionnant une alternance, à vérifier")
        if docs.truth_warnings and company.status != "approved":  # never send unbacked claims unreviewed
            return _hold_for_review(ctx, company, contact, docs, history)
        email = build_email(ctx, company, contact, docs)
    except Exception as exc:
        log.exception("Préparation de la candidature pour %s impossible", company.name)
        return _Outcome("failed", f"{company.name} : préparation impossible ({exc})")
    simulate = ctx.dry_run and not getattr(run.mailer, "dry_run", False)
    if pause and not ctx.dry_run:
        run.pacer.between_applications()
    try:
        sent = None if simulate else run.mailer.send(email)
    except Exception as exc:
        return _send_failure(ctx, company, contact, run.pacer, exc)
    return _after_send(ctx, company, contact, docs, sent, run, history)


def _no_contact(ctx: AppContext, company: Company, pick: sel.Pick, history: sel.History) -> _Outcome:
    if not pick.deferred and history.state == "fresh":
        ctx.repo.upsert_company(company.model_copy(update={"status": "no_email"}))
    return _Outcome("skipped", f"{company.name} : {pick.reason}")


def _process(ctx: AppContext, company: Company, run: _Run, pause: bool, simulated: int) -> _Outcome:
    history = sel.company_history(list(run.apps), company, ctx.settings.outreach)
    reason = _skip_reason(ctx, company, history)
    if reason:
        return _Outcome("skipped", f"{company.name} : ignorée ({reason})")
    pick = sel.pick_contact(ctx, company, paused=run.paused, simulated=simulated)
    if pick.contact is None:
        return _no_contact(ctx, company, pick, history)
    if run.review and company.status != "approved":
        return _prepare_for_review(ctx, company, pick.contact, history)
    if run.mailer is None:
        return _Outcome("skipped", f"{company.name} : validée mais envoi impossible (SMTP non configuré)")
    try:
        run.pacer.ensure_available(CHANNEL)
    except QuotaExceeded as exc:
        return _Outcome("stop", str(exc))
    return _send(ctx, company, pick.contact, run, pause, history)


def _start(ctx: AppContext, mailer: Any, review: bool) -> _Run:
    try:
        ramp.check_reset(ctx)  # too many bounces this week: back to the ramp's start (recorded + notified)
    except Exception:  # the cap is still computed by the Pacer
        log.exception("Vérification de la montée progressive impossible")
    apps = ctx.repo.list_applications()
    paused = sel.guesses_paused(apps, ctx.settings.outreach)
    if paused:
        log.warning("Candidatures spontanées : %s", paused)
    return _Run(mailer=mailer, pacer=_build_pacer(ctx), review=review, apps=tuple(apps), paused=paused)


def run_spontaneous_campaign(ctx: AppContext, mailer: Mailer | None, *, limit: int | None = None) -> CampaignReport:
    """Send (or, in review mode, prepare) spontaneous applications to enriched companies, and retry bounced ones."""
    from jobbot.preferences import effective_mode

    review = effective_mode(ctx.settings) == "review"
    if mailer is None and not review:
        msg = "Aucune candidature spontanée envoyée : SMTP non configuré"
        log.warning(msg)
        return CampaignReport(sent=0, skipped=0, failed=0, details=[msg])
    run = _start(ctx, mailer, review)
    budget = limit if limit is not None else run.pacer.remaining(CHANNEL)
    counts = {"sent": 0, "skipped": 0, "failed": 0}
    relevant, off = _targets(ctx, run)
    details: list[str] = ([f"Info : {run.paused}"] if run.paused else []) + relevance.report_lines(off)
    acted = real_sends = simulated_guesses = 0
    for company in relevant:
        if acted >= budget:
            break
        try:
            outcome = _process(ctx, company, run, pause=real_sends > 0, simulated=simulated_guesses)
        except Exception as exc:  # one company never aborts the campaign
            log.exception("Candidature spontanée à %s impossible", company.name)
            outcome = _Outcome("failed", f"{company.name} : erreur inattendue ({exc})")
        details.append(outcome.detail)
        counts["failed"] += int(outcome.kind == "abort")
        if outcome.kind in ("stop", "abort"):
            break
        key = {"simulated": "sent", "pending": "skipped"}.get(outcome.kind, outcome.kind)
        counts[key] += 1
        acted += int(outcome.kind in ("sent", "simulated", "pending", "failed"))
        real_sends += int(outcome.kind == "sent")
        simulated_guesses += int(outcome.kind == "simulated" and outcome.guess)
    log.info("Campagne spontanée : %d envoyées, %d ignorées, %d échecs, %d hors cible", counts["sent"],
             counts["skipped"], counts["failed"], len(off))
    return CampaignReport(sent=counts["sent"], skipped=counts["skipped"], failed=counts["failed"],
                          irrelevant=len(off), details=details)
