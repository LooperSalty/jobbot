"""Priority of a spontaneous target (0-100 + French reasons) and the ready queue the day campaign works through.

Points (the night stores them in ``raw["priority"]`` / ``raw["priority_reasons"]``; the campaign sorts by them):

- sector: core IT NAF +30, else adjacent (other digital NAF / label, targeted NAF, IT consulting) +15, else large
  organisation (≥ 250 employees, IT department likely) +10;
- hiring signal: a developer offer published in the last 60 days (``raw["hiring_signal"]``) +25;
- contact quality (the address the campaign would pick): recruitment / HR role address +20, named HR or tech
  manager (website / Hunter) +15, guess from the domain's learned pattern +5, unverified guess -10, no usable
  address → 0 and out of the queue;
- distance to the nearest search area: ≤ 20 km +10, ≤ 40 km +5;
- 10-249 employees (an apprentice needs a tutor) +10;
- profile skills named on the company website: +2 each, 10 max.

Already contacted, blacklisted, excluded, set aside or recently applied → excluded (0).
"""

from __future__ import annotations

import functools
from datetime import datetime, timedelta
from typing import NamedTuple

from jobbot import clock
from jobbot.config import OutreachSettings, Profile
from jobbot.context import AppContext
from jobbot.db import Repository
from jobbot.matching.geo import haversine_km
from jobbot.matching.lexicon import find_skills
from jobbot.models import Company, ContactEmail
from jobbot.outreach._registry import CORE_NAF, SCORE_KEY, TRANCHE_LABELS
from jobbot.outreach.people import is_small_company
from jobbot.outreach.relevance import headcount_min, is_relevant_employer, naf_key, sector_fit
from jobbot.outreach.selection import is_guess, is_nominative, is_role_address, sort_contacts, usable
from jobbot.textutil import normalize_company, truncate

PRIORITY_KEY, REASONS_KEY, AT_KEY = "priority", "priority_reasons", "priority_at"
CORE_POINTS, ADJACENT_POINTS, LARGE_POINTS = 30, 15, 10
HIRING_POINTS, HIRING_DAYS = 25, 60
RECRUITMENT_POINTS, NAMED_POINTS, LEARNED_GUESS_POINTS, UNVERIFIED_GUESS_POINTS = 20, 15, 5, -10
NEAR_KM, NEAR_POINTS, MID_KM, MID_POINTS = 20, 10, 40, 5
TUTOR_MIN, TUTOR_MAX, TUTOR_POINTS = 10, 249, 10
SKILL_POINTS, MAX_SKILL_POINTS = 2, 10
RECENT_DAYS = 60
QUEUE_STATUSES = ("approved", "enriched")
EXCLUDED_STATUSES = {"contacted": "déjà contactée", "blacklisted": "liste noire", "irrelevant": "hors cible",
                     "rejected": "écartée à la validation"}
_CORE_KEYS = frozenset(naf_key(code) for code in CORE_NAF)

Part = tuple[float, str]  # points, French reason ("" when there is nothing to say)


class Ranked(NamedTuple):
    company: Company
    score: float
    reasons: list[str]


# --------------------------------------------------------------------------- parts


def _sector(company: Company, profile: Profile) -> Part:
    code = company.naf_code or company.naf_label or ""
    if naf_key(company.naf_code) in _CORE_KEYS:
        return CORE_POINTS, f"Secteur numérique cœur de cible (NAF {code}) +{CORE_POINTS}"
    fit = sector_fit(company, profile)
    if fit in ("digital", "consulting"):
        return ADJACENT_POINTS, f"Secteur proche du numérique ({truncate(code, 50)}) +{ADJACENT_POINTS}"
    if fit == "large":
        return LARGE_POINTS, f"Grande structure (≥ 250 salariés) : service informatique probable +{LARGE_POINTS}"
    return 0, ""


def _signal_date(signal: dict) -> datetime | None:
    try:
        return clock.parse_dt(str(signal.get("date") or "")) if isinstance(signal, dict) else None
    except ValueError:
        return None


def _hiring(company: Company, now: datetime) -> Part:
    since = now - timedelta(days=HIRING_DAYS)
    dated = [(d, s) for s in company.raw.get("hiring_signal") or [] if (d := _signal_date(s)) and d >= since]
    if not dated:
        return 0, ""
    when, latest = max(dated, key=lambda pair: pair[0])
    title = truncate(str(latest.get("title") or "offre de développeur"), 60)
    return HIRING_POINTS, (f"Recrute des développeurs : « {title} » "
                           f"({clock.to_paris(when):%d/%m}) +{HIRING_POINTS}")


def _contact(company: Company, contact: ContactEmail) -> Part:
    if contact.kind == "recruitment" or (contact.kind == "hr" and is_role_address(contact.email)):
        return RECRUITMENT_POINTS, f"Adresse recrutement / RH ({contact.email}) +{RECRUITMENT_POINTS}"
    if is_guess(contact):
        if company.raw.get("email_pattern"):
            return LEARNED_GUESS_POINTS, (f"Adresse devinée d'après le format appris du domaine ({contact.email}) "
                                          f"+{LEARNED_GUESS_POINTS}")
        return UNVERIFIED_GUESS_POINTS, f"Adresse devinée non vérifiée ({contact.email}) {UNVERIFIED_GUESS_POINTS}"
    if contact.kind in ("hr", "tech") and is_nominative(contact):
        role = "RH" if contact.kind == "hr" else "responsable technique"
        return NAMED_POINTS, f"Contact {role} nommé ({contact.person_name or contact.email}) +{NAMED_POINTS}"
    return 0, f"Adresse générale ({contact.email})"


