"""Hiring signals: employers of the job offers stored in the database (any status — an offer set aside for
seniority still means the company recruits developers: a great spontaneous target for an apprenticeship).

Offers kept: a developer title (never a school / CFA offer or a sales job), a named employer, a place inside a
search area (+ ``settings.night.extra_radius_km``). French employers are resolved to their SIREN by
:mod:`jobbot.night.siren` (several registry queries — department, postal code, cleaned name variants, no place —
scored by :mod:`jobbot.night.siren_match`; only a confident match, never a guess), most offers first, at most
``settings.night.max_siren_lookups`` registry queries per night; the answer is cached in kv
(``night:siren:<pays>:<nom>``; not identified → looked up again after 7 days). A company is located at its
establishment near the offers (the local agency of a group, not its head office); an IT company without any local
agency (ESN, consulting) is kept at its head office but works at the offer's place (``raw["agency"] =
"absente"``, :mod:`jobbot.night.siren_remote`). Luxembourg has no SIREN: its
employers are kept by name (domain discovery only at enrichment). Each company gets
``raw["hiring_signal"] = [{title, source, date}]`` (merged with what is stored).
"""

from __future__ import annotations

import logging
import re
from collections import Counter

import httpx

from jobbot import clock
from jobbot.context import AppContext
from jobbot.http import make_client
from jobbot.matching.filters import sales_title_reason, school_offer_reason
from jobbot.matching.geo import department_from_location, department_from_postal, haversine_km
from jobbot.matching.target_rules import offer_country
from jobbot.models import Company, JobOffer
from jobbot.night import siren
from jobbot.night.budget import Deadline
from jobbot.night.harvest import harvest_radius
from jobbot.night.siren_match import OfferGroup, Zone
from jobbot.night.store import SIGNAL_KEY, merge_signals
from jobbot.outreach.relevance import is_dev_title
from jobbot.sources.countries import location_country
from jobbot.textutil import normalize_company, stable_id, strip_accents, truncate

__all__ = ["OfferGroup", "eligible", "foreign_company", "hiring_companies", "job_zone", "offer_groups", "zone_of"]

log = logging.getLogger(__name__)

_ANONYMOUS = re.compile(r"confidenti|anonyme|non (?:communiqu|renseign|precis|divulgu)|entreprise du secteur|"
                        r"cabinet de recrutement|^(?:n/?a|-+|\?+|x+)$")


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


def zone_of(ctx: AppContext) -> Zone:
    """Where an employer's establishment counts as local: target departments + French areas (radius + extra)."""
    extra = ctx.settings.night.extra_radius_km
    areas = tuple((loc.latitude, loc.longitude, float(harvest_radius(loc, extra)))
                  for loc in ctx.profile.target.locations
                  if loc.latitude is not None and loc.longitude is not None and location_country(loc) == "FR")
    return Zone(departments=frozenset(_target_departments(ctx)), areas=areas)


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


# --------------------------------------------------------------------------- groups → companies


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


def _excluded(ctx: AppContext, group: OfferGroup) -> bool:
    return ctx.repo.is_blacklisted(group.name) or group.key in {normalize_company(x) for x in
                                                                ctx.profile.target.exclude_companies}


def _known(ctx: AppContext, entry: dict | None) -> Company | None:
    """The stored company of a cached SIREN, at its cached establishment (no registry query)."""
    if not entry or not entry.get("siren"):
        return None
    stored = ctx.repo.get_company(stable_id("company", str(entry["siren"])))
    return siren.with_cached_place(stored, entry) if stored is not None else None


def _french_company(ctx: AppContext, client: httpx.Client, group: OfferGroup, zone: Zone,
                    lookups: siren.Lookups) -> Company | None:
    entry = siren.cached(ctx.repo, group)
    known = _known(ctx, entry)
    if known is not None:
        return _with_signals(known, group)
    if siren.missing_is_fresh(entry) or _excluded(ctx, group) or not lookups.available():
        return None  # not identified recently / excluded / no query left tonight (the rest next night)
    result = siren.resolve(client, group, zone, lookups)
    company = siren.company_of(result.match, group) if result.match is not None else None
    if company is None:
        if result.complete:
            siren.remember_missing(ctx.repo, group, result.tried)
            log.info("Employeur « %s » non identifié avec certitude (%s)", group.name, ", ".join(result.tried))
        return None
    siren.remember_found(ctx.repo, group, result, company)
    where = f"siège ({company.address}), recrute pour {company.city}" if result.match.remote else \
        f"établissement de {company.city}"
    log.info("Employeur « %s » → SIREN %s, %s (%s)%s", group.name, company.siren, where, result.strategy,
             "".join(f" — {n}" for n in result.match.notes))
    return _with_signals(company, group)


def hiring_companies(ctx: AppContext, deadline: Deadline) -> list[Company]:
    """Companies (new or stored) carrying the hiring signals of the stored offers, employers with the most offers
    first; stops at the deadline."""
    out: list[Company] = []
    zone, lookups = zone_of(ctx), siren.Lookups(ctx.settings.night.max_siren_lookups)
    with make_client() as client:
        for group in sorted(offer_groups(ctx), key=lambda g: -len(g.signals)):
            if deadline.expired():
                break
            try:
                company = foreign_company(group) if group.country != "FR" else \
                    _french_company(ctx, client, group, zone, lookups)
            except Exception as exc:  # one employer never stops the others
                log.warning("Employeur « %s » : résolution impossible (%s)", group.name, exc)
                continue
            if company is not None:
                out.append(company)
    capped = " — plafond atteint, la suite la nuit prochaine" if lookups.capped else ""
    log.info("Employeurs des offres : %d entreprise(s) avec un signal de recrutement, %d recherche(s) au registre%s",
             len(out), lookups.used, capped)
    return out
