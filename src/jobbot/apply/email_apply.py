"""Application by e-mail to the contact address given in an offer.

Body = the short application e-mail written with the letter (``docs.email_body``, fallback: the letter text),
attachments = tailored CV + cover letter PDF. Sending goes through ``mail.Mailer`` (blacklist, threading,
dry-run ``.eml``). Quotas and pauses are handled by ``apply.service`` (channel ``email``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from jobbot.apply.base import ApplyResult
from jobbot.context import AppContext
from jobbot.models import ApplyMethod, JobOffer, JobStatus, TailoredDocuments
from jobbot.textutil import normalize_company, truncate

if TYPE_CHECKING:
    from jobbot.mail.mailer import Mailer, OutgoingEmail

log = logging.getLogger(__name__)

MAX_DETAIL_CHARS = 240


def _result(ok: bool, status: JobStatus, detail: str, **extra: str | None) -> ApplyResult:
    return ApplyResult(ok=ok, status=status, method=ApplyMethod.EMAIL, detail=detail, **extra)


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


class EmailApplier:
    """Channel ``email``: one personalised e-mail per offer, never twice to a blacklisted address."""

    channel = "email"

    def __init__(self, ctx: AppContext, mailer: Mailer):
        self.ctx = ctx
        self.mailer = mailer
        self.fatal: str | None = None  # set when the whole e-mail channel must stop for this run

    # ------------------------------------------------------------------ Applier protocol

    def can_apply(self, job: JobOffer) -> bool:
        from jobbot.mail.mailer import is_valid_address

        address = (job.contact_email or "").strip().lower()
        if not address or not is_valid_address(address):
            return False
        company_key = normalize_company(job.company)
        return not self.ctx.repo.is_blacklisted(address, _domain(address), job.company, company_key)

    def apply(self, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        from jobbot.mail.mailer import MailError, MailFatalError

        problem = self._precheck(job, docs)
        if problem is not None:
            return problem
        address = (job.contact_email or "").strip().lower()
        if self.ctx.dry_run and not getattr(self.mailer, "dry_run", False):
            return _result(True, JobStatus.SKIPPED, f"[dry-run] E-mail de candidature pour {address} non envoyé.")
        try:
            sent = self.mailer.send(self.build_email(job, docs))
        except MailFatalError as exc:
            self.fatal = str(exc)
            log.error("Canal e-mail arrêté : %s", exc)
            return _result(False, JobStatus.FAILED, truncate(f"Envoi impossible : {exc}", MAX_DETAIL_CHARS))
        except MailError as exc:
            log.warning("Candidature e-mail %s non envoyée : %s", job.id, exc)
            return _result(False, JobStatus.FAILED, truncate(f"E-mail non envoyé : {exc}", MAX_DETAIL_CHARS))
        if sent.dry_run:
            return _result(True, JobStatus.SKIPPED,
                           f"[dry-run] E-mail de candidature écrit dans output/outbox (non envoyé) pour {address}.")
        return _result(True, JobStatus.APPLIED, f"Candidature envoyée par e-mail à {address}.",
                       email_to=address, email_message_id=sent.message_id)

    # ------------------------------------------------------------------ helpers

    def _precheck(self, job: JobOffer, docs: TailoredDocuments) -> ApplyResult | None:
        if self.fatal:
            return _result(False, JobStatus.SKIPPED, f"Envoi d'e-mails suspendu pour aujourd'hui : {self.fatal}")
        if not self.can_apply(job):
            return _result(False, JobStatus.SKIPPED, "Adresse e-mail absente, invalide ou en liste noire.")
        if not docs.cv_pdf or not Path(docs.cv_pdf).is_file():
            return _result(False, JobStatus.FAILED, f"CV introuvable : {docs.cv_pdf or '(aucun)'}")
        return None

    def build_email(self, job: JobOffer, docs: TailoredDocuments) -> OutgoingEmail:
        from jobbot.mail.mailer import OutgoingEmail

        attachments = [p for p in (docs.cv_pdf, docs.letter_pdf) if p and Path(p).is_file()]
        body = docs.email_body.strip() or docs.letter_text.strip()
        subject = docs.email_subject.strip() or default_subject(self.ctx, job)
        return OutgoingEmail(
            to=(job.contact_email or "").strip(), subject=subject, body=body, attachments=attachments,
            kind="application",
        )


def default_subject(ctx: AppContext, job: JobOffer) -> str:
    return f"Candidature – {job.title} – {ctx.profile.identity.full_name}"
