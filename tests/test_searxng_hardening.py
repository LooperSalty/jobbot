"""SearXNG hardening: configured engines, throttle, daily budget, backoff on CAPTCHA and keyless / Brave fallback."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import httpx
import pytest
import respx

from jobbot import clock
from jobbot.config import Secrets, Settings
from jobbot.http import HostThrottle
from jobbot.outreach import _searxng, domains
from jobbot.outreach._searxng import SearxEngine, engine_for, searxng_search
from jobbot.outreach.domains import find_domain_verified

ENGINE = "http://127.0.0.1:8888"
SEARCH = f"{ENGINE}/search"
HTML = {"content-type": "text/html; charset=utf-8"}
CAPTCHA = {"results": [], "unresponsive_engines": [["bing", "CAPTCHA"]]}
FRENCH_BLOCK = {"results": [], "unresponsive_engines": [["bing", "trop de requêtes"],
                                                        ["brave", "Suspendu : accès refusé"]]}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(domains, "_SITE_THROTTLE", HostThrottle(0))
    monkeypatch.setattr(domains, "_DDG", domains._DdgBreaker())
    monkeypatch.setattr(domains, "_resolves", lambda d: False)
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    waits: list[float] = []
    monkeypatch.setattr(_searxng, "_sleep", waits.append)
    monkeypatch.setattr(_searxng, "_POLITE", _searxng._Polite())
    return waits


@pytest.fixture
def searx_ctx(ctx):
    settings = ctx.settings.model_copy(update={"search_engine_url": ENGINE})
    return dataclasses.replace(ctx, settings=settings)


def _engine(ctx, **overrides) -> SearxEngine:
    return dataclasses.replace(engine_for(ctx), **overrides)


def test_settings_defaults_and_engine_names():
    settings = Settings()
    assert settings.search_engine_engines == ["bing"]
    assert Settings(search_engine_engines="Bing, Brave ,").search_engine_engines == ["bing", "brave"]
    assert settings.night.search_min_interval_s == 6.0
    assert settings.night.max_search_queries == 300
    assert settings.night.search_backoff_hours == 6.0


def test_engine_for_follows_the_settings(searx_ctx, ctx):
    assert engine_for(ctx) is None  # no instance configured
    engine = engine_for(searx_ctx)
    assert engine.base_url == ENGINE and engine.engines == ("bing",)
    assert engine.min_interval_s == 6.0 and engine.max_queries_per_day == 300 and engine.repo is searx_ctx.repo


@respx.mock
def test_every_query_asks_the_configured_engines_and_waits_six_seconds(searx_ctx, _offline):
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"results": [
        {"url": "https://exemple-logiciels.fr/", "title": "Exemple Logiciels"}]}))
    with httpx.Client() as client:
        assert searxng_search(client, engine_for(searx_ctx), "exemple logiciels metz")[0][0].startswith("https://")
        searxng_search(client, engine_for(searx_ctx), "autre requête")
    params = route.calls[0].request.url.params
    assert params["engines"] == "bing"
    assert params["format"] == "json"
    assert len(_offline) == 1
    assert 6.0 - 1 <= _offline[0] <= 9.0  # 6 s + jitter (up to 3 s), minus the time already elapsed


@respx.mock
def test_daily_budget_stops_querying(searx_ctx):
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"results": [{"url": "https://a.fr"}]}))
    engine = _engine(searx_ctx, max_queries_per_day=2)
    with httpx.Client() as client:
        results = [searxng_search(client, engine, f"requête {i}") for i in range(4)]
    assert [bool(r) for r in results] == [True, True, False, False]
    assert route.call_count == 2
    assert _searxng.queries_today(searx_ctx.repo) == 2


@pytest.mark.parametrize("payload", [CAPTCHA, FRENCH_BLOCK])
@respx.mock
def test_blocked_engines_start_a_backoff_and_searxng_is_left_alone(searx_ctx, payload):
    engine = _engine(searx_ctx, engines=("bing", "brave") if payload is FRENCH_BLOCK else ("bing",))
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json=payload))
    with httpx.Client() as client:
        assert searxng_search(client, engine, "q1") == []
        assert searxng_search(client, engine, "q2") == []
    assert route.call_count == 1  # the second query never reaches the instance
    until = _searxng.backoff_until(searx_ctx.repo)
    assert until is not None
    assert timedelta(hours=5.9) < until - clock.now() <= timedelta(hours=6)
    last = _searxng.last_backoff(searx_ctx.repo)
    assert "bing" in last["reason"]


@respx.mock
def test_backoff_expires_and_timeouts_do_not_count_as_a_block(searx_ctx):
    repo = searx_ctx.repo
    _searxng.start_backoff(repo, 6, "bing : CAPTCHA", now=clock.now() - timedelta(hours=7))
    assert _searxng.backoff_until(repo) is None  # 6 h are over
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json={
        "results": [], "unresponsive_engines": [["bing", "timeout"]]}))
    with httpx.Client() as client:
        assert searxng_search(client, engine_for(searx_ctx), "q") == []
    assert _searxng.backoff_until(repo) is None


@respx.mock
def test_backoff_started_while_waiting_is_honoured(searx_ctx, monkeypatch):
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"results": [{"url": "https://a.fr"}]}))

    class OtherWorkerHitsACaptcha:
        def wait_turn(self, interval):
            _searxng.start_backoff(searx_ctx.repo, 6, "bing : CAPTCHA")

    monkeypatch.setattr(_searxng, "_POLITE", OtherWorkerHitsACaptcha())
    with httpx.Client() as client:
        assert searxng_search(client, engine_for(searx_ctx), "q") == []
    assert not route.called and _searxng.queries_today(searx_ctx.repo) == 0


def test_blocked_reason_needs_every_wanted_engine():
    partial = {"unresponsive_engines": [["bing", "CAPTCHA"]]}
    assert _searxng.blocked_reason(partial, ("bing", "qwant")) is None
    assert _searxng.blocked_reason(partial, ("bing",)) == "bing : CAPTCHA"
    assert _searxng.blocked_reason(partial, ("Bing ",)) == "bing : CAPTCHA"
    assert _searxng.blocked_reason({"unresponsive_engines": [["bing", "HTTP error"]]}, ("bing",)) is None


@respx.mock
def test_http_429_from_the_instance_starts_a_backoff(searx_ctx):
    respx.get(SEARCH).mock(return_value=httpx.Response(429, text="Too Many Requests"))
    with httpx.Client() as client:
        assert searxng_search(client, engine_for(searx_ctx), "q") == []
    assert _searxng.backoff_until(searx_ctx.repo) is not None


@respx.mock
def test_domain_search_falls_back_to_brave_during_a_backoff(searx_ctx, make_company):
    _searxng.start_backoff(searx_ctx.repo, 6, "bing : CAPTCHA")
    company = make_company(name="EXEMPLE NUMERIQUE", siren="900000001", city="Metz", postal_code="57000",
                           domain=None, website=None, contacts=[], raw={"names": ["EXEMPLE NUMERIQUE"]})
    respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(domains.WIKIDATA_URL).mock(return_value=httpx.Response(200, json={"results": {"bindings": []}}))
    engine = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))
    brave = respx.get(domains.BRAVE_URL).mock(return_value=httpx.Response(200, json={"web": {"results": [
        {"url": "https://exemple-numerique.fr/"}]}}))
    respx.get("https://exemple-numerique.fr/").mock(return_value=httpx.Response(
        200, text="<p>EXEMPLE NUMERIQUE — SIREN 900 000 001</p>", headers=HTML))
    match = find_domain_verified(company, secrets=Secrets(_env_file=None, brave_api_key="cle"),
                                 search_url=engine_for(searx_ctx))
    assert match.domain == "exemple-numerique.fr" and match.method == "brave"
    assert not engine.called and brave.called


@respx.mock
def test_enrichment_passes_the_hardened_engine(searx_ctx, make_company, monkeypatch):
    from jobbot.outreach import emails

    seen = {}

    def fake_find(company, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(emails._domains, "find_domain_verified", fake_find)
    company = make_company(domain=None, website=None, contacts=[])
    emails.enrich_company(searx_ctx, company)
    assert isinstance(seen["search_url"], SearxEngine) and seen["search_url"].engines == ("bing",)
