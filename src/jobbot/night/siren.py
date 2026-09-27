"""SIREN of the employers of stored offers (night « signaux de recrutement »): registry queries in order until one
is confident, company located at its establishment near the offers, kv cache, per-night cap.

Queries (``recherche-entreprises /search``, active companies, 20 results with their matching establishments), until
:func:`jobbot.night.siren_match.decide` accepts a company:

1. ``departement`` — ``q=<name>&departement=<offer's department>`` (the target departments when unknown);
2. ``code_postal`` — ``q=<name>&code_postal=<offer's postal code>``, only when several companies answered (1);
3. ``variante:<q>`` — ``q=<cleaned variant>&departement=…`` (:func:`jobbot.night.siren_match.query_variants`);
4. ``sans_lieu`` — ``q=<cleaned name>`` without place: kept only with an active establishment inside the zone.

Every query goes through :func:`jobbot.outreach.companies.fetch_registry_page` (shared throttle ≤ 4 requests/s,
retries on 429 / 5xx) and counts against ``settings.night.max_siren_lookups`` per night (:class:`Lookups`); three
failed queries in a row stop the lookups until the next night.

Cache (kv ``night:siren:<country>:<name>``): found → ``{siren, siret, place, strategy, at}`` (the establishment is
re-applied to the stored company without any query); not found after every query → ``{siren: null, tried, at,
retry_after}``, looked up again after ``NEGATIVE_RETRY_DAYS`` days. An entry without ``tried`` (single-query
resolver of the previous versions) is retried at once; a lookup cut short by the cap is not cached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

import httpx

from jobbot import clock
from jobbot.db import Repository
from jobbot.matching.geo import lookup_known_place
from jobbot.models import Company
from jobbot.night.siren_match import Match, OfferGroup, Zone, decide, query_variants, rank
from jobbot.night.store import LOCAL_KEY, PLACE_FIELDS
from jobbot.outreach._registry import company_from_result
from jobbot.outreach.companies import CompanySearchError, fetch_registry_page

CACHE_KEY = "night:siren:"
NEGATIVE_RETRY_DAYS = 7  # an employer not identified is looked up again after this
PER_PAGE = 20
ESTABLISHMENTS = 20  # matching establishments per result: the local agency of a group
MAX_VARIANTS = 2
MAX_FAILURES = 3  # failed queries in a row before giving up for the night
MIN_FREE_QUERY = 3  # characters of a query without any filter (API rule)


class Lookups:
    """Registry queries of one night: at most ``limit``; ``MAX_FAILURES`` failed queries in a row stop them."""

    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self.used = 0
        self._failures = 0

    def available(self) -> bool:
        return self.used < self.limit and self._failures < MAX_FAILURES

    @property
    def capped(self) -> bool:
        return self.used >= self.limit

    def search(self, client: httpx.Client, params: dict) -> list[dict]:
        self.used += 1
        try:
            data = fetch_registry_page(client, params)
        except CompanySearchError:
            self._failures += 1
            raise
        self._failures = 0
        return [r for r in data.get("results") or [] if isinstance(r, dict)]


@dataclass(frozen=True)
class Strategy:
    label: str
    params: dict


@dataclass(frozen=True)
class Resolution:
    match: Match | None = None
    strategy: str | None = None
    tried: tuple[str, ...] = ()
    complete: bool = True  # every useful query was made (False: cut short by the per-night cap)


# --------------------------------------------------------------------------- queries


def _query(q: str, **where: str) -> dict:
    return {"q": q, **where, "etat_administratif": "A", "per_page": PER_PAGE,
            "limite_matching_etablissements": ESTABLISHMENTS}


def plan(group: OfferGroup, zone: Zone) -> list[Strategy]:
    """Queries to try, in order (duplicates removed)."""
    dept = group.department or ",".join(sorted(zone.departments))
    where = {"departement": dept} if dept else {}
    variants = query_variants(group.name)[:MAX_VARIANTS]
    steps = [Strategy("departement", _query(group.name, **where))] if where else []
    if group.postal_code:
        steps.append(Strategy("code_postal", _query(group.name, code_postal=group.postal_code)))
    steps.extend(Strategy(f"variante:{v}", _query(v, **where)) for v in variants)
    free = variants[0] if variants else group.name.strip()
    if len(free) >= MIN_FREE_QUERY:
        steps.append(Strategy("sans_lieu", _query(free)))
    unique: dict[str, Strategy] = {}
    for step in steps:
        unique.setdefault(json.dumps(step.params, sort_keys=True), step)
    return list(unique.values())


def resolve(client: httpx.Client, group: OfferGroup, zone: Zone, lookups: Lookups) -> Resolution:
    """Run the queries until one names the employer with confidence (raises :class:`CompanySearchError`)."""
    tried: list[str] = []
    plausible = 0
    for step in plan(group, zone):
        if step.label == "code_postal" and plausible < 2:
            continue  # narrowing to the postal code only helps when several companies answered
        if not lookups.available():
            return Resolution(tried=tuple(tried), complete=False)
        ranked = rank(group, zone, lookups.search(client, step.params))
        tried.append(step.label)
        plausible = len(ranked) if step.label == "departement" else plausible
        match = decide(ranked)
        if match is not None:
            return Resolution(match=match, strategy=step.label, tried=tuple(tried))
    return Resolution(tried=tuple(tried))


# --------------------------------------------------------------------------- company at its local establishment


def local_company(match: Match) -> Company | None:
    """The company (group SIREN) at its establishment near the offers (SIRET in raw, city coordinates as a
    fallback when the registry has none)."""
    company = company_from_result({**match.item, "matching_etablissements": [match.place]}, set())
    if company is None:
        return None
    update: dict = {"raw": {**company.raw, LOCAL_KEY: True}}
    point = lookup_known_place(company.city) if company.latitude is None or company.longitude is None else None
    if point is not None:
        update.update(latitude=point[0], longitude=point[1])
    return company.model_copy(update=update)


def with_cached_place(company: Company, entry: dict | None) -> Company:
    """``company`` moved to the establishment recorded in the cache (unchanged for an entry without one)."""
    place = (entry or {}).get("place")
    if not isinstance(place, dict) or not place.get("city"):
        return company
    fields = {f: place.get(f) for f in PLACE_FIELDS}
    raw = {**company.raw, LOCAL_KEY: True, **({"siret": place["siret"]} if place.get("siret") else {})}
    return company.model_copy(update={**fields, "raw": raw})


# --------------------------------------------------------------------------- cache


def cache_key(group: OfferGroup) -> str:
    return f"{CACHE_KEY}{group.country.lower()}:{group.key}"


def cached(repo: Repository, group: OfferGroup) -> dict | None:
    try:
        data = json.loads(repo.kv_get(cache_key(group)) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def missing_is_fresh(entry: dict | None) -> bool:
    """Not identified recently by every query: nothing to do before ``retry_after``."""
    if not entry or entry.get("siren") or not isinstance(entry.get("tried"), list):
        return False
    try:
        until = clock.parse_dt(str(entry.get("retry_after") or ""))
    except ValueError:
        return False
    return until is not None and clock.now() < until


def remember_found(repo: Repository, group: OfferGroup, result: Resolution, company: Company) -> None:
    place = {**{f: getattr(company, f) for f in PLACE_FIELDS}, "siret": company.raw.get("siret")}
    repo.kv_set(cache_key(group), json.dumps({
        "siren": company.siren, "siret": place["siret"], "place": place, "strategy": result.strategy,
        "at": clock.iso(clock.now()),
    }))


def remember_missing(repo: Repository, group: OfferGroup, tried: tuple[str, ...]) -> None:
    now = clock.now()
    repo.kv_set(cache_key(group), json.dumps({
        "siren": None, "tried": list(tried), "at": clock.iso(now),
        "retry_after": clock.iso(now + timedelta(days=NEGATIVE_RETRY_DAYS)),
    }))
