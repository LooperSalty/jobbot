"""outreach.companies — recherche-entreprises search + discovery (no network: respx + fixtures)."""

from __future__ import annotations

import copy
import dataclasses
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
import respx

from jobbot import clock
from jobbot.http import HostThrottle
from jobbot.models import Company, ContactEmail, ContractType
from jobbot.outreach import companies
from jobbot.outreach.companies import (
    ROMES_DEV,
    CompanySearchError,
    DiscoverReport,
    company_from_result,
    discover_companies,
    distance_km,
    search_companies,
    tranche_codes_from,
)
from jobbot.textutil import stable_id

FIX = Path(__file__).parent / "fixtures" / "outreach"
SEARCH_URL = "https://recherche-entreprises.api.gouv.fr/search"


def _page() -> dict:
    return json.loads((FIX / "search_62.01Z_57_p1.json").read_text(encoding="utf-8"))


def _item(siren: str) -> dict:
    return copy.deepcopy(next(r for r in _page()["results"] if r["siren"] == siren))


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(companies, "_THROTTLE", HostThrottle(0))
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    monkeypatch.setitem(sys.modules, "jobbot.sources.lba", None)  # LBA source absent unless a test installs one


# --------------------------------------------------------------------------- search_companies


@respx.mock
def test_search_maps_live_fixture_and_sends_expected_params():
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page()))
    found = search_companies("62.01Z", "57", min_headcount_code="02", max_pages=1)

    params = route.calls[0].request.url.params
    assert params["activite_principale"] == "62.01Z" and params["departement"] == "57"
    assert params["etat_administratif"] == "A" and params["est_entrepreneur_individuel"] == "false"
    assert params["per_page"] == "25" and params["page"] == "1"
    assert params["tranche_effectif_salarie"].split(",")[:3] == ["02", "03", "11"]
    assert "NN" not in params["tranche_effectif_salarie"]
    assert route.calls[0].request.headers["user-agent"].startswith("jobbot/")

    by_siren = {c.siren: c for c in found}
    assert set(by_siren) == {"519037253", "345051684", "812991172", "792907750", "800065146"}
    le16 = by_siren["345051684"]
    assert le16.name == "LE16SOLUTIONS"  # trade name of the active local establishment
    assert le16.city == "Faulquemont" and le16.postal_code == "57380"
    assert isinstance(le16.latitude, float) and le16.raw["siret"] == "34505168400107"
    trust = by_siren["519037253"]
    assert trust.id == stable_id("company", "519037253")
    assert trust.leaders == []  # only a statutory auditor as natural person
    assert trust.headcount_range == "50 à 99 salariés" and trust.naf_label == "Programmation informatique"
    assert trust.raw["tva"] == "FR65519037253" and "1486" in trust.raw["idcc"]
    assert trust.city == "Ars Laquenexy" and trust.source == "recherche_entreprises"
    bro = by_siren["800065146"]
    assert bro.leaders == ["Luc VERDIER"]
    assert bro.raw["leaders_detail"] == [{"first": "Luc", "last": "VERDIER", "role": "Président de SAS"}]
    assert "YAAKADEV" in by_siren["812991172"].raw["names"]


@respx.mock
def test_search_never_keeps_birth_dates():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page()))
    for company in search_companies("62.01Z", "57", min_headcount_code=None, max_pages=1):
        dumped = json.dumps(company.model_dump(mode="json"), ensure_ascii=False)
        assert "naissance" not in dumped and "1970" not in dumped


@respx.mock
def test_search_paginates_until_last_page():
    first, second = _page(), _page()
    first.update(total_pages=2, page=1)
    second.update(total_pages=2, page=2, results=[_item("800065146")])
    route = respx.get(SEARCH_URL).mock(side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)])
    found = search_companies("62.01Z", "57", min_headcount_code=None, max_pages=5)
    assert route.call_count == 2 and len(found) == 6
    assert "tranche_effectif_salarie" not in route.calls[0].request.url.params


@respx.mock
def test_search_respects_max_pages():
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page()))  # total_pages = 9
    search_companies("62.01Z", "57", min_headcount_code=None, max_pages=2)
    assert route.call_count == 2


@respx.mock
def test_search_400_raises_readable_error():
    body = (FIX / "search_error_400.json").read_text(encoding="utf-8")
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(400, text=body, headers={"content-type": "application/json"}))
    with pytest.raises(CompanySearchError, match="non valide"):
        search_companies("62.10Y", "57", min_headcount_code=None)


