"""Hiring signals: employers of the job offers stored in the database (any status — an offer set aside for
seniority still means the company recruits developers: a great spontaneous target for an apprenticeship).

Offers kept: a developer title (never a school / CFA offer or a sales job), a named employer, a place inside a
search area (+ ``settings.night.extra_radius_km``). French employers are resolved to their SIREN through
``recherche-entreprises /search`` (name + department; rapidfuzz, high confidence only — an ambiguous or weak match
is dropped); the answer is cached in kv (``night:siren:<pays>:<nom>``). Luxembourg has no SIREN: its employers
are kept by name (domain discovery only at enrichment). Each company gets
``raw["hiring_signal"] = [{title, source, date}]`` (merged with what is stored).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

import httpx
from rapidfuzz import fuzz

from jobbot import clock
from jobbot.context import AppContext
from jobbot.http import make_client
from jobbot.matching.filters import sales_title_reason, school_offer_reason
from jobbot.matching.geo import department_from_location, department_from_postal, haversine_km
from jobbot.matching.target_rules import offer_country
from jobbot.models import Company, JobOffer
from jobbot.night.budget import Deadline
from jobbot.night.harvest import harvest_radius
from jobbot.night.store import SIGNAL_KEY, merge_signals
from jobbot.outreach._registry import company_from_result, name_candidates
from jobbot.outreach.companies import fetch_registry_page
from jobbot.outreach.relevance import is_dev_title
from jobbot.sources.countries import location_country
from jobbot.textutil import normalize_company, stable_id, strip_accents, truncate

log = logging.getLogger(__name__)

CACHE_KEY = "night:siren:"
RECHECK_DAYS = 30  # unresolved (or not stored) employers are looked up again after this
ACCEPT_SCORE = 90.0  # name similarity needed to accept a registry match…
PLACE_SCORE = 85.0  # …or this much when the establishment is in the offer's postal code / city
MARGIN = 5.0  # a second company this close to the best one = ambiguous → rejected
SEARCH_RESULTS = 10
_ANONYMOUS = re.compile(r"confidenti|anonyme|non (?:communiqu|renseign|precis|divulgu)|entreprise du secteur|"
                        r"cabinet de recrutement|^(?:n/?a|-+|\?+|x+)$")


@dataclass(frozen=True)
class OfferGroup:
    """Offers of one employer (normalised name + country) inside the search areas."""

    name: str
    key: str
    country: str
    department: str | None
    postal_code: str | None
    city: str
    latitude: float | None
    longitude: float | None
    domain: str | None
    signals: tuple[dict, ...]


# --------------------------------------------------------------------------- offers → groups


def signal_of(job: JobOffer) -> dict:
    when = job.published_at or job.found_at
    return {"title": truncate(job.title, 120), "source": job.source, "date": clock.iso(when)}


def eligible(job: JobOffer) -> bool:
    """A developer offer of a named employer (not a school / CFA, not a sales job)."""
    name = strip_accents(job.company or "").strip().lower()
    if len(name) < 2 or _ANONYMOUS.search(name):
        return False
    if school_offer_reason(job) or sales_title_reason(job):
        return False
    return is_dev_title(job.title)


def _target_departments(ctx: AppContext) -> set[str]:
    deps = {d.upper() for d in ctx.profile.target.departments}
    deps.update(d for loc in ctx.profile.target.locations if (d := department_from_postal(loc.postal_code)))
    return deps


def job_zone(ctx: AppContext, job: JobOffer) -> str | None:
    """Country code of the offer when it lies inside a search area (radius + night margin), else None."""
    country = offer_country(job)
    country = "FR" if country in (None, "FR") else country
    areas = [loc for loc in ctx.profile.target.locations if location_country(loc) == country]
    if not areas:
        return None
    extra = ctx.settings.night.extra_radius_km
    points = [loc for loc in areas if loc.latitude is not None and loc.longitude is not None]
    if job.latitude is not None and job.longitude is not None and points:
        near = any(haversine_km(job.latitude, job.longitude, loc.latitude, loc.longitude) <= harvest_radius(loc, extra)
                   for loc in points)
        return country if near else None
    if country != "FR":
        return country
    dept = department_from_postal(job.postal_code) or department_from_location(job.location)
    return "FR" if dept and dept.upper() in _target_departments(ctx) else None


def _group(name_jobs: list[JobOffer], country: str) -> OfferGroup:
    first = name_jobs[0]
    depts = Counter(d for j in name_jobs if (d := department_from_postal(j.postal_code)
                                               or department_from_location(j.location)))
    postal = next((j.postal_code for j in name_jobs if j.postal_code), None)
    located = next((j for j in name_jobs if j.latitude is not None and j.longitude is not None), first)
    return OfferGroup(
        name=first.company.strip(), key=normalize_company(first.company), country=country,
        department=depts.most_common(1)[0][0] if depts else None, postal_code=postal,
        city=(first.location or "").split("(")[0].split(",")[0].strip(), latitude=located.latitude,
        longitude=located.longitude, domain=next((j.company_domain for j in name_jobs if j.company_domain), None),
        signals=tuple(merge_signals([signal_of(j) for j in name_jobs])),
    )


def offer_groups(ctx: AppContext) -> list[OfferGroup]:
    """Employers of the eligible offers inside the search areas, grouped by normalised name and country."""
    grouped: dict[tuple[str, str], list[JobOffer]] = {}
    for job in ctx.repo.list_jobs():
        if not eligible(job):
            continue
        country = job_zone(ctx, job)
        key = normalize_company(job.company)
        if country and key:
            grouped.setdefault((key, country), []).append(job)
    return [_group(jobs, country) for (_, country), jobs in grouped.items()]


# --------------------------------------------------------------------------- SIREN resolution


def _names(item: dict) -> list[str]:
    return [n for n in [*name_candidates(item), str(item.get("nom_complet") or "")] if n]


def name_score(name: str, item: dict) -> float:
    wanted = normalize_company(name)
    scores = [max(fuzz.ratio(wanted, other), fuzz.token_sort_ratio(wanted, other))
              for other in (normalize_company(n) for n in _names(item)) if other]
    return max(scores, default=0.0)


def _same_place(group: OfferGroup, item: dict) -> bool:
    places = [*(item.get("matching_etablissements") or []), item.get("siege") or {}]
    city = normalize_company(group.city)
    return any((group.postal_code and p.get("code_postal") == group.postal_code)
               or (city and normalize_company(str(p.get("libelle_commune") or "")) == city) for p in places)


def best_match(group: OfferGroup, items: list[dict]) -> dict | None:
    """The registry result naming this employer, only with high confidence (clear winner, high similarity)."""
    best: dict[str, tuple[float, dict]] = {}
    for item in items:
        siren = str(item.get("siren") or "") if isinstance(item, dict) else ""
        if re.fullmatch(r"\d{9}", siren):
            score = name_score(group.name, item)
            if siren not in best or score > best[siren][0]:
                best[siren] = (score, item)
    ranked = sorted(best.values(), key=lambda pair: -pair[0])
    if not ranked:
        return None
    top_score, top = ranked[0]
    if top_score < (PLACE_SCORE if _same_place(group, top) else ACCEPT_SCORE):
        return None
    if len(ranked) > 1 and ranked[1][0] >= top_score - MARGIN:
        return None  # two companies answer to that name: never guess
    return top


def resolve(client: httpx.Client, group: OfferGroup) -> dict | None:
    params: dict[str, str | int] = {"q": group.name, "etat_administratif": "A", "per_page": SEARCH_RESULTS}
    if group.department:
        params["departement"] = group.department
    data = fetch_registry_page(client, params)
    return best_match(group, [r for r in data.get("results") or [] if isinstance(r, dict)])


# --------------------------------------------------------------------------- groups → companies


def _cache_key(group: OfferGroup) -> str:
    return f"{CACHE_KEY}{group.country.lower()}:{group.key}"


def _cached(ctx: AppContext, group: OfferGroup) -> dict | None:
    try:
        data = json.loads(ctx.repo.kv_get(_cache_key(group)) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _remember(ctx: AppContext, group: OfferGroup, siren: str | None) -> None:
    ctx.repo.kv_set(_cache_key(group), json.dumps({"siren": siren, "at": clock.iso(clock.now())}))


def _fresh_cache(entry: dict | None) -> bool:
    at = clock.parse_dt(str(entry.get("at") or "")) if entry else None
    return at is not None and clock.now() - at < timedelta(days=RECHECK_DAYS)


def _with_signals(company: Company, group: OfferGroup) -> Company:
    raw = {**company.raw, SIGNAL_KEY: merge_signals(company.raw.get(SIGNAL_KEY), list(group.signals))}
    update: dict = {"raw": raw}
    if group.domain and not company.website:
        update["website"] = group.domain
    return company.model_copy(update=update)


def foreign_company(group: OfferGroup) -> Company:
    """Employer without SIREN (Luxembourg): kept by name, its website found at enrichment."""
    return Company(
        id=stable_id("company", group.country.lower(), group.key), name=group.name, city=group.city,
        latitude=group.latitude, longitude=group.longitude, website=group.domain, source="offre_emploi",
        found_at=clock.now(), raw={"country": group.country, "names": [group.name],
                                   SIGNAL_KEY: list(group.signals)},
    )


def _french_company(ctx: AppContext, client: httpx.Client, group: OfferGroup) -> Company | None:
    entry = _cached(ctx, group)
    siren = entry.get("siren") if entry else None
    if siren:
        stored = ctx.repo.get_company(stable_id("company", str(siren)))
        if stored is not None:
            return _with_signals(stored, group)  # known employer: no registry call
    elif _fresh_cache(entry):
        return None  # not identified recently: looked up again after RECHECK_DAYS
    if ctx.repo.is_blacklisted(group.name) or group.key in {normalize_company(x) for x in
                                                            ctx.profile.target.exclude_companies}:
        return None
    item = resolve(client, group)
    _remember(ctx, group, str(item["siren"]) if item else None)
    if item is None:
        log.info("Employeur « %s » non identifié avec certitude dans le registre", group.name)
        return None
    company = company_from_result(item, {group.department} if group.department else set()) \
        or company_from_result(item, set())
    return _with_signals(company, group) if company is not None else None


def hiring_companies(ctx: AppContext, deadline: Deadline) -> list[Company]:
    """Companies (new or stored) carrying the hiring signals of the stored offers; stops at the deadline."""
    out: list[Company] = []
    with make_client() as client:
        for group in offer_groups(ctx):
            if deadline.expired():
                break
            try:
                company = foreign_company(group) if group.country != "FR" else _french_company(ctx, client, group)
            except Exception as exc:  # one employer never stops the others
                log.warning("Employeur « %s » : résolution impossible (%s)", group.name, exc)
                continue
            if company is not None:
                out.append(company)
    log.info("Employeurs des offres : %d entreprise(s) avec un signal de recrutement", len(out))
    return out
