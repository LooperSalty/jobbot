"""Deterministic material of an interview prep sheet: the context facts and truthful STAR talking points.

Everything here comes from the database (application, offer, company) or from the master profile: this is the
template used when the LLM is unavailable, and the reference the LLM output is checked against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jobbot.config import Experience, Profile, Project
from jobbot.models import Application, Company, ContractType, JobOffer
from jobbot.reports._logistics import Logistics, extract_logistics
from jobbot.textutil import strip_accents

if TYPE_CHECKING:
    from jobbot.context import AppContext

MONTHS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
             "novembre", "décembre")
STAR_LIMIT = 3
BULLET_CHARS = 320
SUMMARY_CHARS = 700
_RESULT_WORDS = re.compile(
    r"utilisee?s? |quotidien|mise? en production|publie|livre|adopte|deploye|reduit|ameliore"
)
_VOWELS = "aeiouyhàâéèêîïôû"
_FREELANCE_WORDS = re.compile(r"freelance|independant|auto-?entrepreneur|a mon compte")
_ALTERNANCE_WORDS = re.compile(r"alternan|apprenti|contrat pro")


@dataclass(frozen=True)
class StarPoint:
    """One experience told with the STAR method (Situation, Tâche, Action, Résultat)."""

    source: str
    situation: str
    task: str
    action: str
    result: str
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class QA:
    category: str  # technique | alternance | comportement | motivation
    question: str
    answer: str


@dataclass(frozen=True)
class PrepFacts:
    application: Application
    job: JobOffer | None
    company: Company | None
    company_name: str
    job_title: str
    alternance: bool
    job_skills: tuple[str, ...]  # canonical skills of the offer, most mentioned first
    have: tuple[str, ...]  # … that the profile backs
    missing: tuple[str, ...]  # … absent from the profile (never to be claimed)
    soft: tuple[str, ...]
    website: str | None
    email_text: str
    logistics: Logistics
    profile_skills: frozenset[str] = frozenset()  # canonical skills the profile backs (broader ones implied)


# --------------------------------------------------------------------------- small helpers


def fold(text: str) -> str:
    return strip_accents(text or "").lower().replace("’", "'")


def month_label(value: str) -> str:
    """'2026-10' → 'octobre 2026'; '2025' stays '2025'."""
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", (value or "").strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        return (value or "").strip()
    return f"{MONTHS_FR[int(match.group(2)) - 1]} {match.group(1)}"


def of(word: str) -> str:
    """French elision: « de octobre » → « d'octobre »."""
    return f"d'{word}" if word[:1].lower() in _VOWELS else f"de {word}"


