"""Cover letter (lettre de motivation) + short application e-mail, in French.

The LLM writes the four body paragraphs (vous / moi / nous, 250-350 words) and a two-sentence e-mail pitch;
greeting, closing, subject, e-mail frame, apprenticeship-aid sentence and opt-out line are fixed by code. The
LLM text is checked (length, placeholders, clichés, first-person claims not backed by the profile) and fixed once
through feedback; otherwise the deterministic template is used.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobbot.config import Profile
from jobbot.documents._cv_llm import keywords_line, profile_block, target_block
from jobbot.documents._cv_rules import keyword_is_truthful, title_in_sentence
from jobbot.documents._format import clean_spaces
from jobbot.documents._letter_rules import (
    CLOSING,
    GREETING,
    MAX_EMAIL_WORDS,
    LetterInput,
    aid_sentence,
    build_email_body,
    build_subject,
    email_subject,
    make_letter_input,
    rule_paragraphs,
    rule_pitch,
)
from jobbot.documents._truth import number_claims, unsupported_degrees, unsupported_numbers, unsupported_skills
from jobbot.documents.models import CoverLetter
from jobbot.llm import LLMClient, LLMError
from jobbot.matching import lexicon
from jobbot.matching._doccheck import CLICHES, PLACEHOLDER_RX, count_words
from jobbot.matching._text import norm
from jobbot.models import Company, JobOffer, KeywordHit

log = logging.getLogger(__name__)

MIN_WORDS, MAX_WORDS = 240, 370  # accepted body length (target 250-350)
SALVAGE_MIN_WORDS = 200  # after removing unbacked sentences from the last LLM draft
LETTER_ATTEMPTS = 2
MAX_PITCH_WORDS = 70
LEGAL_NUMBERS = frozenset({"1", "2027", "2000", "2000€", "250", "750", "750€", "3", "24"})
_FIRST_PERSON = re.compile(r"\b(?:je|j'|mon|ma|mes|m'|me|moi)\b", re.I)
_LEARNING = re.compile(  # "je souhaite découvrir Scrum" names an offer skill without claiming it
    r"\b(?:decouvr\w*|apprendre|progresser|me former|m'initier|monter en competences?|me familiariser)\b"
)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_REPEATS_APPLICATION = re.compile(r"\bcandidat|\bpostule")

SYSTEM = (
    "Tu es un expert du recrutement tech en France. Tu rédiges des lettres de motivation sobres, précises et "
    "personnalisées, en français impeccable, sans jamais inventer de fait sur le candidat ni sur l'entreprise."
)


class _LetterDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paragraphs: list[str] = Field(min_length=1)
    email_pitch: str = ""


def _intro_rule(inp: LetterInput) -> str:
    alt = inp.profile.alternance
    if inp.alternance and alt is not None:
        return (f"1. Alternance (2-3 phrases) : le candidat prépare le titre « {alt.degree} » à {alt.school}, "
                f"en alternance sur {alt.duration_months} mois au rythme {alt.rhythm}, début {alt.start_date} ; "
                f"il souhaite effectuer cette alternance au sein de {inp.company_name or 'l’entreprise'} "
                f"({title_in_sentence(inp.role)}) ; il est disponible immédiatement.")
    return ("1. Candidature (2 phrases) : dernier diplôme, poste visé, disponibilité immédiate, mobilité "
            "(villes du profil).")


def _prompt(inp: LetterInput, feedback: list[str]) -> str:
    title = inp.job.title if inp.job is not None else inp.role
    parts = [
        target_block(inp.job, inp.company, title),
        "## Profil du candidat (seuls faits utilisables)\n" + profile_block(inp.profile),
        "## Consignes\nRédige le corps de la lettre de motivation en 4 paragraphes (vous / moi / nous), "
        "270 à 330 mots au total (350 au maximum) :\n" + _intro_rule(inp) + "\n"
        "2. « Vous » : ce qui attire le candidat dans l'offre ou l'entreprise, avec un fait précis tiré du texte "
        "ci-dessus (produit, clients, secteur, projet) ; n'invente aucun fait sur l'entreprise.\n"
        f"3. « Moi » : diplôme, 1 ou 2 réalisations concrètes du profil, les compétences {', '.join(inp.skills)}.\n"
        "4. « Nous » : ce que le candidat apportera et viendra apprendre, puis une phrase proposant un entretien.\n"
        "Mots-clés de l'offre que le candidat possède (à citer quand c'est naturel, sans en faire une liste) : "
        + keywords_line([k for k in inp.keywords if k.weight >= 0.8 and keyword_is_truthful(k, inp.truth)]) + "\n"
        "Règles : ni formule d'appel ni formule de politesse finale (ajoutées automatiquement) ; phrases courtes, "
        "pas de conditionnel ; aucun cliché (" + ", ".join(CLICHES) + ") ; aucun champ à compléter ; n'invente "
        "aucune compétence, expérience, chiffre ou diplôme (intitulés exacts du profil : jamais « Master » s'il n'y "
        "figure pas) ; ne parle pas des aides à l'embauche ; ne cite pas l'effectif de l'entreprise ; reste "
        "mesuré (jamais « je maîtrise l'ensemble de… », « expert », « parfaitement »).\n"
        "Compétences que le candidat NE possède PAS (ne jamais les revendiquer) : "
        + keywords_line([k for k in inp.keywords if k.category != "title"
                         and lexicon.canonicalize(k.keyword) in lexicon.SKILLS
                         and lexicon.canonicalize(k.keyword) not in inp.truth.skills]) + "\n"
        "\"email_pitch\" : 2 phrases (50 mots au plus) pour le corps de l'e-mail, placées juste après « Je vous "
        "adresse ma candidature au poste de … au sein de … » : ne répète ni le poste ni l'entreprise ; "
        "formation / alternance puis une réalisation concrète ; sans salutation ni signature.",
    ]
    if feedback:
        parts.append("## Corrige ta version précédente\n- " + "\n- ".join(feedback))
    parts.append("Réponds avec {\"paragraphs\": [...4 paragraphes...], \"email_pitch\": \"...\"}.")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- checks


def _context_text(inp: LetterInput) -> str:
    company = inp.company
    extra = [company.name, company.naf_label or "", company.description or "", company.headcount_range or ""] \
        if company else []
    job = [inp.job.title, inp.job.company, inp.job.description] if inp.job is not None else []
    return "\n".join([*job, *extra])


def claim_issues(text: str, inp: LetterInput) -> list[str]:
    """Claims not backed by the profile. Offer skills may be named when talking about the company ("votre équipe
    travaille en Scrum") or about learning them ("je souhaite découvrir Scrum"), never as the candidate's own."""
    context = _context_text(inp)
    offer_skills = frozenset(lexicon.find_skills(context))
    numbers = LEGAL_NUMBERS | frozenset(number_claims(context))
    issues: list[str] = []
    for sentence in _SENTENCES.split(clean_spaces(text)):
        claims_own = bool(_FIRST_PERSON.search(sentence)) and not _LEARNING.search(norm(sentence))
        skills = unsupported_skills(sentence, inp.truth, allowed=frozenset() if claims_own else offer_skills)
        issues += [f"compétence non justifiée par le profil : {s}" for s in skills]
        issues += [f"chiffre non justifié : {n}" for n in unsupported_numbers(sentence, inp.truth, allowed=numbers)]
        if claims_own:
            issues += [f"diplôme non justifié : {d} (reprends l'intitulé exact du profil)"
                       for d in unsupported_degrees(sentence, inp.truth)]
    return issues


def letter_issues(paragraphs: list[str], inp: LetterInput, *, min_words: int = MIN_WORDS) -> list[str]:
    issues: list[str] = []
    if not 3 <= len(paragraphs) <= 5:
        issues.append(f"{len(paragraphs)} paragraphe(s) : il en faut 4")
    words = sum(count_words(p) for p in paragraphs)
    if not min_words <= words <= MAX_WORDS:
        issues.append(f"la lettre fait {words} mots : vise 280 à 320 mots")
    text = "\n".join(paragraphs)
    if PLACEHOLDER_RX.search(text):
        issues.append("champ à compléter oublié ({...}, [...], XXX)")
    issues += [f"cliché à supprimer : « {c} »" for c in CLICHES if c in norm(text)]
    issues += claim_issues(text, inp)
    return list(dict.fromkeys(issues))


# --------------------------------------------------------------------------- assembly


def _without_unbacked_sentences(paragraphs: list[str], inp: LetterInput) -> list[str]:
    kept = [" ".join(s for s in _SENTENCES.split(p) if not claim_issues(s, inp)) for p in paragraphs]
    return [p for p in kept if p.strip()]


def _feedback(issues: list[str]) -> list[str]:
    if any("compétence" in i or "chiffre" in i for i in issues):
        issues = [*issues, "ne présente jamais ces éléments comme acquis par le candidat : parle-en seulement au "
                           "sujet de l'entreprise (« votre équipe travaille en … ») ou comme un apprentissage visé"]
    return issues


def _llm_body(inp: LetterInput, llm: LLMClient) -> tuple[list[str] | None, str]:
    feedback: list[str] = []
    paragraphs: list[str] = []
    pitch = ""
    for attempt in range(LETTER_ATTEMPTS):
        try:
            draft = llm.complete_json(_prompt(inp, _feedback(feedback)), _LetterDraft, system=SYSTEM)
        except (LLMError, ValidationError, ValueError) as exc:
            log.warning("Lettre de motivation par LLM impossible : %s", exc)
            break
        paragraphs = [clean_spaces(p.replace("**", "")) for p in draft.paragraphs if clean_spaces(p)]
        pitch = clean_spaces(draft.email_pitch)
        feedback = letter_issues(paragraphs, inp)
        if not feedback:
            return paragraphs, pitch
        log.info("Lettre LLM à corriger (essai %d) : %s", attempt + 1, "; ".join(feedback))
    salvaged = _without_unbacked_sentences(paragraphs, inp)
    if salvaged and not letter_issues(salvaged, inp, min_words=SALVAGE_MIN_WORDS):
        log.info("Lettre LLM conservée sans ses phrases non justifiées par le profil")
        return salvaged, pitch
    return None, ""


def _pitch_ok(pitch: str, inp: LetterInput) -> bool:
    return bool(pitch) and count_words(pitch) <= MAX_PITCH_WORDS and not claim_issues(pitch, inp) \
        and not PLACEHOLDER_RX.search(pitch)


def _with_aid(paragraphs: list[str], inp: LetterInput) -> list[str]:
    aid = aid_sentence(inp, with_amount=True)
    if not aid or not paragraphs or aid in paragraphs[-1]:
        return paragraphs
    sentences = _SENTENCES.split(paragraphs[-1])
    last = " ".join([*sentences[:-1], aid, sentences[-1]]) if len(sentences) > 1 else f"{paragraphs[-1]} {aid}"
    return [*paragraphs[:-1], last]


def _without_repeated_application(pitch: str) -> str:
    """Drop pitch sentences that restate the application ("je candidate au poste de…"): the e-mail opening
    already says it."""
    return " ".join(s for s in _SENTENCES.split(pitch) if not _REPEATS_APPLICATION.search(norm(s)))


def _email(inp: LetterInput, pitch: str) -> str:
    pitch = _without_repeated_application(pitch)
    body = build_email_body(inp, pitch if _pitch_ok(pitch, inp) else rule_pitch(inp))
    if count_words(body) > MAX_EMAIL_WORDS:
        body = build_email_body(inp, rule_pitch(inp))
    return body


def write_cover_letter(
    profile: Profile,
    llm: LLMClient,
    *,
    job: JobOffer | None = None,
    company: Company | None = None,
    keywords: list[KeywordHit] | None = None,
) -> CoverLetter:
    """French cover letter for ``job`` (or a spontaneous one for ``company`` when ``job`` is None)."""
    inp = make_letter_input(profile, job, company, keywords)
    paragraphs, pitch = _llm_body(inp, llm) if llm is not None else (None, "")
    paragraphs = rule_paragraphs(inp) if paragraphs is None else _with_aid(paragraphs, inp)
    subject = build_subject(inp)
    return CoverLetter(
        subject=subject,
        greeting=GREETING,
        paragraphs=paragraphs,
        closing=CLOSING,
        email_subject=email_subject(inp, subject),
        email_body=_email(inp, pitch),
        signature=profile.identity.full_name,
    )
