"""Adzuna and Jooble sources (official keyed APIs) — offline with respx, realistic trimmed fixtures."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.config import LocationPref, Secrets
from jobbot.models import ApplyMethod, ContractType
from jobbot.sources import adzuna, jooble, key_redaction
from jobbot.sources.base import SearchQuery, SourceUnavailable
from jobbot.sources.offer_utils import QueryFailed

FIX = Path(__file__).parent / "fixtures" / "sources_new"
METZ = LocationPref(city="Metz", postal_code="57000", radius_km=30, latitude=49.1193, longitude=6.1757)
LUX = LocationPref(city="Luxembourg", postal_code="1111", radius_km=20, country="LU")
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
ADZUNA_FR = "https://api.adzuna.com/v1/api/jobs/fr/search/"
JOOBLE_URL = "https://fr.jooble.org/api/cle-test-jooble"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def q(**kw) -> SearchQuery:
    base = dict(keywords="développeur", location=METZ, contract_types=[ContractType.ALTERNANCE], max_age_days=30)
    base.update(kw)
    return SearchQuery(**base)


def secrets(**values) -> Secrets:
    return Secrets(_env_file=None, **values)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for mod in (adzuna, jooble):
        monkeypatch.setattr(mod, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    monkeypatch.setattr("jobbot.clock.now", lambda: NOW)


# ----------------------------------------------------------------------------- Adzuna


def test_adzuna_needs_both_keys_and_ignores_commented_values():
    for values in ({}, {"adzuna_app_id": "id"}, {"adzuna_app_id": "id", "adzuna_app_key": "# à remplir"}):
        ok, reason = adzuna.AdzunaSource(secrets(**values)).available()
        assert ok is False and "ADZUNA_APP_ID" in reason and "developer.adzuna.com" in reason
    source = adzuna.AdzunaSource(secrets())
    with pytest.raises(SourceUnavailable, match="ADZUNA"):
        list(source.search(q()))
    assert adzuna.AdzunaSource(secrets(adzuna_app_id="id", adzuna_app_key="key")).available() == (True, "")


def test_adzuna_params_contract_remote_and_countries():
    alt = adzuna.build_params(q(), 50)
    assert alt["what"] == "développeur alternance" and alt["where"] == "Metz" and alt["distance"] == 30
    assert alt["max_days_old"] == 30 and alt["sort_by"] == "date" and alt["results_per_page"] == 50
    assert "permanent" not in alt and "contract" not in alt
    cdi = adzuna.build_params(q(contract_types=[ContractType.CDI]), 20)
    assert cdi["permanent"] == 1 and cdi["what"] == "développeur"
    assert adzuna.build_params(q(contract_types=[ContractType.CDD]), 20)["contract"] == 1
    remote = adzuna.build_params(q(location=None, remote=True, contract_types=[]), 50)
    assert remote["what_or"] == adzuna.REMOTE_WORDS and "where" not in remote
    assert adzuna._country(q(location=LUX)) is None  # Adzuna has no Luxembourg site
    assert adzuna._country(q(location=LocationPref(city="Liège", country="BE"))) == "BE"
    assert adzuna._country(q(location=None, remote=True)) == "FR"


@respx.mock
def test_adzuna_search_maps_results():
    route = respx.get(url__startswith=ADZUNA_FR).mock(return_value=httpx.Response(200, json=load("adzuna_search.json")))
    source = adzuna.AdzunaSource(secrets(adzuna_app_id="app-id-test", adzuna_app_key="app-key-test"))
    jobs = list(source.search(q(max_results=50)))
    request = route.calls[0].request
    assert request.url.path == "/v1/api/jobs/fr/search/1"
    assert request.url.params["app_id"] == "app-id-test" and request.url.params["app_key"] == "app-key-test"
    assert [j.external_id for j in jobs] == ["4891234567", "4891234568", "4891234570"]  # sales job dropped
    alt, java, remote = jobs
    assert alt.source == "adzuna" and alt.title == "Développeur Full-Stack en alternance H/F"
    assert alt.contract_type == ContractType.ALTERNANCE and alt.company == "Acme Logiciels"
    assert (alt.location, alt.latitude, alt.longitude) == ("Metz, Moselle", 49.1193, 6.1757)
    assert alt.url.startswith("https://www.adzuna.fr/land/ad/4891234567") and alt.apply_method == ApplyMethod.EXTERNAL
    assert alt.salary is None and alt.raw["salary_predicted"] is True  # predicted salaries are not shown
    assert alt.published_at == datetime(2026, 9, 22, 8, 15, 42, tzinfo=UTC) and "<strong>" not in alt.description
    assert alt.raw["description_truncated"] is True and alt.raw["category"] == "Emplois Informatique"
    assert java.contract_type == ContractType.CDI and java.salary == "34 000 – 40 000 € / an"
    assert remote.remote is True and remote.contract_type == ContractType.OTHER
    assert source.fetch_details(alt) is alt
    assert route.call_count == 1  # 4 results < 50 per page: no second page


@respx.mock
def test_adzuna_pagination_skips_luxembourg_and_errors():
    page = load("adzuna_search.json")
    route = respx.get(url__startswith=ADZUNA_FR).mock(side_effect=[
        httpx.Response(200, json={**page, "count": 8, "results": page["results"][:2]}),
        httpx.Response(500, text="boom"),
    ])
    source = adzuna.AdzunaSource(secrets(adzuna_app_id="i", adzuna_app_key="k"))
    jobs = list(source.search(q(max_results=2, contract_types=[])))
    assert len(jobs) == 2 and route.call_count == 1  # max_results reached on page 1
    assert list(source.search(q(location=LUX))) == []
    respx.get(url__startswith=ADZUNA_FR).mock(return_value=httpx.Response(500))
    with pytest.raises(QueryFailed, match="HTTP 500"):  # one query fails, the source stays usable
        list(source.search(q()))
    assert source.available() == (True, "")
    respx.get(url__startswith=ADZUNA_FR).mock(return_value=httpx.Response(429))
    with pytest.raises(SourceUnavailable, match="quota"):
        list(source.search(q()))
    respx.get(url__startswith=ADZUNA_FR).mock(return_value=httpx.Response(401, json={"exception": "AUTH_FAIL"}))
    with pytest.raises(SourceUnavailable, match="refusé"):
        list(source.search(q()))
    assert source.available()[0] is False  # the source stops for the run


@respx.mock
def test_adzuna_second_page_failure_keeps_first_page():
    page = load("adzuna_search.json")
    full = [dict(page["results"][1], id=str(n)) for n in range(3)]
    route = respx.get(url__startswith=ADZUNA_FR).mock(side_effect=[
        httpx.Response(200, json={"count": 10, "results": full}), *[httpx.Response(503)] * 4,
    ])
    source = adzuna.AdzunaSource(secrets(adzuna_app_id="i", adzuna_app_key="k"))
    jobs = list(source.search(q(max_results=3, contract_types=[])))
    assert [j.external_id for j in jobs] == ["0", "1", "2"] and route.call_count == 1
    full_page = [dict(page["results"][1], id=str(n)) for n in range(adzuna.PER_PAGE)]
    route = respx.get(url__startswith=ADZUNA_FR).mock(side_effect=[
        httpx.Response(200, json={"count": 120, "results": full_page}), *[httpx.Response(503)] * 4,
    ])
    jobs = list(source.search(q(max_results=80, contract_types=[])))
    assert len(jobs) == adzuna.PER_PAGE and route.calls.last.request.url.path.endswith("/search/2")


# ----------------------------------------------------------------------------- Jooble


def test_jooble_key_and_body():
    ok, reason = jooble.JoobleSource(secrets()).available()
    assert ok is False and "JOOBLE_API_KEY" in reason and "jooble.org/api/about" in reason
    assert [jooble.radius_for(km) for km in (0, 3, 30, 40, 55, 200)] == [0, 4, 40, 40, 80, 80]
    body = jooble.build_body(q(), 1, 50)
    assert body == {"keywords": "développeur alternance", "location": "Metz", "radius": "40", "page": 1,
                    "ResultOnPage": 50, "companysearch": False}
    remote = jooble.build_body(q(location=None, remote=True, contract_types=[]), 1, 20)
    assert remote["location"] == "France" and remote["keywords"] == "développeur télétravail"
    assert "radius" not in remote


@respx.mock
def test_jooble_search_maps_jobs():
    route = respx.post(JOOBLE_URL).mock(return_value=httpx.Response(200, json=load("jooble_search.json")))
    source = jooble.JoobleSource(secrets(jooble_api_key="cle-test-jooble"))
    jobs = list(source.search(q(max_results=50)))
    assert json.loads(route.calls[0].request.content)["location"] == "Metz"
    assert [j.external_id for j in jobs] == ["-1234567890123456789", "5550001"]  # sales + too old dropped
    alt, dotnet = jobs
    assert alt.source == "jooble" and alt.title == "Développeur web PHP / Symfony en alternance"
    assert alt.contract_type == ContractType.ALTERNANCE and alt.location == "Metz (57)"
    assert alt.url.startswith("https://fr.jooble.org/desc/") and alt.apply_method == ApplyMethod.EXTERNAL
    assert "Symfony" in alt.description and "<b>" not in alt.description and "\xa0" not in alt.description
    assert alt.raw["board"] == "hellowork.com" and alt.raw["description_truncated"] is True
    assert alt.published_at == datetime(2026, 9, 22, tzinfo=UTC)
    assert dotnet.contract_type == ContractType.CDI and dotnet.salary == "35k - 42k €"
    assert list(source.search(q(location=LUX))) == [] and route.call_count == 1  # French key only


@respx.mock
def test_jooble_refused_key_and_cloudflare_challenge():
    challenge = httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="<title>Just a moment...</title>")
    respx.post(JOOBLE_URL).mock(return_value=challenge)
    source = jooble.JoobleSource(secrets(jooble_api_key="cle-test-jooble"))
    with pytest.raises(SourceUnavailable, match="anti-robot"):
        list(source.search(q()))
    assert source.available() == (True, "")  # a challenge is temporary: the key is not blamed
    respx.post(JOOBLE_URL).mock(return_value=httpx.Response(403, json={"error": "Invalid key"}))
    with pytest.raises(SourceUnavailable, match="refusé la clé"):
        list(source.search(q()))
    assert source.available()[0] is False
    respx.post(JOOBLE_URL).mock(side_effect=httpx.ConnectError("réseau"))
    healthy = jooble.JoobleSource(secrets(jooble_api_key="cle-test-jooble"))
    with pytest.raises(QueryFailed, match="injoignable"):  # this query only: the next ones still run
        list(healthy.search(q()))
    assert healthy.available() == (True, "")
    respx.post(JOOBLE_URL).mock(return_value=httpx.Response(200, json={"totalCount": "?", "jobs": {"x": 1}}))
    assert list(healthy.search(q())) == []  # odd payload: nothing, no crash


@respx.mock
def test_jooble_key_is_encoded_in_the_path():
    route = respx.post("https://fr.jooble.org/api/a%2Fb%3Fc").mock(return_value=httpx.Response(200, json={}))
    assert list(jooble.JoobleSource(secrets(jooble_api_key="a/b?c")).search(q())) == []
    assert route.call_count == 1


# ----------------------------------------------------------------------------- live (opt-in: -m live, keys in .env)


@pytest.mark.live
@pytest.mark.parametrize("factory", [adzuna.AdzunaSource, jooble.JoobleSource], ids=["adzuna", "jooble"])
def test_live_search(request, factory, monkeypatch):
    if "live" not in (expr := request.config.getoption("markexpr") or "") or "not live" in expr:
        pytest.skip("test réseau : lancer explicitement avec -m live")
    monkeypatch.undo()  # real clock and politeness delays
    from jobbot.config import load_secrets

    source = factory(load_secrets(Path(__file__).resolve().parents[1]))
    ok, reason = source.available()
    if not ok:
        pytest.skip(reason)
    jobs = list(source.search(q(max_results=5, contract_types=[])))
    assert all(j.title and j.url.startswith("https://") and j.apply_method == ApplyMethod.EXTERNAL for j in jobs)


# ----------------------------------------------------------------------------- key redaction


def test_api_keys_are_masked_in_http_logs(caplog):
    key_redaction.install()
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            'HTTP Request: GET https://api.adzuna.com/v1/api/jobs/fr/search/1?app_id=ID42&app_key=SECRET42&what=dev '
            '"HTTP/1.1 200 OK"')
        logging.getLogger("httpx").info('HTTP Request: POST https://fr.jooble.org/api/SECRET-JOOBLE "HTTP/1.1 200"')
    text = caplog.text
    assert "SECRET42" not in text and "ID42" not in text and "SECRET-JOOBLE" not in text
    assert "app_key=***" in text and "jooble.org/api/***" in text and "what=dev" in text
