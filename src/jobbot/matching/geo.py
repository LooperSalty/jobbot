"""Distances and French geocoding.

Geocoding uses the IGN Géoplateforme (``data.geopf.fr/geocodage``) — ``api-adresse.data.gouv.fr`` is sunset.
GeoJSON coordinates are ``[lon, lat]``. Results are cached in-process; common cities are resolved offline.
"""

from __future__ import annotations

import logging
import math
import re
import threading

import httpx

from jobbot.http import make_client, request_with_retry
from jobbot.matching._text import norm

log = logging.getLogger(__name__)

GEOCODE_URL = "https://data.geopf.fr/geocodage/search"
MIN_SCORE = 0.4  # Géoplateforme relevance below which a result is considered a wrong guess
EARTH_RADIUS_KM = 6371.0088

# Offline coordinates (city centres) — avoids network calls for the usual places. Key = normalized name.
KNOWN_PLACES: dict[str, tuple[float, float]] = {
    "metz": (49.1193, 6.1757), "nancy": (48.6921, 6.1844), "thionville": (49.3579, 6.1683),
    "luxembourg": (49.6116, 6.1319), "esch sur alzette": (49.4958, 5.9806),
    "montigny les metz": (49.1006, 6.1536), "woippy": (49.1511, 6.1514), "marly": (49.0664, 6.1547),
    "hagondange": (49.2544, 6.1683), "maizieres les metz": (49.2128, 6.1597), "yutz": (49.3589, 6.1906),
    "hayange": (49.3289, 6.0617), "florange": (49.3222, 6.1206), "fameck": (49.2997, 6.1103),
    "amneville": (49.2575, 6.1419), "sainte marie aux chenes": (49.1911, 6.0003),
    "pont a mousson": (48.9053, 6.0547), "saint avold": (49.1044, 6.7069), "forbach": (49.1878, 6.8961),
    "sarreguemines": (49.1100, 7.0683), "sarrebourg": (48.7353, 7.0536), "creutzwald": (49.2053, 6.6958),
    "vandoeuvre les nancy": (48.6567, 6.1683), "villers les nancy": (48.6731, 6.1528), "laxou": (48.6858, 6.1497),
    "maxeville": (48.7106, 6.1639), "luneville": (48.5894, 6.4964), "toul": (48.6750, 5.8917),
    "epinal": (48.1725, 6.4494), "verdun": (49.1597, 5.3844), "bar le duc": (48.7728, 5.1603),
    "strasbourg": (48.5734, 7.7521), "mulhouse": (47.7508, 7.3359), "colmar": (48.0794, 7.3585),
    "reims": (49.2583, 4.0317), "dijon": (47.3220, 5.0415), "paris": (48.8566, 2.3522), "lyon": (45.7640, 4.8357),
    "lille": (50.6292, 3.0573), "toulouse": (43.6047, 1.4442), "bordeaux": (44.8378, -0.5792),
    "nantes": (47.2184, -1.5536), "marseille": (43.2965, 5.3698), "rennes": (48.1173, -1.6778),
    "montpellier": (43.6108, 3.8767), "nice": (43.7102, 7.2620), "grenoble": (45.1885, 5.7245),
    "sarrebruck": (49.2402, 6.9969), "saarbrucken": (49.2402, 6.9969), "treves": (49.7499, 6.6371),
    "trier": (49.7499, 6.6371),
    # other big French cities (also used for "<ville> et périphérie" areas — matching.regions)
    "troyes": (48.2973, 4.0744), "besancon": (47.2378, 6.0241), "saint etienne": (45.4397, 4.3872),
    "clermont ferrand": (45.7772, 3.0870), "aix en provence": (43.5297, 5.4474), "toulon": (43.1242, 5.9280),
    "avignon": (43.9493, 4.8055), "nimes": (43.8367, 4.3601), "perpignan": (42.6887, 2.8948),
    "pau": (43.2951, -0.3708), "la rochelle": (46.1603, -1.1511), "poitiers": (46.5802, 0.3404),
    "limoges": (45.8336, 1.2611), "angers": (47.4784, -0.5632), "le mans": (48.0061, 0.1996),
    "brest": (48.3904, -4.4861), "tours": (47.3941, 0.6848), "orleans": (47.9030, 1.9093),
    "rouen": (49.4432, 1.0999), "le havre": (49.4944, 0.1079), "caen": (49.1829, -0.3707),
    "amiens": (49.8941, 2.2958),
    # Luxembourg business areas often given without coordinates (distinctive names only: no French homonym)
    "kirchberg": (49.6280, 6.1620), "luxembourg kirchberg": (49.6280, 6.1620), "gasperich": (49.5856, 6.1236),
    "strassen": (49.6206, 6.0733), "howald": (49.5822, 6.1447), "hesperange": (49.5728, 6.1506),
    "munsbach": (49.6317, 6.2692), "leudelange": (49.5906, 6.0647), "differdange": (49.5242, 5.8914),
    "dudelange": (49.4806, 6.0875), "mamer": (49.6275, 6.0233), "capellen": (49.6453, 5.9906),
    "windhof": (49.6461, 5.9589), "contern": (49.5858, 6.2267), "sandweiler": (49.6167, 6.2167),
    "walferdange": (49.6583, 6.1319), "ettelbruck": (49.8475, 6.1042), "bettembourg": (49.5186, 6.1028),
    "schifflange": (49.5061, 6.0122), "niederanven": (49.6519, 6.2553), "senningerberg": (49.6497, 6.2297),
    "esch belval": (49.5009, 5.9486),
}
# Department names (normalized) → code, longest names first when matching ("meurthe et moselle" ⊃ "moselle").
DEPARTMENT_NAMES: dict[str, str] = {
    "meurthe et moselle": "54", "moselle": "57", "meuse": "55", "vosges": "88", "bas rhin": "67",
    "haut rhin": "68", "marne": "51", "haute marne": "52", "ardennes": "08", "aube": "10", "paris": "75",
    "hauts de seine": "92", "seine saint denis": "93", "val de marne": "94", "rhone": "69",
    "gironde": "33", "haute garonne": "31", "bouches du rhone": "13", "loire atlantique": "44",
}
_NOT_A_PLACE = re.compile(
    r"^(?:france|france entiere|teletravail|remote|full remote|a distance|hybride|non renseigne|n/?a|"
    r"grand est|lorraine|ile de france|europe|international)$"
)
_STREET_WORDS = re.compile(
    r"\b(?:rue|avenue|av|boulevard|bd|chemin|place|allee|route|impasse|quai|cours|square|zac|za|zi|parc|lieu dit)\b"
)

