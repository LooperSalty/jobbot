"""Live checks against real services (run explicitly: pytest tests/test_outreach_live.py -m live).

Polite: a handful of requests to recherche-entreprises, Clearbit, the Géoplateforme and one small company
website (yaakadev.com, whose legal notice shows its SIREN). Never sends anything.
"""

from __future__ import annotations

import pytest

from jobbot import clock
from jobbot.models import Company
from jobbot.outreach.companies import search_companies
from jobbot.outreach.domains import find_domain_verified, geocode_address
from jobbot.outreach.emails import crawl_site, has_mx
from jobbot.textutil import stable_id

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _only_when_selected(request):
    if "live" not in (request.config.getoption("-m") or "") or "not live" in request.config.getoption("-m"):
        pytest.skip("test live : lancer avec -m live")


def test_live_search_companies_metz():
    found = search_companies("62.01Z", "57", min_headcount_code="03", max_pages=1)
    assert found, "aucune entreprise renvoyée par recherche-entreprises"
    assert all(len(c.siren) == 9 and c.postal_code and c.postal_code.startswith("57") for c in found)
    assert all("naissance" not in str(c.raw) for c in found)


def test_live_find_domain_and_crawl_yaakadev():
    company = Company(id=stable_id("company", "812991172"), name="YAAKADEV", siren="812991172", city="Metz",
                      postal_code="57070", found_at=clock.now(), raw={"names": ["YAAKADEV"]})
    match = find_domain_verified(company)
    assert match is not None and match.domain == "yaakadev.com" and match.verified_by == "siren"
    crawl = crawl_site("yaakadev.com", max_pages=4)
    assert "secretariat@yaakadev.com" in [c.email for c in crawl.contacts]


def test_live_people_registry_and_site_yaakadev():
    """Registry dirigeants fetched by SIREN (no leaders_detail) + the site's pages; no search engine, no Hunter."""
    from jobbot.outreach.people import find_people_detailed

    company = Company(id=stable_id("company", "812991172"), name="YAAKADEV", siren="812991172", city="Metz",
                      domain="yaakadev.com", found_at=clock.now(), raw={"names": ["YAAKADEV"]})
    found = find_people_detailed(company, use_hunter=False, use_search=False)
    assert found.sources == ["registry", "website"]
    # live 2026-09-24: YAAKADEV's dirigeants are two holdings (personnes morales) → no registry person kept
    assert [p for p in found.people if p.source == "registry"] == []
    assert all(p.first and p.last and p.category for p in found.people)


def test_live_mx_and_geocoding():
    assert has_mx("yaakadev.com") is True
    assert has_mx("nonexistent-domain-xyz-123456.fr") is False
    lat, lon = geocode_address("Metz", postcode="57000")
    assert 49.0 < lat < 49.2 and 6.1 < lon < 6.3
