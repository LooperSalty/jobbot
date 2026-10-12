"""night.large — « grandes entreprises » of any sector: ETI / GE whose local site is big, nearest to the center
(respx, fictitious companies, no network)."""

from __future__ import annotations

import dataclasses
import sys
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx

from jobbot import clock
from jobbot.config import LargeCompanies
from jobbot.http import HostThrottle
from jobbot.night.budget import Deadline
from jobbot.night.harvest import harvest
from jobbot.night.large import large_companies, large_company
from jobbot.night.store import merge_company
from jobbot.outreach import companies
from tests.night_fakes import NEAR_URL, SEARCH_URL, page, registry_item

LATER = Deadline(clock.now() + timedelta(hours=2))
METZ = (49.1193, 6.1757)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(companies, "_THROTTLE", HostThrottle(0))
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    monkeypatch.setitem(sys.modules, "jobbot.sources.lba", None)


def _site(tranche: str, lat: float, lon: float, cp: str = "57000", city: str = "METZ", *, n: int = 1,
          active: bool = True) -> dict[str, Any]:
    return {"siret": f"9000000{n:02d}00011", "code_postal": cp, "libelle_commune": city, "latitude": str(lat),
            "longitude": str(lon), "adresse": f"{n} RUE DE L'EXEMPLE {cp} {city}", "departement": cp[:2],
            "etat_administratif": "A" if active else "F", "tranche_effectif_salarie": tranche}


def _large(siren: str, name: str, *sites: dict[str, Any], naf: str = "64.19Z", categorie: str = "GE") -> dict:
    item = registry_item(siren, name, naf=naf, tranche="53", categorie=categorie)
    return {**item, "matching_etablissements": list(sites)}


def _focus(ctx, enabled: bool):
    outreach = ctx.settings.outreach.model_copy(update={"large_companies": LargeCompanies(enabled=enabled)})
    return dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={"outreach": outreach}))


def _enabled(ctx):
    return _focus(ctx, True)


def test_settings_default_off_with_rings_around_the_first_area():
    cfg = LargeCompanies()
    assert cfg.enabled is False
    assert (cfg.min_site_tranche, cfg.max_km, cfg.rings_km) == ("21", 75, [15, 30, 50, 75])
    for bad in ("NN", "13", "99"):  # caught when the settings load, not at night
        with pytest.raises(ValueError, match="tranche"):
            LargeCompanies(min_site_tranche=bad)
    with pytest.raises(ValueError, match="division"):
        LargeCompanies(skip_divisions=["4"])


def test_the_biggest_local_site_becomes_the_company_place():
    item = _large("900000101", "BANQUE EXEMPLE", _site("11", 49.12, 6.18, n=1),
                  _site("22", 49.15, 6.15, "57140", "WOIPPY", n=2))
    company = large_company(item, LargeCompanies(enabled=True), METZ)
    assert company is not None
    assert (company.city, company.postal_code, company.raw["siret"]) == ("Woippy", "57140", "90000000200011")
    assert company.raw["site_tranche"] == "22" and company.raw["large_target"] is True
    assert company.raw["categorie_entreprise"] == "GE"


def test_a_digital_group_is_kept_but_never_flagged_as_outside_the_digital_sector():
    esn = _large("900000110", "CONSEIL NUMERIQUE EXEMPLE", _site("31", 49.12, 6.18), naf="62.02A")
    company = large_company(esn, LargeCompanies(enabled=True), METZ)
    assert company is not None and "large_target" not in company.raw  # its letter speaks of its own business


def test_coordinates_not_published_are_ignored():
    hidden = {**_site("31", 49.12, 6.18), "latitude": "[NON-DIFFUSIBLE]", "longitude": "[NON-DIFFUSIBLE]"}
    assert large_company(_large("900000111", "DISCRET EXEMPLE", hidden), LargeCompanies(enabled=True), METZ) is None


@pytest.mark.parametrize(("label", "item"), [
    ("site trop petit", _large("900000102", "PETITE AGENCE", _site("12", 49.12, 6.18))),
    ("effectif local inconnu", _large("900000103", "SANS EFFECTIF", _site("NN", 49.12, 6.18))),
    ("trop loin", _large("900000104", "LOINTAIN", _site("32", 48.57, 7.75))),  # Strasbourg's coordinates
    ("magasin", _large("900000105", "HYPER EXEMPLE", _site("22", 49.12, 6.18), naf="47.11D")),
    ("concession automobile", _large("900000109", "AUTOS EXEMPLE", _site("22", 49.12, 6.18), naf="45.11Z")),
    ("restauration (terrain)", _large("900000106", "CANTINE EXEMPLE", _site("22", 49.12, 6.18), naf="56.29A")),
    ("maison de retraite", _large("900000107", "EHPAD EXEMPLE", _site("22", 49.12, 6.18), naf="87.10A")),
    ("site fermé", _large("900000108", "FERME EXEMPLE", _site("32", 49.12, 6.18, active=False))),
])
def test_small_unknown_far_closed_sites_and_excluded_sectors_are_dropped(label, item):
    assert large_company(item, LargeCompanies(enabled=True), METZ) is None, label


