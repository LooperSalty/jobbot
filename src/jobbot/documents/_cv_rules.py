"""Deterministic CV tailoring (no LLM) and the shared tailoring context.

Headline mirrors the offer title when it belongs to one of the candidate's title families (research §6.5 step 2);
skills and bullets are reordered by keyword relevance (matched first), the most relevant projects are kept, and a
"key skills" sentence lists the offer's top keywords the candidate really has. Nothing is ever added that the
profile does not state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobbot import clock
from jobbot.config import Education, Experience, Profile, Project, SkillGroup
from jobbot.documents._format import has_started, merge_unbalanced, month_label
from jobbot.documents._truth import TruthBase, build_truth, phrase_supported, unsupported_skills
from jobbot.documents.models import TailoredCV
from jobbot.matching import lexicon
from jobbot.matching._text import display_title, family_of, norm, normalize_title
from jobbot.matching.ats import keyword_coverage
from jobbot.models import Company, ContractType, JobOffer, KeywordHit

MAX_SKILL_GROUPS = 6
MAX_PROJECTS = 2
MAX_INTERESTS = 5
MAX_KEY_SKILLS = 5
BULLET_CAP = {"seasonal": 1}
DEFAULT_BULLET_CAP = 4
HEADLINE_SEPARATOR = " — "
_LEVEL_RX = re.compile(r"bac\s*\+\s*\d", re.I)
_ALTERNANCE_RX = re.compile(r"\b(?:alternan\w*|apprenti\w*|contrat pro\w*)\b")


@dataclass(frozen=True)
class TailorInput:
    profile: Profile
    keywords: list[KeywordHit]
    job: JobOffer | None
    company: Company | None
    truth: TruthBase
    title: str  # target title used for the ATS title score
    alternance: bool
    truthful: list[KeywordHit]  # offer keywords the candidate really has
    forbidden: list[KeywordHit]  # offer keywords the candidate does not have


def is_alternance_context(profile: Profile, job: JobOffer | None) -> bool:
    if profile.alternance is None:
        return False
    if job is None:
        return ContractType.ALTERNANCE in profile.target.contract_types
    if job.contract_type == ContractType.ALTERNANCE:
        return True
    if job.contract_type == ContractType.OTHER:
        return bool(_ALTERNANCE_RX.search(norm(f"{job.title} {job.description[:1500]}")))
    return False


def target_title(profile: Profile, job: JobOffer | None) -> str:
    if job is not None and job.title.strip():
        return job.title
    if profile.target.job_titles:
        return profile.target.job_titles[0]
    return profile.headline.split(HEADLINE_SEPARATOR)[0]


def keyword_is_truthful(kw: KeywordHit, truth: TruthBase) -> bool:
    if kw.category == "title":
        family = family_of(normalize_title(kw.keyword))
        return family is not None and family in truth.families
    name = lexicon.canonicalize(kw.keyword)
    if name in lexicon.SKILLS:
        return name in truth.skills
    if name in lexicon.SOFT_SKILLS:
        return name in truth.soft
    if name in lexicon.LANGUAGE_SKILLS:
        return name in truth.languages
    return phrase_supported(kw.keyword, truth)


def make_input(profile: Profile, keywords: list[KeywordHit], job: JobOffer | None,
               company: Company | None) -> TailorInput:
    truth = build_truth(profile)
    truthful = [k for k in keywords if keyword_is_truthful(k, truth)]
    forbidden = [k for k in keywords if k not in truthful and k.category != "title"]
    return TailorInput(
        profile=profile, keywords=list(keywords), job=job, company=company, truth=truth,
        title=target_title(profile, job), alternance=is_alternance_context(profile, job),
        truthful=truthful, forbidden=forbidden,
    )


def relevance(text: str, keywords: list[KeywordHit]) -> float:
    """Sum of the weights of the keywords ``text`` evidences (synonym-aware, same rules as the ATS)."""
    if not text.strip() or not keywords:
        return 0.0
    matched, _ = keyword_coverage(text, keywords)
    return sum(k.weight for k in matched)


# --------------------------------------------------------------------------- headline & summary


def _word_in_sentence(word: str) -> str:
    core = word.strip("()[],;:/&")
    keep = (not core or core.isupper() or any(c.isdigit() or c == "." for c in core)
            or lexicon.canonicalize(core) in lexicon.SKILLS)
    return word if keep else word.lower()


def title_in_sentence(title: str) -> str:
    """Job title inside a sentence: 'Développeur Full-Stack Web & Logiciel' → 'développeur full-stack web &
    logiciel'; tools and acronyms keep their case ('développeur .NET / C#', 'développeur React', 'développeur IA')."""
    return " ".join(_word_in_sentence(w) for w in title.split(" "))


