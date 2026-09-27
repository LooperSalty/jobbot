"""Night harvest: every relevant company around every search area.

1. ``recherche-entreprises /near_point`` around each French search area (radius = the area's radius +
   ``settings.night.extra_radius_km``, API maximum 50 km) for every relevant NAF code (profile ``naf_codes`` + the
   built-in IT list of :mod:`jobbot.outreach.relevance`), every page up to the API limit (10 000 results; beyond,
   one query per NAF code), ≤ 4 requests/s (shared throttle of :mod:`jobbot.outreach.companies`);
2. « recruteurs La Bonne Alternance » for the developer ROME codes around each French alternance area;
3. employers of the job offers stored in the database (:mod:`jobbot.night.hiring`).

Foreign areas (Luxembourg) have no registry: their employers come from the offers only. Everything goes through
the relevance gate and is deduplicated (:mod:`jobbot.night.store`).
"""

from __future__ import annotations

import logging
import re

import httpx

from jobbot.config import LocationPref, Profile
from jobbot.context import AppContext
from jobbot.http import make_client
from jobbot.models import Company, ContractType
from jobbot.night.budget import Deadline
from jobbot.night.models import HarvestResult
from jobbot.night.store import store_candidates
from jobbot.outreach._registry import DEFAULT_NAF_CODES, TRANCHE_CODES, company_from_result, tranche_codes_from
from jobbot.outreach.companies import (
    MAX_RESULTS,
    PER_PAGE,
    ROMES_DEV,
    CompanySearchError,
    fetch_registry_page,
    lba_company,
    lba_location_ok,
    with_distance_score,
)

log = logging.getLogger(__name__)

MAX_RADIUS_KM = 50  # /near_point accepts 0.001-50 km
IT_NAF_EXTRA = ("61.10Z", "61.20Z", "61.30Z", "61.90Z", "72.19Z")  # telecoms + R&D: digital for the gate
_FULL_NAF = re.compile(r"^\d{2}\.\d{2}[A-Z]$")


def harvest_nafs(profile: Profile) -> list[str]:
    """Full NAF codes of the profile (prefixes such as '62' are covered by the IT list), then the IT list."""
    wanted = [c.strip().upper() for c in profile.target.naf_codes if _FULL_NAF.match(c.strip().upper())]
    return list(dict.fromkeys([*wanted, *DEFAULT_NAF_CODES, *IT_NAF_EXTRA]))


def harvest_radius(loc: LocationPref, extra_km: int) -> int:
    return max(1, min(MAX_RADIUS_KM, loc.radius_km + extra_km))


def french_areas(profile: Profile) -> list[LocationPref]:
    return [loc for loc in profile.target.locations
            if loc.latitude is not None and loc.longitude is not None and (loc.country or "FR").upper() == "FR"]


# --------------------------------------------------------------------------- registry around a point


def _big_enough(company: Company, allowed: frozenset[str] | None) -> bool:
    return allowed is None or str(company.raw.get("tranche_effectif_salarie") or "NN") in allowed


def _page_companies(data: dict, allowed: frozenset[str] | None) -> list[Company]:
    out = []
    for item in data.get("results") or []:
        company = company_from_result(item, set()) if isinstance(item, dict) else None
        if company is not None and _big_enough(company, allowed):
            out.append(company)
    return out