@respx.mock
def test_search_retries_html_429_then_succeeds():
    route = respx.get(SEARCH_URL).mock(side_effect=[
        httpx.Response(429, text="<html><title>429 Too Many Requests</title></html>"),
        httpx.Response(200, json={**_page(), "total_pages": 1}),
    ])
    assert len(search_companies("62.01Z", "57", min_headcount_code=None)) == 5
    assert route.call_count == 2


@respx.mock
def test_search_network_error_and_bad_json_raise():
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(CompanySearchError, match="injoignable"):
        search_companies("62.01Z", "57", min_headcount_code=None)
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="pas du json"))
    with pytest.raises(CompanySearchError, match="non JSON"):
        search_companies("62.01Z", "57", min_headcount_code=None)
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(CompanySearchError, match="inattendue"):
        search_companies("62.01Z", "57", min_headcount_code=None)


def test_tranche_codes_from():
    assert tranche_codes_from("02")[0] == "02" and tranche_codes_from("02")[-1] == "53"
    assert "NN" not in tranche_codes_from("NN")
    with pytest.raises(ValueError):
        tranche_codes_from("99")


def test_company_from_result_rejections():
    ok = _item("800065146")
    assert company_from_result(ok, {"57"}) is not None
    assert company_from_result({**ok, "siren": "12"}, {"57"}) is None
    assert company_from_result({**ok, "etat_administratif": "C"}, {"57"}) is None
    assert company_from_result({**ok, "complements": {"est_entrepreneur_individuel": True}}, {"57"}) is None
    closed = copy.deepcopy(ok)
    for est in closed["matching_etablissements"]:
        est["etat_administratif"] = "F"
    assert company_from_result(closed, {"57"}) is None
    assert company_from_result(ok, {"54"}) is None  # no active establishment in 54
    no_match = {**ok, "matching_etablissements": []}
    assert company_from_result(no_match, {"57"}).postal_code == ok["siege"]["code_postal"]  # falls back to HQ


def test_distance_km_metz_nancy():
    assert 45 < distance_km(49.1193, 6.1757, 48.6921, 6.1844) < 50


# --------------------------------------------------------------------------- discover_companies


def _shard_responder(error_naf: str | None = None):
    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params["activite_principale"] == error_naf:
            return httpx.Response(400, json={"erreur": "code NAF refusé"})
        if params["activite_principale"] == "62.01Z" and params["departement"] == "57":
            return httpx.Response(200, json={**_page(), "total_pages": 1})
        return httpx.Response(200, json={"results": [], "total_results": 0, "total_pages": 0})

    return respond


def _install_lba(monkeypatch, recruiters=None, error: Exception | None = None):
    calls = []

    class FakeLBA:
        def __init__(self, secrets):
            self.secrets = secrets

        def search_recruiters(self, latitude, longitude, radius_km, romes):
            calls.append((latitude, longitude, radius_km, romes))
            if error:
                raise error
            return list(recruiters or [])

    module = types.ModuleType("jobbot.sources.lba")
    module.LaBonneAlternanceSource = FakeLBA
    monkeypatch.setitem(sys.modules, "jobbot.sources.lba", module)
    return calls


def _lba_company(siren: str, name: str, lat: float = 49.11, lon: float = 6.17) -> Company:
    return Company(id=f"lba-{siren}", name=name, siren=siren, city="Metz", latitude=lat, longitude=lon,
                   source="lba_recruteur", found_at=clock.now(), raw={"hasEmail": True, "siret": siren + "00038"})


@respx.mock
def test_discover_filters_dedupes_and_merges_lba(ctx, monkeypatch):
    route = respx.get(SEARCH_URL).mock(side_effect=_shard_responder(error_naf="63.12Z"))
    lba_calls = _install_lba(monkeypatch, [_lba_company("519037253", "TRUSTTEAM"), _lba_company("788876522", "EFLUID")])
    ctx.repo.blacklist_add("YAAKADEV", "test")

    report = discover_companies(ctx)

    assert isinstance(report, DiscoverReport)
    assert route.call_count == len(ctx.profile.target.naf_codes) * len(ctx.profile.target.departments)
    stored = {c.siren: c for c in ctx.repo.list_companies()}
    # YAAKADEV blacklisted, CYBERG (Stiring-Wendel, ~56 km) too far
    assert set(stored) == {"519037253", "345051684", "800065146", "788876522"}
    assert report.found == 4 and report.new == 4
    assert len(report.errors) == 2 and all("63.12Z" in e for e in report.errors)  # one per department
    assert stored["519037253"].source == "lba_recruteur" and stored["519037253"].raw["hasEmail"] is True
    assert stored["788876522"].id == stable_id("company", "788876522")
    assert all(call[3] == list(ROMES_DEV) for call in lba_calls) and {c[2] for c in lba_calls} == {40, 25}
    assert stored["519037253"].raw["outreach_score"] > stored["800065146"].raw["outreach_score"]


