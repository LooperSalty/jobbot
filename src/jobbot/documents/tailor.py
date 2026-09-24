"""CV tailoring: LLM rewrite constrained to the master profile, iterated on truthful missing keywords.

``tailor_cv`` asks the LLM for a tailored CV (French prompt), sanitizes it against the profile (nothing
invented), scores it with the deterministic ATS, and — while the score is under ``target_score`` — asks again
with the offer keywords that are still missing but that the profile justifies. The best version is kept.
Without LLM (``None`` or ``LLMError``) a deterministic tailoring is returned (skills and bullets reordered by
keyword relevance, headline mirroring the offer title).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from jobbot.config import Profile
from jobbot.documents._cv_llm import SYSTEM, CVPlan, build_prompt, plan_to_cv
from jobbot.documents._cv_rules import TailorInput, build_rule_cv, keyword_is_truthful, make_input
from jobbot.documents._format import clean_spaces
from jobbot.documents._truth import TruthBase, build_truth, similar, unsupported_claims
from jobbot.documents.models import TailoredCV, cv_to_text
from jobbot.llm import LLMClient, LLMError
from jobbot.matching.ats import score_document
from jobbot.models import ATSReport, Company, JobOffer, KeywordHit
from jobbot.textutil import normalize_company

log = logging.getLogger(__name__)

RULE_MARGIN = 5.0  # the deterministic CV only wins over the LLM one when clearly better


@dataclass(frozen=True)
class _Attempt:
    cv: TailoredCV
    report: ATSReport
    warnings: list[str]
    plan: CVPlan | None = None


def score_cv(cv: TailoredCV, keywords: list[KeywordHit], title: str) -> ATSReport:
    return score_document(cv_to_text(cv), keywords, title, kind="cv")


def truthful_missing(missing: list[KeywordHit], profile: Profile) -> list[KeywordHit]:
    """Missing offer keywords the candidate can truthfully claim (skill, soft skill, language or phrase present
    in the profile; title variants of one of the candidate's title families)."""
    truth = build_truth(profile)
    return [kw for kw in missing if keyword_is_truthful(kw, truth)]


def tailor_cv(
    profile: Profile,
    keywords: list[KeywordHit],
    llm: LLMClient,
    *,
    job: JobOffer | None = None,
    company: Company | None = None,
    target_score: float = 80,
    max_iterations: int = 3,
) -> tuple[TailoredCV, ATSReport, list[str]]:
    """Best tailored CV, its ATS report (on the CV text) and the truth warnings (empty when all is backed)."""
    inp = make_input(profile, keywords, job, company)
    rule_cv = build_rule_cv(inp)
    rule = _Attempt(rule_cv, score_cv(rule_cv, inp.keywords, inp.title), check_truthfulness(rule_cv, profile))
    attempts = _llm_attempts(inp, llm, rule_cv, target_score, max_iterations) if llm is not None else []
    best = _pick_best(attempts, rule, target_score)
    log.info("CV adapté : score ATS %.1f (%d version(s) LLM, %s)", best.report.score, len(attempts),
             "LLM" if best is not rule else "règles")
    return best.cv, best.report, best.warnings


def _llm_attempts(inp: TailorInput, llm: LLMClient, fallback: TailoredCV, target: float,
                  max_iterations: int) -> list[_Attempt]:
    attempts: list[_Attempt] = []
    previous: _Attempt | None = None
    missing: list[KeywordHit] = []
    for iteration in range(max(1, max_iterations)):
        prompt = build_prompt(inp, previous.plan if previous else None, previous.report if previous else None,
                              missing)
        try:
            plan = llm.complete_json(prompt, CVPlan, system=SYSTEM)
        except (LLMError, ValidationError, ValueError) as exc:
            log.warning("Adaptation du CV par LLM impossible (itération %d) : %s", iteration + 1, exc)
            break
        cv, _ = plan_to_cv(plan, inp, fallback)
        attempt = _Attempt(cv, score_cv(cv, inp.keywords, inp.title), check_truthfulness(cv, inp.profile), plan)
        attempts.append(attempt)
        if attempt.report.score >= target:
            break
        missing = [k for k in attempt.report.missing if keyword_is_truthful(k, inp.truth)]
        if not missing:
            break
        previous = attempt
    return attempts


def _pick_best(attempts: list[_Attempt], rule: _Attempt, target: float = 100.0) -> _Attempt:
    """Best LLM version (better written) unless untruthful, or under target and clearly beaten by the rules."""
    if not attempts:
        return rule
    best = max(attempts, key=lambda a: (not a.warnings, a.report.score))
    if best.warnings and not rule.warnings:
        return rule
    if best.report.score >= target:
        return best
    return rule if rule.report.score > best.report.score + RULE_MARGIN else best


# --------------------------------------------------------------------------- truthfulness


def check_truthfulness(cv: TailoredCV, profile: Profile) -> list[str]:
    """French warnings for every CV claim not backed by the profile (skills/tools, numbers, employers, titles,
    dates, degrees, schools, languages, projects)."""
    truth = build_truth(profile)
    warnings: list[str] = []
    if cv.identity != profile.identity:
        warnings.append("Coordonnées différentes de celles du profil")
    warnings += _text_warnings("Titre", [cv.headline], truth)
    warnings += _text_warnings("Profil", [cv.summary], truth)
    warnings += _experience_warnings(cv, profile, truth)
    warnings += _education_warnings(cv, profile, truth)
    warnings += _project_warnings(cv, profile, truth)
    for group in cv.skills:
        warnings += _text_warnings(f"Compétences ({group.category})", group.items, truth, lenient=True)
    for lang in cv.languages:
        if not any(similar(lang.name, p.name, 90) and similar(lang.level, p.level, 90) for p in profile.languages):
            warnings.append(f"Langue absente du profil : {lang.name} ({lang.level})")
    return list(dict.fromkeys(warnings))


def _text_warnings(where: str, texts: list[str], truth: TruthBase, *, lenient: bool = False) -> list[str]:
    out = []
    for text in texts:
        for claim in unsupported_claims(text, truth, lenient=lenient):
            out.append(f"{where} : {claim} absent(e) du profil")
    return out


def _experience_warnings(cv: TailoredCV, profile: Profile, truth: TruthBase) -> list[str]:
    out: list[str] = []
    for exp in cv.experiences:
        same_company = [p for p in profile.experiences
                        if normalize_company(p.company) == normalize_company(exp.company)
                        or similar(p.company, exp.company, 90)]
        if not same_company:
            out.append(f"Employeur absent du profil : {exp.company}")
            continue
        if not any(similar(p.title, exp.title) for p in same_company):
            out.append(f"Intitulé de poste modifié chez {exp.company} : {exp.title}")
        if not any((p.start, p.end) == (exp.start, exp.end) for p in same_company):
            out.append(f"Dates modifiées chez {exp.company} : {exp.start} – {exp.end or 'présent'}")
        out += _text_warnings(f"Expérience {exp.company}", exp.bullets, truth)
    return out


def _education_warnings(cv: TailoredCV, profile: Profile, truth: TruthBase) -> list[str]:
    out: list[str] = []
    for ed in cv.education:
        matches = [p for p in profile.education if similar(p.degree, ed.degree) and similar(p.school, ed.school)]
        if not matches:
            out.append(f"Diplôme absent du profil : {ed.degree} ({ed.school})")
        elif not any((p.start, p.end) == (ed.start, ed.end) for p in matches):
            out.append(f"Dates de formation modifiées : {ed.degree}")
        out += _text_warnings(f"Formation {clean_spaces(ed.degree)[:40]}", [ed.details], truth)
    return out


def _project_warnings(cv: TailoredCV, profile: Profile, truth: TruthBase) -> list[str]:
    out: list[str] = []
    for project in cv.projects:
        if not any(similar(project.name, p.name) for p in profile.projects):
            out.append(f"Projet absent du profil : {project.name}")
        out += _text_warnings(f"Projet {project.name}", [project.description, *project.skills], truth)
    return out
