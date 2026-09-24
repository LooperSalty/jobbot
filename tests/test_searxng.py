"""Self-hosted SearXNG (settings.search_engine_url): used before Brave / DuckDuckGo, short politeness delay."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobbot.config import Secrets, Settings
from jobbot.http import HostThrottle
from jobbot.outreach import _searxng, domains
from jobbot.outreach._searxng import searxng_search
from jobbot.outreach.domains import find_domain_verified
from jobbot.outreach.people import find_people_detailed

ENGINE = "http://127.0.0.1:8888"
SEARCH = f"{ENGINE}/search"
HTML = {"content-type": "text/html; charset=utf-8"}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(domains, "_SITE_THROTTLE", HostThrottle(0))
    monkeypatch.setattr(domains, "_DDG", domains._DdgBreaker())
    monkeypatch.setattr(domains, "_sleep", lambda s: pytest.fail("pas d'attente DuckDuckGo avec SearXNG"))
    monkeypatch.setattr(domains, "_resolves", lambda d: False)
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    waits: list[float] = []
    monkeypatch.setattr(_searxng, "_sleep", waits.append)
    monkeypatch.setattr(_searxng, "_POLITE", _searxng._Polite())
    return waits


@pytest.fixture
def yaaka(make_company):
    return make_company(name="YAAKADEV", siren="812991172", city="Metz", postal_code="57070", domain=None,
                        website=None, contacts=[], raw={"names": ["YAAKADEV"]})


def test_settings_validate_the_engine_url():
    assert Settings(search_engine_url=" http://127.0.0.1:8888/ ").search_engine_url == ENGINE
    assert Settings(search_engine_url="").search_engine_url is None
    with pytest.raises(ValueError, match="SearXNG"):
        Settings(search_engine_url="127.0.0.1:8888")


@respx.mock
def test_searxng_search_parses_json_and_waits_a_little(_offline):
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"results": [
        {"url": "https://yaakadev.com/", "title": "Yaakadev — agence web"}, {"title": ""}, "bruit",
    ]}))
    with httpx.Client() as client:
        assert searxng_search(client, ENGINE + "/", "yaakadev metz") == [("https://yaakadev.com/",
                                                                          "Yaakadev — agence web")]
        searxng_search(client, ENGINE, "autre requête")
    params = route.calls[0].request.url.params
    assert params["q"] == "yaakadev metz" and params["format"] == "json"
    assert len(_offline) == 1 and _searxng.MIN_INTERVAL_S - 1 <= _offline[0] <= _searxng.MIN_INTERVAL_S + 1


@respx.mock
def test_searxng_failures_return_nothing():
    respx.get(SEARCH).mock(side_effect=[httpx.Response(403, text="format json interdit"),
                                        httpx.ConnectError("refusé"), httpx.Response(200, text="<html>")])
    with httpx.Client() as client:
        assert searxng_search(client, ENGINE, "q") == []
        assert searxng_search(client, ENGINE, "q") == []
        assert searxng_search(client, ENGINE, "q") == []
        assert searxng_search(client, None, "q") == []


@respx.mock
def test_find_domain_uses_searxng_before_brave_and_duckduckgo(yaaka):
    respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(domains.WIKIDATA_URL).mock(return_value=httpx.Response(200, json={"results": {"bindings": []}}))
    engine = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"results": [
        {"url": "https://www.societe.com/societe/yaakadev-812991172.html", "title": "annuaire"},
        {"url": "https://yaakadev-metz.fr/", "title": "Yaakadev"},
    ]}))
    respx.get("https://yaakadev-metz.fr/").mock(return_value=httpx.Response(
        200, text="<h1>Yaakadev</h1> agence web, 57070 Metz", headers=HTML))
    respx.get(url__startswith="https://yaakadev-metz.fr/").mock(return_value=httpx.Response(404))
    brave = respx.get(domains.BRAVE_URL).mock(return_value=httpx.Response(200, json={}))
    ddg = respx.get(domains.DDG_URL).mock(return_value=httpx.Response(200, text=""))
    match = find_domain_verified(yaaka, secrets=Secrets(_env_file=None, brave_api_key="k"), search_url=ENGINE)
    assert match.method == "searxng" and match.domain == "yaakadev-metz.fr"
    assert engine.called and not brave.called and not ddg.called


@respx.mock
def test_people_search_titles_come_from_searxng(make_company):
    company = make_company(name="BROMAKER", siren="800065146", domain=None, website=None, contacts=[],
                           leaders=[], raw={"leaders_detail": [], "names": ["BROMAKER"]})
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"results": [
        {"url": "https://fr.linkedin.com/in/c", "title": "Claire Dubois - Chargée de recrutement - Bromaker | LinkedIn"},
    ]}))
    ddg = respx.get(domains.DDG_URL).mock(return_value=httpx.Response(200, text=""))
    found = find_people_detailed(company, use_hunter=False, search_url=ENGINE)
    assert [p.name for p in found.people] == ["Claire Dubois"] and not ddg.called


@respx.mock
def test_people_search_falls_back_when_searxng_is_down(make_company, monkeypatch):
    monkeypatch.setattr(domains, "_sleep", lambda s: None)
    company = make_company(name="BROMAKER", siren="800065146", domain=None, website=None, contacts=[],
                           leaders=[], raw={"leaders_detail": [], "names": ["BROMAKER"]})
    respx.get(SEARCH).mock(side_effect=httpx.ConnectError("arrêté"))
    brave = respx.get(domains.BRAVE_URL).mock(return_value=httpx.Response(200, json={"web": {"results": [
        {"title": "Karim Benali – CTO chez Bromaker | LinkedIn", "url": "https://fr.linkedin.com/in/k"}]}}))
    found = find_people_detailed(company, use_hunter=False, search_url=ENGINE,
                                 secrets=Secrets(_env_file=None, brave_api_key="bk"))
    assert brave.called and [p.name for p in found.people] == ["Karim Benali"]
