"""Spontaneous e-mail composition: personalised greeting, GDPR art. 14 source line, mandatory opt-out.

The greeting is set at send time from the chosen contact ("Bonjour Julie Martin," for a person's address,
"Madame, Monsieur," for a role address), replacing the documents' generic greeting, so a retry after a bounce
greets the new recipient correctly.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jobbot.context import AppContext
from jobbot.models import Company, ContactEmail, ContractType, TailoredDocuments
from jobbot.outreach._extract import describe_source
from jobbot.outreach._people_parse import display_name, looks_like_name
from jobbot.outreach.selection import is_nominative

OPT_OUT_SENTENCE = (
    "Si vous ne souhaitez plus être contacté(e), répondez simplement « STOP » et je ne vous recontacterai pas."
)
GENERIC_GREETING = "Madame, Monsieur,"
GREETING_SEARCH_LINES = 3
_OPT_OUT_RE = re.compile(
    r"\bstop\b|ne plus (?:être|etre) contact|ne vous recontacterai|non merci|d[ée]sinscri|ne souhaitez plus",
    re.I,
)
_SOURCE_RE = re.compile(r"(?:trouvé|trouve|obtenu|relevé|déduit)e? votre adresse", re.I)
_GREETING_LINE = re.compile(
    r"^(?:(?:bonjour|bonsoir|hello)\s+madame,?\s*monsieur\s*,?|(?:bonjour|bonsoir|hello)(?:\s+[^,\n]{1,40})?\s*,|"
    r"(?:bonjour|bonsoir|hello)\s*!?|madame,?\s*monsieur\s*,?|monsieur,?\s*madame\s*,?|(?:madame|monsieur)\s*,|"
    r"ch[eè]re?\s[^,\n]{1,40},)\s*$",
    re.I,
)
HUNTER_SENTENCE = "J'ai trouvé votre adresse sur l'annuaire d'adresses professionnelles Hunter.io."
_PERSON_ORIGINS = {
    "registry": "le registre public des entreprises",
    "website": "votre site internet",
    "search": "votre profil professionnel public",
    "hunter": "l'annuaire professionnel Hunter.io",
}


# --------------------------------------------------------------------------- opt-out & source


def ensure_opt_out(body: str, source: str | None = None, *, sentence: str | None = None) -> str:
    """Append the address-source line (``sentence``, else "J'ai trouvé votre adresse sur {source}.") and the
    opt-out sentence when the body lacks them."""
    text = (body or "").rstrip()
    additions: list[str] = []
    line = sentence or (f"J'ai trouvé votre adresse sur {source}." if source else None)
    if line and not _SOURCE_RE.search(text):
        additions.append(line)
    if not _OPT_OUT_RE.search(text):
        additions.append(OPT_OUT_SENTENCE)
    if not additions:
        return text
    return f"{text}\n\nPS : {' '.join(additions)}".lstrip()


def source_sentence(company: Company, contact: ContactEmail) -> str | None:
    """Where the address comes from (GDPR art. 14), None for addresses the user entered himself."""
    if contact.source == "manual":
        return None
    origin = (company.raw.get("email_sources") or {}).get(contact.email)
    if contact.source == "pattern":
        person_origin = origin.split(":", 1)[1] if origin and origin.startswith("pattern:") else "registry"
        where = _PERSON_ORIGINS.get(person_origin, _PERSON_ORIGINS["registry"])
        return f"J'ai déduit votre adresse de votre nom, relevé sur {where}, et du format d'adresse de votre domaine."
    if contact.source == "hunter" or origin == "hunter":
        return HUNTER_SENTENCE
    return f"J'ai trouvé votre adresse sur {describe_source(origin)}."


# --------------------------------------------------------------------------- greeting


def greeting_for(contact: ContactEmail) -> str:
    """"Bonjour Prénom Nom," for a person's address with a usable name, else "Madame, Monsieur,"."""
    name = display_name(contact.person_name or "")
    if name and is_nominative(contact) and looks_like_name(name):
        return f"Bonjour {name},"
    return GENERIC_GREETING


def with_greeting(body: str, greeting: str) -> str:
    """Replace the body's greeting line (among its first lines, e.g. after "Objet : …") by ``greeting``, or put
    it on top."""
    lines = (body or "").strip("\n").split("\n")
    filled = [i for i, line in enumerate(lines) if line.strip()]
    if not filled:
        return greeting
    found = next((i for i in filled[:GREETING_SEARCH_LINES] if _GREETING_LINE.match(lines[i].strip())), None)
    if found is not None:
        return "\n".join([*lines[:found], greeting, *lines[found + 1:]])
    return f"{greeting}\n\n" + "\n".join(lines[filled[0]:])


# --------------------------------------------------------------------------- subject & fallback body


def job_title(ctx: AppContext) -> str:
    titles = ctx.profile.target.job_titles
    return titles[0] if titles else "développeur"


def is_alternance(ctx: AppContext) -> bool:
    return ContractType.ALTERNANCE in ctx.profile.target.contract_types and ctx.profile.alternance is not None


def documents_match_target(ctx: AppContext, docs: TailoredDocuments) -> bool:
    """Prevent a draft seeking an apprenticeship from being sent for a CDD/CDI search."""
    if is_alternance(ctx):
        return True
    text = f"{docs.email_subject}\n{docs.email_body}\n{docs.letter_text}"
    return not re.search(r"\b(?:alternance|apprentissage|apprenti(?:e|ssage)?)\b", text, re.I)


def default_subject(ctx: AppContext) -> str:
    name = ctx.profile.identity.full_name
    if is_alternance(ctx):
        months = ctx.profile.alternance.duration_months  # type: ignore[union-attr]
        return f"Candidature spontanée – alternance {job_title(ctx)} ({months} mois) – {name}"
    return f"Candidature spontanée – {job_title(ctx)} – {name}"


def fallback_body(ctx: AppContext, company: Company) -> str:
    identity = ctx.profile.identity
    return (
        f"{GENERIC_GREETING}\n\nJe me permets de vous adresser ma candidature spontanée ({job_title(ctx)}) au sein de "
        f"{company.name}. Vous trouverez mon CV et ma lettre de motivation en pièces jointes.\n\n"
        f"Je reste à votre disposition pour un échange.\n\nBien cordialement,\n{identity.full_name}\n{identity.phone}"
    )


def build_email(ctx: AppContext, company: Company, contact: ContactEmail, docs: TailoredDocuments) -> Any:
    """The ``OutgoingEmail`` for this contact: greeting, body, source line + opt-out, CV and letter attached."""
    from jobbot.mail.mailer import OutgoingEmail

    if not docs.cv_pdf or not Path(docs.cv_pdf).is_file():
        raise FileNotFoundError(f"CV introuvable : {docs.cv_pdf}")
    attachments = [p for p in (docs.cv_pdf, docs.letter_pdf) if p and Path(p).is_file()]
    body = docs.email_body.strip() or docs.letter_text.strip() or fallback_body(ctx, company)
    body = with_greeting(body, greeting_for(contact))
    return OutgoingEmail(
        to=contact.email,
        subject=docs.email_subject.strip() or default_subject(ctx),
        body=ensure_opt_out(body, sentence=source_sentence(company, contact)),
        attachments=attachments,
        kind="spontaneous",
    )
