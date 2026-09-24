"""matching.geo: haversine, Géoplateforme geocoding (respx, no network), department helpers."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.matching import geo

FIXTURES = Path(__file__).parent / "fixtures" / "matching"


@pytest.fixture(autouse=True)
def _fresh_cache():
    geo.clear_cache()
    yield
    geo.clear_cache()


def test_haversine_metz_nancy():
    assert geo.haversine_km(49.1193, 6.1757, 48.6921, 6.1844) == pytest.approx(47.5, abs=0.5)
    assert geo.haversine_km(49.1193, 6.1757, 49.1193, 6.1757) == 0.0


@respx.mock
def test_geocode_parses_lon_lat_order_and_caches():
    payload = json.loads((FIXTURES / "geopf_talange.json").read_text(encoding="utf-8"))
    route = respx.get(geo.GEOCODE_URL).mock(return_value=httpx.Response(200, json=payload))
    with httpx.Client() as client:
        first = geo.geocode("Talange", client=client)
        second = geo.geocode("  talange ", client=client)
    assert first == pytest.approx((49.2353, 6.1719))  # (lat, lon) although GeoJSON is [lon, lat]
    assert second == first
    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["q"] == "Talange" and params["autocomplete"] == "0" and params["type"] == "municipality"


@respx.mock
def test_geocode_without_client_uses_own_client_and_sends_postcode():
    payload = json.loads((FIXTURES / "geopf_talange.json").read_text(encoding="utf-8"))
    route = respx.get(geo.GEOCODE_URL).mock(return_value=httpx.Response(200, json=payload))
    assert geo.geocode("12 rue des Écoles, 57525 Talange") == pytest.approx((49.2353, 6.1719))
    params = route.calls[0].request.url.params
    assert params["postcode"] == "57525" and "type" not in params  # street address → no municipality filter


@respx.mock
def test_geocode_no_result_and_low_score_return_none():
    respx.get(geo.GEOCODE_URL).mock(
        side_effect=[
            httpx.Response(200, json={"features": []}),
            httpx.Response(200, json={"features": [{"geometry": {"coordinates": [1, 2]}, "properties": {"score": 0.1}}]}),
        ]
    )
    assert geo.geocode("Nullepart-sur-Rien") is None
    assert geo.geocode("Autre Endroit") is None


@respx.mock
def test_geocode_errors_are_not_cached(monkeypatch):
    monkeypatch.setattr("jobbot.http.time.sleep", lambda _s: None)  # no real backoff in tests
    route = respx.get(geo.GEOCODE_URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.ConnectError("boom"), httpx.Response(503),
                     httpx.Response(503), httpx.Response(200, json={"features": "bad"}),
                     httpx.Response(200, json={"features": [{"geometry": {}}]})]
    )
    assert geo.geocode("Talange") is None  # network error (2 attempts)
    assert geo.geocode("Talange") is None  # 503 (2 attempts)
    assert geo.geocode("Talange") is None  # malformed "features" → treated as no result
    geo.clear_cache()
    assert geo.geocode("Talange") is None  # feature without coordinates
    assert route.call_count == 6


def test_geocode_offline_known_places_and_pseudo_locations():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(geo.GEOCODE_URL)
        assert geo.geocode("Metz (57)") == (49.1193, 6.1757)
        assert geo.geocode("57 - METZ") == (49.1193, 6.1757)
        assert geo.geocode("Télétravail") is None
        assert geo.geocode("France") is None
        assert geo.geocode("") is None
        assert route.call_count == 0


@pytest.mark.parametrize(
    ("location", "city", "department"),
    [
        ("Metz (57)", "metz", "57"), ("57 - METZ", "metz", "57"), ("Metz - 57", "metz", "57"),
        ("57000 Metz", "metz", "57"), ("Saint-Avold", "saint avold", None), ("St-Avold (57)", "saint avold", "57"),
        ("Metz, Grand Est, France", "metz", None), ("Meurthe-et-Moselle", "meurthe et moselle", "54"),
        ("Paris 15e Arrondissement", "paris", "75"), ("Ajaccio (2A)", "ajaccio", "2A"),
    ],
)
def test_city_key_and_department(location, city, department):
    assert geo.city_key(location) == city
    assert geo.department_from_location(location) == department


def test_department_from_postal_code():
    assert geo.department_from_postal("57000") == "57"
    assert geo.department_from_postal("20000") == "2A"
    assert geo.department_from_postal("20200") == "2B"
    assert geo.department_from_postal("97400") == "974"
    assert geo.department_from_postal("L-1234") is None
    assert geo.department_from_postal(None) is None
    assert geo.department_from_location(None) is None


def test_is_place():
    assert geo.is_place("Metz") and not geo.is_place("Télétravail") and not geo.is_place(" ")