@respx.mock
def test_discover_second_run_keeps_enrichment(ctx):
    respx.get(SEARCH_URL).mock(side_effect=_shard_responder())
    discover_companies(ctx, naf_codes=["62.01Z"], departments=["57"])
    trust = ctx.repo.get_company(stable_id("company", "519037253"))
    enriched = trust.model_copy(update={
        "status": "enriched", "domain": "trustteam.eu",
        "contacts": [ContactEmail(email="contact@trustteam.fr", kind="generic")],
        "raw": {**trust.raw, "email_sources": {"contact@trustteam.fr": "https://www.trustteam.eu/fr-fr"}},
    })
    ctx.repo.upsert_company(enriched)

    report = discover_companies(ctx, naf_codes=["62.01Z"], departments=["57"])

    assert report.new == 0 and report.found == 4  # CYBERG still too far
    again = ctx.repo.get_company(trust.id)
    assert again.status == "enriched" and again.domain == "trustteam.eu" and again.contacts
    assert again.raw["email_sources"] and again.raw["siret"] == "51903725300084"


def _with_target(ctx, **update):
    return dataclasses.replace(ctx, profile=ctx.profile.model_copy(update={
        "target": ctx.profile.target.model_copy(update=update),
    }))


@respx.mock
def test_discover_limit_keeps_best_scored(ctx):
    respx.get(SEARCH_URL).mock(side_effect=_shard_responder())
    report = discover_companies(ctx, naf_codes=["62.01Z"], departments=["57"], limit=1)
    assert report.found == 1 and [c.siren for c in ctx.repo.list_companies()] == ["519037253"]


@respx.mock
def test_discover_skips_excluded_companies(ctx):
    respx.get(SEARCH_URL).mock(side_effect=_shard_responder())
    excluded_ctx = _with_target(ctx, exclude_companies=["Trustteam SAS"])
    report = discover_companies(excluded_ctx, naf_codes=["62.01Z"], departments=["57"])
    assert report.found == 3
    assert "519037253" not in {c.siren for c in excluded_ctx.repo.list_companies()}


@respx.mock
def test_discover_records_lba_failures(ctx, monkeypatch):
    respx.get(SEARCH_URL).mock(side_effect=_shard_responder())
    _install_lba(monkeypatch, error=RuntimeError("LBA en panne"))
    report = discover_companies(ctx, naf_codes=["62.01Z"], departments=["57"])
    assert report.found == 4
    assert any("LBA" in e and "panne" in e for e in report.errors)


@respx.mock
def test_discover_skips_lba_when_not_looking_for_alternance(ctx, monkeypatch):
    respx.get(SEARCH_URL).mock(side_effect=_shard_responder())
    calls = _install_lba(monkeypatch, [_lba_company("788876522", "EFLUID")])
    discover_companies(_with_target(ctx, contract_types=[ContractType.CDI]), naf_codes=["62.01Z"], departments=["57"])
    assert calls == []


@respx.mock
def test_discover_geocodes_missing_coordinates(ctx, monkeypatch):
    page = _page()
    for item in page["results"]:
        for est in item["matching_etablissements"]:
            est["latitude"] = est["longitude"] = None
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={**page, "total_pages": 1}))
    geocoded = []

    def fake_geocode(query, *, postcode=None, client=None):
        geocoded.append((query, postcode))
        return (43.6, 3.87) if "FAULQUEMONT" in query else None  # Montpellier → too far

    monkeypatch.setattr("jobbot.outreach.domains.geocode_address", fake_geocode)
    report = discover_companies(ctx, naf_codes=["62.01Z"], departments=["57"])
    assert len(geocoded) == 5
    assert "345051684" not in {c.siren for c in ctx.repo.list_companies()}
    assert report.found == 4  # the others have no coordinates → kept (department filter applies)


def test_discover_uses_location_departments_when_profile_has_none(ctx, monkeypatch):
    seen = []
    monkeypatch.setattr(companies, "search_companies",
                        lambda naf, dep, **kw: seen.append((naf, dep)) or [])
    report = discover_companies(_with_target(ctx, departments=[], naf_codes=[]))
    assert {d for _, d in seen} == {"54", "57"}
    assert {n for n, _ in seen} == set(companies.DEFAULT_NAF_CODES)
    assert report.found == 0 and report.errors == []
