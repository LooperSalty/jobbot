"""Reply classification: deterministic FR/EN rules first, fast LLM as fallback.

Order (research §5.12, §6.6): bounce → out-of-office → opt-out → rejection → scheduling (interview)
→ acknowledgement/auto → interview → positive → info request → LLM → other.
Rules run on accent-free lower-case text, on the NEW part of the reply only (quotes stripped).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from jobbot.llm import LLMClient, LLMError
from jobbot.mail.messages import InboundMessage, ReplyCategory, ReplyClassification
from jobbot.mail.parsing import strip_quoted
from jobbot.textutil import strip_accents, truncate

log = logging.getLogger(__name__)

MAX_RULE_CHARS = 5000
SUMMARY_CHARS = 220


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(parts), re.IGNORECASE | re.MULTILINE)


OOF_SUBJECT = _rx(
    r"^\s*(?:(?:re|tr|fw|fwd)\s*:\s*)*(?:reponse automatique|message automatique|automatic reply"
    r"|auto(?:matic)?[- ]?reply|autoreply|out of (?:the )?office|absence|absent|en conges?"
    r"|accuse de reception|abwesenheit)",
)
UNSUBSCRIBE = _rx(
    r"desinscri", r"desabonn", r"unsubscribe", r"ne plus (me|nous) (contacter|ecrire|solliciter|envoyer)",
    r"retir(er|ez) (mon|notre|cette) adresse", r"merci de ne plus", r"^\s*stop\b", r"\bnon merci\b",
    r"supprim(er|ez) (mon|notre|nos) (adresse|donnees|coordonnees)",
    r"(do not|don't) (contact|e-?mail) (me|us)", r"remove (me|us|my address|our address) from",
)
REJECTION = _rx(
    r"ne (pas |pouvons pas |pourrons pas |sommes pas en mesure de )?(pouvoir )?donner (une )?suite",
    r"pas (de |une )?suite favorable", r"(ne|n') ?(correspond|correspondent) pas", r"n'y a pas de suite",
    r"(poste|offre) (a ete |est )?(deja )?pourvu",
    r"(avons|avez) (retenu|choisi|selectionne) (une autre|un autre|d'autres)",
    r"n'(a|avons|ont) pas ete retenue?", r"pas (ete )?retenue?s? pour", r"ne (sera|seront) pas retenue?",
    r"malheureusement[^.]{0,200}(candidature|profil|suite|retenu|poste|opportunite)",
    r"pas en mesure de (donner suite|retenir|poursuivre)", r"decide de ne pas (donner suite|poursuivre|retenir)",
    r"regret to inform", r"not (be )?moving forward", r"decided (not )?to (move|proceed) forward with other",
    r"decided to pursue other candidates", r"(will|have decided to) not (be )?(proceed|progress)",
    r"position has (already )?been filled", r"not been selected",
    r"unfortunately[^.]{0,200}(application|position|profile)",
)
INTERVIEW_STRONG = _rx(
    r"(vos|quelles sont vos|vos prochaines) disponibilites", r"\bcreneaux?\b", r"calendly", r"google meet",
    r"meet\.google", r"zoom\.us", r"microsoft teams", r"\bvisio",
    r"(echange|entretien|appel|rendez-vous) telephonique", r"(vous|te) (rencontrer|recevoir|r?appel)",
    r"(convier|convoquer|inviter)[^.]{0,40}entretien",
    r"(fixer|planifier|organiser|caler|programmer|proposer) (un|une|des) "
    r"(entretien|rendez-vous|rdv|echange|call|appel)",
    r"entretien (le|ce|demain|lundi|mardi|mercredi|jeudi|vendredi|\d)",
    r"(schedule|arrange|set up|book) (a|an) (call|interview|meeting)",
    r"(your|share your|let me know your) availability", r"invite you (to|for) (an? )?(interview|call|chat|meeting)",
    r"etes-vous disponible", r"seriez-vous disponible", r"are you available",
)
SCHEDULING = _rx(
    r"calendly", r"google meet", r"meet\.google", r"zoom\.us", r"microsoft teams", r"\bvisio",
    r"(vos|quelles sont vos) disponibilites", r"\bcreneaux?\b", r"invite you (to|for)", r"are you available",
    r"etes-vous disponible", r"seriez-vous disponible", r"(schedule|book) (a|an) (call|interview|meeting)",
)
ACKNOWLEDGEMENT = _rx(
    r"(nous avons|avons|on a) bien (recu|pris en compte|enregistre) (votre|ta) candidature",
    r"votre candidature a bien ete (recue|enregistree|transmise|prise en compte)", r"accuse de reception",
    r"we (have )?received your application", r"thank you for (your application|applying)",
    r"we will review your application", r"ceci est un (message|e-?mail) automatique", r"merci de ne pas repondre",
)
INTERVIEW_WEAK = _rx(r"\bentretien\b", r"\binterview\b", r"\brdv\b", r"rendez-vous")
POSITIVE = _rx(
    r"(profil|candidature) (a|ont) retenu (toute )?notre attention", r"votre profil (nous )?interesse",
    r"(souhaitons|souhaiterions|aimerions|voudrions) (donner suite|poursuivre|aller plus loin|avancer)",
    r"bonne nouvelle", r"felicitations", r"nous sommes (ravis|heureux)",
    r"(we would|we'd) like to (move forward|proceed)", r"pleased to", r"good news", r"interested in your profile",
    r"(je|nous) (suis|sommes) interesse",
)
INFO_REQUEST = _rx(
    r"pourriez-vous (nous |me )?(envoyer|transmettre|preciser|indiquer|communiquer|faire parvenir)",
    r"pouvez-vous (nous |me )?(envoyer|transmettre|preciser|indiquer|communiquer)",
    r"(quel|quelle|quels|quelles) (est|sont) (votre|vos)",
    r"merci de (nous |me )?(transmettre|envoyer|faire parvenir|preciser)",
    r"rythme d'alternance", r"date de debut", r"could you (please )?(send|provide|share|tell)",
    r"can you (please )?(send|provide|share)",
)
OOF_BODY = _rx(
    r"je suis (actuellement )?(absente?|en conges|en deplacement)", r"je serai (absente?|de retour)",
    r"(de retour|back) (le|on)\b", r"out of (the )?office", r"i am (currently )?(out|away)",
    r"je n'aurai pas acces a mes (e-?mails|mails|messages)",
)

_MONTHS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12, "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6, "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_RETURN_DATE = re.compile(
    r"(?:jusqu'?au|de retour le|retour (?:prevu )?le|back on|until|a partir du|des le)\s+"
    r"(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|monday|tuesday|wednesday|thursday|friday)\s+)?"
    r"(\d{1,2})(?:er)?(?:\s+|/|\.)(" + "|".join(_MONTHS) + r"|\d{1,2})(?:(?:\s+|/|\.)(\d{4}|\d{2}))?",
    re.IGNORECASE,
)


class _LLMVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: ReplyCategory
    summary: str = ""
    confidence: float = 0.6


def fold(text: str) -> str:
    """Accent-free lower-case text with straight apostrophes (what the rules expect)."""
    return strip_accents(text or "").lower().replace("’", "'").replace("‘", "'")


def summarize(text: str) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    return truncate(flat, SUMMARY_CHARS) if flat else "(message vide)"


def is_rejection(text: str) -> bool:
    """Content rule shared with platform notifications (their footers carry 'unsubscribe' links)."""
    return bool(REJECTION.search(fold(text)[:MAX_RULE_CHARS]))


def _verdict(category: ReplyCategory, text: str, confidence: float) -> ReplyClassification:
    return ReplyClassification(category=category, summary=summarize(text), confidence=confidence)


def _header_rules(msg: InboundMessage, new_text: str) -> ReplyClassification | None:
    if msg.is_bounce:
        who = msg.bounced_recipient or "destinataire inconnu"
        return ReplyClassification(category="bounce", summary=f"Adresse invalide : {who}", confidence=0.99)
    if OOF_SUBJECT.search(fold(msg.subject)):
        return _verdict("auto_reply", new_text, 0.9)
    return None


def classify_by_rules(msg: InboundMessage) -> ReplyClassification | None:
    new_text = strip_quoted(msg.body_text)
    verdict = _header_rules(msg, new_text)
    if verdict is not None:
        return verdict
    folded = fold(f"{msg.subject}\n{new_text}")[:MAX_RULE_CHARS]
    # machines cannot opt out on someone's behalf (ATS footers carry "se désinscrire" links)
    if not msg.auto_submitted and UNSUBSCRIBE.search(folded):
        return _verdict("unsubscribe", new_text, 0.9)
    if REJECTION.search(folded):
        return _verdict("rejection", new_text, 0.85)
    if ACKNOWLEDGEMENT.search(folded) and not SCHEDULING.search(folded):
        return _verdict("auto_reply", new_text, 0.8)
    if INTERVIEW_STRONG.search(folded):
        return _verdict("interview", new_text, 0.85)
    if msg.auto_submitted:
        return _verdict("auto_reply", new_text, 0.75)
    for category, pattern, confidence in (
        ("interview", INTERVIEW_WEAK, 0.7), ("positive", POSITIVE, 0.7), ("info_request", INFO_REQUEST, 0.7),
        ("auto_reply", OOF_BODY, 0.75),
    ):
        if pattern.search(folded):
            return _verdict(category, new_text, confidence)
    return None


def _llm_prompt(msg: InboundMessage, new_text: str) -> str:
    return (
        "Tu tries les réponses reçues par un candidat à ses candidatures (emploi / alternance).\n"
        "Catégories : interview (proposition d'entretien ou d'appel), rejection (refus), positive (intérêt, "
        "suite favorable sans date), info_request (demande d'information ou de document), auto_reply (message "
        "automatique, accusé de réception, absence), unsubscribe (ne plus être contacté), bounce (non distribué), "
        "other (autre).\n"
        "Donne la catégorie, un résumé en français d'une phrase et ta confiance entre 0 et 1.\n\n"
        f"Expéditeur : {msg.from_name} <{msg.from_addr}>\nObjet : {msg.subject}\n\n"
        f"Message :\n{truncate(new_text, 3000)}"
    )


def classify_with_llm(msg: InboundMessage, llm: LLMClient | None) -> ReplyClassification:
    new_text = strip_quoted(msg.body_text)
    if llm is None:
        return _verdict("other", new_text, 0.3)
    try:
        verdict = llm.complete_json(_llm_prompt(msg, new_text), _LLMVerdict, fast=True)
    except (LLMError, ValueError) as exc:
        log.info("Classification LLM indisponible (%s) : catégorie 'other'", exc)
        return _verdict("other", new_text, 0.3)
    confidence = min(1.0, max(0.0, float(verdict.confidence)))
    summary = summarize(verdict.summary) if verdict.summary.strip() else summarize(new_text)
    return ReplyClassification(category=verdict.category, summary=summary, confidence=confidence)


def classify_reply(msg: InboundMessage, llm: LLMClient | None) -> ReplyClassification:
    """Rules first (bounce, auto-reply, opt-out, rejection, interview...), then the fast LLM."""
    return classify_by_rules(msg) or classify_with_llm(msg, llm)


def parse_return_date(text: str, reference: datetime) -> date | None:
    """Return date announced by an out-of-office ('absent jusqu'au 5 octobre', 'back on 12/10')."""
    match = _RETURN_DATE.search(fold(text))
    if not match:
        return None
    day_s, month_s, year_s = match.groups()
    month = int(month_s) if month_s.isdigit() else _MONTHS.get(month_s, 0)
    ref = reference.date()
    year = ref.year
    if year_s:
        year = int(year_s) + (2000 if len(year_s) == 2 else 0)
    try:
        found = date(year, month, int(day_s))
    except ValueError:
        return None
    if not year_s and found < ref:
        try:
            found = found.replace(year=ref.year + 1)
        except ValueError:
            return None
    return found