def shorten(text: str, limit: int) -> str:
    """Cut on a word boundary (never in the middle of a word), with an ellipsis."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    cut = clean[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:(")
    return f"{cut}…"


def period(start: str, end: str) -> str:
    begin, finish = month_label(start), month_label(end) or "aujourd'hui"
    if not begin:
        return finish if end else ""
    return begin if begin == finish else f"{begin} – {finish}"


def item_label(item: Experience | Project) -> str:
    if isinstance(item, Experience):
        return f"{item.title} — {item.company}"
    return f"Projet « {item.name} »"


def _job_text(job: JobOffer | None, company: Company | None) -> str:
    parts = [job.title, job.description] if job else []
    if company is not None:
        parts += [company.description or "", company.naf_label or ""]
    return "\n".join(p for p in parts if p)


def _is_alternance(job: JobOffer | None, profile: Profile) -> bool:
    if job is not None and job.contract_type == ContractType.ALTERNANCE:
        return True
    if job is not None and (_ALTERNANCE_WORDS.search(fold(job.title))):
        return True
    if job is not None and job.contract_type != ContractType.OTHER:
        return False
    return profile.alternance is not None and ContractType.ALTERNANCE in profile.target.contract_types


def _website(job: JobOffer | None, company: Company | None) -> str | None:
    if company is not None and (company.website or company.domain):
        return company.website or f"https://{company.domain}"
    if job is not None and job.company_domain:
        return f"https://{job.company_domain}"
    return None


# --------------------------------------------------------------------------- facts


def gather_facts(ctx: AppContext, app: Application, email_text: str, contact: str) -> PrepFacts:
    """Offer, company, skills (backed / missing) and logistics of one application."""
    from jobbot.documents._truth import build_truth
    from jobbot.matching import lexicon

    job = ctx.repo.get_job(app.job_id) if app.job_id else None
    company = ctx.repo.get_company(app.company_id) if app.company_id else None
    text = _job_text(job, company)
    skills = tuple(skill for skill, _ in lexicon.count_skills(text).most_common())
    truth = build_truth(ctx.profile)
    name = app.company_name or (company.name if company else "") or (job.company if job else "") or "l'entreprise"
    return PrepFacts(
        application=app, job=job, company=company, company_name=name,
        job_title=app.job_title or (job.title if job else ""), alternance=_is_alternance(job, ctx.profile),
        job_skills=skills, have=tuple(s for s in skills if s in truth.skills),
        missing=tuple(s for s in skills if s not in truth.skills),
        soft=tuple(sorted(lexicon.find_soft_skills(text))), website=_website(job, company), email_text=email_text,
        logistics=extract_logistics(email_text, contact=contact, exclude_phones=(ctx.profile.identity.phone,)),
        profile_skills=truth.skills,
    )


def company_summary(facts: PrepFacts) -> str:
    """What the company does, from the data we have (template: no LLM)."""
    company, job = facts.company, facts.job
    if company is not None and company.description:
        return shorten(company.description, SUMMARY_CHARS)
    if job is not None and job.description:
        return f"Extrait de l'annonce : {shorten(job.description, SUMMARY_CHARS)}"
    return "Pas de description enregistrée : lis leur site et leur page LinkedIn avant l'entretien."


# --------------------------------------------------------------------------- STAR talking points


def item_skills(item: Experience | Project) -> set[str]:
    """Canonical skills of an experience / project (declared + found in its text, broader ones implied)."""
    from jobbot.matching import lexicon

    found = {c for c in (lexicon.canonicalize(s) for s in item.skills) if c in lexicon.SKILLS}
    texts = item.bullets if isinstance(item, Experience) else [item.description]
    for text in texts:
        found |= lexicon.find_skills(text)
    return lexicon.expand_with_ancestors(found)


def _bullet_skills(text: str) -> set[str]:
    from jobbot.matching import lexicon

    return lexicon.expand_with_ancestors(lexicon.find_skills(text))


def _relevance(item: Experience | Project, wanted: set[str]) -> int:
    overlap = len(item_skills(item) & wanted)
    if not overlap:
        return 0
    return 2 * overlap + (1 if isinstance(item, Experience) and item.kind != "seasonal" else 0)


def relevant_items(profile: Profile, job_skills: tuple[str, ...], limit: int = STAR_LIMIT
                   ) -> list[Experience | Project]:
    """Experiences / projects sharing the most skills with the offer (profile order breaks ties)."""
    wanted = set(job_skills)
    items: list[Experience | Project] = [*profile.experiences, *profile.projects]
    scored = sorted(((_relevance(item, wanted), index) for index, item in enumerate(items)),
                    key=lambda pair: (-pair[0], pair[1]))
    chosen = [items[index] for score, index in scored if score > 0][:limit]
    for item in profile.experiences:  # no skill in common (spontaneous, empty offer): main experiences
        if len(chosen) >= min(limit, 2):
            break
        if item not in chosen and item.kind != "seasonal":
            chosen.append(item)
    return chosen


NO_RESULT = "À formuler avec tes mots : ce que ton travail a apporté (utilisateurs, équipe), sans inventer de chiffre."


def _actions(bullets: list[str], wanted: set[str]) -> list[str]:
    """Up to two bullets sharing skills with the offer (best first), else the first one."""
    scored = sorted(((len(_bullet_skills(b) & wanted), i) for i, b in enumerate(bullets)), key=lambda p: (-p[0], p[1]))
    relevant = [bullets[i] for overlap, i in scored if overlap][:2]
    return relevant or bullets[:1]


def _situation(exp: Experience) -> str:
    if exp.kind == "freelance":  # no « chez Indépendant »
        who = exp.title if _FREELANCE_WORDS.search(fold(exp.title)) else f"{exp.title} (à mon compte)"
    else:
        who = f"{exp.title} chez {exp.company}"
    where = ", ".join(p for p in (exp.location, period(exp.start, exp.end)) if p)
    return f"{who} — {where}." if where else f"{who}."


def _star_experience(exp: Experience, wanted: set[str]) -> StarPoint:
    bullets = [b.strip() for b in exp.bullets if b.strip()]
    task = bullets[0] if bullets else exp.title
    rest = bullets[1:]
    result = next((b for b in rest if _RESULT_WORDS.search(fold(b))), None)
    actions = _actions([b for b in rest if b != result], wanted)
    return StarPoint(
        source=item_label(exp), situation=_situation(exp), task=shorten(task, BULLET_CHARS),
        action=" ; ".join(shorten(a, BULLET_CHARS).rstrip(".") for a in actions) + "." if actions
        else "Décris les étapes que tu as menées toi-même.",
        result=shorten(result, BULLET_CHARS) if result else NO_RESULT,
        skills=tuple(sorted(item_skills(exp) & wanted)),
    )


def _star_project(project: Project, wanted: set[str]) -> StarPoint:
    link = f" Démo / code : {project.url}" if project.url else ""
    return StarPoint(
        source=item_label(project), situation=f"Projet personnel « {project.name} ».",
        task=shorten(project.description, BULLET_CHARS),
        action="Technologies utilisées : " + (", ".join(project.skills) or "à préciser") + ".",
        result=(NO_RESULT + link).strip(), skills=tuple(sorted(item_skills(project) & wanted)),
    )


def star_points(profile: Profile, facts: PrepFacts, limit: int = STAR_LIMIT) -> list[StarPoint]:
    wanted = set(facts.job_skills)
    points = []
    for item in relevant_items(profile, facts.job_skills, limit):
        maker = _star_experience if isinstance(item, Experience) else _star_project
        points.append(maker(item, wanted))
    return points
