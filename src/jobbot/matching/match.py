"""Relevance of an offer for the candidate: hard filters, then a deterministic 0-100 score with French reasons.

Score = title 30 + skills 30 + location 15 + contract 15 + freshness 10, then adjustments (non-dev title,
experience asked, alternance level / duration). Hard filters return 0 with the reason. Country / per-area
contract types, relocation and the salary floor live in ``target_rules``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from rapidfuzz import fuzz

from jobbot import clock
from jobbot.config import Profile
from jobbot.matching import filters, lexicon, relayed, target_rules
from jobbot.matching._text import GENDER_RX, family_of, norm, normalize_title
from jobbot.matching.geo import geocode, is_place
from jobbot.matching.location import job_coordinates, job_department
from jobbot.matching.target_rules import CONTRACT_LABELS  # noqa: F401  (re-exported)
from jobbot.models import ContractType, JobOffer, JobStatus

if TYPE_CHECKING:
    from jobbot.context import AppContext

log = logging.getLogger(__name__)

WEIGHTS = {"title": 30.0, "skills": 30.0, "location": 15.0, "contract": 15.0, "freshness": 10.0}
NON_DEV_FACTOR = 0.6  # titles outside software development are kept only when everything else fits
EXPERIENCE_PENALTY = 10.0
HANDLED_STATUSES = frozenset(
    {JobStatus.SHORTLISTED, JobStatus.DOCS_READY, JobStatus.PENDING_REVIEW, JobStatus.APPROVED, JobStatus.APPLIED,
     JobStatus.NEEDS_MANUAL}
)
_FRESHNESS = ((3, 1.0), (7, 0.9), (14, 0.75), (30, 0.5), (60, 0.25))


class MatchReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scored: int = 0
    shortlisted: int = 0
    filtered_out: int = 0


@dataclass(frozen=True)
class ProfileSkills:
    direct: frozenset[str]  # canonical skills the candidate lists or evidences
    covered: frozenset[str]  # + broader skills they imply (Next.js → React → JavaScript)


@dataclass(frozen=True)
class _Part:
    score: float  # 0..1
    reason: str | None = None


def profile_skills(profile: Profile) -> ProfileSkills:
    direct = {lexicon.canonicalize(s) for s in profile.all_skills()} | lexicon.find_skills(profile.full_text())
    direct = {s for s in direct if s in lexicon.SKILLS}
    return ProfileSkills(frozenset(direct), frozenset(lexicon.expand_with_ancestors(direct)))


def match_job(job: JobOffer, profile: Profile) -> tuple[float, list[str]]:
    """Deterministic relevance 0-100 of ``job`` for ``profile`` and the French reasons behind it."""
    return _match(job, profile, profile_skills(profile))


def _match(job: JobOffer, profile: Profile, skills: ProfileSkills) -> tuple[float, list[str]]:
    contract = filters.detect_contract(job)
    text = f"{job.title}\n{job.description or ''}\n{job.raw.get('experienceLibelle') or ''}"
    years = filters.required_experience_years(text)
    blocking = _blocking_reason(job, profile, contract, years, text)
    if blocking:
        return 0.0, [blocking]
    location = target_rules.location_fit(job, profile, contract)
    if location.blocking:
        return 0.0, [location.blocking]
    title = _title_part(job, profile)
    parts = {
        "title": title, "skills": _skills_part(job, skills), "location": _Part(location.score, location.reason),
        "contract": _contract_part(profile, contract), "freshness": _freshness_part(job),
    }
    score = sum(WEIGHTS[name] * part.score for name, part in parts.items())
    reasons = [part.reason for part in parts.values() if part.reason]
    score, extra = _adjust(job, profile, contract, years, text, score, title)
    bonus = relayed.school_bonus(job)
    if bonus:
        score, extra = score + bonus, [relayed.BONUS_REASON.format(bonus=bonus), *extra]
    return round(max(0.0, min(100.0, score)), 1), reasons + extra


# --------------------------------------------------------------------------- hard filters


def _blocking_reason(
    job: JobOffer, profile: Profile, contract: ContractType, years: int | None, text: str
) -> str | None:
    school = relayed.blocking_reason(job, profile) if relayed.is_relayed(job) else filters.school_offer_reason(job)
    reason = filters.sales_title_reason(job) or school  # a school-relayed post names the school: not a fake offer
    reason = reason or _excluded_title(job, profile) or _excluded_description(job, profile)
    reason = reason or target_rules.contract_reason(job, profile, contract)
    if reason:
        return reason
    limit = profile.target.max_experience_years
    if years is not None and years > limit and not _lenient_experience(contract, years, limit, text):
        return f"Écartée : expérience demandée {years} an(s) (maximum {limit})"
    return target_rules.salary_reason(job, profile, contract)


def _lenient_experience(contract: ContractType, years: int, limit: int, text: str) -> bool:
    return years <= limit + 1 and (contract == ContractType.ALTERNANCE or filters.junior_friendly(text))


def _excluded_title(job: JobOffer, profile: Profile) -> str | None:
    title = " " + " ".join(GENDER_RX.sub(" ", norm(job.title)).replace("-", " ").split()) + " "
    degree = _degree_head(profile)
    if degree:
        title = title.replace(degree, " ")  # the school's own diploma title ("Expert en ingénierie…") is fine
    for keyword in sorted(profile.target.exclude_title_keywords, key=len, reverse=True):  # most specific first
        k = norm(keyword).replace("-", " ")
        if k and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", title):
            return f"Écartée : mot exclu dans l'intitulé (« {keyword} »)"
    return None


def _degree_head(profile: Profile) -> str | None:
    if not profile.alternance or not profile.alternance.degree:
        return None
    head = norm(profile.alternance.degree.split("(")[0]).replace("-", " ").split()[:3]
    return " ".join(head) if len(head) == 3 else None


def _excluded_description(job: JobOffer, profile: Profile) -> str | None:
    description = " ".join(norm(job.description or "").split())
    for keyword in sorted(profile.target.exclude_description_keywords, key=len, reverse=True):
        k = norm(keyword)
        if k and k in description:
            return f"Écartée : mot exclu dans la description (« {keyword} »)"
    return None


# --------------------------------------------------------------------------- score parts


def title_similarity(job_title: str, targets: list[str]) -> tuple[float, str | None]:
    """Best 0-100 similarity between an offer title and the target titles (+ the best target)."""
    jt = normalize_title(job_title)
    if not jt or not targets:
        return 50.0, None
    family = family_of(jt)
    best, best_target = -1.0, None
    for target in targets:
        tn = normalize_title(target)
        ratio = float(fuzz.token_set_ratio(jt, tn)) if tn else 0.0
        target_family = family_of(tn)
        if family and target_family == family:
            ratio = max(ratio, 75.0)
        elif family and target_family:
            ratio = max(ratio, 55.0)
        if ratio > best:
            best, best_target = ratio, target
    return best, best_target


def _title_part(job: JobOffer, profile: Profile) -> _Part:
    best, target = title_similarity(job.title, profile.target.job_titles)
    score = max(0.0, min(1.0, (best - 30.0) / 70.0))
    if target is None:
        return _Part(score, None)
    if best >= 60:
        return _Part(score, f"Intitulé proche de « {target} » ({best:.0f} %)")
    return _Part(score, f"Intitulé éloigné des postes visés ({best:.0f} %)")


def _skill_credit(skill: str, skills: ProfileSkills) -> float:
    if skill in skills.covered:
        return 1.0
    if lexicon.ancestors(skill) & skills.direct:
        return 0.5
    return 0.3 if lexicon.related(skill) & skills.direct else 0.0


def _skills_part(job: JobOffer, skills: ProfileSkills) -> _Part:
    wanted = lexicon.count_skills(f"{job.title}\n{job.description or ''}")
    if not wanted:
        return _Part(0.5, "Aucune compétence technique identifiée dans l'annonce")
    in_title = lexicon.find_skills(job.title)
    total = earned = 0.0
    have, lack = [], []
    for skill, _ in wanted.most_common():
        weight = 2.0 if skill in in_title else 1.0
        credit = _skill_credit(skill, skills)
        total, earned = total + weight, earned + weight * credit
        (have if credit >= 1.0 else lack).append(skill)
    reason = f"Compétences en commun ({len(have)}/{len(wanted)}) : " + (", ".join(have[:8]) or "aucune")
    if lack:
        reason += " — à renforcer : " + ", ".join(lack[:5])
    return _Part(earned / total, reason)


def _contract_part(profile: Profile, contract: ContractType) -> _Part:
    if contract == ContractType.OTHER:
        return _Part(0.5, "Type de contrat non précisé")
    wanted = profile.target.contract_types
    if wanted and contract == wanted[0]:
        return _Part(1.0, f"Contrat : {CONTRACT_LABELS[contract]} (recherché en priorité)")
    return _Part(0.85, f"Contrat : {CONTRACT_LABELS[contract]}")


def _freshness_part(job: JobOffer) -> _Part:
    published = clock.parse_dt(job.published_at)
    if published is None:
        return _Part(0.6, "Date de publication inconnue")
    days = max(0, (clock.now() - published).days)
    score = next((s for limit, s in _FRESHNESS if days <= limit), 0.1)
    label = "aujourd'hui" if days == 0 else f"il y a {days} jour(s)"
    return _Part(score, f"Publiée {label}" + (" — offre ancienne" if days > 30 else ""))


def _adjust(
    job: JobOffer, profile: Profile, contract: ContractType, years: int | None, text: str, score: float, title: _Part
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if title.score < 0.4 and family_of(normalize_title(job.title)) is None:
        score *= NON_DEV_FACTOR
        reasons.append("Intitulé hors développement logiciel")
    limit = profile.target.max_experience_years
    if years is not None and years > limit:
        score -= EXPERIENCE_PENALTY
        reasons.append(
            f"Expérience demandée {years} an(s) (maximum {limit}) — toléré pour une alternance/un poste junior"
        )
    elif years is not None:
        reasons.append(f"Expérience demandée : {years} an(s)")
    if filters.junior_friendly(text):
        reasons.append("Débutants / juniors acceptés")
    if contract == ContractType.ALTERNANCE:
        score, alt_reasons = _alternance_fit(text, score)
        reasons += alt_reasons
    return score, reasons


def _alternance_fit(text: str, score: float) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if filters.contains(filters.BAC5_RX, text):
        reasons.append("Niveau Bac+5 visé (compatible avec la formation préparée)")
    elif filters.contains(filters.BAC2_RX, text) and not filters.contains(filters.BAC3_RX, text):
        score -= 10
        reasons.append("Vise un Bac+2 (BTS/DUT) alors que la formation préparée est un Bac+5")
    elif filters.contains(filters.BAC3_RX, text):
        score -= 5
        reasons.append("Vise un Bac+3 alors que la formation préparée est un Bac+5")
    if filters.contains(filters.LONG_CONTRACT_RX, text):
        reasons.append("Durée compatible (24 mois)")
    elif filters.contains(filters.SHORT_CONTRACT_RX, text):
        score -= 3
        reasons.append("Contrat court (12 mois) alors que la formation dure 24 mois")
    return score, reasons


# --------------------------------------------------------------------------- batch scoring


def score_new_jobs(ctx: AppContext) -> MatchReport:
    """Score every NEW job: SHORTLISTED if ≥ min_match_score, else FILTERED_OUT; duplicates of an offer already
    shortlisted/applied on another board → SKIPPED. One failing job never stops the batch."""
    threshold = ctx.settings.thresholds.min_match_score
    skills = profile_skills(ctx.profile)
    counts: Counter[str] = Counter()
    for job in ctx.repo.list_jobs(JobStatus.NEW):
        try:
            outcome = _score_and_store(ctx, job, skills, threshold)
        except Exception:  # noqa: BLE001 — isolate each job
            log.exception("Score impossible pour l'offre %s (%s)", job.id, job.title)
            continue
        counts["scored"] += 1
        counts[outcome] += 1
    report = MatchReport(
        scored=counts["scored"], shortlisted=counts["shortlisted"], filtered_out=counts["filtered_out"]
    )
    log.info("Scoring : %d offre(s) notée(s), %d retenue(s), %d écartée(s), %d doublon(s)",
             report.scored, report.shortlisted, report.filtered_out, counts["skipped"])
    return report


def score_one(ctx: AppContext, job: JobOffer) -> JobOffer:
    """Score and store one job exactly like ``score_new_jobs`` (location, duplicates, threshold); returns the
    stored version (status SHORTLISTED, FILTERED_OUT or SKIPPED)."""
    _score_and_store(ctx, job, profile_skills(ctx.profile), ctx.settings.thresholds.min_match_score)
    return ctx.repo.get_job(job.id) or job


def _score_and_store(ctx: AppContext, job: JobOffer, skills: ProfileSkills, threshold: float) -> str:
    job = _locate(ctx, job)
    score, reasons = _match(job, ctx.profile, skills)
    duplicate = _handled_duplicate(ctx, job)
    if duplicate is not None:
        note = f"Doublon d'une offre déjà retenue ({duplicate.source} : {duplicate.title} — {duplicate.company})"
        ctx.repo.set_job_match(job.id, score, [note, *reasons], JobStatus.SKIPPED)
        return "skipped"
    status = JobStatus.SHORTLISTED if score >= threshold else JobStatus.FILTERED_OUT
    ctx.repo.set_job_match(job.id, score, reasons, status)
    return status.value


def _handled_duplicate(ctx: AppContext, job: JobOffer) -> JobOffer | None:
    if not job.company.strip():
        return None  # no company → the dedup key would only be the title
    return next((d for d in ctx.repo.find_duplicate_jobs(job) if d.status in HANDLED_STATUSES), None)


def _locate(ctx: AppContext, job: JobOffer) -> JobOffer:
    """Best-effort coordinates for offers whose place is neither known offline nor tied to a department.
    The Géoplateforme only knows France: a foreign place ("Belval, Luxembourg") is never geocoded."""
    if job_coordinates(job) is not None or job_department(job) is not None or not is_place(job.location):
        return job
    if target_rules.offer_country(job) not in (None, target_rules.FRANCE):
        return job
    query = job.location.split(",")[0] if "," in job.location and not re.search(r"\d", job.location) else job.location
    coords = geocode(query)
    if coords is None:
        return job
    located = job.model_copy(update={"latitude": coords[0], "longitude": coords[1]})
    try:
        ctx.repo.save_job(located)
    except Exception as exc:  # noqa: BLE001 — coordinates are a nice-to-have
        log.warning("Coordonnées non enregistrées pour l'offre %s : %s", job.id, exc)
    return located
