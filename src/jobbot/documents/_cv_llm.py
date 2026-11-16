"""LLM tailoring of the CV: French prompt, JSON plan, and a sanitizer that only keeps what the profile backs.

The LLM never writes employers, titles, dates or degrees: it references experiences/projects by number and
only rewrites bullets, summary and the selection/order of skills; the headline is fixed by code (offer
title mirrored when it fits the candidate, research §6.5). Every rewritten text is checked
against the truth base; an unsupported bullet falls back to the closest original bullet, an unsupported
summary to the deterministic one, an unknown skill item is dropped.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz

from jobbot.config import Experience, Profile, Project, SkillGroup
from jobbot.documents._cv_rules import TailorInput, build_headline
from jobbot.documents._format import clean_spaces, merge_unbalanced, strip_emphasis
from jobbot.documents._truth import phrase_supported, similar, unsupported_claims
from jobbot.documents.models import TailoredCV
from jobbot.matching import lexicon
from jobbot.models import ATSReport, Company, JobOffer, KeywordHit

log = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 5000
MAX_BULLETS = 5
MAX_GROUP_ITEMS = 10


class _Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PlanExperience(_Plan):
    ref: int
    bullets: list[str] = Field(default_factory=list)


class PlanProject(_Plan):
    ref: int
    description: str = ""


class PlanSkillGroup(_Plan):
    category: str
    items: list[str] = Field(default_factory=list)


class CVPlan(_Plan):
    summary: str
    skills: list[PlanSkillGroup] = Field(default_factory=list)
    experiences: list[PlanExperience] = Field(default_factory=list)
    projects: list[PlanProject] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)


SYSTEM = (
    "Tu es un expert du recrutement tech en France et de l'optimisation de CV pour les ATS. Tu adaptes le CV "
    "d'un candidat à une offre en respectant une règle absolue : tu n'inventes jamais rien. Tu n'utilises que les "
    "faits du profil fourni ; tu peux reformuler, réordonner, fusionner, raccourcir et sélectionner."
)

_RULES = """Règles impératives :
1. N'invente RIEN : aucune compétence, outil, technologie, chiffre, employeur, date, diplôme, qualité ou
   responsabilité absent du profil. Les mots-clés « interdits » ci-dessus ne doivent apparaître nulle part.
   Un chiffre garde son sens exact (« 210 commits sur 450 » = la part du candidat, pas le total).
   Diplômes : reprends exactement les intitulés du profil (jamais « Master », « Licence », « ingénieur »…
   s'ils n'y figurent pas).
2. Le titre du CV est fixé : « {headline} ». "summary" : 2 ou 3 phrases (250 à 420 caractères), cohérentes
   avec ce titre, au style CV (phrases nominales, ni « je » ni « il ») ; cite 3 à 5 compétences clés de l'offre
   que le candidat possède et un fait concret du profil.
3. "skills" : 4 à 6 groupes à intitulé court ; éléments pris UNIQUEMENT dans le catalogue de compétences
   (même outil = tu peux reprendre l'orthographe de l'offre) ; éléments demandés par l'offre en premier ;
   3 à 9 éléments par groupe ; pas de langues (section dédiée).
4. "experiences" : une entrée par expérience conservée (ref = numéro E), dans l'ordre chronologique du profil ;
   2 à 4 puces pour les expériences clés, 1 pour les autres, jamais plus de puces que le profil n'en a pour
   cette expérience (tu peux fusionner ou retirer, pas délayer) ; 12 à 32 mots par puce, commençant par un nom
   d'action (« Conception… », « Développement… », « Mise en production… ») ; tu peux mettre en **gras** 1
   expression clé par puce. Le job saisonnier peut être omis.
5. "projects" : 0 à 2 projets pertinents (ref = numéro P), description d'une phrase de 25 mots au plus, sans
   répéter le nom du projet.
