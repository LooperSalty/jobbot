"""Registry candidates for the employer of stored offers (pure, no I/O): local establishment, confidence, tie-breaks.
Names live in :mod:`jobbot.night.siren_names`, the tie-breaks in :mod:`jobbot.night.siren_prefer`, the fallback
without any local agency in :mod:`jobbot.night.siren_remote`, the queries in :mod:`jobbot.night.siren`.

- **Names**: :func:`jobbot.night.siren_names.name_score` (exact 100, sigle, initials, contained name…).
- **Place**: the best *active* establishment inside the zone (the offer's department, the target departments, the
  search areas' radius): same postal code / commune +8, ≤ 15 km +6, ≤ 40 km +3, same department +2. A company
  without such an establishment is not a candidate here.
- **Activity**: IT (NAF 62 / 63.1 / 58.2, 71.12B, 70.22Z, or Syntec outside a holding) +4, ETI / GE +3, ≥ 10
  employees +1. A short name (acronym, ≤ 4 characters) of a company outside IT counts ``SHORT_CAP`` at most: the
  place and the size must decide. A holding (NAF 64, 70.10Z) is never IT.

The best SIREN is accepted at ``ACCEPT`` points or more and ``MARGIN`` points ahead of any other one; without that
margin, the company the tie-breaks prefer over every close one (operating IT company over a holding, more
establishments…) with a strong name; else nothing (never guess). A holding never wins over an operating IT
company with a strong name.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

from jobbot.matching.geo import city_key, department_from_postal, haversine_km, lookup_known_place
from jobbot.matching.regions import region_of
# names re-exported for jobbot.night.siren and the tests
from jobbot.night.siren_names import (  # noqa: F401
    ACRONYM_SCORE, SUBSET_SCORE, base_name, is_regional, is_short, legal_name, name_score, pair_score,
    query_variants, registry_names, wanted_names, words,
)
from jobbot.night.siren_prefer import (
    HOLDING, IT, OTHER, STRONG_NAME, Traits, by_proximity, compare, operating_it, winner,
)
from jobbot.outreach._registry import CORE_NAF, SYNTEC_IDCC, TRANCHE_CODES

ACCEPT = 96.0  # total score needed to accept a company…
MARGIN = 6.0  # …ahead of any other SIREN by at least this much
NAME_MIN = 85.0  # below this name similarity a result is not a candidate at all
SHORT_CAP = 84.0  # name score of a short name for a company outside IT
SAME_PLACE, NEAR, AROUND, SAME_DEPT = 8.0, 6.0, 3.0, 2.0
NEAR_KM, AROUND_KM = 15.0, 40.0
IT_POINTS, BIG_POINTS, STAFF_POINTS = 4.0, 3.0, 1.0
IT_NAF_PREFIXES = ("62.", "63.1", "58.2")
SYNTEC_NAF = frozenset({"71.12B", "70.22Z"})  # engineering, IT consulting (Syntec)
HOLDING_NAF_PREFIXES = ("64.", "70.10")


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


@dataclass(frozen=True)
class Zone:
    """Where an establishment counts as local: target departments, or within a search area's radius (km)."""

    departments: frozenset[str]
    areas: tuple[tuple[float, float, float], ...] = ()  # (latitude, longitude, radius_km)


@dataclass(frozen=True, eq=False)
class Match:
    """A registry company (unité légale) with its establishment chosen near the offers (its head office when
    ``remote``: no local agency, :mod:`jobbot.night.siren_remote`)."""

    siren: str
    item: dict
    place: dict
    score: float
    name_score: float
    traits: Traits = Traits(tier=OTHER, name=0.0)
    notes: tuple[str, ...] = ()  # French: why this company / this place
    remote: bool = False


# --------------------------------------------------------------------------- place


def _float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def place_point(place: dict) -> tuple[float, float] | None:
    """Coordinates of an establishment, else of its city when well known (offline)."""
    lat, lon = _float(place.get("latitude")), _float(place.get("longitude"))
    if lat is not None and lon is not None:
        return lat, lon
    return lookup_known_place(str(place.get("libelle_commune") or ""))


def group_point(group: OfferGroup) -> tuple[float, float] | None:
    if group.latitude is not None and group.longitude is not None:
        return group.latitude, group.longitude
    return lookup_known_place(group.city) if group.city else None


