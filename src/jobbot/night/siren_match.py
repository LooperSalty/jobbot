"""Registry candidates for the employer of stored offers (pure, no I/O): name variants, similarity, local
establishment, confidence. The queries themselves live in :mod:`jobbot.night.siren`.

- **Names**: the offer's employer name, cleaned (« H/F », legal forms, « groupe », « France », a trailing region or
  city, acronym dots, ampersands: « T&S I.T. » → « t&s it », « ts it »), against every registry name (raison
  sociale, sigle, nom complet, enseignes, noms commerciaux): equality without punctuation → 100, rapidfuzz ratio /
  token_sort_ratio, token_set_ratio for a name contained in the other (``SUBSET_SCORE``, fewer points per extra
  word), initials (« SII » = « Société pour l'Informatique Industrielle ») → ``ACRONYM_SCORE``.
- **Place**: the best *active* establishment inside the zone (the offer's department, the target departments, the
  search areas' radius): same postal code / commune +8, ≤ 15 km +6, ≤ 40 km +3, same department +2. A company
  without such an establishment is not a candidate.
- **Activity**: IT (NAF 62 / 63.1 / 58.2 or Syntec) +4, ETI / GE +3, ≥ 10 employees +1. A short name (acronym,
  ≤ 4 characters) of a company outside IT counts ``SHORT_CAP`` at most: the place and the size must decide.

The best SIREN is accepted at ``ACCEPT`` points or more and ``MARGIN`` points ahead of any other one; else nothing
(never guess).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from jobbot.matching.geo import KNOWN_PLACES, city_key, department_from_postal, haversine_km, lookup_known_place
from jobbot.outreach._registry import CORE_NAF, SYNTEC_IDCC, TRANCHE_CODES, name_candidates
from jobbot.textutil import strip_accents

ACCEPT = 96.0  # total score needed to accept a company…
MARGIN = 6.0  # …ahead of any other SIREN by at least this much
NAME_MIN = 85.0  # below this name similarity a result is not a candidate at all
SUBSET_SCORE = 88.0  # one name contained in the other, one extra word…
SUBSET_STEP = 4.0  # …minus this per further extra word
ACRONYM_SCORE = 90.0  # « SII » = initials of « Société pour l'Informatique Industrielle »
SHORT_NAME = 4  # characters (without spaces) of an acronym-like name
SHORT_CAP = 84.0  # name score of a short name for a company outside IT
SAME_PLACE, NEAR, AROUND, SAME_DEPT = 8.0, 6.0, 3.0, 2.0
NEAR_KM, AROUND_KM = 15.0, 40.0
IT_POINTS, BIG_POINTS, STAFF_POINTS = 4.0, 3.0, 1.0
IT_NAF_PREFIXES = ("62.", "63.1", "58.2")

_GENDER = re.compile(r"\(?\b[hfm]\s*/\s*[hfm]\b\)?")
_ACRONYM_DOTS = re.compile(r"\b(?:[a-z0-9]\.){2,}")
_LEGAL = re.compile(r"\b(?:sas|sasu|sarl|eurl|sa|sci|sca|snc|scop|selarl|group|groupe|france|holding|inc|ltd|gmbh)\b")
_STOP = frozenset({"a", "au", "aux", "d", "de", "des", "du", "en", "et", "l", "la", "le", "les", "pour", "sur",
                   "and", "of", "the"})
_REGIONS = ("nord", "sud", "est", "ouest", "centre", "grand est", "lorraine", "alsace", "moselle",
            "meurthe et moselle", "ile de france")
_TAIL = re.compile(r"\s+(?:agence\s+(?:de\s+|d\s+)?)?(?:"
                   + "|".join(re.escape(p) for p in sorted({*_REGIONS, *KNOWN_PLACES}, key=len, reverse=True))
                   + r")$")


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
    """A registry company (unité légale) with its establishment chosen near the offers."""

    siren: str
    item: dict
    place: dict
    score: float
    name_score: float


# --------------------------------------------------------------------------- names


def _undotted(text: str) -> str:
    """Lowercase, no accent, no « H/F », acronym dots removed ('i.t.' → 'it')."""
    t = _GENDER.sub(" ", strip_accents(text or "").lower())
    return _ACRONYM_DOTS.sub(lambda m: m.group(0).replace(".", ""), t)


def words(name: str) -> str:
    """Comparable form: 'T&S I.T. (H/F)' → 't s it', 'Exemple S.A.S.' → 'exemple'."""
    t = re.sub(r"[^a-z0-9]+", " ", _undotted(name))
    stripped = " ".join(_LEGAL.sub(" ", t).split())
    return stripped or " ".join(t.split())


def _compact(text: str) -> str:
    return text.replace(" ", "")


def cleaned_name(name: str) -> str:
    """Registry query form: no « H/F », legal form, « groupe », « France », trailing region / city; '&' kept."""
    t = re.sub(r"\s*&\s*", "&", re.sub(r"[^a-z0-9&]+", " ", _undotted(name)))
    t = " ".join(_LEGAL.sub(" ", t).split())
    while (shorter := _TAIL.sub("", t)) != t and len(_compact(shorter)) >= 3:
        t = shorter
    return " ".join(t.split())


def query_variants(name: str) -> list[str]:
    """Cleaned alternatives of ``name`` worth their own query ('T&S I.T.' → ['t&s it', 'ts it'])."""
    plain = " ".join(strip_accents(name or "").lower().split())
    cleaned = cleaned_name(name)
    out: list[str] = []
    for variant in (cleaned, cleaned.replace("&", "")):
        variant = " ".join(variant.split())
        if len(_compact(variant)) >= 2 and variant != plain and variant not in out:
            out.append(variant)
    return out


def wanted_names(name: str) -> list[str]:
    """Comparable forms of the offer's employer name and of its variants."""
    forms = [words(name), *(words(v) for v in query_variants(name))]
    return [f for f in dict.fromkeys(forms) if f]


