"""Country of a search location (``LocationPref.country``, ISO 3166 alpha-2) and the names job boards expect.

Shared by the sources that can search abroad (Luxembourg track) and by ``matching.target_rules``.
"""

from __future__ import annotations

from jobbot.config import LocationPref
from jobbot.textutil import normalize

FRANCE = "FR"
# ISO code → French name: France Travail « référentiel pays » lookup, LinkedIn « location », French reasons.
COUNTRY_NAMES = {
    "FR": "France", "LU": "Luxembourg", "BE": "Belgique", "DE": "Allemagne", "CH": "Suisse",
    "ES": "Espagne", "IT": "Italie", "NL": "Pays-Bas", "GB": "Royaume-Uni", "MC": "Monaco",
}
# ISO code → python-jobspy ``country_indeed`` value (docs/research/other-job-sources.md).
INDEED_COUNTRIES = {
    "FR": "france", "LU": "luxembourg", "BE": "belgium", "DE": "germany", "CH": "switzerland",
    "ES": "spain", "IT": "italy", "NL": "netherlands", "GB": "uk",
}
# A place named like its country: profiles written before ``country`` existed default to "FR".
_COUNTRY_CITIES = {"luxembourg": "LU"}


def location_country(loc: LocationPref | None) -> str:
    """ISO code of a search location ("FR" when unknown or national)."""
    if loc is None:
        return FRANCE
    code = (loc.country or "").strip().upper() or FRANCE
    if code == FRANCE:
        return _COUNTRY_CITIES.get(normalize(loc.city), FRANCE)
    return code


def is_foreign(loc: LocationPref | None) -> bool:
    return location_country(loc) != FRANCE


def country_name(code: str) -> str:
    """French name of an ISO code ("LU" → "Luxembourg"); the code itself when unknown."""
    key = (code or "").strip().upper()
    return COUNTRY_NAMES.get(key, key)