def nearest_km(company: Company, profile: Profile) -> tuple[float, str] | None:
    """(km, city) of the nearest search area, from coordinates, else the distance stored at discovery."""
    if company.latitude is not None and company.longitude is not None:
        dists = [(haversine_km(company.latitude, company.longitude, loc.latitude, loc.longitude), loc.city)
                 for loc in profile.target.locations if loc.latitude is not None and loc.longitude is not None]
        if dists:
            return min(dists)
    stored = company.raw.get("distance_km")
    return (float(stored), "ta zone") if isinstance(stored, (int, float)) else None


def _distance(company: Company, profile: Profile) -> Part:
    found = nearest_km(company, profile)
    if found is None:
        return 0, ""
    km, city = found
    points = 0
    if km <= NEAR_KM:
        points = NEAR_POINTS
    elif km <= MID_KM:
        points = MID_POINTS
    return points, f"À {km:.0f} km de {city}" + (f" +{points}" if points else "")


def _size(company: Company) -> Part:
    size = headcount_min(company)
    if size is None or not TUTOR_MIN <= size <= TUTOR_MAX:
        return 0, ""
    label = company.headcount_range or TRANCHE_LABELS.get(str(company.raw.get("tranche_effectif_salarie")), "")
    return TUTOR_POINTS, f"Taille adaptée au tutorat d'un alternant ({label or f'{size}+ salariés'}) +{TUTOR_POINTS}"


@functools.lru_cache(maxsize=8)
def _skills_of(text: str) -> frozenset[str]:
    return frozenset(find_skills(text))


def _skills(company: Company, profile: Profile) -> Part:
    site = set(company.raw.get("site_skills") or []) or find_skills(company.description or "")
    common = sorted(site & _skills_of(", ".join(profile.all_skills())))
    points = min(MAX_SKILL_POINTS, SKILL_POINTS * len(common))
    return (points, f"Compétences du profil sur son site ({', '.join(common[:5])}) +{points}") if points else (0, "")


# --------------------------------------------------------------------------- priority


def exclusion(company: Company, profile: Profile, repo: Repository) -> str | None:
    """Why ``company`` must not be written to (French), else None."""
    if company.status in EXCLUDED_STATUSES:
        return EXCLUDED_STATUSES[company.status]
    if repo.is_blacklisted(company.name, company.siren or "", company.domain or ""):
        return "liste noire"
    if normalize_company(company.name) in {normalize_company(x) for x in profile.target.exclude_companies}:
        return "entreprise exclue"
    if repo.recently_applied_to_company(company.name, RECENT_DAYS):
        return f"candidature envoyée il y a moins de {RECENT_DAYS} jours"
    return None


def best_contact(company: Company, repo: Repository, cfg: OutreachSettings) -> ContactEmail | None:
    """The address the campaign would pick (daily caps ignored)."""
    for contact in sort_contacts(list(company.contacts), small_company=is_small_company(company)):
        if usable(contact, cfg) and not repo.is_blacklisted(contact.email, contact.email.split("@", 1)[-1]):
            return contact
    return None


def potential(company: Company, profile: Profile, *, now: datetime | None = None) -> float:
    """Priority without the contact part (order of enrichment: the most promising companies first)."""
    at = now or clock.now()
    parts = (_sector(company, profile), _hiring(company, at), _distance(company, profile), _size(company))
    return float(sum(points for points, _ in parts))


def company_priority(company: Company, profile: Profile, repo: Repository, *, cfg: OutreachSettings | None = None,
                     now: datetime | None = None) -> tuple[float, list[str]]:
    """0-100 priority and its French reasons; 0 = not in the queue (excluded or no usable address)."""
    reason = exclusion(company, profile, repo)
    if reason:
        return 0.0, [f"Exclue : {reason}"]
    contact = best_contact(company, repo, cfg or OutreachSettings())
    if contact is None:
        return 0.0, ["Aucune adresse utilisable : hors file d'attente"]
    parts = [_sector(company, profile), _hiring(company, now or clock.now()), _contact(company, contact),
             _distance(company, profile), _size(company), _skills(company, profile)]
    score = max(1.0, min(100.0, float(sum(points for points, _ in parts))))
    return score, [text for _, text in parts if text]


def with_priority(company: Company, score: float, reasons: list[str]) -> Company:
    raw = {**company.raw, PRIORITY_KEY: score, REASONS_KEY: reasons, AT_KEY: clock.iso(clock.now())}
    return company.model_copy(update={"raw": raw})


def _outreach_score(company: Company) -> int:
    try:
        return int(company.raw.get(SCORE_KEY, 0))
    except (TypeError, ValueError):
        return 0


def ready_queue(ctx: AppContext, *, now: datetime | None = None) -> list[Ranked]:
    """Companies the campaign can write to, validated ones first, then by priority (then discovery score)."""
    ranked: list[Ranked] = []
    for company in ctx.repo.list_companies(QUEUE_STATUSES):
        if not is_relevant_employer(company, ctx.profile)[0]:
            continue
        score, reasons = company_priority(company, ctx.profile, ctx.repo, cfg=ctx.settings.outreach, now=now)
        if score > 0:
            ranked.append(Ranked(company, score, reasons))
    return sorted(ranked, key=lambda r: (r.company.status != "approved", -r.score, -_outreach_score(r.company)))
