"""Company address geocoding through the Géoplateforme (IGN) — api-adresse.data.gouv.fr is sunset.

Re-exported by ``outreach.domains`` (historical home of these names).
"""

from __future__ import annotations

import logging

import httpx

from jobbot.http import make_client, request_with_retry

log = logging.getLogger(__name__)

GEOCODE_URL = "https://data.geopf.fr/geocodage/search"


def geocode_address(
    query: str, *, postcode: str | None = None, client: httpx.Client | None = None
) -> tuple[float, float] | None:
    """(lat, lon) via the Géoplateforme (IGN) geocoder — api-adresse.data.gouv.fr is sunset."""
    if not query or not query.strip():
        return None
    params: dict[str, str | int] = {"q": query.strip()[:200], "limit": 1, "autocomplete": 0}
    if postcode:
        params["postcode"] = postcode
    own = client is None
    http = client or make_client()
    try:
        resp = request_with_retry(http, "GET", GEOCODE_URL, params=params, retries=2, backoff_s=1.0)
        if resp.status_code != 200:
            return None
        features = resp.json().get("features") or []
        lon, lat = features[0]["geometry"]["coordinates"][:2]  # GeoJSON order: [lon, lat]
        return float(lat), float(lon)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        log.info("Géocodage impossible pour %r : %s", query, exc)
        return None
    finally:
        if own:
            http.close()
