"""Status e-mails sent by job platforms (La Bonne Alternance, HelloWork, LinkedIn, Indeed, WTTJ).

LBA subjects are documented (docs/research/la-bonne-alternance.md §6.6). HelloWork/LinkedIn subjects are not:
they are recognised by keywords and matched to an application by company name.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from jobbot.mail.classify import INTERVIEW_WEAK, classify_text, fold, is_rejection
from jobbot.mail.messages import InboundMessage
from jobbot.mail.parsing import strip_quoted
from jobbot.models import ApplicationStatus, ApplyMethod

PLATFORM_DOMAINS: dict[str, tuple[str, ...]] = {
    "lba": ("labonnealternance", "apprentissage.beta.gouv.fr"),
    "hellowork": ("hellowork.com", "hellowork.io", "regionsjob.com"),
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com", "indeed.fr", "indeedemail.com"),
    "wttj": ("welcometothejungle.com", "wttj.co"),
}
PLATFORM_METHODS: dict[str, tuple[ApplyMethod, ...]] = {
    "lba": (ApplyMethod.LBA,),
    "hellowork": (ApplyMethod.HELLOWORK,),
    "linkedin": (ApplyMethod.LINKEDIN_EASY_APPLY,),
    "indeed": (ApplyMethod.EXTERNAL, ApplyMethod.MANUAL),
    "wttj": (ApplyMethod.EXTERNAL, ApplyMethod.MANUAL),
}
_I = re.IGNORECASE
_DASH = r"\s*[-–—:]\s*"
LBA_REPLY = "reply"  # « Suite donnée »: any answer of the recruiter, its status comes from the recruiter's words
_LBA_RULES: tuple[tuple[re.Pattern[str], ApplicationStatus | None, str, str], ...] = (
    (re.compile(r"^\s*suite donn[ée]e [àa] votre candidature" + _DASH + r"(?P<company>.+)$", _I),
     ApplicationStatus.RESPONSE, LBA_REPLY, "Le recruteur a répondu via La Bonne Alternance"),
    (re.compile(r"^\s*r[ée]ponse n[ée]gative de (?P<company>.+?) [àa] la candidature", _I),
     ApplicationStatus.REJECTED, "rejected", "Réponse négative via La Bonne Alternance"),
    (re.compile(r"^\s*votre candidature n'a pas pu [êe]tre envoy[ée]e [àa] (?P<company>.+)$", _I),
     ApplicationStatus.BOUNCED, "bounce", "La Bonne Alternance n'a pas pu transmettre la candidature"),
    (re.compile(r"^\s*[ée]chec d'envoi de votre candidature", _I),
     None, "note", "Échec d'envoi La Bonne Alternance : candidature à renvoyer manuellement"),
    (re.compile(r"^\s*votre candidature chez (?P<company>.+)$", _I),
     None, "note", "Candidature confirmée par La Bonne Alternance"),
)
_APPLICATION_WORDS = re.compile(r"candidature|application|postul|applied", _I)
_VIEWED = re.compile(r"consult[ée]e|a [ée]t[ée] vue|vue par|viewed|a vu votre|ouvert votre", _I)
_SENT = re.compile(r"envoy[ée]e|was sent|bien [ée]t[ée] (transmise|re[çc]ue|envoy[ée]e)|has been sent|submitted", _I)
_ACCEPTED = re.compile(r"\baccept[ée]e\b|a retenu votre candidature|shortlist", _I)
_SUBJECT_COMPANY = re.compile(
    r"(?:\bchez|\bpar|\bà|\bde la part de|\bat|\bto|\bby|\bfrom)\s+(?P<company>[^|–—:!?]{2,80}?)\s*[.!]?\s*$", _I
)


class PlatformNotice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    company: str | None = None  # extracted from the subject when possible
    status: ApplicationStatus | None = None  # None → event only
    event_kind: str = "note"
    detail: str = ""
    methods: tuple[ApplyMethod, ...] = ()


def platform_of(address: str) -> str | None:
    domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
    for platform, needles in PLATFORM_DOMAINS.items():
        if any(domain == n or domain.endswith("." + n) or n in domain for n in needles):
            return platform
    return None


def _clean_subject(subject: str) -> str:
    return re.sub(r"^\s*((re|tr|fw|fwd)\s*:\s*)+", "", subject.replace("’", "'"), flags=_I).strip()


# The recruiter's own words inside LBA's template (« … vous répond : <message> À vous maintenant ! ✅ … »); the
# template's footer (« Préparez-vous pour un éventuel entretien ») is LBA's, in every reply.
_LBA_RECRUITER_TEXT = re.compile(r"vous r[ée]pond\s*:(?P<text>.*?)(?:[àa] vous maintenant|$)", _I | re.DOTALL)
_LBA_VERDICTS: dict[str, tuple[ApplicationStatus, str, str]] = {
    "interview": (ApplicationStatus.INTERVIEW, "interview", "Le recruteur propose un entretien (La Bonne Alternance)"),
    "rejection": (ApplicationStatus.REJECTED, "rejected", "Refus du recruteur via La Bonne Alternance"),
    "unsubscribe": (ApplicationStatus.REJECTED, "rejected", "Refus du recruteur via La Bonne Alternance"),
    "info_request": (ApplicationStatus.RESPONSE, LBA_REPLY,
                     "Le recruteur demande des informations (La Bonne Alternance)"),
    "positive": (ApplicationStatus.RESPONSE, LBA_REPLY, "Le recruteur s'intéresse à ta candidature (La Bonne Alternance)"),
    "auto_reply": (ApplicationStatus.RESPONSE, LBA_REPLY,
                   "Le recruteur a accusé réception de ta candidature (La Bonne Alternance)"),
}


def lba_recruiter_text(body: str) -> str:
    """The recruiter's message relayed by LBA ('' when the template is not recognised)."""
    match = _LBA_RECRUITER_TEXT.search(body or "")
    return match.group("text").strip() if match else ""