def is_short(wanted: list[str]) -> bool:
    return bool(wanted) and min(len(_compact(w)) for w in wanted) <= SHORT_NAME


def registry_names(item: dict) -> list[str]:
    """Every name of a registry result: raison sociale, sigle, nom complet, enseignes, noms commerciaux."""
    names = [*name_candidates(item), str(item.get("nom_complet") or "")]
    for place in [item.get("siege") or {}, *(item.get("matching_etablissements") or [])]:
        names.extend(str(e) for e in place.get("liste_enseignes") or [])
    return list(dict.fromkeys(n.strip() for n in names if n and n.strip()))


def _initials(text: str) -> str:
    tokens = [w for w in text.split() if w not in _STOP]
    return "".join(w[0] for w in tokens) if len(tokens) >= 2 else ""


def _acronym_of(short: str, long: str) -> bool:
    return " " not in short and 2 <= len(short) <= 6 and _initials(long) == short


def _contained(a: str, b: str) -> float:
    """One name inside the other ('acme' in 'acme nord'): fewer points per extra word."""
    ta, tb = set(a.split()), set(b.split())
    small = ta if len(ta) <= len(tb) else tb
    if len(_compact(" ".join(small - _STOP))) < 4 or fuzz.token_set_ratio(a, b) < 100:
        return 0.0
    extra = len(ta ^ tb)
    return max(0.0, SUBSET_SCORE - SUBSET_STEP * max(0, extra - 1))


def pair_score(a: str, b: str) -> float:
    """Similarity (0-100) of two comparable names (:func:`words`)."""
    if not a or not b:
        return 0.0
    if _compact(a) == _compact(b):
        return 100.0
    score = max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))
    if _acronym_of(a, b) or _acronym_of(b, a):
        score = max(score, ACRONYM_SCORE)
    return max(score, _contained(a, b))


def name_score(wanted: list[str], item: dict) -> float:
    names = [w for w in (words(n) for n in registry_names(item)) if w]
    return max((pair_score(a, b) for a in wanted for b in names), default=0.0)


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


