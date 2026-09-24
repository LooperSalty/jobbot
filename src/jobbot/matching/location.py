"""Location fit of an offer against the profile's search areas (offline, deterministic).

Order of evidence: offer coordinates → known city name → postal department. Full remote offers pass
whatever the distance when ``profile.target.remote_ok``.
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
from jobbot.models import ContractType, JobOffer

REMOTE_SCORE = 0.9
SAME_DEPARTMENT_SCORE = 0.7
UNKNOWN_SCORE = 0.5


@dataclass(frozen=True)
class LocationFit:
    score: float  # 0..1
    reason: str
    blocking: str | None = None  # hard-filter reason (too far)
    distance_km: float | None = None


@dataclass(frozen=True)
class _Area:
    city: str
    radius_km: int
    coords: tuple[float, float] | None
    department: str | None


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
    return _Area(loc.city, loc.radius_km, coords, department)


def job_coordinates(job: JobOffer) -> tuple[float, float] | None:
    if job.latitude is not None and job.longitude is not None:
        return job.latitude, job.longitude
    return lookup_known_place(job.location) if is_place(job.location) else None


def job_department(job: JobOffer) -> str | None:
    return department_from_postal(job.postal_code) or department_from_location(job.location)


def assess_location(job: JobOffer, profile: Profile, contract: ContractType | None = None) -> LocationFit:
    """Score 0..1 + French reason; ``blocking`` is set when the offer is out of every search area.

    ``contract`` (the offer's detected contract) ignores the areas restricted to other contract types."""
    areas = _areas(profile, contract)
    remote_only_offer = bool(job.remote) and not is_place(job.location)  # e.g. LinkedIn "Remote", "France"
    remote = profile.target.remote_ok and (filters.is_full_remote(job) or remote_only_offer)
    if not areas:
        return LocationFit(UNKNOWN_SCORE, "Aucune zone de recherche configurée dans le profil")
    coords = job_coordinates(job)
    with_coords = [a for a in areas if a.coords]
    if coords and with_coords:
        return _by_distance(job, coords, with_coords, remote)
    return _by_department(job, areas, remote)


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
    partial = " — télétravail partiel seulement" if filters.mentions_remote(job) else ""
    reason = f"Écartée : trop loin ({distance:.0f} km de {area.city}, rayon {area.radius_km} km){partial}"
    return LocationFit(0.0, reason, blocking=reason, distance_km=distance)


def _by_department(job: JobOffer, areas: list[_Area], remote: bool) -> LocationFit:
    department = job_department(job)
    if remote:
        return LocationFit(REMOTE_SCORE, "Télétravail complet")
    if department is None:
        if not is_place(job.location):
            return LocationFit(UNKNOWN_SCORE, "Localisation non précisée")
        return LocationFit(UNKNOWN_SCORE, f"Localisation à vérifier ({job.location})")
    if department in {a.department for a in areas if a.department}:
        return LocationFit(SAME_DEPARTMENT_SCORE, f"Dans le département {department}")
    reason = f"Écartée : hors zone (département {department})"
    return LocationFit(0.0, reason, blocking=reason)
