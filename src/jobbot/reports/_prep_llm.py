"""Optional LLM layer of the interview prep sheet: one draft (quality model), checked against the profile.

A STAR point survives only when it names a real experience/project of the profile and claims no skill, number
or degree the profile does not back (``documents._truth``). Answers may name the offer's skills (to admit
a gap honestly) but nothing else. Whatever is rejected or missing falls back to the template.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from jobbot.config import Experience, Profile, Project
from jobbot.llm import LLMClient, LLMError
from jobbot.reports._prep_facts import NO_RESULT, QA, STAR_LIMIT, PrepFacts, StarPoint, fold, item_label, item_skills
from jobbot.textutil import truncate

if TYPE_CHECKING:
    from jobbot.documents._truth import TruthBase

log = logging.getLogger(__name__)

SYSTEM = ("Tu es un coach d'entretien d'embauche francophone, précis et honnête : tu n'inventes jamais rien sur le "
          "candidat et tu t'appuies uniquement sur son profil.")
CATEGORIES = ("technique", "alternance", "comportement", "motivation")
MIN_QUESTIONS = 4
MAX_QUESTIONS = 14
MIN_TO_ASK = 3
MAX_ITEMS = 8
PROFILE_CHARS = 5000
JOB_CHARS = 3500
MAIL_CHARS = 1500


class _StarDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    experience: str = ""
    situation: str = ""
    task: str = ""
    action: str = ""
    result: str = ""


class _QADraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: str = "comportement"
    question: str = ""
    answer: str = ""


class PrepDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company_summary: str = ""
    role_needs: list[str] = Field(default_factory=list)
    star: list[_StarDraft] = Field(default_factory=list)
    questions: list[_QADraft] = Field(default_factory=list)
    questions_to_ask: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class PrepContent:
    company_summary: str
    role_needs: tuple[str, ...]
    stars: tuple[StarPoint, ...]
    questions: tuple[QA, ...]
    to_ask: tuple[str, ...]
    llm_used: bool = False


# --------------------------------------------------------------------------- prompt


def _profile_block(profile: Profile) -> str:
    from jobbot.documents._truth import truth_text

    labels = [f"- {item_label(item)}" for item in (*profile.experiences, *profile.projects)]
    return f"{truncate(truth_text(profile), PROFILE_CHARS)}\n\nIntitulés exacts utilisables :\n" + "\n".join(labels)


def _offer_block(facts: PrepFacts) -> str:
    job, company = facts.job, facts.company
    lines = [f"Entreprise : {facts.company_name}", f"Site : {facts.website or 'inconnu'}"]
    if company is not None:
        lines += [f"Secteur : {company.naf_label or company.naf_code or 'inconnu'}",
                  f"Description : {company.description or 'inconnue'}"]
    if job is not None:
        lines += [f"Poste : {job.title} ({job.contract_type.value}) — {job.location}",
                  f"Annonce :\n{truncate(job.description, JOB_CHARS)}"]
    else:
        lines.append("Candidature spontanée (pas d'annonce).")
    return "\n".join(lines)


def build_prompt(profile: Profile, facts: PrepFacts) -> str:
    missing = ", ".join(facts.missing) or "aucune"
    alternance = " Inclure des questions spécifiques à l'alternance (rythme, école, tuteur)." if facts.alternance else ""
    return (
        f"Prépare la fiche d'entretien de {profile.identity.first_name} pour {facts.company_name}"
        f" ({facts.job_title or 'candidature spontanée'}).\n"
        "Règles STRICTES :\n"
        "- Pour parler du candidat, n'utilise QUE les faits du profil ci-dessous : aucune compétence, entreprise, "
        "chiffre, diplôme ou résultat absent du profil.\n"
        f"- Compétences demandées absentes du profil ({missing}) : ne jamais prétendre les maîtriser, proposer une "
        "réponse honnête.\n"
        "- Français, phrases courtes ; réponses à la première personne, 1 à 3 phrases.\n"
        "- star : les 2 ou 3 expériences du profil les plus utiles pour ce poste ; « experience » = un intitulé "
        "exact de la liste.\n"
        f"- questions : 8 à 12 questions probables (catégories : {', '.join(CATEGORIES)}) avec une piste de réponse."
        f"{alternance}\n"
        "- questions_to_ask : 4 à 6 questions pertinentes à poser au recruteur.\n"
        "- company_summary : 2 à 4 phrases sur l'entreprise, uniquement d'après les informations fournies "
        "(chaîne vide si rien).\n"
        "- role_needs : 3 à 6 points sur ce que le poste demande vraiment.\n\n"
        f"{_offer_block(facts)}\n\nMessage du recruteur :\n{truncate(facts.email_text, MAIL_CHARS) or '(aucun)'}\n\n"
        f"Profil du candidat (seule source de vérité) :\n{_profile_block(profile)}"
    )


def ask_llm(llm: LLMClient | None, profile: Profile, facts: PrepFacts) -> PrepDraft | None:
    """Draft from the quality model, or None (no LLM, LLM error, invalid JSON)."""
    if llm is None:
        return None
    try:
        return llm.complete_json(build_prompt(profile, facts), PrepDraft, system=SYSTEM)
    except (LLMError, ValueError) as exc:
        log.info("Fiche d'entretien rédigée sans LLM (%s)", exc)
        return None


# --------------------------------------------------------------------------- validation


def _match_item(label: str, profile: Profile) -> Experience | Project | None:
    from jobbot.documents._truth import similar

    wanted = fold(label)
    for exp in profile.experiences:
        company = fold(exp.company)
        if (len(company) >= 3 and company in wanted) or similar(label, exp.title) or \
                similar(label, item_label(exp)):
            return exp
    for project in profile.projects:
        if fold(project.name) in wanted or similar(label, project.name):
            return project
    return None


def _truthful(text: str, truth: TruthBase, *, allowed_skills: frozenset[str] = frozenset(),
              allowed_numbers: frozenset[str] = frozenset()) -> bool:
    from jobbot.documents._truth import unsupported_claims

    claims = unsupported_claims(text, truth, allowed_skills=allowed_skills, allowed_numbers=allowed_numbers)
    if claims:
        log.info("Passage écarté de la fiche d'entretien (absent du profil) : %s", ", ".join(claims))
    return not claims


def accept_stars(draft: PrepDraft, profile: Profile, facts: PrepFacts, truth: TruthBase) -> list[StarPoint]:
    wanted = set(facts.job_skills)
    out: list[StarPoint] = []
    for item in draft.star[: STAR_LIMIT + 2]:
        source = _match_item(item.experience, profile)
        text = " ".join((item.situation, item.task, item.action, item.result))
        if source is None or not item.situation.strip() or not item.action.strip() or not _truthful(text, truth):
            continue
        out.append(StarPoint(
            source=item_label(source), situation=truncate(item.situation.strip(), 400),
            task=truncate(item.task.strip(), 400), action=truncate(item.action.strip(), 500),
            result=truncate(item.result.strip(), 400) or NO_RESULT, skills=tuple(sorted(item_skills(source) & wanted)),
        ))
    return out[:STAR_LIMIT]


def accept_questions(draft: PrepDraft, facts: PrepFacts, truth: TruthBase) -> list[QA]:
    from jobbot.documents._truth import number_claims

    job_text = f"{facts.job.title}\n{facts.job.description}" if facts.job else ""
    numbers = frozenset(number_claims(job_text))
    out: list[QA] = []
    for qa in draft.questions[:MAX_QUESTIONS]:
        question, answer = qa.question.strip(), qa.answer.strip()
        if not question or not answer:
            continue
        if not _truthful(answer, truth, allowed_skills=frozenset(facts.job_skills), allowed_numbers=numbers):
            continue
        category = fold(qa.category).strip()
        out.append(QA(category if category in CATEGORIES else "comportement", truncate(question, 220),
                      truncate(answer, 700)))
    return out


def _texts(items: list[str], limit: int = MAX_ITEMS) -> tuple[str, ...]:
    return tuple(truncate(t.strip(), 220) for t in items if t and t.strip())[:limit]


def merge(base: PrepContent, draft: PrepDraft, profile: Profile, facts: PrepFacts) -> PrepContent:
    """Template content with every section the LLM got right (and truthful) replaced by the LLM version."""
    from jobbot.documents._truth import build_truth

    truth = build_truth(profile)
    llm_stars = accept_stars(draft, profile, facts, truth)
    kept = {s.source for s in llm_stars}
    stars = llm_stars + [s for s in base.stars if s.source not in kept][: max(0, STAR_LIMIT - len(llm_stars))]
    questions = accept_questions(draft, facts, truth)
    needs, to_ask = _texts(draft.role_needs), _texts(draft.questions_to_ask)
    summary = draft.company_summary.strip()
    ok = {"summary": len(summary) >= 20, "needs": len(needs) >= 2, "stars": bool(llm_stars),
          "questions": len(questions) >= MIN_QUESTIONS, "to_ask": len(to_ask) >= MIN_TO_ASK}
    return PrepContent(
        company_summary=truncate(summary, 900) if ok["summary"] else base.company_summary,
        role_needs=needs if ok["needs"] else base.role_needs,
        stars=tuple(stars) if ok["stars"] else base.stars,
        questions=tuple(questions) if ok["questions"] else base.questions,
        to_ask=to_ask if ok["to_ask"] else base.to_ask,
        llm_used=any(ok.values()),
    )