_CACHE: dict[str, tuple[float, float] | None] = {}
_LOCK = threading.Lock()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def city_key(location: str) -> str:
    """'Metz (57)' / '57 - METZ' / 'Metz, Grand Est, France' / 'Saint-Avold' → 'metz' / 'saint avold'."""
    t = norm(location)
    t = re.sub(r"\([^)]*\)", " ", t)
    t = t.split(",")[0]
    t = re.sub(r"\b\d{5}\b|^\s*(?:\d{2}|2[ab])\s*-\s*|\s*-\s*(?:\d{2}|2[ab])\s*$|\bcedex\b.*$", " ", t)
    t = re.sub(r"\b(?:\d{1,2}(?:e|er|eme)|arrondissement)\b", " ", t)
    t = re.sub(r"[^a-z' ]+", " ", t).replace("'", " ")
    t = re.sub(r"\bste\b", "sainte", re.sub(r"\bst\b", "saint", t))
    return " ".join(t.split())


def lookup_known_place(location: str) -> tuple[float, float] | None:
    """Offline coordinates for common cities, else None."""
    return KNOWN_PLACES.get(city_key(location))


def department_from_postal(postal_code: str | None) -> str | None:
    code = re.sub(r"\s", "", postal_code or "")
    if not re.fullmatch(r"\d{5}", code):
        return None
    if code.startswith("20"):
        return "2A" if int(code) < 20200 else "2B"
    return code[:3] if code.startswith("97") else code[:2]


def department_from_location(location: str | None) -> str | None:
    """Department code from a free location string: '57000 Metz', 'Metz (57)', '57 - METZ', 'Moselle'."""
    t = norm(location or "")
    if not t:
        return None
    postal = re.search(r"\b(\d{5})\b", t)
    if postal:
        return department_from_postal(postal.group(1))
    m = (re.search(r"\((\d{2}|2[ab]|97\d)\)", t) or re.match(r"^\s*(\d{2}|2[ab])\s*-", t)
         or re.search(r"-\s*(\d{2}|2[ab])\s*$", t))
    if m:
        return m.group(1).upper()
    flat = " " + re.sub(r"[^a-z]+", " ", t) + " "
    for name in sorted(DEPARTMENT_NAMES, key=len, reverse=True):
        if f" {name} " in flat:
            return DEPARTMENT_NAMES[name]
    return None


def is_place(location: str | None) -> bool:
    """False for empty / country-wide / remote pseudo-locations ('France', 'Télétravail'…)."""
    key = norm(location or "").strip(" .-")
    return bool(key) and not _NOT_A_PLACE.match(key)


def geocode(query: str, client: httpx.Client | None = None) -> tuple[float, float] | None:
    """(lat, lon) of a French place or address via the Géoplateforme; None if unknown or on error."""
    q = " ".join((query or "").split())
    if not is_place(q):
        return None
    key = norm(q)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    known = lookup_known_place(q)
    if known:
        result = known
    else:
        try:
            result = _query_geoplateforme(q, client)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Géocodage impossible pour « %s » : %s", q, exc)
            return None  # transient: not cached
    with _LOCK:
        _CACHE[key] = result
    return result


def _geocode_params(query: str) -> dict[str, str | int]:
    cleaned = re.sub(r"\((?:\d{2}|2[AB]|97\d)\)", " ", query)
    cleaned = re.sub(r"^\s*(?:\d{2}|2[AB])\s*-\s*|\s*-\s*(?:\d{2}|2[AB])\s*$", " ", cleaned)
    cleaned = " ".join(cleaned.replace(",", " ").split())
    params: dict[str, str | int] = {"q": cleaned[:200], "limit": 1, "autocomplete": 0}
    postal = re.search(r"\b(\d{5})\b", cleaned)
    if postal:
        params["postcode"] = postal.group(1)
    if not _STREET_WORDS.search(norm(cleaned)):
        params["type"] = "municipality"
    return params


def _query_geoplateforme(query: str, client: httpx.Client | None) -> tuple[float, float] | None:
    own = client is None
    http = client or make_client(timeout=10.0)
    try:
        resp = request_with_retry(http, "GET", GEOCODE_URL, params=_geocode_params(query), retries=1, backoff_s=1.0)
    finally:
        if own:
            http.close()
    if resp.status_code != 200:
        raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
    return _parse_feature(resp.json())


def _parse_feature(payload: object) -> tuple[float, float] | None:
    features = payload.get("features") if isinstance(payload, dict) else None
    if not features:
        return None
    feat = features[0]
    try:
        lon, lat = feat["geometry"]["coordinates"][:2]
        score = float(feat.get("properties", {}).get("score", 1.0))
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"réponse de géocodage inattendue: {exc}") from exc
    if score < MIN_SCORE:
        return None
    return float(lat), float(lon)