@respx.mock
def test_every_department_is_read_page_by_page_with_the_category_filter(ctx):
    big = _large("900000201", "ASSURANCES EXEMPLE", _site("31", 49.11, 6.17))
    small = _large("900000202", "AGENCE EXEMPLE", _site("11", 49.11, 6.17))
    nancy = _large("900000203", "ENERGIE EXEMPLE", _site("41", 48.69, 6.18, "54000", "NANCY"), naf="35.11Z")

    def answer(request):
        params = request.url.params
        assert params["categorie_entreprise"] == "ETI,GE" and params["etat_administratif"] == "A"
        assert params["limite_matching_etablissements"] == "100"  # the API returns 10 establishments by default
        if params["departement"] == "57":
            items = [big] if params["page"] == "1" else [small]
            return httpx.Response(200, json=page(items, total=30))
        return httpx.Response(200, json=page([nancy]))

    route = respx.get(SEARCH_URL).mock(side_effect=answer)
    found, stopped = large_companies(_enabled(ctx), LATER, [])
    assert not stopped
    assert sorted(c.name for c in found) == ["ASSURANCES EXEMPLE", "ENERGIE EXEMPLE"]
    assert len(route.calls) == 3  # 57: 2 pages, 54: 1 page


@respx.mock
def test_one_failing_department_never_loses_the_others(ctx):
    nancy = _large("900000203", "ENERGIE EXEMPLE", _site("41", 48.69, 6.18, "54000", "NANCY"), naf="35.11Z")
    respx.get(SEARCH_URL).mock(side_effect=lambda r: httpx.Response(500, json={}) if r.url.params["departement"] == "57"
                               else httpx.Response(200, json=page([nancy])))
    errors: list[str] = []
    found, _ = large_companies(_enabled(ctx), LATER, errors)
    assert [c.name for c in found] == ["ENERGIE EXEMPLE"]
    assert errors and "57" in errors[0]


@respx.mock
def test_one_company_in_two_departments_is_kept_once_at_its_nearest_site(ctx):
    in_metz = _large("900000204", "TRANSPORTS EXEMPLE", _site("22", 49.11, 6.17, n=1), naf="49.10Z")
    in_nancy = _large("900000204", "TRANSPORTS EXEMPLE", _site("41", 48.69, 6.18, "54000", "NANCY", n=2),
                      naf="49.10Z")
    respx.get(SEARCH_URL).mock(side_effect=lambda r: httpx.Response(
        200, json=page([in_metz] if r.url.params["departement"] == "57" else [in_nancy])))
    found, _ = large_companies(_enabled(ctx), LATER, [])
    assert [(c.city, c.raw["siret"]) for c in found] == [("Metz", "90000000100011")]


@respx.mock
def test_the_deadline_stops_in_the_middle_of_a_department(ctx):
    calls = []

    def answer(request):
        calls.append(request.url.params["page"])
        return httpx.Response(200, json=page([_large("900000205", "ASSURANCES EXEMPLE", _site("31", 49.11, 6.17))],
                                             total=75))

    respx.get(SEARCH_URL).mock(side_effect=answer)
    found, stopped = large_companies(_enabled(ctx), Deadline(clock.now() - timedelta(seconds=1)), [])
    assert (stopped, found, calls) == (True, [], [])
    soon = Deadline(clock.now() + timedelta(hours=2))
    found, stopped = large_companies(_enabled(ctx), soon, [])
    assert not stopped and len(found) == 1


@respx.mock
def test_disabled_means_no_request(ctx):
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page([])))
    assert large_companies(_focus(ctx, False), LATER, []) == ([], False)  # whatever settings.yaml says
    assert not route.called


def _fresh_site(distance: float):
    found = large_company(_large("900000401", "BANQUE EXEMPLE", _site("31", 49.15, 6.15, "57140", "WOIPPY", n=4)),
                          LargeCompanies(enabled=True), METZ)
    return found.model_copy(update={"raw": {**found.raw, "distance_km": distance}})


def test_a_stored_place_keeps_its_own_siret_when_it_is_as_good(make_company):
    existing = make_company(siren="900000401", city="Metz", postal_code="57000", latitude=49.12, longitude=6.18,
                            raw={"siret": "90000040199999", "distance_km": 1.0})
    merged = merge_company(existing, _fresh_site(4.0))
    assert (merged.city, merged.raw["siret"], merged.raw["distance_km"]) == ("Metz", "90000040199999", 1.0)
    assert "site_tranche" not in merged.raw


def test_an_lba_recruiter_never_loses_its_establishment(make_company):
    existing = make_company(siren="900000401", source="lba_recruteur", city="Thionville", latitude=49.36,
                            longitude=6.17, raw={"siret": "90000040188888", "distance_km": 26.0, "hasEmail": True})
    merged = merge_company(existing, _fresh_site(4.0))
    assert (merged.city, merged.raw["siret"]) == ("Thionville", "90000040188888")


def test_a_far_head_office_gives_way_to_the_big_local_site(make_company):
    existing = make_company(siren="900000401", city="Paris", postal_code="75008", latitude=48.87, longitude=2.31,
                            raw={"siret": "90000040177777", "distance_km": 280.0})
    merged = merge_company(existing, _fresh_site(4.0))
    assert (merged.city, merged.raw["siret"], merged.raw["site_tranche"]) == ("Woippy", "90000000400011", "31")
    assert merged.latitude == 49.15


@respx.mock
def test_harvest_stores_the_large_sites_through_the_relevance_gate(ctx):
    respx.get(NEAR_URL).mock(return_value=httpx.Response(200, json=page([])))
    bank = _large("900000301", "BANQUE REGIONALE EXEMPLE", _site("41", 49.11, 6.17))
    respx.get(SEARCH_URL).mock(side_effect=lambda r: httpx.Response(
        200, json=page([bank] if r.url.params.get("categorie_entreprise") else [])))
    result = harvest(_enabled(ctx), LATER)
    stored = next(c for c in ctx.repo.list_companies() if c.siren == "900000301")
    assert stored.status == "new" and stored.raw.get("large_target") is True
    assert any("grandes entreprises : 1" in d for d in result.details)
