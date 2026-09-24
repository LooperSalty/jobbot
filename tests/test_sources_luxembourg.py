"""v2 search queries: per-location contract types and the Luxembourg track on every capable source (no network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobbot.config import LocationPref, Secrets
from jobbot.models import ContractType
from jobbot.sources import france_travail as ft
from jobbot.sources import free_work, jobspy_source, registry, wttj
from jobbot.sources import linkedin as li
from jobbot.sources.base import SearchQuery

LUX = LocationPref(city="Luxembourg", postal_code="", radius_km=25, latitude=49.6116, longitude=6.1319,
                   country="LU", contract_types=[ContractType.CDI])
LUX_NO_COORDS = LocationPref(city="Luxembourg", radius_km=25, country="LU", contract_types=[ContractType.CDI])
ESCH = LocationPref(city="Esch-sur-Alzette", radius_km=15, country="LU")
METZ = LocationPref(city="Metz", postal_code="57000", radius_km=40, latitude=49.1193, longitude=6.1757)
TOKEN = {"expires_in": 1499, "token_type": "Bearer", "access_token": "tok-1"}
PAYS = [{"code": "99100", "libelle": "France"}, {"code": "99137", "libelle": "LUXEMBOURG"},
        {"code": "99131", "libelle": "Belgique"}]


def q(**kw) -> SearchQuery:
    return SearchQuery(**{"keywords": "développeur full-stack", "location": LUX,
                          "contract_types": [ContractType.CDI], "max_results": 20, **kw})


def with_target(profile, **updates):
    return profile.model_copy(update={"target": profile.target.model_copy(update=updates)})


@pytest.fixture(autouse=True)
def _fast(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return  # live tests stay polite
    monkeypatch.setattr("jobbot.http.time.sleep", lambda _s: None)
    for mod in (free_work, jobspy_source, wttj):
        monkeypatch.setattr(mod, "MIN_INTERVAL_S", 0.0)


# --------------------------------------------------------------------------- build_queries


def test_luxembourg_queries_are_cdi_only(profile, settings):
    queries = registry.build_queries(profile, settings)
    lux = [qq for qq in queries if qq.location is not None and qq.location.country == "LU"]
    assert lux and all(qq.contract_types == [ContractType.CDI] and not qq.remote for qq in lux)
    alternance = [qq for qq in queries if qq.contract_types == [ContractType.ALTERNANCE]]
    assert all(qq.location is None or qq.location.country == "FR" for qq in alternance)


def test_location_contract_types_can_add_a_contract(profile, settings):
    locs = [loc.model_copy(update={"contract_types": [ContractType.CDI, ContractType.CDD]})
            if loc.country == "LU" else loc for loc in profile.target.locations]
    queries = registry.build_queries(with_target(profile, locations=locs), settings)
    cdd = [qq for qq in queries if qq.contract_types == [ContractType.CDD]]
    assert cdd and all(qq.location is not None and qq.location.city == "Luxembourg" for qq in cdd)
    assert len(cdd) == len({qq.keywords for qq in queries})  # one per title, no remote CDD query


def test_empty_target_contracts_with_a_restricted_location(profile, settings):
    queries = registry.build_queries(with_target(profile, contract_types=[]), settings)
    by_contract = {tuple(qq.contract_types) for qq in queries}
    assert by_contract == {(), (ContractType.CDI,)}
    assert all(qq.location is not None and qq.location.country == "LU"
               for qq in queries if qq.contract_types == [ContractType.CDI])
    assert not any(qq.location is not None and qq.location.country == "LU" for qq in queries if not qq.contract_types)


def test_luxembourg_capable_sources_keep_foreign_queries(profile, settings):
    queries = registry.build_queries(profile, settings)
    for name in ("france_travail", "indeed", "linkedin", "wttj", "free_work"):
        assert registry._supported(name, queries) == queries
    for name in ("lba", "apec"):
        assert all(qq.location is None or qq.location.country == "FR" for qq in registry._supported(name, queries))
    legacy = q(location=LocationPref(city="Luxembourg"))  # older profiles: no country field
    assert registry._supported("lba", [legacy]) == []


# --------------------------------------------------------------------------- France Travail


def ft_source() -> ft.FranceTravailSource:
    creds = Secrets(_env_file=None, france_travail_client_id="PAR_x", france_travail_client_secret="s")  # type: ignore[call-arg]
    return ft.FranceTravailSource(creds)


@respx.mock
def test_ft_luxembourg_uses_pays_continent_resolved_once():
    respx.post(ft.TOKEN_URL).respond(json=TOKEN)
    pays = respx.get(ft.COUNTRIES_URL).respond(json=PAYS, headers={"Content-Range": "pays */3"})
    geo = respx.get(ft.GEO_COMMUNES_URL).respond(json=[])
    search = respx.get(ft.SEARCH_URL).respond(204)
    src = ft_source()
    assert list(src.search(q())) == []
    assert list(src.search(q(location=ESCH))) == []
    params = dict(search.calls[0].request.url.params)
    assert params["paysContinent"] == "99137" and params["typeContrat"] == "CDI"
    assert not {"commune", "departement", "distance", "inclureLimitrophes"} & params.keys()
    assert pays.call_count == 1 and geo.call_count == 0 and search.call_count == 2
    assert pays.calls[0].request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(500), httpx.Response(200, json={"oops": 1}),
    httpx.Response(200, json=[{"code": "1", "libelle": "Italie"}]),
])
def test_ft_luxembourg_query_skipped_when_country_unknown(response, caplog):
    respx.post(ft.TOKEN_URL).respond(json=TOKEN)
    pays = respx.get(ft.COUNTRIES_URL).mock(return_value=response)
    search = respx.get(ft.SEARCH_URL).respond(204)
    src = ft_source()
    assert list(src.search(q())) == [] and list(src.search(q())) == []
    assert search.call_count == 0 and pays.call_count <= 4  # 500 is retried, then cached for the run
    assert "Luxembourg" in caplog.text


@respx.mock
def test_ft_referential_network_error_skips_the_query():
    respx.post(ft.TOKEN_URL).respond(json=TOKEN)
    respx.get(ft.COUNTRIES_URL).mock(side_effect=httpx.ConnectError("boom"))
    search = respx.get(ft.SEARCH_URL).respond(204)
    assert list(ft_source().search(q())) == [] and search.call_count == 0


def test_pick_country():
    assert ft.pick_country(PAYS, "Luxembourg") == "99137"
    grand_duchy = [{"code": "7", "libelle": "Luxembourg (Grand-Duché)"}, {"libelle": "x"}]
    assert ft.pick_country(grand_duchy, "Luxembourg") == "7"
    assert ft.pick_country([{"code": "", "libelle": "Luxembourg"}, "junk"], "Luxembourg") is None


# --------------------------------------------------------------------------- Indeed (JobSpy)


def test_jobspy_luxembourg_location_searches_indeed_luxembourg(monkeypatch):
    monkeypatch.setattr(jobspy_source.JobSpySource, "available", lambda self: (True, ""))
    seen: list[dict] = []
    monkeypatch.setattr(jobspy_source, "_run_worker", lambda py, kw, timeout_s: seen.append(kw) or [])
    assert list(jobspy_source.JobSpySource().search(q())) == []
    (kw,) = seen
    assert kw["country_indeed"] == "luxembourg" and kw["location"] == "Luxembourg" and kw["distance"] == 16
    assert kw["hours_old"] == 30 * 24 and kw["search_term"] == "développeur full-stack"
    both = jobspy_source.JobSpySource(countries=("france", "luxembourg"))
    list(both.search(q(location=ESCH)))
    assert seen[-1]["country_indeed"] == "luxembourg" and seen[-1]["location"] == "Esch-sur-Alzette"
    assert len(seen) == 2  # never an Indeed-France search around a Luxembourg place
    list(jobspy_source.JobSpySource().search(q(location=LocationPref(city="Oslo", country="NO"))))
    assert len(seen) == 2  # country unknown to Indeed/JobSpy → query skipped


def test_jobspy_kwargs_for_a_foreign_location():
    kw = jobspy_source._build_scrape_kwargs(q(), "luxembourg")
    assert (kw["location"], kw["distance"], kw["country_indeed"]) == ("Luxembourg", 16, "luxembourg")
    country_wide = jobspy_source._build_scrape_kwargs(q(location=METZ), "luxembourg")
    assert country_wide["location"] == "Luxembourg" and "distance" not in country_wide


# --------------------------------------------------------------------------- Free-Work


@pytest.mark.parametrize(("loc", "expected"), [
    (LUX, "lu~~~"), (ESCH, "lu~~~"), (LocationPref(city="Arlon", country="BE"), "be~~~"),
    (LocationPref(city="Luxembourg"), "lu~~~"),
])
def test_free_work_country_keys(loc, expected):
    assert free_work._location_key(loc) == expected


@respx.mock
def test_free_work_luxembourg_query_and_no_implicit_luxembourg_pass():
    route = respx.get(free_work.API_URL).respond(json={"hydra:member": []})
    src = free_work.FreeWorkSource(client=httpx.Client())
    assert list(src.search(q())) == []
    params = route.calls[0].request.url.params
    assert params["locationKeys"] == "lu~~~" and "locationRadius" not in params and params["contracts"] == "permanent"
    list(src.search(q(location=METZ)))
    assert [c.request.url.params["locationKeys"] for c in route.calls] == ["lu~~~", "fr~grand-est~moselle~metz"]


# --------------------------------------------------------------------------- LinkedIn


def test_linkedin_luxembourg_location():
    params = li.build_search_params(q(), easy_apply_only=False)
    assert params["geoId"] == li.GEO_IDS["luxembourg"] and params["location"] == "Luxembourg"
    assert "distance" not in params
    esch = li.build_search_params(q(location=ESCH), easy_apply_only=False)
    assert esch["location"] == "Luxembourg" and esch["geoId"] == li.GEO_IDS["luxembourg"]
    arlon = li.build_search_params(q(location=LocationPref(city="Arlon", country="BE")), easy_apply_only=False)
    assert arlon["location"] == "Arlon, Belgique" and "geoId" not in arlon
    belgium = li.build_search_params(q(location=LocationPref(city="", country="BE")), easy_apply_only=False)
    assert belgium["location"] == "Belgique"
    metz = li.build_search_params(q(location=METZ), easy_apply_only=True)
    assert metz["geoId"] == li.GEO_IDS["metz"] and "location" not in metz


# --------------------------------------------------------------------------- WTTJ


def test_wttj_luxembourg_search_body():
    body = wttj._build_search_body(q(), 0, 20)
    assert body["aroundLatLng"] == "49.6116,6.1319" and body["aroundRadius"] == 25000
    assert "contract_type:full_time" in body["filters"]
    no_coords = wttj._build_search_body(q(location=LUX_NO_COORDS), 0, 20)
    assert "offices.country_code:LU" in no_coords["filters"] and "offices.city" not in no_coords["filters"]
    assert "aroundLatLng" not in no_coords
    esch = wttj._build_search_body(q(location=ESCH), 0, 20)
    assert 'offices.city:"Esch-sur-Alzette"' in esch["filters"] and "offices.country_code:LU" in esch["filters"]


# --------------------------------------------------------------------------- live (opt-in: -m live)


@pytest.mark.live
def test_live_france_travail_luxembourg():
    from pathlib import Path

    creds = Secrets(_env_file=Path(__file__).resolve().parents[1] / ".env")  # type: ignore[call-arg]
    src = ft.FranceTravailSource(creds)
    if not src.available()[0]:
        pytest.skip("identifiants France Travail absents")
    assert src._country_code("LU")  # the « pays » referential knows Luxembourg
    jobs = list(src.search(q(keywords="développeur", max_results=5)))
    assert all(j.source == "france_travail" for j in jobs)


@pytest.mark.live
def test_live_free_work_luxembourg():
    from jobbot.matching.target_rules import offer_country

    jobs = list(free_work.FreeWorkSource().search(q(keywords="développeur", max_results=5, max_age_days=365)))
    assert jobs and any(offer_country(j) == "LU" for j in jobs)
