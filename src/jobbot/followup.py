"""Follow-ups ("relances"): scheduling in working days, French messages, in-thread sending, give-up.

Research (docs/research/email-sending-inbox.md §1, §6.7, §6.11): working days with Moselle holidays, max 2
relances, never after a human reply, Tue–Thu mornings preferred, same thread (Re: + In-Reply-To + References).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from jobbot import clock
from jobbot.config import FollowupSettings, Profile
from jobbot.llm import LLMClient, LLMError
from jobbot.models import TERMINAL_APPLICATION_STATUSES, Application, ApplicationEvent, ApplicationStatus
from jobbot.pacing import Pacer, QuotaExceeded
from jobbot.textutil import truncate

if TYPE_CHECKING:
    from jobbot.context import AppContext
    from jobbot.mail.mailer import Mailer

log = logging.getLogger(__name__)

CHANNEL = "followup"
SLOT_START = time(9, 0)  # Paris time; + a per-application offset up to SLOT_SPREAD_MIN
SLOT_SPREAD_MIN = 110
MAX_BODY_CHARS = 1500
MIN_BODY_CHARS = 80
_OPEN_STATUSES = (ApplicationStatus.APPLIED, ApplicationStatus.FOLLOWED_UP)
_PLACEHOLDER = re.compile(r"(?:[{}\[\]]|X{3,})")
_MONTHS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
              "novembre", "décembre")


# --------------------------------------------------------------------------- working days (France + Moselle)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    wk = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * wk) // 433
    month = (h + wk - 7 * m + 90) // 25
    return date(year, month, (h + wk - 7 * m + 33 * month + 19) % 32)


@lru_cache(maxsize=16)
def holidays_moselle(year: int) -> frozenset[date]:
    """French public holidays + Alsace-Moselle extras (Good Friday, 26 December)."""
    easter = _easter(year)
    fixed = [(1, 1), (5, 1), (5, 8), (7, 14), (8, 15), (11, 1), (11, 11), (12, 25), (12, 26)]
    moving = [easter - timedelta(days=2), easter + timedelta(days=1), easter + timedelta(days=39),
              easter + timedelta(days=50)]
    return frozenset([date(year, m, d) for m, d in fixed] + moving)


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays_moselle(d.year)


def add_working_days(d: date, n: int) -> date:
    current = d
    remaining = max(0, n)
    while remaining:
        current += timedelta(days=1)
        if is_working_day(current):
            remaining -= 1
    return current


def _next_working_day(d: date) -> date:
    current = d
    while not is_working_day(current):
        current += timedelta(days=1)
    return current


def _slot(d: date, app_id: int | None) -> datetime:
    """Tuesday–Thursday morning when possible (Monday → Tuesday, Friday → Thursday)."""
    day = _next_working_day(d)
    if day.weekday() == 0:
        day = _next_working_day(day + timedelta(days=1))
    elif day.weekday() == 4 and is_working_day(day - timedelta(days=1)):
        day -= timedelta(days=1)
    minutes = ((app_id or 0) * 37) % SLOT_SPREAD_MIN
    local = datetime.combine(day, SLOT_START, clock.PARIS) + timedelta(minutes=minutes)
    return local.astimezone(UTC)


# --------------------------------------------------------------------------- scheduling


def _give_up_at(app: Application, cfg: FollowupSettings, base: datetime, *, last: bool) -> datetime:
    deadline = clock.parse_dt(app.applied_at) + timedelta(days=cfg.give_up_after_days)
    if not last:
        return deadline
    grace = datetime.combine(add_working_days(clock.to_paris(base).date(), cfg.second_after_days),
                             time(18, 0), clock.PARIS).astimezone(UTC)
    return max(deadline, grace)


def compute_next_followup(app: Application, cfg: FollowupSettings, *, after: datetime | None = None) -> datetime | None:
    """Next relance date (working days), or — once every relance is sent — the give-up checkpoint.

    None when the application is closed or a human already answered.
    """
    if app.status not in _OPEN_STATUSES or app.last_response_at is not None:
        return None
    base = clock.parse_dt(after or app.last_contact_at or app.applied_at)
    if app.followup_count >= cfg.max_followups:
        return _give_up_at(app, cfg, base, last=True)
    days = cfg.first_after_days if app.followup_count == 0 else cfg.second_after_days
    due = _slot(add_working_days(clock.to_paris(base).date(), days), app.id)
    if due <= base:
        due = base + timedelta(days=1)
    return min(due, _give_up_at(app, cfg, base, last=False))


# --------------------------------------------------------------------------- writing


def _is_spontaneous(app: Application) -> bool:
    return app.job_id is None


def _month_label(value: str) -> str:
    """'2026-10' → 'octobre 2026' (anything else is returned unchanged)."""
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", (value or "").strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        return value
    return f"{_MONTHS_FR[int(match.group(2)) - 1]} {match.group(1)}"


def _signature(profile: Profile) -> str:
    ident = profile.identity
    return "\n".join(p for p in (ident.full_name, ident.phone) if p)


def _what(app: Application, profile: Profile) -> str:
    if _is_spontaneous(app):
        kind = "en alternance" if profile.alternance else ""
        return f"ma candidature spontanée {kind}".strip()
    return f"ma candidature au poste de « {app.job_title} »" if app.job_title else "ma candidature"


def _new_element(profile: Profile) -> str:
    alt = profile.alternance
    if alt and alt.degree:
        start = _month_label(alt.start_date)
        rhythm = f" ({alt.rhythm})" if alt.rhythm else ""
        when = f" et je peux démarrer dès {start}" if start else ""
        return f"Pour rappel, je prépare le titre {alt.degree} à {alt.school}{rhythm}{when}."
    availability = profile.answers.get("availability_date") or profile.answers.get("notice_period")
    return f"Je reste disponible à partir de : {availability}." if availability else ""


def _opt_out(app: Application) -> str:
    if not _is_spontaneous(app):
        return ""
    return "\n\nPS : si vous ne souhaitez plus être contacté(e), un simple « non merci » en réponse suffit."


def _template(profile: Profile, app: Application, last: bool) -> str:
    sent_on = clock.french_date(app.applied_at)
    what = _what(app, profile)
    element = _new_element(profile)
    if last:
        text = (
            f"Bonjour,\n\nUn dernier message au sujet de {what}, envoyée le {sent_on} : si le moment n'est pas "
            "opportun, je le comprends tout à fait. Une réponse, même négative, m'aiderait beaucoup, et je ne "
            f"vous relancerai plus.\n{element}\n\nBelle journée,\n{_signature(profile)}"
        )
    else:
        text = (
            f"Bonjour,\n\nJe me permets de revenir vers vous au sujet de {what}, envoyée le {sent_on}.\n{element}\n\n"
            "Auriez-vous quelques minutes pour en échanger, ou pourriez-vous m'indiquer la personne à contacter ?"
            f"\n\nBien cordialement,\n{_signature(profile)}"
        )
    return re.sub(r"\n{3,}", "\n\n", text) + _opt_out(app)


def _default_subject(app: Application, profile: Profile) -> str:
    if _is_spontaneous(app):
        return f"Re: Candidature spontanée – {profile.identity.full_name}"
    return f"Re: Candidature – {app.job_title or app.company_name or profile.identity.full_name}"


def _llm_prompt(profile: Profile, app: Application, number: int, last: bool) -> str:
    context = "candidature spontanée" if _is_spontaneous(app) else f"offre « {app.job_title} »"
    return (
        f"Rédige le corps d'un e-mail de relance n°{number} en français pour une {context} chez "
        f"{app.company_name or 'une entreprise'}, envoyée le {clock.french_date(app.applied_at)}.\n"
        "Règles : vouvoiement ; 4 à 8 lignes ; poli, sans pression ni culpabilisation ; rappeler la candidature et "
        "sa date ; apporter UN élément nouveau VRAI tiré du profil ; poser UNE seule question ; ne jamais parler de "
        "salaire ; ne rien inventer ; aucun champ à compléter entre crochets.\n"
        + ("C'est la dernière relance : l'annoncer avec tact.\n" if last else "")
        + f"Commence par « Bonjour, » et termine par une formule de politesse puis cette signature exacte :\n"
        f"{_signature(profile)}\n\nProfil (seule source de vérité) :\n{truncate(profile.full_text(), 2500)}\n\n"
        "Réponds uniquement avec le texte de l'e-mail, sans objet."
    )


def _valid_llm_body(text: str, profile: Profile) -> str | None:
    body = re.sub(r"^\s*objet\s*:.*\n", "", (text or "").strip(), flags=re.IGNORECASE).strip()
    if not MIN_BODY_CHARS <= len(body) <= MAX_BODY_CHARS or _PLACEHOLDER.search(body):
        return None
    if profile.identity.last_name.lower() not in body.lower():
        body = f"{body}\n\n{_signature(profile)}"
    return body


def write_followup(profile: Profile, app: Application, llm: LLMClient | None, *, number: int) -> tuple[str, str]:
    """(subject, body) for relance ``number`` (1-based). LLM when available, French template otherwise."""
    last = number >= 2
    subject = _default_subject(app, profile)
    body: str | None = None
    if llm is not None:
        try:
            body = _valid_llm_body(llm.complete(_llm_prompt(profile, app, number, last)), profile)
        except LLMError as exc:
            log.info("Relance rédigée sans LLM (%s)", exc)
    if not body:
        return subject, _template(profile, app, last)
    opt_out = _opt_out(app)
    if opt_out and "non merci" not in body.lower():
        body += opt_out
    return subject, body


# --------------------------------------------------------------------------- run


class FollowupReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sent: int
    manual: int
    closed: int
    details: list[str] = Field(default_factory=list)


def _build_pacer(ctx: AppContext) -> Pacer:
    return Pacer(ctx.repo, ctx.settings.limits)


def _label(app: Application) -> str:
    title = f" – {app.job_title}" if app.job_title else ""
    return f"{app.company_name or 'entreprise inconnue'}{title}"


def _event(ctx: AppContext, app: Application, kind: str, detail: str) -> None:
    if app.id is not None:
        ctx.repo.add_event(ApplicationEvent(application_id=app.id, at=clock.now(), kind=kind, detail=detail))


def _thread_chain(app: Application) -> list[str]:
    ids = ([app.email_message_id] if app.email_message_id else []) + list(app.email_thread_ids)
    return list(dict.fromkeys(i for i in ids if i))


def _close(ctx: AppContext, app: Application) -> str:
    detail = f"Sans réponse après {app.followup_count} relance(s) : candidature close"
    ctx.repo.update_application(app.model_copy(update={"status": ApplicationStatus.NO_RESPONSE,
                                                       "next_followup_at": None}))
    _event(ctx, app, "status_change", detail)
    return f"{_label(app)} : {detail}"


def _cancel(ctx: AppContext, app: Application, reason: str) -> str:
    ctx.repo.update_application(app.model_copy(update={"next_followup_at": None}))
    _event(ctx, app, "note", f"Relances annulées : {reason}")
    return f"{_label(app)} : relances annulées ({reason})"


def _manual(ctx: AppContext, app: Application, number: int, now: datetime, why: str) -> str:
    detail = f"Relance n°{number} à faire manuellement ({app.method.value}{why}) : {_label(app)}"
    progressed = app.model_copy(update={"followup_count": app.followup_count + 1})
    nxt = compute_next_followup(progressed, ctx.settings.followup, after=now)
    ctx.repo.update_application(progressed.model_copy(update={"next_followup_at": nxt}))
    _event(ctx, app, "manual_followup", detail)
    return detail


def _triage(ctx: AppContext, app: Application, mailer: Mailer | None, now: datetime) -> tuple[str, str]:
    """Everything that does not send: cancel, close, manual. ('send', '') when an e-mail must go out."""
    cfg = ctx.settings.followup
    if app.last_response_at is not None:
        return "skipped", _cancel(ctx, app, "réponse reçue")
    deadline = clock.parse_dt(app.applied_at) + timedelta(days=cfg.give_up_after_days)
    if app.followup_count >= cfg.max_followups or now >= deadline:
        return "closed", _close(ctx, app)
    number = app.followup_count + 1
    if not app.email_to:
        return "manual", _manual(ctx, app, number, now, "")
    if mailer is None:
        return "manual", _manual(ctx, app, number, now, f", SMTP non configuré : écrire à {app.email_to}")
    domain = app.email_to.rsplit("@", 1)[-1]
    if ctx.repo.is_blacklisted(app.email_to, domain):
        return "skipped", _cancel(ctx, app, "adresse en liste noire")
    return "send", ""


def _compose(ctx: AppContext, app: Application, number: int):
    """OutgoingEmail for relance ``number``: original subject as 'Re:', threaded on our last message."""
    from jobbot.mail.mailer import SUBJECT_KV_PREFIX, OutgoingEmail, reply_subject

    subject, body = write_followup(ctx.profile, app, ctx.llm, number=number)
    chain = _thread_chain(app)
    original = ctx.repo.kv_get(SUBJECT_KV_PREFIX + chain[0]) if chain else None
    if original:
        subject = reply_subject(original)
    elif not chain:  # nothing to thread on: do not pretend it is a reply
        subject = "Relance – " + re.sub(r"^\s*re\s*:\s*", "", subject, flags=re.IGNORECASE)
    cv = [app.cv_path] if number == 1 and app.cv_path and Path(app.cv_path).is_file() else []
    return OutgoingEmail(to=app.email_to or "", subject=subject, body=body, attachments=cv,
                         in_reply_to=chain[-1] if chain else None, references=chain, kind="followup",
                         application_id=app.id)


def _send(
    ctx: AppContext, app: Application, mailer: Mailer, pacer: Pacer, now: datetime, pause: bool
) -> tuple[str, str]:
    from jobbot.mail.mailer import MailError, MailFatalError

    number = app.followup_count + 1
    simulate = bool(getattr(mailer, "dry_run", False))
    if ctx.dry_run and not simulate:  # never send for real during a dry run
        return "sent", f"[simulation] Relance n°{number} non envoyée (dry-run) : {_label(app)}"
    email = _compose(ctx, app, number)
    if pause and not simulate:
        pacer.between_applications()
    try:
        sent = mailer.send(email)
    except MailFatalError as exc:
        pacer.record(CHANNEL, False, str(exc))
        return "fatal", f"{_label(app)} : {exc}"
    except MailError as exc:
        pacer.record(CHANNEL, False, str(exc))
        return "error", f"{_label(app)} : relance non envoyée ({exc})"
    if simulate or sent.dry_run:
        return "sent", f"[simulation] Relance n°{number} préparée : {_label(app)} ({app.email_to})"
    pacer.record(CHANNEL, True, f"relance {number} → {app.email_to}")
    updated = app.model_copy(update={
        "followup_count": number, "email_thread_ids": [*_thread_chain(app), sent.message_id],
        "last_contact_at": now, "status": ApplicationStatus.FOLLOWED_UP,
    })
    nxt = compute_next_followup(updated, ctx.settings.followup, after=now)
    ctx.repo.update_application(updated.model_copy(update={"next_followup_at": nxt}))
    _event(ctx, app, "followup_sent", f"Relance n°{number} envoyée à {app.email_to}")
    return "sent", f"Relance n°{number} envoyée : {_label(app)} ({app.email_to})"


def _attempt(ctx: AppContext, app: Application, mailer: Mailer | None, pacer: Pacer, now: datetime,
             blocked: str | None, sent_so_far: int) -> tuple[str, str]:
    outcome, detail = _triage(ctx, app, mailer, now)
    if outcome != "send" or mailer is None:
        return outcome, detail
    if blocked:
        return "deferred", f"{_label(app)} : relance reportée ({blocked})"
    try:
        pacer.ensure_available(CHANNEL)
    except QuotaExceeded as exc:
        return "quota", str(exc)
    return _send(ctx, app, mailer, pacer, now, pause=sent_so_far > 0)


def run_followups(ctx: AppContext, mailer: Mailer | None) -> FollowupReport:
    """Process due applications: send in-thread relances, list manual ones, close the silent ones."""
    now = clock.now()
    pacer = _build_pacer(ctx)
    counts: dict[str, int] = {}
    details: list[str] = []
    blocked: str | None = None
    for app in ctx.repo.due_followups(now):
        if app.status in TERMINAL_APPLICATION_STATUSES:
            continue
        try:
            outcome, detail = _attempt(ctx, app, mailer, pacer, now, blocked, counts.get("sent", 0))
        except Exception as exc:  # one broken application never stops the others
            log.exception("Relance impossible pour la candidature %s", app.id)
            outcome, detail = "error", f"{_label(app)} : erreur inattendue ({exc})"
        if outcome in ("fatal", "quota"):
            blocked = blocked or detail
        counts[outcome] = counts.get(outcome, 0) + 1
        if detail:
            details.append(detail)
    return FollowupReport(sent=counts.get("sent", 0), manual=counts.get("manual", 0),
                          closed=counts.get("closed", 0), details=details)