def _next_pages(client: httpx.Client, params: dict, first: dict, deadline: Deadline,
                allowed: frozenset[str] | None) -> tuple[list[Company], bool]:
    """Pages 2… of a query (API cap: 10 000 results); a failing page keeps what was read before it."""
    out: list[Company] = []
    pages = min(int(first.get("total_pages") or 1), MAX_RESULTS // PER_PAGE)
    for page in range(2, pages + 1):
        if deadline.expired():
            return out, True
        try:
            data = fetch_registry_page(client, {**params, "page": page}, "/near_point")
        except CompanySearchError as exc:
            log.warning("Registre : page %d illisible (%s), pages suivantes ignorées", page, exc)
            break
        out.extend(_page_companies(data, allowed))
    return out, False


def near_point(client: httpx.Client, loc: LocationPref, radius: int, nafs: list[str], deadline: Deadline,
               allowed: frozenset[str] | None = None) -> tuple[list[Company], bool]:
    """(companies, stopped) around ``loc``: every page; one query per NAF code beyond the API's 10 000 results."""
    params = {"lat": loc.latitude, "long": loc.longitude, "radius": radius, "activite_principale": ",".join(nafs),
              "per_page": PER_PAGE}
    first = fetch_registry_page(client, {**params, "page": 1}, "/near_point")
    if int(first.get("total_results") or 0) > MAX_RESULTS and len(nafs) > 1:
        return _split_by_naf(client, loc, radius, nafs, deadline, allowed)
    rest, stopped = _next_pages(client, params, first, deadline, allowed)
    return [*_page_companies(first, allowed), *rest], stopped


def _split_by_naf(client: httpx.Client, loc: LocationPref, radius: int, nafs: list[str], deadline: Deadline,
                  allowed: frozenset[str] | None) -> tuple[list[Company], bool]:
    out: list[Company] = []
    for naf in nafs:
        if deadline.expired():
            return out, True
        try:
            found, stopped = near_point(client, loc, radius, [naf], deadline, allowed)
        except CompanySearchError as exc:  # one NAF code failing never loses the others
            log.warning("Registre autour de %s, NAF %s : %s", loc.city, naf, exc)
            continue
        out.extend(found)
        if stopped:
            return out, True
    return out, False


def _allowed_tranches(profile: Profile) -> frozenset[str] | None:
    code = profile.target.min_headcount_code
    return frozenset(tranche_codes_from(code)) if code and code in TRANCHE_CODES else None


def registry_companies(ctx: AppContext, deadline: Deadline, errors: list[str]) -> tuple[list[Company], bool]:
    extra, nafs = ctx.settings.night.extra_radius_km, harvest_nafs(ctx.profile)
    allowed = _allowed_tranches(ctx.profile)
    out: list[Company] = []
    with make_client() as client:
        for loc in french_areas(ctx.profile):
            if deadline.expired():
                return out, True
            try:
                found, stopped = near_point(client, loc, harvest_radius(loc, extra), nafs, deadline, allowed)
            except Exception as exc:  # one area failing never stops the others
                log.warning("Entreprises autour de %s indisponibles : %s", loc.city, exc)
                errors.append(f"registre autour de {loc.city} : {exc}")
                continue
            log.info("Registre : %d entreprise(s) autour de %s", len(found), loc.city)
            out.extend(found)
            if stopped:
                return out, True
    return out, False


# --------------------------------------------------------------------------- La Bonne Alternance


def lba_recruiters(ctx: AppContext, deadline: Deadline, errors: list[str]) -> list[Company]:
    if ContractType.ALTERNANCE not in ctx.profile.target.contract_types:
        return []
    try:
        from jobbot.sources.lba import LaBonneAlternanceSource

        source = LaBonneAlternanceSource(ctx.secrets)
    except Exception as exc:
        errors.append(f"LBA recruteurs : {exc}")
        return []
    out: list[Company] = []
    for loc in french_areas(ctx.profile):
        if deadline.expired() or not lba_location_ok(loc):
            continue
        try:
            radius = harvest_radius(loc, ctx.settings.night.extra_radius_km)
            found = source.search_recruiters(loc.latitude, loc.longitude, radius, list(ROMES_DEV))
            out.extend(lba_company(c) for c in found)
        except Exception as exc:
            log.warning("Recruteurs LBA (%s) indisponibles : %s", loc.city, exc)
            errors.append(f"LBA recruteurs ({loc.city}) : {exc}")
    return out


# --------------------------------------------------------------------------- orchestration


def harvest(ctx: AppContext, deadline: Deadline, *, store_until: Deadline | None = None) -> HarvestResult:
    """Collect every source until ``deadline`` (each isolated), then store what was collected through the
    relevance gate until ``store_until`` (default: ``deadline``) — collected candidates are never thrown away."""
    from jobbot.night.hiring import hiring_companies

    errors: list[str] = []
    registry, stopped = registry_companies(ctx, deadline, errors)
    lba = [] if deadline.expired() else lba_recruiters(ctx, deadline, errors)
    try:
        hiring = [] if deadline.expired() else hiring_companies(ctx, deadline)
    except Exception as exc:
        log.exception("Employeurs des offres : résolution impossible")
        hiring, errors = [], [*errors, f"employeurs des offres : {exc}"]
    stopped = stopped or deadline.expired()
    locations = list(ctx.profile.target.locations)
    candidates = [with_distance_score(c, locations) for c in (*registry, *lba, *hiring)]
    stored = store_candidates(ctx, candidates, store_until or deadline)
    details = [f"Registre : {len(registry)}, recruteurs LBA : {len(lba)}, employeurs des offres : {len(hiring)}"]
    return stored.model_copy(update={"stopped": stopped or stored.stopped, "errors": [*errors, *stored.errors],
                                     "details": details})
