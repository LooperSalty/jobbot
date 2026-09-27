"""SIREN of the employers of stored offers (night « signaux de recrutement »): registry queries in order until one
is confident, company located at its establishment near the offers, kv cache, per-night cap.

Queries (``recherche-entreprises /search``, active companies, 20 results with their matching establishments), until
:func:`jobbot.night.siren_match.decide` accepts a company:

1. ``departement`` — ``q=<name>&departement=<offer's department>`` (the target departments when unknown);
2. ``code_postal`` — ``q=<name>&code_postal=<offer's postal code>``, only when several companies answered (1);
3. ``variante:<q>`` — ``q=<cleaned variant>&departement=…`` (:func:`jobbot.night.siren_match.query_variants`);
4. ``sans_lieu`` — ``q=<cleaned name>`` without place: kept with an active establishment inside the zone; else,
   when no operating IT company answered with a local establishment, ``siege``: the IT company recruiting for the
   offer's place from its head office (:mod:`jobbot.night.siren_remote` — ESN / consulting without local agency),
   stored at its head office but working at the offer's place (``raw["agency"] = "absente"``, French note).

Every query goes through :func:`jobbot.outreach.companies.fetch_registry_page` (shared throttle ≤ 4 requests/s,
retries on 429 / 5xx) and counts against ``settings.night.max_siren_lookups`` per night (:class:`Lookups`); three
failed queries in a row stop the lookups until the next night.

Cache (kv ``night:siren:<country>:<name>``): found → ``{siren, siret, place, strategy, at}`` (the establishment —
or the offer's place with the agency notes — is re-applied to the stored company without any query); not found
after every query → ``{siren: null, tried, at, retry_after}``, looked up again after ``NEGATIVE_RETRY_DAYS`` days.
An entry without ``tried`` (single-query resolver of the previous versions) is retried at once; a lookup cut short
by the cap is not cached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import NamedTuple

import httpx

from jobbot import clock
from jobbot.db import Repository
from jobbot.matching.geo import lookup_known_place
from jobbot.models import Company
from jobbot.night.siren_match import Match, OfferGroup, Zone, blocks_remote, decide, group_point, query_variants, rank
from jobbot.night.siren_remote import decide_remote
from jobbot.night.store import AGENCY_ABSENT, AGENCY_KEY, AGENCY_NOTES_KEY, LOCAL_KEY, PLACE_FIELDS
from jobbot.outreach._registry import company_from_result
from jobbot.outreach.companies import CompanySearchError, fetch_registry_page

CACHE_KEY = "night:siren:"
NEGATIVE_RETRY_DAYS = 7  # an employer not identified is looked up again after this
PER_PAGE = 20
ESTABLISHMENTS = 20  # matching establishments per result: the local agency of a group
MAX_VARIANTS = 2
MAX_FAILURES = 3  # failed queries in a row before giving up for the night
MIN_FREE_QUERY = 3  # characters of a query without any filter (API rule)
FREE_STEP, REMOTE_STRATEGY = "sans_lieu", "siege"


class Answer(NamedTuple):
    results: list[dict]
    total: int  # results the registry has for that query (more than one page: a common name)


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

    def search(self, client: httpx.Client, params: dict) -> Answer:
        self.used += 1
        try:
            data = fetch_registry_page(client, params)
        except CompanySearchError:
            self._failures += 1
            raise
        self._failures = 0
        results = [r for r in data.get("results") or [] if isinstance(r, dict)]
        total = data.get("total_results")
        return Answer(results, total if isinstance(total, int) and total > 0 else len(results))


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
        steps.append(Strategy(FREE_STEP, _query(free)))
    unique: dict[str, Strategy] = {}
    for step in steps:
        unique.setdefault(json.dumps(step.params, sort_keys=True), step)
    return list(unique.values())


def _judge(group: OfferGroup, zone: Zone, label: str, answer: Answer, ranked: list[Match],
           local_it: bool) -> Match | None:
    """A local company with confidence; after the query without place, when no operating IT company answered
    with a local establishment, the head office of an IT company without local agency."""
    match = decide(ranked)
    if match is None and label == FREE_STEP and not local_it:
        return decide_remote(group, zone, answer.results, answer.total)
    return match


def resolve(client: httpx.Client, group: OfferGroup, zone: Zone, lookups: Lookups) -> Resolution:
    """Run the queries until one names the employer with confidence (raises :class:`CompanySearchError`)."""
    tried: list[str] = []
    plausible, local_it = 0, False
    for step in plan(group, zone):
        if step.label == "code_postal" and plausible < 2:
            continue  # narrowing to the postal code only helps when several companies answered
        if not lookups.available():
            return Resolution(tried=tuple(tried), complete=False)
        answer = lookups.search(client, step.params)
        ranked = rank(group, zone, answer.results)
        tried.append(step.label)
        plausible = len(ranked) if step.label == "departement" else plausible
        local_it = local_it or blocks_remote(ranked)
        match = _judge(group, zone, step.label, answer, ranked, local_it)
        if match is not None:
            strategy = REMOTE_STRATEGY if match.remote else step.label
            return Resolution(match=match, strategy=strategy, tried=tuple(tried))
    return Resolution(tried=tuple(tried))


# --------------------------------------------------------------------------- company at its local establishment


def local_company(match: Match) -> Company | None:
    """The company (group SIREN) at its establishment near the offers (SIRET in raw, city coordinates as a
    fallback when the registry has none)."""
    company = company_from_result({**match.item, "matching_etablissements": [match.place]}, set())
    if company is None:
        return None
    update: dict = {"raw": {**company.raw, LOCAL_KEY: True, **_notes(match)}}
    point = lookup_known_place(company.city) if company.latitude is None or company.longitude is None else None
    if point is not None:
        update.update(latitude=point[0], longitude=point[1])
    return company.model_copy(update=update)


def _notes(match: Match) -> dict:
    return {AGENCY_NOTES_KEY: list(match.notes)} if match.notes else {}


def remote_company(match: Match, group: OfferGroup) -> Company | None:
    """The company at its head office (address, SIRET) working where the offer is: city, postal code and
    coordinates of the offer (distance, priority), ``raw["agency"] = "absente"`` and the French notes."""
    company = company_from_result({**match.item, "matching_etablissements": []}, set())
    if company is None:
        return None
    point = group_point(group)
    raw = {**company.raw, LOCAL_KEY: True, AGENCY_KEY: AGENCY_ABSENT, **_notes(match)}
    return company.model_copy(update={
        "city": group.city or company.city, "postal_code": group.postal_code,
        "latitude": point[0] if point else None, "longitude": point[1] if point else None, "raw": raw,
    })


def company_of(match: Match, group: OfferGroup) -> Company | None:
    return remote_company(match, group) if match.remote else local_company(match)


def with_cached_place(company: Company, entry: dict | None) -> Company:
    """``company`` moved to the place recorded in the cache — its establishment, or the offer's place with the
    agency notes (unchanged for an entry without one)."""
    place = (entry or {}).get("place")
    if not isinstance(place, dict) or not place.get("city"):
        return company
    fields = {f: place.get(f) for f in PLACE_FIELDS}
    raw = {k: v for k, v in company.raw.items() if k not in (AGENCY_KEY, AGENCY_NOTES_KEY)}
    raw.update({LOCAL_KEY: True, **({"siret": place["siret"]} if place.get("siret") else {})})
    if place.get(AGENCY_KEY):
        raw[AGENCY_KEY] = place[AGENCY_KEY]
    if isinstance(place.get(AGENCY_NOTES_KEY), list) and place[AGENCY_NOTES_KEY]:
        raw[AGENCY_NOTES_KEY] = list(place[AGENCY_NOTES_KEY])
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
    place = {**{f: getattr(company, f) for f in PLACE_FIELDS}, "siret": company.raw.get("siret"),
             **{k: company.raw[k] for k in (AGENCY_KEY, AGENCY_NOTES_KEY) if company.raw.get(k)}}
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