def _dept(place: dict) -> str:
    return str(place.get("departement") or department_from_postal(str(place.get("code_postal") or "")) or "")


def km_to_offer(group: OfferGroup, place: dict) -> float | None:
    a, b = group_point(group), place_point(place)
    return haversine_km(a[0], a[1], b[0], b[1]) if a and b else None


def active_places(item: dict) -> list[dict]:
    """Active establishments of a result: matching ones, then the head office (closed ones are returned too)."""
    out: dict[str, dict] = {}
    for place in [*(item.get("matching_etablissements") or []), item.get("siege") or {}]:
        if isinstance(place, dict) and place and place.get("etat_administratif", "A") == "A":
            out.setdefault(str(place.get("siret") or len(out)), place)
    return list(out.values())


def in_zone(group: OfferGroup, zone: Zone, place: dict) -> bool:
    dept = _dept(place)
    if dept and (dept in zone.departments or dept == group.department):
        return True
    point = place_point(place)
    return point is not None and any(haversine_km(point[0], point[1], lat, lon) <= radius
                                     for lat, lon, radius in zone.areas)


def place_points(group: OfferGroup, place: dict) -> float:
    if group.postal_code and str(place.get("code_postal") or "") == group.postal_code:
        return SAME_PLACE
    if group.city and city_key(str(place.get("libelle_commune") or "")) == city_key(group.city):
        return SAME_PLACE
    km = km_to_offer(group, place)
    if km is not None and km <= NEAR_KM:
        return NEAR
    if km is not None and km <= AROUND_KM:
        return AROUND
    return SAME_DEPT if group.department and _dept(place) == group.department else 0.0


def best_place(group: OfferGroup, zone: Zone, item: dict) -> tuple[dict, float] | None:
    """The active establishment of ``item`` nearest to the offers, inside the zone (with its points)."""
    options = [(place_points(group, p), km_to_offer(group, p), p) for p in active_places(item)
               if in_zone(group, zone, p)]
    if not options:
        return None
    points, _, place = max(options, key=lambda o: (o[0], -(o[1] if o[1] is not None else math.inf)))
    return place, points


# --------------------------------------------------------------------------- activity, traits


def _naf(item: dict) -> str:
    return str(item.get("activite_principale") or "")


def tier(item: dict) -> int:
    """IT / Syntec NAF → ``IT``, holding → ``HOLDING``, else ``OTHER``."""
    naf = _naf(item)
    if naf.startswith(HOLDING_NAF_PREFIXES):
        return HOLDING
    return IT if naf in CORE_NAF or naf in SYNTEC_NAF or naf.startswith(IT_NAF_PREFIXES) else OTHER


def is_it(item: dict) -> bool:
    """IT activity: IT / Syntec NAF, or the Syntec agreement outside a holding."""
    idcc = {*((item.get("siege") or {}).get("liste_idcc") or []),
            *((item.get("complements") or {}).get("liste_idcc") or [])}
    level = tier(item)
    return level == IT or (level == OTHER and SYNTEC_IDCC in idcc)


def activity_points(item: dict) -> float:
    tranche = str(item.get("tranche_effectif_salarie") or "NN")
    staffed = tranche in TRANCHE_CODES and TRANCHE_CODES.index(tranche) >= TRANCHE_CODES.index("11")
    points = IT_POINTS if is_it(item) else 0.0
    points += BIG_POINTS if item.get("categorie_entreprise") in ("ETI", "GE") else 0.0
    return points + (STAFF_POINTS if staffed else 0.0)


def _count(value: object) -> int:
    number = _float(value)
    return int(number) if number is not None and number > 0 else 0


def target_regions(group: OfferGroup, zone: Zone) -> set[str]:
    return {r for d in {*zone.departments, group.department or ""} if (r := region_of(d))}


def traits_of(group: OfferGroup, zone: Zone, item: dict, name: float) -> Traits:
    siege = item.get("siege") or {}
    legal = legal_name(item)
    tranche = str(item.get("tranche_effectif_salarie") or "NN")
    return Traits(
        tier=tier(item), name=name, sibling=bool(legal) and base_name(legal) == base_name(group.name),
        regional=is_regional(legal), hq_km=km_to_offer(group, siege) if siege else None,
        establishments=_count(item.get("nombre_etablissements_ouverts")),
        tranche=TRANCHE_CODES.index(tranche) if tranche in TRANCHE_CODES[1:] else -1,
        hq_in_region=bool(siege) and region_of(_dept(siege)) in target_regions(group, zone),
    )


