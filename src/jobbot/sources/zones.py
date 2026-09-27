"""Does an offer fall inside a search query's area? For sources that list a whole employer (company career
boards) instead of searching around a place: only offers near the query's place (or full remote for a remote
query) are kept. Offline first (offer coordinates, known cities, postal department); a French place can be
geocoded through an injected ``locate`` function.
"""

from __future__ import annotations

from collections.abc import Callable

from jobbot.config import LocationPref
from jobbot.matching.geo import department_from_location, department_from_postal, haversine_km, lookup_known_place
from jobbot.models import JobOffer
from jobbot.sources.base import SearchQuery
from jobbot.sources.countries import FRANCE, country_name, is_foreign, location_country
from jobbot.textutil import normalize

Locate = Callable[[JobOffer], tuple[float, float] | None]


def offer_country(job: JobOffer) -> str | None:
    """ISO code stored by the source (``raw["country_code"]``, WTTJ ``office_country``), None when unknown."""
    code = job.raw.get("country_code") or job.raw.get("office_country")
    return str(code).upper() if code else None


def in_query_zone(job: JobOffer, query: SearchQuery, locate: Locate | None = None) -> bool:
    """True when ``job`` answers ``query``'s place: same country, then distance ≤ radius (coordinates), else
    same department; a remote query keeps full-remote offers of the country (France)."""
    loc = None if query.remote else query.location
    wanted = location_country(loc)
    code = offer_country(job)
    if code and code != wanted:
        return False
    if query.remote:
        return job.remote is True
    if loc is None:
        return True
    if is_foreign(loc):
        return code == wanted or normalize(country_name(wanted)) in normalize(job.location)
    return _near(job, loc, locate)


def _near(job: JobOffer, loc: LocationPref, locate: Locate | None) -> bool:
    area = _area_coords(loc)
    department = department_from_postal(job.postal_code) or department_from_location(job.location)
    area_department = department_from_postal(loc.postal_code) or department_from_location(loc.city)
    if area is not None:
        coords = _offer_coords(job) or (locate(job) if locate and department != area_department else None)
        if coords is not None:
            return haversine_km(*coords, *area) <= loc.radius_km
    return department is not None and department == area_department


def _area_coords(loc: LocationPref) -> tuple[float, float] | None:
    if loc.latitude is not None and loc.longitude is not None:
        return loc.latitude, loc.longitude
    return lookup_known_place(loc.city)


def _offer_coords(job: JobOffer) -> tuple[float, float] | None:
    if job.latitude is not None and job.longitude is not None:
        return job.latitude, job.longitude
    return lookup_known_place(job.location) if job.location else None


def is_french_place(job: JobOffer) -> bool:
    return offer_country(job) in (None, FRANCE)
