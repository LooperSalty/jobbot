"""Location fit of an offer against the profile's search areas (offline, deterministic).

Order of evidence: offer coordinates → known city name / big-city area (« Lyon et périphérie ») → postal
department → whole region (« Île-de-France », « Grand Est ») → department named in the text. Full remote
offers pass whatever the distance when ``profile.target.remote_ok``.

- Outside every search area (distance, department or region) and not remote → hard filter « trop loin :
  <lieu> (hors de tes zones) » (relocation is decided by ``target_rules``).
- A whole region that contains a search area (« Grand Est » for Metz) is kept with a penalty (lieu imprécis).
- A country-only place (« France », empty) is kept with a bigger penalty (lieu non précisé), unless remote.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobbot.config import LocationPref, Profile
from jobbot.matching import filters
from jobbot.matching.geo import (
    department_from_location,
    department_from_postal,
    haversine_km,
    is_place,
    lookup_known_place,
)
from jobbot.matching.regions import VaguePlace, is_country_only, place_label, vague_place
from jobbot.models import ContractType, JobOffer
from jobbot.sources.countries import FRANCE, location_country

REMOTE_SCORE = 0.9
SAME_DEPARTMENT_SCORE = 0.7
UNKNOWN_SCORE = 0.5
VAGUE_REGION_PENALTY = 10.0  # points off the total score: « Grand Est » for a Metz search
COUNTRY_ONLY_PENALTY = 15.0  # « France » / no place at all, remote unknown
# Sources whose ``remote=True`` means full remote (France Travail / LBA / Indeed also flag hybrid work).
FULL_REMOTE_FLAG_SOURCES = frozenset({"linkedin", "hellowork", "wttj", "apec", "free_work", "adzuna", "jooble",
                                      "company_ats"})


@dataclass(frozen=True)
class LocationFit:
    score: float  # 0..1
    reason: str
    blocking: str | None = None  # hard-filter reason (too far)
    distance_km: float | None = None
    penalty: float = 0.0  # points removed from the total score (imprecise place)


@dataclass(frozen=True)
class _Area:
    city: str
    radius_km: int
    coords: tuple[float, float] | None
    department: str | None
    country: str = FRANCE


def _areas(profile: Profile, contract: ContractType | None = None) -> list[_Area]:
    """Search areas; one restricted to some contracts (e.g. Luxembourg → CDI) only counts for those contracts."""
    locations = list(profile.target.locations)
    if contract is not None and contract != ContractType.OTHER:
        allowed = [loc for loc in locations if not loc.contract_types or contract in loc.contract_types]
        locations = allowed or locations
    return [_area(loc) for loc in locations]


def _area(loc: LocationPref) -> _Area:
    coords = (loc.latitude, loc.longitude) if loc.latitude is not None and loc.longitude is not None else None
    coords = coords or lookup_known_place(loc.city)
    department = department_from_postal(loc.postal_code) or department_from_location(loc.city)
    return _Area(loc.city, loc.radius_km, coords, department, location_country(loc))


def job_coordinates(job: JobOffer) -> tuple[float, float] | None:
    if job.latitude is not None and job.longitude is not None:
        return job.latitude, job.longitude
    if not is_place(job.location):
        return None
    vague = vague_place(job.location)
    return lookup_known_place(job.location) or (vague.center if vague else None)


def job_department(job: JobOffer) -> str | None:
    return department_from_postal(job.postal_code) or department_from_location(job.location)


def job_region(job: JobOffer) -> VaguePlace | None:
    """The whole region / big-city area the offer names instead of a precise place (no own coordinates nor
    postal code), else None."""
    if (job.latitude is not None and job.longitude is not None) or department_from_postal(job.postal_code):
        return None
    return vague_place(job.location)


def job_departments(job: JobOffer) -> tuple[str, ...]:
    """Departments the offer may be in: its own, else those of the region / big-city area it names."""
    vague = job_region(job)
    if vague is not None:
        return vague.departments
    department = job_department(job)
    return (department,) if department else ()


def assess_location(job: JobOffer, profile: Profile, contract: ContractType | None = None) -> LocationFit:
    """Score 0..1 + French reason; ``blocking`` is set when the offer is out of every search area.

    ``contract`` (the offer's detected contract) ignores the areas restricted to other contract types."""
    areas = _areas(profile, contract)
    vague = job_region(job)
    remote = profile.target.remote_ok and _remote_evidence(job, vague)
    if not areas:
        return LocationFit(UNKNOWN_SCORE, "Aucune zone de recherche configurée dans le profil")
    coords = job_coordinates(job)
    with_coords = [a for a in areas if a.coords]
    if coords and with_coords:
        return _by_distance(job, coords, with_coords, remote)
    if vague is not None:
        return _by_region(job, vague, areas, remote)
    return _by_department(job, areas, remote)


def _remote_evidence(job: JobOffer, vague: VaguePlace | None) -> bool:
    """Full remote said in the offer, or a remote flag that can be trusted (see FULL_REMOTE_FLAG_SOURCES)."""
    if filters.is_full_remote(job):
        return True
    if not job.remote:
        return False
    if vague is not None:
        return job.source in FULL_REMOTE_FLAG_SOURCES
    return not is_place(job.location)  # e.g. LinkedIn "Remote", "France"


def _too_far(job: JobOffer, detail: str = "", distance_km: float | None = None) -> LocationFit:
    place = place_label(job.location) or job.postal_code or "lieu de l'offre"
    hybrid = filters.mentions_remote(job) and not filters.is_full_remote(job)  # full remote: the profile refuses it
    partial = " — télétravail partiel seulement" if hybrid else ""
    reason = f"Écartée : trop loin : {place} (hors de tes zones){detail}{partial}"
    return LocationFit(0.0, reason, blocking=reason, distance_km=distance_km)


def _by_distance(job: JobOffer, coords: tuple[float, float], areas: list[_Area], remote: bool) -> LocationFit:
    distances = [(haversine_km(*coords, *a.coords), a) for a in areas if a.coords]
    distance, area = min(distances, key=lambda pair: pair[0] - pair[1].radius_km)
    if distance <= area.radius_km:
        score = 1.0 - 0.5 * distance / max(area.radius_km, 1)
        extra = " — télétravail partiel possible" if filters.mentions_remote(job) else ""
        return LocationFit(round(score, 3), f"À {distance:.0f} km de {area.city}{extra}", distance_km=distance)
    if remote:
        return LocationFit(REMOTE_SCORE, f"Télétravail complet (poste basé à {distance:.0f} km de {area.city})",
                           distance_km=distance)
    return _too_far(job, f" — {distance:.0f} km de {area.city}, rayon {area.radius_km} km", distance)


def _by_region(job: JobOffer, vague: VaguePlace, areas: list[_Area], remote: bool) -> LocationFit:
    """A whole region: kept (with a penalty) when it contains a search area, else too far."""
    if remote:
        return LocationFit(REMOTE_SCORE, f"Télétravail complet ({vague.label})")
    french = [a for a in areas if a.country == FRANCE]
    undecided = any(a.department is None for a in french)  # an area whose department is unknown
    if undecided or any(a.department in vague.departments for a in french):
        reason = f"Localisation : lieu imprécis ({vague.label}), -{VAGUE_REGION_PENALTY:g} points"
        return LocationFit(UNKNOWN_SCORE, reason, penalty=VAGUE_REGION_PENALTY)
    return _too_far(job)


def _by_department(job: JobOffer, areas: list[_Area], remote: bool) -> LocationFit:
    department = job_department(job)
    if remote:
        return LocationFit(REMOTE_SCORE, "Télétravail complet")
    if department is None:
        return _unknown_place(job)
    if department in {a.department for a in areas if a.department}:
        return LocationFit(SAME_DEPARTMENT_SCORE, f"Dans le département {department}")
    return _too_far(job, f" — département {department}")


def _unknown_place(job: JobOffer) -> LocationFit:
    if is_country_only(job.location):
        where = f" ({place_label(job.location)})" if place_label(job.location) else ""
        reason = f"Localisation : lieu non précisé{where}, -{COUNTRY_ONLY_PENALTY:g} points"
        return LocationFit(UNKNOWN_SCORE, reason, penalty=COUNTRY_ONLY_PENALTY)
    if not is_place(job.location):
        return LocationFit(UNKNOWN_SCORE, "Localisation non précisée")
    return LocationFit(UNKNOWN_SCORE, f"Localisation à vérifier ({job.location})")
