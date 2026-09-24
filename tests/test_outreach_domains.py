"""outreach.domains — website discovery + SIREN verification + geocoding (no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from jobbot.config import Secrets
from jobbot.http import HostThrottle
from jobbot.outreach import domains
from jobbot.outreach.domains import (
    DomainMatch,
    find_domain,
    find_domain_verified,
    geocode_address,
    normalize_domain,
    page_has_siren,
    slug_candidates,
    verify_domain,
)

FIX = Path(__file__).parent / "fixtures" / "outreach"
YAAKA = "812991172"
HTML = {"content-type": "text/html; charset=utf-8"}


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(domains, "_SITE_THROTTLE", HostThrottle(0))
    monkeypatch.setattr(domains, "_DDG", domains._DdgBreaker())
    monkeypatch.setattr(domains, "_sleep", lambda s: None)
    monkeypatch.setattr(domains, "_resolves", lambda d: False)
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)


@pytest.fixture
def yaaka(make_company):
    return make_company(name="YAAKADEV", siren=YAAKA, city="Metz", postal_code="57070", domain=None,
                        website=None, contacts=[], raw={"names": ["YAAKADEV"]})


def _mock_yaakadev_site() -> None:
    respx.get("https://yaakadev.com/").mock(return_value=httpx.Response(200, text=_fx("yaakadev_home.html"), headers=HTML))
    respx.get("https://yaakadev.com/mentions-legales").mock(
        return_value=httpx.Response(200, text=_fx("yaakadev_mentions.html"), headers=HTML))


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(("value", "expected"), [
    ("https://www.Acme.fr/contact", "acme.fr"),
    ("acme-logiciels.fr", "acme-logiciels.fr"),
    ("http://sub.acme.io:8080/x", "sub.acme.io"),
    ("pas un domaine", None),
    ("", None),
    (None, None),
])
def test_normalize_domain(value, expected):
    assert normalize_domain(value) == expected


def test_slug_candidates_strip_legal_forms():
    slugs = slug_candidates(["NANCY SERVICES SAS", "LE16SOLUTIONS", "AB"])
    assert "nancyservices.fr" in slugs and "nancy-services.com" in slugs and "le16solutions.com" in slugs
    assert not any(s.startswith("ab.") for s in slugs) and not any("sas" in s for s in slugs)


def test_page_has_siren_with_spaces_siret_and_tva():
    assert page_has_siren(_fx("yaakadev_mentions.html"), YAAKA)
    assert not page_has_siren(_fx("yaakadev_home.html"), YAAKA)
    assert page_has_siren("<p>SIRET : 519&nbsp;037&nbsp;253 00043</p>", "519037253")
    assert page_has_siren("TVA intracommunautaire FR65519037253", "519037253")
    assert page_has_siren("RCS 519.037.253", "519037253")
    assert not page_has_siren("anything", "12")


# --------------------------------------------------------------------------- verification


@respx.mock
def test_verify_domain_finds_siren_in_legal_notice():
    _mock_yaakadev_site()
    score, host = verify_domain(httpx.Client(), "yaakadev.com", YAAKA, ["YAAKADEV"], "57070")
    assert score == 1.0 and host == "yaakadev.com"


@respx.mock
def test_verify_domain_name_match_and_fallback_to_www():
    respx.get("https://acme.fr/").mock(side_effect=httpx.ConnectError("no bare domain"))
    respx.get("https://www.acme.fr/").mock(return_value=httpx.Response(
        200, text="<h1>Acme Logiciels</h1><p>12 rue X 57000 Metz</p>", headers=HTML))
    respx.get(url__startswith="https://www.acme.fr/").mock(return_value=httpx.Response(404))
    score, host = verify_domain(httpx.Client(), "acme.fr", "123456789", ["Acme Logiciels"], "57000")
    assert 0.75 <= score <= 0.9 and host == "acme.fr"


@respx.mock
def test_verify_domain_rejects_parked_directory_and_dead_sites():
    respx.get("https://parked.fr/").mock(return_value=httpx.Response(
        200, text="<p>Acme Logiciels — ce domaine à vendre sur Sedo</p>", headers=HTML))
    respx.get(url__startswith="https://parked.fr/").mock(return_value=httpx.Response(404))
    assert verify_domain(httpx.Client(), "parked.fr", "123456789", ["Acme Logiciels"], None)[0] == 0.0

    respx.get("https://redirected.fr/").mock(return_value=httpx.Response(
        301, headers={"location": "https://www.societe.com/societe/acme-123456789.html"}))
    respx.get("https://www.societe.com/societe/acme-123456789.html").mock(
        return_value=httpx.Response(200, text="SIREN 123456789", headers=HTML))
    assert verify_domain(httpx.Client(follow_redirects=True), "redirected.fr", "123456789", ["Acme"], None)[0] == 0.0

    respx.get("https://dead.fr/").mock(return_value=httpx.Response(503))
    respx.get("https://www.dead.fr/").mock(return_value=httpx.Response(404))
    assert verify_domain(httpx.Client(), "dead.fr", "123456789", ["Acme"], None) == (0.0, "dead.fr")


# --------------------------------------------------------------------------- find_domain


@respx.mock
def test_find_domain_via_clearbit_and_siren(yaaka):
    route = respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(200, text=_fx("clearbit_yaakadev.json")))
    _mock_yaakadev_site()
    match = find_domain_verified(yaaka)
    assert match == DomainMatch(domain="yaakadev.com", confidence=1.0, method="clearbit", verified_by="siren")
    assert route.calls[0].request.url.params["query"] == "YAAKADEV"
    assert find_domain(yaaka) == "yaakadev.com"


@respx.mock
def test_find_domain_checks_known_website_first(yaaka):
    _mock_yaakadev_site()
    clearbit = respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(200, json=[]))
    match = find_domain_verified(yaaka.model_copy(update={"website": "https://www.yaakadev.com"}))
    assert match.method == "website" and match.confidence == 1.0 and not clearbit.called


@respx.mock
def test_find_domain_ignores_lookalikes_and_directories(yaaka):
    respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(200, json=[
        {"name": "Trust1Team", "domain": "trust1team.com"},
        {"name": "YAAKADEV", "domain": "pappers.fr"},
    ]))
    respx.get(domains.WIKIDATA_URL).mock(return_value=httpx.Response(200, json={"results": {"bindings": []}}))
    respx.get(domains.DDG_URL).mock(return_value=httpx.Response(202, text="anomaly.js?cc=botnet"))
    assert find_domain(yaaka) is None


@respx.mock
def test_find_domain_slug_guess_with_dns(yaaka, monkeypatch):
    monkeypatch.setattr(domains, "_resolves", lambda d: d == "yaakadev.com")
    respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(500))
    _mock_yaakadev_site()
    match = find_domain_verified(yaaka)
    assert match.method == "slug" and match.domain == "yaakadev.com"


@respx.mock
def test_find_domain_wikidata_by_siren(make_company):
    company = make_company(name="SOPRA STERIA GROUP", siren="326820065", domain=None, website=None)
    respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(200, json=[]))
    wd = respx.get(domains.WIKIDATA_URL).mock(return_value=httpx.Response(200, json={
        "results": {"bindings": [{"site": {"value": "https://www.soprasteria.com/"}}]}}))
    respx.get("https://soprasteria.com/").mock(return_value=httpx.Response(
        200, text="<footer>RCS Annecy 326 820 065</footer>", headers=HTML))
    match = find_domain_verified(company, contact="https://github.com/jean-dupont-exemple")
    assert match.domain == "soprasteria.com" and match.method == "wikidata"
    assert "326820065" in wd.calls[0].request.url.params["query"]
    assert wd.calls[0].request.headers["user-agent"] == (
        "jobbot/0.1 (recherche d'emploi personnelle; https://github.com/jean-dupont-exemple)")


def test_bot_user_agent_contact_comes_from_the_profile(profile):
    assert domains.bot_user_agent(None) == domains.BOT_UA == "jobbot/0.1 (recherche d'emploi personnelle)"
    assert domains.bot_user_agent("  ") == domains.BOT_UA
    assert domains.profile_contact(profile) == profile.identity.github
    no_links = profile.model_copy(update={"identity": profile.identity.model_copy(
        update={"github": None, "portfolio": None, "linkedin": None})})
    assert domains.profile_contact(no_links) == profile.identity.email


@respx.mock
def test_find_domain_brave_then_name_match_skips_duckduckgo(yaaka):
    respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(domains.WIKIDATA_URL).mock(return_value=httpx.Response(403))
    brave = respx.get(domains.BRAVE_URL).mock(return_value=httpx.Response(200, json={"web": {"results": [
        {"url": "https://www.societe.com/societe/yaakadev-812991172.html"},
        {"url": "https://yaakadev-metz.fr/"},
    ]}}))
    respx.get("https://yaakadev-metz.fr/").mock(return_value=httpx.Response(
        200, text="<h1>Yaakadev</h1> agence web, 57070 Metz", headers=HTML))
    respx.get(url__startswith="https://yaakadev-metz.fr/").mock(return_value=httpx.Response(404))
    ddg = respx.get(domains.DDG_URL).mock(return_value=httpx.Response(200, text=""))
    match = find_domain_verified(yaaka, secrets=Secrets(_env_file=None, brave_api_key="k"))
    assert match.method == "brave" and match.verified_by == "name" and match.confidence < 1.0
    assert brave.calls[0].request.headers["x-subscription-token"] == "k"
    assert not ddg.called


@respx.mock
def test_duckduckgo_last_resort_and_circuit_breaker(yaaka):
    respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(domains.WIKIDATA_URL).mock(return_value=httpx.Response(200, json={"results": {"bindings": []}}))
    ddg_html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.pappers.fr%2Fx&rut=1">P</a>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fyaakadev.com%2F&amp;rut=2">Y</a>'
    )
    ddg = respx.get(domains.DDG_URL).mock(side_effect=[
        httpx.Response(200, text=ddg_html),
        httpx.Response(202, text="<script src='anomaly.js?cc=botnet'></script>"),
    ])
    _mock_yaakadev_site()
    assert find_domain_verified(yaaka).method == "duckduckgo"
    other = yaaka.model_copy(update={"name": "INCONNUE", "raw": {}})
    assert find_domain(other) is None  # 202 → breaker trips
    assert find_domain(other) is None and ddg.call_count == 2  # no further DDG request


def test_provider_errors_do_not_abort(yaaka, monkeypatch):
    def boom(client, names):
        raise httpx.ConnectError("clearbit down")

    monkeypatch.setattr(domains, "_clearbit", boom)
    monkeypatch.setattr(domains, "_wikidata", lambda c, s, ua: [])
    monkeypatch.setattr(domains, "_ddg", lambda c, q: [])
    assert find_domain(yaaka) is None


# --------------------------------------------------------------------------- geocoding


@respx.mock
def test_geocode_address_returns_lat_lon():
    route = respx.get(domains.GEOCODE_URL).mock(return_value=httpx.Response(200, json={
        "type": "FeatureCollection",
        "features": [{"geometry": {"type": "Point", "coordinates": [6.194891, 49.108385]},
                      "properties": {"label": "Metz", "citycode": "57463"}}],
    }))
    assert geocode_address("Metz", postcode="57000") == (49.108385, 6.194891)
    params = route.calls[0].request.url.params
    assert params["autocomplete"] == "0" and params["postcode"] == "57000"


@respx.mock
def test_geocode_address_failures_return_none():
    route = respx.get(domains.GEOCODE_URL).mock(side_effect=[
        httpx.Response(200, json={"features": []}),
        httpx.Response(400, json={"detail": "bad"}),
    ])
    assert geocode_address("nowhere") is None
    assert geocode_address("bad") is None
    route.mock(side_effect=httpx.ConnectError("down"))
    assert geocode_address("down") is None
    assert geocode_address("   ") is None
