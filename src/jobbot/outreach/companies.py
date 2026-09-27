"""Company discovery for spontaneous applications.

Source: ``recherche-entreprises.api.gouv.fr`` (keyless, 7 req/s per IP — we stay ≤ 4 req/s), sharded by
department × NAF code, plus — when the LBA source module is available — La Bonne Alternance "recruteurs"
(companies likely to hire apprentices). Result mapping lives in :mod:`jobbot.outreach._registry`. Candidates
outside the target (public works, HVAC… for a developer: :mod:`jobbot.outreach.relevance`) are never stored.
"""

from __future__ import annotations

import logging
import math
import re

import httpx
from pydantic import BaseModel, ConfigDict, Field

from jobbot.config import LocationPref
from jobbot.context import AppContext
from jobbot.db import Repository
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import Company, ContractType
from jobbot.outreach._registry import (
    DEFAULT_NAF_CODES,
    NAF_LABELS,
    SCORE_KEY,
    TRANCHE_CODES,
    TRANCHE_LABELS,
    base_score,
    company_from_result,
    name_candidates,
    person_leaders,
    tranche_codes_from,
)
from jobbot.outreach.relevance import is_relevant_employer, set_aside
from jobbot.textutil import normalize_company, stable_id

__all__ = [
    "DEFAULT_NAF_CODES", "NAF_LABELS", "ROMES_DEV", "TRANCHE_CODES", "TRANCHE_LABELS", "CompanySearchError",
    "DiscoverReport", "base_score", "company_from_result", "discover_companies", "distance_km", "name_candidates",
    "person_leaders", "search_companies", "tranche_codes_from",
]

log = logging.getLogger(__name__)

API_BASE = "https://recherche-entreprises.api.gouv.fr"
API_UA = "jobbot/0.1 (outil personnel de recherche d'emploi; python-httpx)"
PER_PAGE = 25  # API maximum
MAX_RESULTS = 10_000  # page * per_page cap of the API
MAX_GEOCODE_PER_RUN = 25
ROMES_DEV = ("M1855", "M1805", "M1827", "M1818", "M1836", "M1852", "M1853")  # ROME 4.0, dev jobs
LBA_BONUS = 4  # LBA flags these companies as likely to hire apprentices

_THROTTLE = HostThrottle(0.25)


class CompanySearchError(RuntimeError):
    """The company registry API refused or failed a query."""


class DiscoverReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    found: int
    new: int
    irrelevant: int = 0  # found but outside the target (not stored; already stored ones set aside)
    errors: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- API search


def search_companies(
    naf_code: str,
    department: str,
    *,
    min_headcount_code: str | None,
    client: httpx.Client | None = None,
    max_pages: int = 5,
) -> list[Company]:
    """Active (non sole-trader) companies of ``naf_code`` (comma list allowed) with an active establishment
    in ``department``. Raises :class:`CompanySearchError` when the API rejects or fails the query."""
    params: dict[str, str | int] = {
        "activite_principale": naf_code,
        "departement": department,
        "etat_administratif": "A",
        "est_entrepreneur_individuel": "false",
        "per_page": PER_PAGE,
    }
    if min_headcount_code:
        params["tranche_effectif_salarie"] = ",".join(tranche_codes_from(min_headcount_code))
    own = client is None
    http = client or make_client()
    try:
        return _collect_pages(http, params, {department}, max_pages)
    finally:
        if own:
            http.close()


def _collect_pages(client: httpx.Client, params: dict, departments: set[str], max_pages: int) -> list[Company]:
    companies: list[Company] = []
    for page in range(1, max(1, max_pages) + 1):
        data = _fetch_page(client, {**params, "page": page})
        for item in data.get("results") or []:
            company = company_from_result(item, departments) if isinstance(item, dict) else None
            if company is not None:
                companies.append(company)
        if page >= int(data.get("total_pages") or 0) or page * PER_PAGE >= MAX_RESULTS:
            break
    return companies