def _recase(title: str) -> str:
    return title[:1].upper() + title[1:].lower() if title.isupper() else title[:1].upper() + title[1:]


def headline_title(inp: TailorInput) -> str:
    base = inp.profile.headline.split(HEADLINE_SEPARATOR)[0].strip() or inp.profile.headline
    if inp.job is None:
        return base
    shown = display_title(inp.job.title)
    family = family_of(normalize_title(inp.job.title))
    if not shown or family not in inp.truth.families or unsupported_skills(shown, inp.truth):
        return base
    return _recase(shown)


def headline_suffix(inp: TailorInput) -> str:
    alt = inp.profile.alternance
    if inp.alternance and alt is not None:
        level_match = _LEVEL_RX.search(alt.degree)
        level = level_match.group(0).replace(" ", "") if level_match else f"RNCP niveau {alt.rncp_level}".strip()
        started = has_started(alt.start_date, clock.to_paris(clock.now()))
        when = "disponible immédiatement" if started or not alt.start_date else f"dès {month_label(alt.start_date)}"
        months = f" ({alt.duration_months} mois)" if alt.duration_months else ""
        return f"Alternance {level}{months} {when}"
    availability = inp.profile.answers.get("notice_period", "").strip()
    return f"Junior, {availability[:1].lower()}{availability[1:]}" if availability else "Junior"


def build_headline(inp: TailorInput) -> str:
    return f"{headline_title(inp)}{HEADLINE_SEPARATOR}{headline_suffix(inp)}"


def _catalogue_item(name: str, profile: Profile) -> str | None:
    """Profile spelling of a canonical skill ("API" → "API REST", "Next.js" → "Next.js 15")."""
    wanted = {name} | lexicon.descendants(name)
    for group in profile.skills:
        for item in group.items:
            if lexicon.canonicalize(item) in wanted:
                return item
    return None


def key_skills(inp: TailorInput, already: str, *, limit: int = MAX_KEY_SKILLS) -> list[str]:
    """Top offer skills the candidate has, in the catalogue's spelling, not yet named in ``already``."""
    present = lexicon.expand_with_ancestors(lexicon.find_skills(already))
    names: list[str] = []
    for kw in sorted(inp.truthful, key=lambda k: -k.weight):
        name = lexicon.canonicalize(kw.keyword)
        item = _catalogue_item(name, inp.profile) if kw.category in ("hard", "tool") else None
        if item and name not in present and item not in names:
            names.append(item)
    return names[:limit]


def join_fr(items: list[str]) -> str:
    """'a, b et c'."""
    return ", ".join(items[:-1]) + f" et {items[-1]}" if len(items) > 1 else "".join(items)


def _soft_sentence(inp: TailorInput, text: str) -> str:
    present = lexicon.find_soft_skills(text)
    wanted = [lexicon.canonicalize(k.keyword) for k in inp.truthful if k.category == "soft"]
    names = [n for n in dict.fromkeys(wanted) if n in inp.truth.soft and n not in present]
    return f"Atouts : {join_fr([n[:1].lower() + n[1:] for n in names])}." if names else ""