6. "interests" : 3 à 5 centres d'intérêt repris du profil.
7. Le CV doit tenir sur UNE page A4 : 380 à 600 mots au total.
8. Pas de bourrage : un mot-clé apparaît au plus 3 fois dans tout le CV."""


def profile_block(profile: Profile, *, include_alternance: bool = True) -> str:
    lines = ([f"Titre actuel : {profile.headline}", f"Résumé : {' '.join(profile.summary.split())}"]
             if include_alternance else [])
    if profile.qualities:
        lines.append("Qualités : " + ", ".join(profile.qualities))
    lines.append("Expériences (ne réécris que les puces ; employeur, intitulé et dates sont repris tels quels) :")
    for i, exp in enumerate(profile.experiences):
        lines.append(f"[E{i}] {exp.title} | {exp.company} | {exp.start or '?'} → {exp.end or 'en cours'} ({exp.kind})")
        lines.extend(f"  - {b}" for b in exp.bullets)
        if exp.skills:
            lines.append("  Compétences démontrées : " + ", ".join(exp.skills))
    lines.append("Projets :")
    lines.extend(f"[P{i}] {p.name} : {p.description} ({', '.join(p.skills)})" for i, p in enumerate(profile.projects))
    lines.append("Catalogue de compétences (n'utilise QUE ces éléments) :")
    lines.extend(f"- {g.category} : {', '.join(g.items)}" for g in profile.skills)
    lines.append("Formation : " + " ; ".join(f"{e.degree} ({e.school})" for e in profile.education))
    lines.append("Langues : " + ", ".join(f"{lang.name} {lang.level}" for lang in profile.languages))
    lines.append("Centres d'intérêt : " + " ; ".join(merge_unbalanced(profile.interests)))
    alt = profile.alternance
    if alt is not None and include_alternance:
        lines.append(f"Alternance : {alt.degree} à {alt.school}, {alt.duration_months} mois, début {alt.start_date}, "
                     f"rythme {alt.rhythm}")
    return "\n".join(lines)


def target_block(job: JobOffer | None, company: Company | None, title: str) -> str:
    if job is not None:
        return (f"## Offre visée\nIntitulé : {job.title}\nEntreprise : {job.company or '(non précisée)'}\n"
                f"Lieu : {job.location}\nContrat : {job.contract_type.value}\n"
                f"Description :\n{(job.description or '')[:MAX_DESCRIPTION_CHARS]}")
    if company is None:
        return f"## Candidature spontanée\nPoste recherché : {title}"
    activity = company.naf_label or (f"code NAF {company.naf_code}" if company.naf_code else "(inconnue)")
    block = (f"## Candidature spontanée\nPoste recherché : {title}\nEntreprise : {company.name}\n"
             f"Activité : {activity}\nVille : {company.city}\n"
             f"Description : {company.description or '(aucune)'}")
    if company.raw.get("large_target"):  # « grandes entreprises » of any sector (jobbot.night.large)
        block += ("\nContexte : grande structure dont le métier n'est pas forcément le numérique ; le candidat vise son "
                  "service informatique interne (applications et outils métier, transformation numérique). Ne cite "
                  "aucun projet informatique précis de l'entreprise qui ne figure pas ci-dessus.")
    return block


def keywords_line(keywords: list[KeywordHit]) -> str:
    return ", ".join(f"{k.keyword} ({k.weight:.1f})" for k in keywords) or "(aucun)"


def build_prompt(inp: TailorInput, previous: CVPlan | None = None, report: ATSReport | None = None,
                 missing: list[KeywordHit] | None = None) -> str:
    parts = [
        target_block(inp.job, inp.company, inp.title),
        "## Mots-clés ATS de l'offre que le candidat possède (à intégrer naturellement)\n"
        + keywords_line(inp.truthful),
        "## Mots-clés que le candidat NE possède PAS (interdits)\n" + keywords_line(inp.forbidden),
        "## Profil du candidat (SOURCE DE VÉRITÉ)\n" + profile_block(inp.profile, include_alternance=inp.alternance),
        _RULES.format(headline=build_headline(inp)),
    ]
    if previous is not None:
        score = f"{report.score:.0f}/100" if report else "?"
        parts.append(
            f"## Ta version précédente (score ATS {score})\n{json.dumps(previous.model_dump(), ensure_ascii=False)}\n"
            "Mots-clés encore absents alors que le profil les justifie : " + keywords_line(missing or [])
            + "\nIntègre-les là où le profil le justifie (puces, profil ou compétences), sans rien inventer ni "
            "répéter à l'excès, puis renvoie le CV complet."
        )
    parts.append("Réponds avec le CV adapté (summary, skills, experiences, projects, interests).")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- sanitizer


def _ok(text: str, inp: TailorInput, *, lenient: bool = False) -> bool:
    return bool(clean_spaces(text)) and not unsupported_claims(text, inp.truth, lenient=lenient)


def _skill_item_ok(item: str, inp: TailorInput) -> bool:
    canonical = lexicon.canonicalize(strip_emphasis(item))
    if canonical in lexicon.SKILLS:
        return canonical in inp.truth.skills and _ok(item, inp, lenient=True)
    return _ok(item, inp, lenient=True) and phrase_supported(item, inp.truth)


def _skills(plan: CVPlan, inp: TailorInput, fallback: list[SkillGroup], fixes: list[str]) -> list[SkillGroup]:
    groups: list[SkillGroup] = []
    seen: set[str] = set()
    for group in plan.skills:
        items = []
        for raw in group.items:
            item = strip_emphasis(raw)
            key = lexicon.canonicalize(item).lower()
            if not item or key in seen or lexicon.find_languages(item):
                continue  # languages have their own section
            if not _skill_item_ok(item, inp):
                fixes.append(f"Compétence retirée (absente du profil) : {item}")
                continue
            seen.add(key)
            items.append(item)
        if items and clean_spaces(group.category):
            groups.append(SkillGroup(category=strip_emphasis(group.category), items=items[:MAX_GROUP_ITEMS]))
    return groups or fallback


def _closest(bullet: str, originals: list[str], used: set[str]) -> str | None:
    candidates = [o for o in originals if o not in used]
    if not candidates:
        return None
    return max(candidates, key=lambda o: fuzz.token_set_ratio(strip_emphasis(bullet), o))


def _bullets(raw: list[str], exp: Experience, inp: TailorInput, fixes: list[str]) -> list[str]:
    kept: list[str] = []
    used: set[str] = set()
    for bullet in (clean_spaces(b) for b in raw[:min(MAX_BULLETS, len(exp.bullets))]):
        if not bullet:
            continue
        if _ok(bullet, inp):
            kept.append(bullet)
            continue
        fixes.append(f"Puce remplacée ({', '.join(unsupported_claims(bullet, inp.truth))}) : {strip_emphasis(bullet)}")
        original = _closest(bullet, exp.bullets, used)
        if original is not None:
            used.add(original)
            kept.append(original)
    return kept or exp.bullets[:1]


def _experiences(plan: CVPlan, inp: TailorInput, fallback: list[Experience], fixes: list[str]) -> list[Experience]:
    profile_exps = inp.profile.experiences
    refs = sorted({p.ref: p for p in plan.experiences if 0 <= p.ref < len(profile_exps)}.items())
    out = [profile_exps[ref].model_copy(update={"bullets": _bullets(p.bullets, profile_exps[ref], inp, fixes)})
           for ref, p in refs]
    return out or fallback


def _projects(plan: CVPlan, inp: TailorInput, fixes: list[str]) -> list[Project]:
    profile_projects = inp.profile.projects
    out: list[Project] = []
    seen: set[int] = set()
    for p in plan.projects:
        if not 0 <= p.ref < len(profile_projects) or p.ref in seen:
            continue
        seen.add(p.ref)
        original = profile_projects[p.ref]
        description = clean_spaces(p.description)
        if description and not _ok(description, inp):
            fixes.append(f"Description de projet remplacée : {original.name}")
            description = ""
        out.append(original.model_copy(update={"description": description or original.description}))
    return out[:2]


def _interests(plan: CVPlan, inp: TailorInput, fallback: list[str]) -> list[str]:
    own_interests = merge_unbalanced(inp.profile.interests)
    kept = [strip_emphasis(i) for i in plan.interests
            if any(similar(i, own, 80) for own in own_interests) and _ok(i, inp, lenient=True)]
    return kept[:5] or fallback


def plan_to_cv(plan: CVPlan, inp: TailorInput, fallback: TailoredCV) -> tuple[TailoredCV, list[str]]:
    """Turn an LLM plan into a CV that only states profile facts; returns the CV and the fixes applied."""
    fixes: list[str] = []
    summary = clean_spaces(plan.summary)
    if not _ok(summary, inp):
        fixes.append(f"Profil remplacé ({', '.join(unsupported_claims(summary, inp.truth))})")
        summary = fallback.summary
    cv = fallback.model_copy(update={
        "summary": summary,
        "skills": _skills(plan, inp, fallback.skills, fixes),
        "experiences": _experiences(plan, inp, fallback.experiences, fixes),
        "projects": _projects(plan, inp, fixes),
        "interests": _interests(plan, inp, fallback.interests),
    })
    for fix in fixes:
        log.info("Adaptation du CV corrigée : %s", fix)
    return cv, fixes