def _lba_reply(notice: PlatformNotice, body: str) -> PlatformNotice:
    """« Suite donnée »: interview, refusal or plain response according to what the recruiter wrote."""
    text = lba_recruiter_text(body)
    verdict = _LBA_VERDICTS.get(classify_text(text) or "") if text else None
    if verdict is None:
        return notice
    status, kind, detail = verdict
    return notice.model_copy(update={"status": status, "event_kind": kind, "detail": detail})


def _lba_notice(subject: str, body: str) -> PlatformNotice | None:
    for pattern, status, kind, detail in _LBA_RULES:
        match = pattern.search(subject)
        if match:
            company = match.groupdict().get("company")
            notice = PlatformNotice(
                platform="lba", company=company.strip(" .") if company else None, status=status,
                event_kind=kind, detail=detail, methods=PLATFORM_METHODS["lba"],
            )
            return _lba_reply(notice, body) if kind == LBA_REPLY else notice
    return None


def _generic_status(subject: str, body: str) -> tuple[ApplicationStatus | None, str, str] | None:
    if is_rejection(f"{subject}\n{body}"):
        return ApplicationStatus.REJECTED, "rejected", "Refus notifié par la plateforme"
    if INTERVIEW_WEAK.search(fold(subject)):
        return ApplicationStatus.INTERVIEW, "interview", "Entretien notifié par la plateforme"
    if _ACCEPTED.search(subject):
        return ApplicationStatus.RESPONSE, "reply", "Candidature retenue par le recruteur (plateforme)"
    if _VIEWED.search(subject):
        return None, "note", "Candidature consultée par le recruteur"
    if _SENT.search(subject):
        return None, "note", "Envoi de la candidature confirmé par la plateforme"
    return None


def detect_platform_notice(msg: InboundMessage) -> PlatformNotice | None:
    """Status notification from a job platform, or None (alerts/marketing are left to other handlers)."""
    platform = platform_of(msg.from_addr)
    if platform is None and "bonne alternance" not in fold(msg.body_text[:3000]):
        return None
    subject = _clean_subject(msg.subject)
    if platform in (None, "lba"):
        return _lba_notice(subject, msg.body_text)
    body = strip_quoted(msg.body_text)[:3000]
    if not _APPLICATION_WORDS.search(subject):
        return None
    verdict = _generic_status(subject, body)
    if verdict is None:
        return None
    status, kind, detail = verdict
    match = _SUBJECT_COMPANY.search(subject)
    return PlatformNotice(
        platform=platform, company=match.group("company").strip() if match else None, status=status,
        event_kind=kind, detail=f"{detail} ({platform})", methods=PLATFORM_METHODS.get(platform, ()),
    )
