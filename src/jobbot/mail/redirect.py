"""Follow clear recruiter instructions to use another address, or flag a careers link."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from jobbot.context import AppContext
from jobbot.mail.messages import InboundMessage
from jobbot.mail.parsing import strip_quoted
from jobbot.models import Application, ApplicationEvent, ContactEmail
from jobbot import clock
from jobbot.textutil import extract_emails, stable_id

_REQUEST = re.compile(r"(?:postul|candidatur|recrut|apply|application).{0,180}(?:adress|envoy|transmet|send|via|site|portail|platform|plateforme)|(?:adress|envoy|transmet|send).{0,100}(?:candidatur|cv|application)", re.I | re.S)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def follow_redirect(ctx: AppContext, app: Application, msg: InboundMessage, *, strong: bool) -> str:
    """Follow an explicit reply on an existing thread. Unknown sites remain for the user to handle."""
    if not strong or msg.is_bounce or app.id is None:
        return ""
    body = strip_quoted(msg.body_text or "")[:6000]
    if not _REQUEST.search(body):
        return ""
    sender_domain = msg.from_addr.lower().rsplit("@", 1)[-1]
    original_domain = (app.email_to or "").lower().rsplit("@", 1)[-1]
    addresses = [a.lower() for a in extract_emails(body) if a.lower() not in
                 {msg.from_addr.lower(), (app.email_to or "").lower(), ctx.profile.identity.email.lower()}]
    candidates = [a for a in addresses if a.rsplit("@", 1)[-1] == sender_domain == original_domain]
    urls = [u.rstrip(".,;)") for u in _URL.findall(body) if urlparse(u).hostname]
    if len(set(candidates)) == 1:
        result = _send_to_address(ctx, app, candidates[0])
        if "à vérifier" in result or "en attente" in result:
            from jobbot.notify import notify

            notify(ctx, "Candidature à poursuivre", f"{app.company_name} : {result}", level="important")
        return result
    if addresses or urls:
        target = (addresses or urls)[0]
        from jobbot.notify import notify

        notify(ctx, "Candidature à poursuivre", f"{app.company_name} demande de postuler ici : {target}", level="important")
        return f"Redirection à vérifier : {target}"
    return ""


def _send_to_address(ctx: AppContext, app: Application, address: str) -> str:
    from jobbot.mail.mailer import build_mailer
    from jobbot.outreach._compose import build_email, documents_match_target
    from jobbot.outreach.selection import usable
    from jobbot.outreach.spontaneous import _prepare
    from jobbot.pacing import Pacer
    from jobbot.preferences import effective_mode

    key = "inbox:redirect:" + stable_id(str(app.id), address)
    if ctx.repo.kv_get(key):
        return "Redirection déjà traitée"
    company = ctx.repo.get_company(app.company_id) if app.company_id else None
    contact = ContactEmail(email=address, source="manual")
    country = ctx.settings.outreach.country
    if (company is None or (country and str(company.raw.get("country", "")).upper() != country.upper())
            or not usable(contact, ctx.settings.outreach) or ctx.repo.is_blacklisted(address)
            or effective_mode(ctx.settings) != "auto"):
        return f"Redirection à vérifier : {address}"
    pacer = Pacer(ctx.repo, ctx.settings.limits)
    if not pacer.remaining("spontaneous"):
        return f"Redirection en attente (quota atteint) : {address}"
    mailer = build_mailer(ctx)
    if mailer is None:
        return f"Redirection en attente (e-mail indisponible) : {address}"
    docs = _prepare(ctx, company)
    if docs.truth_warnings or not documents_match_target(ctx, docs):
        return f"Redirection à vérifier (CV) : {address}"
    email = build_email(ctx, company, contact, docs)
    mailer.send(email)
    if not ctx.dry_run:
        ctx.repo.kv_set(key, clock.iso(clock.now()) or "1")
        pacer.record("spontaneous", True, f"redirection {company.name}")
        ctx.repo.add_event(ApplicationEvent(application_id=app.id, at=clock.now(), kind="note",
                                            detail=f"Candidature renvoyée à {address} à la demande du recruteur"))
    return f"Candidature {'simulée' if ctx.dry_run else 'renvoyée'} à {address}"
