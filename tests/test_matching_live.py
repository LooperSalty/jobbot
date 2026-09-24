"""Live checks against the real Géoplateforme (read-only). Skipped unless run with ``-m live``."""

from __future__ import annotations

import pytest

from jobbot.matching import geo

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _only_when_selected(request):
    markexpr = request.config.getoption("-m") or ""
    if "live" not in markexpr or "not live" in markexpr:
        pytest.skip("test réseau : lancer avec -m live")
    geo.clear_cache()


def test_live_geoplateforme_municipality():
    coords = geo.geocode("Pagny-sur-Moselle")
    assert coords is not None
    assert geo.haversine_km(*coords, 48.984, 6.022) < 5


def test_live_geoplateforme_address_with_postcode():
    coords = geo.geocode("1 place d'Armes 57000 Metz")
    assert coords is not None
    assert geo.haversine_km(*coords, 49.1196, 6.1764) < 2