def _fetch_page(client: httpx.Client, params: dict) -> dict:
    try:
        resp = request_with_retry(
            client, "GET", f"{API_BASE}/search", params=params, headers={"User-Agent": API_UA},
            throttle=_THROTTLE, retries=4, backoff_s=1.0,
        )
    except httpx.HTTPError as exc:
        raise CompanySearchError(f"API recherche-entreprises injoignable : {exc}") from exc
    if resp.status_code != 200:
        raise CompanySearchError(f"API recherche-entreprises : HTTP {resp.status_code} — {_api_error(resp)}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise CompanySearchError("Réponse non JSON de l'API recherche-entreprises") from exc
    if not isinstance(data, dict):
        raise CompanySearchError("Réponse inattendue de l'API recherche-entreprises")
    return data


def _api_error(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("erreur", ""))[:300]
    except (ValueError, AttributeError):
        return resp.text[:200]


# --------------------------------------------------------------------------- distance & scoring


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _distances(company: Company, locations: list[LocationPref]) -> list[tuple[float, LocationPref]]:
    if company.latitude is None or company.longitude is None:
        return []
    return [
        (distance_km(company.latitude, company.longitude, loc.latitude, loc.longitude), loc)
        for loc in locations
        if loc.latitude is not None and loc.longitude is not None
    ]


def _within_reach(company: Company, locations: list[LocationPref]) -> bool:
    """True without coordinates (department filter already applies), else within one location's radius."""
    dists = _distances(company, locations)
    return not dists or any(d <= loc.radius_km for d, loc in dists)


def _distance_bonus(dist_km: float) -> int:
    if dist_km < 15:
        return 2
    return 1 if dist_km < 30 else 0


def _score_of(company: Company) -> int:
    try:
        return int(company.raw.get(SCORE_KEY, 0))
    except (TypeError, ValueError):
        return 0


def _with_distance_score(company: Company, locations: list[LocationPref]) -> Company:
    dists = _distances(company, locations)
    if not dists:
        return company
    nearest = round(min(d for d, _ in dists), 1)
    base = _score_of(company) - int(company.raw.get("distance_bonus", 0) or 0)
    bonus = _distance_bonus(nearest)
    raw = {**company.raw, SCORE_KEY: base + bonus, "distance_km": nearest, "distance_bonus": bonus}
    return company.model_copy(update={"raw": raw})


# --------------------------------------------------------------------------- discovery helpers


def _rejection(ctx: AppContext, company: Company) -> str | None:
    if ctx.repo.is_blacklisted(company.name, company.siren or "", company.domain or ""):
        return "liste noire"
    if normalize_company(company.name) in {normalize_company(x) for x in ctx.profile.target.exclude_companies}:
        return "entreprise exclue"
    if not _within_reach(company, list(ctx.profile.target.locations)):
        return "trop loin"
    return None


def _merge(existing: Company, fresh: Company) -> Company:
    """Refresh descriptive fields, never status/contacts/domain found by enrichment."""
    update: dict = {
        "naf_code": fresh.naf_code or existing.naf_code,
        "naf_label": fresh.naf_label or existing.naf_label,
        "headcount_range": fresh.headcount_range or existing.headcount_range,
        "leaders": fresh.leaders or existing.leaders,
        "raw": {**existing.raw, **fresh.raw, SCORE_KEY: max(_score_of(existing), _score_of(fresh))},
    }
    if existing.latitude is None and fresh.latitude is not None:
        update.update(latitude=fresh.latitude, longitude=fresh.longitude)
    if fresh.source == "lba_recruteur":
        update["source"] = fresh.source
    return existing.model_copy(update=update)


def _store(repo: Repository, company: Company) -> bool:
    existing = repo.get_company(company.id)
    if existing is None:
        repo.upsert_company(company)
        return True
    repo.upsert_company(_merge(existing, company))
    return False


def _combined(companies: list[Company]) -> list[Company]:
    """One candidate per company (same SIREN from several departments or sources), best-scored first and used as the
    base, the others' details merged in before judging it: an LBA recruiter gets the registry's NAF and headcount."""
    merged: dict[str, Company] = {}
    for company in companies:
        merged[company.id] = _merge(merged[company.id], company) if company.id in merged else company
    return list(merged.values())


def _off_target(ctx: AppContext, company: Company) -> str | None:
    """French reason when ``company`` is outside the target; an already stored one still waiting is set aside."""
    relevant, reason = is_relevant_employer(company, ctx.profile)
    if relevant:
        return None
    try:
        stored = ctx.repo.get_company(company.id)
        if stored is not None:
            set_aside(ctx.repo, stored, reason)
    except Exception:
        log.exception("Entreprise hors cible %s non marquée", company.name)
    return reason


def _from_lba(company: Company) -> Company:
    """Key LBA recruiters by SIREN (dedupe with the registry) and give them their bonus."""
    update: dict = {"raw": {**company.raw, SCORE_KEY: _score_of(company) + LBA_BONUS}}
    if company.siren and re.fullmatch(r"\d{9}", company.siren):
        update["id"] = stable_id("company", company.siren)
    return company.model_copy(update=update)


def _departments(ctx: AppContext, departments: list[str] | None) -> list[str]:
    if departments:
        return list(departments)
    if ctx.profile.target.departments:
        return list(ctx.profile.target.departments)
    return sorted({loc.postal_code[:2] for loc in ctx.profile.target.locations if loc.postal_code})


def _api_companies(ctx: AppContext, nafs: list[str], deps: list[str], errors: list[str]) -> list[Company]:
    out: list[Company] = []
    with make_client() as client:
        for dep in deps:
            for naf in nafs:
                try:
                    out.extend(search_companies(
                        naf, dep, min_headcount_code=ctx.profile.target.min_headcount_code, client=client,
                    ))
                except Exception as exc:  # one shard failing never stops the others
                    log.warning("Recherche d'entreprises NAF %s / dépt %s échouée : %s", naf, dep, exc)
                    errors.append(f"NAF {naf} / dépt {dep} : {exc}")
    return out


def _lba_location(loc: LocationPref) -> bool:
    """LBA is French alternance only: skip foreign or alternance-less locations (e.g. Luxembourg, CDI only)."""
    if (loc.country or "FR").upper() != "FR":
        return False
    return not loc.contract_types or ContractType.ALTERNANCE in loc.contract_types


def _lba_companies(ctx: AppContext, errors: list[str]) -> list[Company]:
    if ContractType.ALTERNANCE not in ctx.profile.target.contract_types:
        return []
    try:
        from jobbot.sources.lba import LaBonneAlternanceSource
    except ImportError:
        log.info("Source La Bonne Alternance indisponible : recruteurs LBA ignorés")
        return []
    out: list[Company] = []
    try:
        source = LaBonneAlternanceSource(ctx.secrets)
    except Exception as exc:
        errors.append(f"LBA recruteurs : {exc}")
        return []
    for loc in ctx.profile.target.locations:
        if loc.latitude is None or loc.longitude is None or not _lba_location(loc):
            continue
        try:
            found = source.search_recruiters(loc.latitude, loc.longitude, loc.radius_km, list(ROMES_DEV))
            out.extend(_from_lba(c) for c in found)
        except Exception as exc:
            log.warning("Recruteurs LBA (%s) indisponibles : %s", loc.city, exc)
            errors.append(f"LBA recruteurs ({loc.city}) : {exc}")
    return out


def _geocoded(company: Company) -> Company:
    from jobbot.outreach.domains import geocode_address

    point = geocode_address(company.address or "", postcode=company.postal_code)
    if point is None:
        return company
    return company.model_copy(update={"latitude": point[0], "longitude": point[1]})


# --------------------------------------------------------------------------- discovery


def discover_companies(
    ctx: AppContext,
    *,
    naf_codes: list[str] | None = None,
    departments: list[str] | None = None,
    limit: int = 300,
) -> DiscoverReport:
    """Find companies to contact spontaneously and upsert them, best-scored first (existing companies keep
    their status, contacts and domain). Companies outside the target (:mod:`jobbot.outreach.relevance`),
    blacklisted, excluded and too-distant ones are skipped."""
    errors: list[str] = []
    nafs = list(naf_codes or ctx.profile.target.naf_codes or DEFAULT_NAF_CODES)
    candidates = _api_companies(ctx, nafs, _departments(ctx, departments), errors) + _lba_companies(ctx, errors)
    locations = list(ctx.profile.target.locations)
    scored = sorted((_with_distance_score(c, locations) for c in candidates), key=lambda c: -_score_of(c))
    accepted: set[str] = set()
    new = geocoded = irrelevant = 0
    for company in _combined(scored):
        if len(accepted) >= limit:
            break
        off = _off_target(ctx, company)
        if off:
            irrelevant += 1
            log.debug("Entreprise %s hors cible : %s", company.name, off)
            continue
        if company.latitude is None and company.address and geocoded < MAX_GEOCODE_PER_RUN:
            geocoded += 1
            company = _with_distance_score(_geocoded(company), locations)
        reason = _rejection(ctx, company)
        if reason:
            log.debug("Entreprise %s ignorée : %s", company.name, reason)
            continue
        try:
            new += int(_store(ctx.repo, company))
            accepted.add(company.id)
        except Exception as exc:
            log.exception("Enregistrement de %s impossible", company.name)
            errors.append(f"{company.name} : {exc}")
    log.info("Découverte : %d entreprises retenues dont %d nouvelles, %d hors cible", len(accepted), new, irrelevant)
    return DiscoverReport(found=len(accepted), new=new, irrelevant=irrelevant, errors=errors)