# --------------------------------------------------------------------------- candidates


def usable(item: dict) -> bool:
    siren = str(item.get("siren") or "")
    sole = (item.get("complements") or {}).get("est_entrepreneur_individuel")
    return bool(re.fullmatch(r"\d{9}", siren)) and item.get("etat_administratif", "A") == "A" and not sole


def capped_name(similarity: float, wanted: list[str], item: dict) -> float:
    return min(similarity, SHORT_CAP) if is_short(wanted) and not is_it(item) else similarity


def candidate(group: OfferGroup, zone: Zone, item: dict, wanted: list[str]) -> Match | None:
    """``item`` scored for this employer; None when its name is too far or it has no establishment in the zone."""
    similarity = name_score(wanted, item) if usable(item) else 0.0
    located = best_place(group, zone, item) if similarity >= NAME_MIN else None
    if located is None:
        return None
    name = capped_name(similarity, wanted, item)
    place, points = located
    return Match(siren=str(item["siren"]), item=item, place=place, score=name + points + activity_points(item),
                 name_score=name, traits=traits_of(group, zone, item, name))


def rank(group: OfferGroup, zone: Zone, items: list[dict]) -> list[Match]:
    """Plausible companies (best score per SIREN), best first."""
    wanted = wanted_names(group.name)
    best: dict[str, Match] = {}
    for item in items:
        match = candidate(group, zone, item, wanted) if isinstance(item, dict) else None
        if match is not None and (match.siren not in best or match.score > best[match.siren].score):
            best[match.siren] = match
    return sorted(best.values(), key=lambda m: -m.score)


# --------------------------------------------------------------------------- decision


def without_holdings(pool: list[Match]) -> list[Match]:
    """A holding never wins over an operating IT company with a strong name: holdings dropped when there is one."""
    if not any(operating_it(m.traits) for m in pool):
        return pool
    return [m for m in pool if m.traits.tier != HOLDING]


def _city(place: dict) -> str:
    return str(place.get("libelle_commune") or "").strip().title()


def _label(match: Match) -> str:
    return f"{legal_name(match.item)} ({_city(match.item.get('siege') or {})})"


def sibling_notes(chosen: Match, others: list[Match]) -> list[str]:
    """Why a group sibling closer to the offer was preferred over the one bearing the offer's name more exactly."""
    literal = [m for m in others if m.traits.name > chosen.traits.name and by_proximity(chosen.traits, m.traits) > 0]
    if not literal:
        return []
    other = max(literal, key=lambda m: m.traits.name)
    return [f"Entité du groupe la plus proche de l'offre : {_label(chosen)} plutôt que {_label(other)}"]


def _tie_break(pool: list[Match]) -> Match | None:
    close = [m for m in pool if pool[0].score - m.score < MARGIN]
    index = winner([m.traits for m in close])
    if index is None:
        return None
    best = close[index]
    if best.score < ACCEPT or best.name_score < STRONG_NAME:
        return None
    others = [m for m in pool if m is not best and m.score > best.score - MARGIN]
    if any(compare(best.traits, m.traits) <= 0 for m in others):
        return None  # a company close to the preferred one is as good: never guess
    return replace(best, notes=(*best.notes, *sibling_notes(best, others)))


def decide(ranked: list[Match]) -> Match | None:
    """The best company only with high confidence: ``ACCEPT`` points and ``MARGIN`` ahead of the next SIREN, else
    the one the tie-breaks prefer over every close one; a holding never over an operating IT company."""
    pool = without_holdings(ranked)
    if not pool or pool[0].score < ACCEPT:
        return None
    if len(pool) == 1 or pool[0].score - pool[1].score >= MARGIN:
        return pool[0]
    return _tie_break(pool)  # two companies answer to that name: only a clear preference, never a guess


def blocks_remote(ranked: list[Match]) -> bool:
    """A local operating IT company answers to that name (even without confidence): never look further away."""
    return any(operating_it(m.traits) for m in ranked)


def pick(group: OfferGroup, zone: Zone, items: list[dict]) -> Match | None:
    return decide(rank(group, zone, items))
