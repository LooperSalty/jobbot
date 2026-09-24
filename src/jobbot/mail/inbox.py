"""Inbox processing: read new mail (\\All + \\Junk), match it to applications, update statuses.

Public API (docs/ARCHITECTURE.md): InboundMessage, ReplyClassification, InboxReport, fetch_new_messages,
classify_reply, process_inbox. The heavy lifting lives in imap_client / parsing / classify / platforms / matching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING

from jobbot import clock
from jobbot.config import Secrets
from jobbot.db import Repository
from jobbot.followup import add_working_days
from jobbot.mail.classify import classify_reply, parse_return_date
from jobbot.mail.imap_client import SCAN_FLAGS, FolderCursor, ImapFactory, scan_mailbox
from jobbot.mail.mailer import MailError
from jobbot.mail.matching import ApplicationIndex, learn_own_message, match_by_thread, remember_thread
from jobbot.mail.messages import InboundMessage, InboxReport, ReplyClassification
from jobbot.mail.parsing import strip_quoted
from jobbot.mail.platforms import PlatformNotice, detect_platform_notice
from jobbot.models import Application, ApplicationEvent, ApplicationStatus, JobOffer
from jobbot.notify import notify
from jobbot.textutil import normalize_company, stable_id

if TYPE_CHECKING:
    from jobbot.context import AppContext

__all__ = [
    "InboundMessage", "InboxReport", "ReplyClassification", "classify_reply", "fetch_new_messages", "process_inbox",
]

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DONE_KV_PREFIX = "inbox:done:"
LEGACY_LAST_UID_KEY = "imap_last_uid"
_S = ApplicationStatus
_REPLY_STATUS = {
    "interview": _S.INTERVIEW, "rejection": _S.REJECTED, "unsubscribe": _S.REJECTED, "bounce": _S.BOUNCED,
    "positive": _S.RESPONSE, "info_request": _S.RESPONSE, "other": _S.RESPONSE,
}
_EVENT_KIND = {"interview": "interview", "rejection": "rejected", "unsubscribe": "rejected", "bounce": "bounce"}
_LABEL = {
    "interview": "Entretien", "rejection": "Refus", "positive": "Réponse positive",
    "info_request": "Demande d'informations", "unsubscribe": "Désinscription (ne plus contacter)",
    "bounce": "Adresse invalide", "auto_reply": "Réponse automatique", "other": "Réponse",
}
_NOTIFY_CATEGORIES = {"interview", "positive", "info_request", "other"}


@dataclass(frozen=True)
class _Outcome:
    matched: bool
    update: str | None = None


# --------------------------------------------------------------------------- cursors


def _flag_key(flag: str) -> str:
    return flag.strip("\\").lower()


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value and value.strip().isdigit() else None


def load_cursors(repo: Repository) -> dict[str, FolderCursor]:
    cursors: dict[str, FolderCursor] = {}
    for flag in SCAN_FLAGS:
        key = _flag_key(flag)
        last = _int_or_none(repo.kv_get(f"imap:{key}:last_uid"))
        if last is None and flag == "\\All":
            last = _int_or_none(repo.kv_get(LEGACY_LAST_UID_KEY))
        cursors[flag] = FolderCursor(_int_or_none(repo.kv_get(f"imap:{key}:uidvalidity")), last)
    return cursors


def save_cursors(repo: Repository, cursors: dict[str, FolderCursor]) -> None:
    for flag, cursor in cursors.items():
        key = _flag_key(flag)
        if cursor.uidvalidity is not None:
            repo.kv_set(f"imap:{key}:uidvalidity", str(cursor.uidvalidity))
        if cursor.last_uid is not None:
            repo.kv_set(f"imap:{key}:last_uid", str(cursor.last_uid))
            if flag == "\\All":
                repo.kv_set(LEGACY_LAST_UID_KEY, str(cursor.last_uid))


# --------------------------------------------------------------------------- public API


def fetch_new_messages(
    secrets: Secrets, since_uid: int, *, imap_factory: ImapFactory | None = None, limit: int = DEFAULT_LIMIT
) -> list[InboundMessage]:
    """New messages of \\All and \\Junk with UID > since_uid (each folder; ≤ 0 → last 14 days), ours excluded.

    Raises MailError when the connection or login fails. ``limit`` applies per folder.
    """
    start = since_uid if since_uid and since_uid > 0 else None
    cursors = {flag: FolderCursor(None, start) for flag in SCAN_FLAGS}
    own = frozenset({secrets.smtp_user.strip().lower()}) if secrets.smtp_user else frozenset()
    result = scan_mailbox(secrets, cursors, imap_factory=imap_factory, limit=limit, own_addresses=own)
    return list(result.messages)


def process_inbox(ctx: AppContext, *, imap_factory: ImapFactory | None = None) -> InboxReport:
    """Read new mail, update matching applications, ingest job alerts, notify interviews/positive replies."""
    if not (ctx.secrets.smtp_user and ctx.secrets.smtp_password):
        return InboxReport(scanned=0, matched=0, updates=[
            "Boîte mail non lue : SMTP_USER / SMTP_PASSWORD (mot de passe d'application Gmail) absents du .env."])
    own = frozenset(a.strip().lower() for a in (ctx.secrets.smtp_user, ctx.profile.identity.email) if a)
    try:
        result = scan_mailbox(ctx.secrets, load_cursors(ctx.repo), imap_factory=imap_factory,
                              limit=DEFAULT_LIMIT, own_addresses=own)
    except MailError as exc:
        log.warning("Lecture de la boîte mail impossible : %s", exc)
        return InboxReport(scanned=0, matched=0, updates=[str(exc)])
    for mine in result.own_messages:
        _safe(learn_own_message, ctx.repo, mine)
    updates, matched = list(result.errors), 0
    index = ApplicationIndex.build(ctx.repo) if result.messages else ApplicationIndex(ctx.repo, ())
    for msg in sorted(result.messages, key=lambda m: (m.date or _EPOCH, m.uid)):
        try:
            outcome = _process_message(ctx, msg, index)
        except Exception as exc:  # one bad e-mail never aborts the batch
            log.exception("Traitement du message « %s » impossible", msg.subject)
            outcome = _Outcome(False, f"Erreur sur le message « {msg.subject[:80]} » : {exc}")
        matched += int(outcome.matched)
        if outcome.update:
            updates.append(outcome.update)
    save_cursors(ctx.repo, result.cursors)
    return InboxReport(scanned=len(result.messages), matched=matched, updates=updates)


def _safe(func, *args) -> None:
    try:
        func(*args)
    except Exception:
        log.warning("Étape ignorée (%s)", getattr(func, "__name__", func), exc_info=True)


# --------------------------------------------------------------------------- per message


def _message_key(msg: InboundMessage) -> str:
    return msg.message_id or stable_id(msg.folder, msg.from_addr, msg.subject, str(msg.date))


def _process_message(ctx: AppContext, msg: InboundMessage, index: ApplicationIndex) -> _Outcome:
    done_key = DONE_KV_PREFIX + _message_key(msg)
    if ctx.repo.kv_get(done_key):
        return _Outcome(False)
    outcome = _dispatch(ctx, msg, index)
    if outcome.matched or outcome.update:
        ctx.repo.kv_set(done_key, clock.iso(clock.now()) or "1")
    return outcome


def _dispatch(ctx: AppContext, msg: InboundMessage, index: ApplicationIndex) -> _Outcome:
    app = match_by_thread(ctx.repo, msg)
    if app is not None:
        return _handle_reply(ctx, app, msg, strong=True)
    if msg.is_bounce and msg.bounced_recipient:
        bounced = ctx.repo.find_applications_by_email(msg.bounced_recipient)
        if bounced:
            return _handle_reply(ctx, bounced[0], msg, strong=True)
    notice = detect_platform_notice(msg)
    if notice is not None:
        return _handle_notice(ctx, notice, msg, index)
    alert = _handle_alert(ctx, msg)
    if alert is not None:
        return alert
    app = index.match_sender(msg.from_addr)
    if app is not None and not msg.is_bounce:
        return _handle_reply(ctx, app, msg, strong=False)
    return _Outcome(False)


def _label(app: Application) -> str:
    title = f" – {app.job_title}" if app.job_title else ""
    return f"{app.company_name or 'Entreprise inconnue'}{title}"


def _junk_note(msg: InboundMessage) -> str:
    folder = msg.folder.lower()
    if not any(word in folder for word in ("spam", "junk", "indésirable", "pourriel")):
        return ""
    return " (trouvé dans les spams : marque-le « Non spam » dans Gmail)"


def _event(ctx: AppContext, app: Application, kind: str, detail: str) -> None:
    if app.id is not None:
        ctx.repo.add_event(ApplicationEvent(application_id=app.id, at=clock.now(), kind=kind, detail=detail))


_KEEP_ON_PLAIN_RESPONSE = frozenset({_S.INTERVIEW, _S.OFFER, _S.REJECTED, _S.WITHDRAWN})


def _next_status(app: Application, target: ApplicationStatus) -> ApplicationStatus:
    """A plain 'response' never overrides a more informative status (interview, offer, rejection...)."""
    if target == _S.RESPONSE and app.status in _KEEP_ON_PLAIN_RESPONSE:
        return app.status
    return target


# --------------------------------------------------------------------------- replies


def _handle_reply(ctx: AppContext, app: Application, msg: InboundMessage, *, strong: bool) -> _Outcome:
    cls = classify_reply(msg, ctx.llm)
    remember_thread(ctx.repo, msg.gm_thread_id, app.id)
    if cls.category == "auto_reply":
        line = _auto_reply(ctx, app, msg, cls)
    elif cls.category == "other" and not strong:
        _event(ctx, app, "note", f"Message de {msg.from_addr} : {cls.summary}")
        line = f"{_label(app)} : message reçu de {msg.from_addr} (non classé)"
    else:
        line = _human_reply(ctx, app, msg, cls)
    return _Outcome(True, line + _junk_note(msg))


def _auto_reply(ctx: AppContext, app: Application, msg: InboundMessage, cls: ReplyClassification) -> str:
    back = parse_return_date(strip_quoted(msg.body_text) or msg.subject, msg.date or clock.now())
    detail = f"Réponse automatique de {msg.from_addr} : {cls.summary}"
    if back and app.next_followup_at is not None:
        resume = datetime.combine(add_working_days(back, 2), time(9, 0), clock.PARIS)
        if resume > app.next_followup_at:
            ctx.repo.update_application(app.model_copy(update={"next_followup_at": resume}))
            detail += f" — relance repoussée au {clock.french_date(resume)}"
    _event(ctx, app, "note", detail)
    suffix = f" (retour annoncé le {back.strftime('%d/%m/%Y')})" if back else ""
    return f"{_label(app)} : réponse automatique{suffix}"


def _human_reply(ctx: AppContext, app: Application, msg: InboundMessage, cls: ReplyClassification) -> str:
    category = cls.category
    update: dict = {"status": _next_status(app, _REPLY_STATUS[category]), "next_followup_at": None}
    if category == "bounce":
        address = msg.bounced_recipient or app.email_to
        if address:
            ctx.repo.blacklist_add(address, "adresse invalide (bounce)")
    else:
        update["last_response_at"] = msg.date or clock.now()
    if category == "unsubscribe":
        for address in {msg.from_addr, app.email_to} - {None, ""}:
            ctx.repo.blacklist_add(address, "demande de ne plus être contacté")
    ctx.repo.update_application(app.model_copy(update=update))
    label = _LABEL.get(category, "Réponse")
    _event(ctx, app, _EVENT_KIND.get(category, "reply"), f"{label} ({msg.from_addr}) : {cls.summary}")
    if category in _NOTIFY_CATEGORIES:
        level = "urgent" if category == "interview" else "important"
        notify(ctx, f"{label} — {app.company_name or msg.from_addr}", cls.summary, level=level)
    prep = _interview_prep(ctx, app, msg) if category == "interview" else ""
    return f"{_label(app)} : {label.lower()} — {cls.summary}{prep}"


def _interview_prep(ctx: AppContext, app: Application, msg: InboundMessage) -> str:
    """Prep sheet for an interview invitation (``reports.interview_prep``); never breaks the inbox."""
    try:
        from jobbot.reports.interview import prep_on_interview

        path = prep_on_interview(ctx, app, msg)
    except Exception:
        log.warning("Fiche d'entretien non générée pour « %s »", app.company_name, exc_info=True)
        return ""
    return f" — fiche d'entretien : {path.name}" if path else ""


# --------------------------------------------------------------------------- platforms & alerts


def _handle_notice(
    ctx: AppContext, notice: PlatformNotice, msg: InboundMessage, index: ApplicationIndex
) -> _Outcome:
    text = f"{msg.subject}\n{msg.body_text[:3000]}"
    app = index.match_company(notice.company, text, notice.methods)
    who = notice.company or "entreprise non identifiée"
    if app is None:
        orphan = f"Notification {notice.platform} ({who}) sans candidature correspondante : {notice.detail}"
        return _Outcome(False, orphan)
    if notice.status is not None:
        update: dict = {"status": _next_status(app, notice.status), "next_followup_at": None}
        if notice.status != _S.BOUNCED:
            update["last_response_at"] = msg.date or clock.now()
        ctx.repo.update_application(app.model_copy(update=update))
    _event(ctx, app, notice.event_kind, f"{notice.detail} — {msg.subject}")
    if notice.status in (_S.INTERVIEW, _S.RESPONSE):
        level = "urgent" if notice.status == _S.INTERVIEW else "important"
        notify(ctx, f"{notice.detail} — {app.company_name}", msg.subject, level=level)
    prep = _interview_prep(ctx, app, msg) if notice.status == _S.INTERVIEW else ""
    return _Outcome(True, f"{_label(app)} : {notice.detail}{prep}{_junk_note(msg)}")


def _skip_job(ctx: AppContext, job: JobOffer) -> bool:
    excluded = {normalize_company(c) for c in ctx.profile.target.exclude_companies}
    key = normalize_company(job.company)
    return bool(key and key in excluded) or ctx.repo.is_blacklisted(job.company, job.company_domain or "")


def _handle_alert(ctx: AppContext, msg: InboundMessage) -> _Outcome | None:
    try:
        from jobbot.sources.alerts import jobs_from_alert_email
    except ImportError:
        return None
    try:
        jobs = jobs_from_alert_email(msg.from_addr, msg.subject, msg.body_html, msg.body_text)
    except Exception:  # the alert parser must never stop the inbox
        log.warning("Analyse de l'alerte « %s » impossible", msg.subject, exc_info=True)
        return None
    if not jobs:
        return None
    new = 0
    for job in jobs:
        if _skip_job(ctx, job):
            continue
        try:
            new += int(ctx.repo.upsert_job(job))
        except Exception:
            log.warning("Offre d'alerte non enregistrée : %s", job.url, exc_info=True)
    sender = msg.from_addr.rsplit("@", 1)[-1]
    return _Outcome(False, f"Alerte e-mail {sender} : {len(jobs)} offre(s), dont {new} nouvelle(s)")
