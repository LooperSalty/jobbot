"""Interview prep sheets (« fiches d'entretien ») — ``output/entretiens/<date>-<entreprise>.md``.

Built when the inbox classifies a reply as an interview invitation (``settings.reports.interview_prep``), or on
demand (``jobbot prep <id>``, dashboard « Entretiens »). Content: company summary, what the role needs, truthful
STAR talking points (profile facts only), likely questions with answer leads, questions to ask, logistics read
from the e-mail. Quality LLM when available, deterministic template otherwise (and for every rejected section).

kv keys: ``reports:prep:<application id>`` (JSON: path, at, logistics) and ``reports:interview_mail:<id>``
(JSON: text, contact, subject of the invitation, so that a later regeneration keeps the logistics).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jobbot import clock
from jobbot.mail.messages import InboundMessage
from jobbot.models import Application, ApplicationStatus
from jobbot.notify import notify
from jobbot.reports._prep_facts import PrepFacts, company_summary, gather_facts, star_points
from jobbot.reports._prep_llm import PrepContent, ask_llm, merge
from jobbot.reports._prep_questions import likely_questions, questions_to_ask
from jobbot.reports._prep_render import render_prep
from jobbot.textutil import html_to_text, slugify, truncate

if TYPE_CHECKING:
    from jobbot.context import AppContext

log = logging.getLogger(__name__)

PREP_DIR = "entretiens"
PREP_KV_PREFIX = "reports:prep:"
MAIL_KV_PREFIX = "reports:interview_mail:"
MAIL_CHARS = 6000
EVENT_KINDS = ("interview", "reply")

__all__ = ["build_interview_prep", "generate_prep", "interview_rows", "prep_on_interview", "prep_path"]


# --------------------------------------------------------------------------- public API


def build_interview_prep(ctx: AppContext, application: Application, *,
                         message: InboundMessage | None = None) -> Path:
    """Write the prep sheet of ``application`` and return its path. ``message`` = the invitation e-mail."""
    app = _fresh(ctx, application)
    email_text, contact = _invitation(ctx, app, message)
    facts = gather_facts(ctx, app, email_text, contact)
    text = render_prep(ctx.profile, facts, _content(ctx, facts), generated_at=clock.now())
    path = _write(ctx, facts, text)
    _remember(ctx, app, path, facts)
    when = facts.logistics.short() or "date à confirmer"
    notify(ctx, f"Fiche d'entretien prête — {facts.company_name}", f"{when} — {path.name}")
    log.info("Fiche d'entretien écrite : %s", path)
    return path


def prep_on_interview(ctx: AppContext, application: Application,
                      message: InboundMessage | None = None) -> Path | None:
    """Inbox hook: sheet for an interview invitation when ``reports.interview_prep`` is on. Never raises."""
    if not ctx.settings.reports.interview_prep:
        return None
    try:
        return build_interview_prep(ctx, application, message=message)
    except Exception:  # the inbox must go on whatever happens here
        log.warning("Fiche d'entretien impossible pour « %s »", application.company_name, exc_info=True)
        return None


def generate_prep(ctx: AppContext, application_id: int) -> str:
    """CLI / dashboard action: (re)build the sheet of one application. French message."""
    app = ctx.repo.get_application(application_id)
    if app is None:
        return f"Candidature {application_id} introuvable."
    try:
        path = build_interview_prep(ctx, app)
    except Exception as exc:
        log.exception("Fiche d'entretien impossible pour la candidature %s", application_id)
        return f"Fiche d'entretien impossible : {exc}"
    return f"Fiche d'entretien prête : {path}"


def prep_path(ctx: AppContext, application_id: int | None) -> Path | None:
    """Last prep sheet written for this application (None if none or deleted)."""
    stored = _stored(ctx, PREP_KV_PREFIX, application_id)
    path = Path(str(stored.get("path", ""))) if stored else None
    return path if path is not None and path.is_file() else None


def interview_rows(ctx: AppContext) -> list[dict[str, Any]]:
    """Dashboard rows: applications at the interview stage or with a prep sheet (most recent first)."""
    from jobbot.cli_support import APP_STATUS_FR, METHOD_FR, label

    rows = []
    for app in ctx.repo.list_applications():
        stored = _stored(ctx, PREP_KV_PREFIX, app.id)
        if app.status != ApplicationStatus.INTERVIEW and not stored:
            continue
        sheet = prep_path(ctx, app.id)
        rows.append({
            "id": app.id, "entreprise": app.company_name or "Entreprise inconnue", "poste": app.job_title,
            "canal": label(METHOD_FR, app.method), "statut": label(APP_STATUS_FR, app.status),
            "reponse_le": clock.to_paris(app.last_response_at).strftime("%d/%m/%Y") if app.last_response_at else "",
            "fiche": str(sheet) if sheet else None, "logistique": str((stored or {}).get("logistics", "")),
        })
    return rows


# --------------------------------------------------------------------------- internals


def _fresh(ctx: AppContext, app: Application) -> Application:
    """The stored version (status just updated by the inbox), else the given one."""
    return (ctx.repo.get_application(app.id) if app.id is not None else None) or app


def _stored(ctx: AppContext, prefix: str, application_id: int | None) -> dict[str, Any] | None:
    if application_id is None:
        return None
    raw = ctx.repo.kv_get(f"{prefix}{application_id}")
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _message_text(message: InboundMessage) -> str:
    from jobbot.mail.parsing import strip_quoted

    text = strip_quoted(message.body_text) or html_to_text(message.body_html)
    return truncate(text.strip(), MAIL_CHARS)


def _invitation(ctx: AppContext, app: Application, message: InboundMessage | None) -> tuple[str, str]:
    """(new text of the invitation, sender) — from the message, the stored copy, or the events."""
    if message is not None:
        contact = f"{message.from_name} <{message.from_addr}>" if message.from_name else message.from_addr
        text = _message_text(message)
        if app.id is not None:
            payload = {"text": text, "contact": contact, "subject": message.subject}
            ctx.repo.kv_set(f"{MAIL_KV_PREFIX}{app.id}", json.dumps(payload, ensure_ascii=False))
        return text, contact
    stored = _stored(ctx, MAIL_KV_PREFIX, app.id)
    if stored:
        return str(stored.get("text", "")), str(stored.get("contact", ""))
    events = ctx.repo.list_events(app.id) if app.id is not None else []
    return "\n".join(e.detail for e in events if e.kind in EVENT_KINDS), ""


def _content(ctx: AppContext, facts: PrepFacts) -> PrepContent:
    stars = star_points(ctx.profile, facts)
    base = PrepContent(
        company_summary=company_summary(facts), role_needs=(), stars=tuple(stars),
        questions=tuple(likely_questions(ctx.profile, facts, stars)), to_ask=tuple(questions_to_ask(facts)),
    )
    draft = ask_llm(ctx.llm, ctx.profile, facts)
    return base if draft is None else merge(base, draft, ctx.profile, facts)


def _write(ctx: AppContext, facts: PrepFacts, text: str) -> Path:
    folder = ctx.output_dir / PREP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    day = clock.to_paris(clock.now()).date().isoformat()
    path = folder / f"{day}-{slugify(facts.company_name, 50)}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _remember(ctx: AppContext, app: Application, path: Path, facts: PrepFacts) -> None:
    if app.id is None:
        return
    payload = {"path": str(path), "at": clock.iso(clock.now()), "logistics": facts.logistics.short()}
    ctx.repo.kv_set(f"{PREP_KV_PREFIX}{app.id}", json.dumps(payload, ensure_ascii=False))
