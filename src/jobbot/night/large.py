"""« Grandes entreprises » (``settings.outreach.large_companies``): ETI / GE of any sector with a big local site.

``recherche-entreprises /search`` per department of ``profile.target.departments`` (``categorie_entreprise=ETI,GE``,
active companies, up to 100 matching establishments each; ``/near_point`` ignores that filter), every page up to
the API limit, ≤ 4 requests/s (shared throttle of :mod:`jobbot.outreach.companies`). A company is kept when its
largest active establishment matching the department has at least ``min_site_tranche`` employees (a regional head
office, a plant, a big office — not a bank branch or a shop), lies within ``max_km`` of the center (the first
French search area: Metz) and its sector is neither field work (:data:`jobbot.outreach.relevance.FIELD_DIVISIONS`)
nor ``skip_divisions`` (car dealers, stores, hotels, care homes, personal services). That establishment becomes the
company's place; a company found in several departments is kept once, at its site nearest to the center. Outside
the digital sector it is flagged ``large_target``: its letter aims at its internal IT department. Everything then
goes through the relevance gate (large organisation: an IT department is likely) like every other source.
"""

from __future__ import annotations

import logging

import httpx

from jobbot.config import LargeCompanies, Profile
from jobbot.context import AppContext
from jobbot.http import make_client
from jobbot.matching.geo import department_from_postal, haversine_km
from jobbot.models import Company
from jobbot.night.budget import Deadline
from jobbot.outreach._registry import TRANCHE_CODES, company_from_result, tranche_codes_from
from jobbot.outreach.companies import MAX_RESULTS, PER_PAGE, CompanySearchError, fetch_registry_page
from jobbot.outreach.priority import LARGE_FLAG, center_of
from jobbot.outreach.relevance import DIGITAL_NAF_PREFIXES, FIELD_DIVISIONS, naf_key

log = logging.getLogger(__name__)

CATEGORIES = "ETI,GE"
MATCHING_LIMIT = 100  # establishments returned per company (API default: 10)
SITE_TRANCHE = "site_tranche"  # INSEE size code of the local site kept


def _coords(site: dict) -> tuple[float, float] | None:
    try:
        return float(site["latitude"]), float(site["longitude"])
    except (KeyError, TypeError, ValueError):  # absent or « [NON-DIFFUSIBLE] »
        return None


def _big_site(item: dict, allowed: frozenset[str]) -> dict | None:
    """Largest active establishment with coordinates and at least the minimum size, else None (the API returns
    only the establishments matching the department asked for)."""
    sites = [s for s in item.get("matching_etablissements") or []
             if s.get("etat_administratif", "A") == "A" and str(s.get("tranche_effectif_salarie")) in allowed
             and _coords(s) is not None]
    return max(sites, key=lambda s: TRANCHE_CODES.index(str(s["tranche_effectif_salarie"])), default=None)


def large_company(item: dict, cfg: LargeCompanies, center: tuple[float, float]) -> Company | None:
    """The company of one registry result, placed at its big local site; None when it is not a target."""
    division = str(item.get("activite_principale") or "")[:2]
    if division in FIELD_DIVISIONS or division in set(cfg.skip_divisions):
        return None
    site = _big_site(item, frozenset(tranche_codes_from(cfg.min_site_tranche)))
    coords = _coords(site) if site is not None else None
    if site is None or coords is None or haversine_km(*coords, *center) > cfg.max_km:
        return None
    company = company_from_result({**item, "matching_etablissements": [site]}, set())
    if company is None:
        return None
    raw = {**company.raw, SITE_TRANCHE: str(site["tranche_effectif_salarie"])}
    if not naf_key(item.get("activite_principale")).startswith(DIGITAL_NAF_PREFIXES):
        raw[LARGE_FLAG] = True
    return company.model_copy(update={"raw": raw})


def _departments(profile: Profile) -> list[str]:
    wanted = [d.strip() for d in profile.target.departments if d and d.strip()]
    if wanted:
        return wanted
    first = next((loc for loc in profile.target.locations if (loc.country or "FR").upper() == "FR"), None)
    department = department_from_postal(first.postal_code) if first is not None else None
    return [department] if department else []


def _page_companies(data: dict, cfg: LargeCompanies, center: tuple[float, float]) -> list[Company]:
    out: list[Company] = []
    for item in data.get("results") or []:
        try:
            company = large_company(item, cfg, center) if isinstance(item, dict) else None
        except Exception:  # one malformed result never loses the page
            log.debug("Grandes entreprises : résultat illisible ignoré", exc_info=True)
            continue
        if company is not None:
            out.append(company)
    return out


def _department(client: httpx.Client, department: str, cfg: LargeCompanies, center: tuple[float, float],
                deadline: Deadline) -> tuple[list[Company], bool]:
    params = {"departement": department, "categorie_entreprise": CATEGORIES, "etat_administratif": "A",
              "limite_matching_etablissements": MATCHING_LIMIT, "per_page": PER_PAGE}
    first = fetch_registry_page(client, {**params, "page": 1}, "/search")
    out = _page_companies(first, cfg, center)
    try:
        pages = min(int(first.get("total_pages") or 1), MAX_RESULTS // PER_PAGE)
    except (TypeError, ValueError):
        pages = 1
    for page in range(2, pages + 1):
        if deadline.expired():
            return out, True
        try:
            data = fetch_registry_page(client, {**params, "page": page}, "/search")
        except CompanySearchError as exc:
            log.warning("Grandes entreprises (%s) : page %d illisible (%s), pages suivantes ignorées",
                        department, page, exc)
            break
        out.extend(_page_companies(data, cfg, center))
    return out, False


def _nearest_once(found: list[Company], center: tuple[float, float]) -> list[Company]:
    """One candidate per company (found in several departments): its site nearest to the center."""
    def km(company: Company) -> float:
        return haversine_km(company.latitude, company.longitude, *center)  # type: ignore[arg-type]

    best: dict[str, Company] = {}
    for company in found:
        if company.id not in best or km(company) < km(best[company.id]):
            best[company.id] = company
    return list(best.values())


def large_companies(ctx: AppContext, deadline: Deadline, errors: list[str]) -> tuple[list[Company], bool]:
    """(companies, stopped) — every department of the profile, each one isolated; nothing when disabled."""
    cfg = ctx.settings.outreach.large_companies
    center = center_of(ctx.profile)
    if not cfg.enabled or center is None:
        return [], False
    out: list[Company] = []
    stopped = False
    with make_client() as client:
        for department in _departments(ctx.profile):
            if deadline.expired():
                stopped = True
                break
            try:
                found, stopped = _department(client, department, cfg, center, deadline)
            except Exception as exc:  # one department failing never stops the others
                log.warning("Grandes entreprises du département %s indisponibles : %s", department, exc)
                errors.append(f"grandes entreprises ({department}) : {exc}")
                continue
            log.info("Grandes entreprises : %d site(s) retenu(s) dans le département %s", len(found), department)
            out.extend(found)
            if stopped:
                break
    return _nearest_once(out, center), stopped