_ALTERNANCE_SENTENCE = re.compile(r"alternan|apprenti|rentree|formation en|ecole", re.I)


def _cdi_summary(inp: TailorInput) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(inp.profile.summary.split()))
    kept = [s for s in sentences if s and not _ALTERNANCE_SENTENCE.search(norm(s))]
    degree = next((e for e in inp.profile.education if not _is_programme(e.school, inp)), None)
    contract = inp.job.contract_type.value.upper() if inp.job and inp.job.contract_type.value in ("cdi", "cdd") else ""
    intro = f"Titulaire d'un {degree.degree.split(HEADLINE_SEPARATOR)[0]} ({degree.school})" if degree else ""
    role = headline_title(inp)
    wish = f"je recherche un poste de {title_in_sentence(role)}{' en ' + contract if contract else ''}."
    return " ".join(x for x in [f"{intro}, {wish}" if intro else wish[:1].upper() + wish[1:], *kept] if x)


def build_summary(inp: TailorInput) -> str:
    summary = " ".join(inp.profile.summary.split()) if inp.alternance else _cdi_summary(inp)
    names = key_skills(inp, summary)
    label = "Compétences clés pour ce poste" if inp.job is not None else "Compétences clés"
    skills = f"{label} : {join_fr(names)}." if names else ""
    return " ".join(x for x in (summary, skills, _soft_sentence(inp, summary)) if x)


def _is_programme(school: str, inp: TailorInput) -> bool:
    alt = inp.profile.alternance
    return alt is not None and bool(alt.school) and norm(alt.school) in norm(school)


# --------------------------------------------------------------------------- sections


def order_skills(groups: list[SkillGroup], keywords: list[KeywordHit]) -> list[SkillGroup]:
    scored = []
    for index, group in enumerate(groups):
        weights = [relevance(item, keywords) for item in group.items]
        order = sorted(range(len(weights)), key=lambda i: (-weights[i], i))
        ranked = [group.items[i] for i in order]
        scored.append((sum(weights), index, SkillGroup(category=group.category, items=ranked)))
    scored.sort(key=lambda s: (s[0] <= 0, s[1]))
    return [group for _, _, group in scored[:MAX_SKILL_GROUPS] if group.items]


def order_bullets(exp: Experience, keywords: list[KeywordHit]) -> Experience:
    ranked = sorted(enumerate(exp.bullets), key=lambda p: (-relevance(p[1], keywords), p[0]))
    cap = BULLET_CAP.get(exp.kind, DEFAULT_BULLET_CAP)
    return exp.model_copy(update={"bullets": [b for _, b in ranked][:cap]})


def select_projects(projects: list[Project], keywords: list[KeywordHit]) -> list[Project]:
    scored = sorted(((relevance(f"{p.description} {' '.join(p.skills)}", keywords), i, p)
                     for i, p in enumerate(projects)), key=lambda s: (-s[0], s[1]))
    relevant = [p for score, _, p in scored if score > 0]
    return relevant[:MAX_PROJECTS] if relevant else [p for _, _, p in scored[:1]]


def select_education(inp: TailorInput) -> list[Education]:
    """Every diploma; the apprenticeship programme only when applying for an alternance."""
    return [e for e in inp.profile.education if inp.alternance or not _is_programme(e.school, inp)]


def build_rule_cv(inp: TailorInput) -> TailoredCV:
    profile = inp.profile
    return TailoredCV(
        identity=profile.identity,
        headline=build_headline(inp),
        summary=build_summary(inp),
        experiences=[order_bullets(e, inp.keywords) for e in profile.experiences],
        projects=select_projects(profile.projects, inp.keywords),
        education=select_education(inp),
        skills=order_skills(profile.skills, inp.keywords),
        languages=list(profile.languages),
        interests=merge_unbalanced(profile.interests)[:MAX_INTERESTS],
        alternance=profile.alternance if inp.alternance else None,
    )