def _group_point(group: OfferGroup) -> tuple[float, float] | None:
    if group.latitude is not None and group.longitude is not None:
        return group.latitude, group.longitude
    return lookup_known_place(group.city) if group.city else None


def _dept(place: dict) -> str:
    return str(place.get("departement") or department_from_postal(str(place.get("code_postal") or "")) or "")


def _km(group: OfferGroup, place: dict) -> float | None:
    a, b = _group_point(group), place_point(place)
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
    km = _km(group, place)
    if km is not None and km <= NEAR_KM:
        return NEAR
    if km is not None and km <= AROUND_KM:
        return AROUND
    return SAME_DEPT if group.department and _dept(place) == group.department else 0.0


def best_place(group: OfferGroup, zone: Zone, item: dict) -> tuple[dict, float] | None:
    """The active establishment of ``item`` nearest to the offers, inside the zone (with its points)."""
    options = [(place_points(group, p), _km(group, p), p) for p in active_places(item) if in_zone(group, zone, p)]
    if not options:
        return None
    points, _, place = max(options, key=lambda o: (o[0], -(o[1] if o[1] is not None else math.inf)))
    return place, points


# --------------------------------------------------------------------------- candidates


def is_it(item: dict) -> bool:
    naf = str(item.get("activite_principale") or "")
    idcc = {*((item.get("siege") or {}).get("liste_idcc") or []),
            *((item.get("complements") or {}).get("liste_idcc") or [])}
    return naf in CORE_NAF or naf.startswith(IT_NAF_PREFIXES) or SYNTEC_IDCC in idcc


def activity_points(item: dict) -> float:
    tranche = str(item.get("tranche_effectif_salarie") or "NN")
    staffed = tranche in TRANCHE_CODES and TRANCHE_CODES.index(tranche) >= TRANCHE_CODES.index("11")
    points = IT_POINTS if is_it(item) else 0.0
    points += BIG_POINTS if item.get("categorie_entreprise") in ("ETI", "GE") else 0.0
    return points + (STAFF_POINTS if staffed else 0.0)


def _usable(item: dict) -> bool:
    siren = str(item.get("siren") or "")
    sole = (item.get("complements") or {}).get("est_entrepreneur_individuel")
    return bool(re.fullmatch(r"\d{9}", siren)) and item.get("etat_administratif", "A") == "A" and not sole


def candidate(group: OfferGroup, zone: Zone, item: dict, wanted: list[str]) -> Match | None:
    """``item`` scored for this employer; None when its name is too far or it has no establishment in the zone."""
    similarity = name_score(wanted, item) if _usable(item) else 0.0
    located = best_place(group, zone, item) if similarity >= NAME_MIN else None
    if located is None:
        return None
    name = min(similarity, SHORT_CAP) if is_short(wanted) and not is_it(item) else similarity
    place, points = located
    return Match(siren=str(item["siren"]), item=item, place=place, score=name + points + activity_points(item),
                 name_score=name)


def rank(group: OfferGroup, zone: Zone, items: list[dict]) -> list[Match]:
    """Plausible companies (best score per SIREN), best first."""
    wanted = wanted_names(group.name)
    best: dict[str, Match] = {}
    for item in items:
        match = candidate(group, zone, item, wanted) if isinstance(item, dict) else None
        if match is not None and (match.siren not in best or match.score > best[match.siren].score):
            best[match.siren] = match
    return sorted(best.values(), key=lambda m: -m.score)


def decide(ranked: list[Match]) -> Match | None:
    """The best company only with high confidence: ``ACCEPT`` points and ``MARGIN`` ahead of the next SIREN."""
    if not ranked or ranked[0].score < ACCEPT:
        return None
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < MARGIN:
        return None  # two companies answer to that name: never guess
    return ranked[0]


def pick(group: OfferGroup, zone: Zone, items: list[dict]) -> Match | None:
    return decide(rank(group, zone, items))
