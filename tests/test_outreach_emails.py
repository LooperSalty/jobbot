"""outreach.emails — extraction, crawl, ranking, MX/Hunter (no network)."""

from __future__ import annotations

from pathlib import Path

import dns.resolver
import httpx
import pytest
import respx

from jobbot.http import HostThrottle
from jobbot.models import ContactEmail
from jobbot.outreach import _mailcheck, domains
from jobbot.outreach.emails import (
    SiteCrawl,
    crawl_site,
    crawl_site_emails,
    decode_cfemail,
    deobfuscate,
    guess_patterns,
    has_mx,
    hunter_domain_search,
    is_sendable,
    rank_contacts,
    scrape_emails,
)

FIX = Path(__file__).parent / "fixtures" / "outreach"
HTML = {"content-type": "text/html; charset=utf-8"}


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(domains, "_SITE_THROTTLE", HostThrottle(0))
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)


# --------------------------------------------------------------------------- extraction


def test_decode_cfemail_live_sample():
    assert decode_cfemail("61020e0f15000215210d04120b0e0804120514020e05044f0713") == "contact@lesjoiesducode.fr"
    for bad in ("", "6", "zz01", "610"):
        with pytest.raises(ValueError):
            decode_cfemail(bad)


@pytest.mark.parametrize(("raw", "expected"), [
    ("contact [at] acme (dot) fr", "contact@acme.fr"),
    ("rh (arobase) acme.fr", "rh@acme.fr"),
    ("jobs&#64;acme.fr", "jobs@acme.fr"),
    ("jobs%40acme.fr", "jobs@acme.fr"),
    ("info at acme point fr", "info@acme.fr"),
])
def test_deobfuscate(raw, expected):
    assert expected in deobfuscate(raw)


def test_scrape_emails_on_live_pages():
    assert scrape_emails(_fx("yaakadev_contact.html"), "yaakadev.com") == ["secretariat@yaakadev.com"]  # votre@email.com dropped
    trust = scrape_emails(_fx("trustteam_home.html"), "trustteam.eu")
    assert "contact@trustteam.fr" in trust  # sibling TLD, from JSON-LD contactPoint
    assert not any("@2." in e or "aos@" in e for e in trust)


def test_scrape_emails_filters_false_positives():
    page = """
    <script type="application/ld+json">{"@graph": [
      {"@type": "Person", "name": "dev", "email": "eric@agence-web.fr"},
      {"@type": "Organization", "contactPoint": {"@type": "ContactPoint", "email": "mailto:Recrutement@Acme.fr"}}
    ]}</script>
    <script type="application/ld+json">{not json</script>
    <img src="/img/logo@2x.png"> lodash@4.17.21 votre@email.com prenom.nom@domaine.fr
    https://0123456789abcdef0123@o123.ingest.sentry.io/42 contact@ovh.com
    <a href="/cdn-cgi/l/email-protection#61020e0f15000215210d04120b0e0804120514020e05044f0713">x</a>
    <span data-cfemail="zz">bad</span> marie.durand@gmail.com contact [at] acme (dot) fr
    """
    assert scrape_emails(page, "acme.fr") == ["recrutement@acme.fr", "marie.durand@gmail.com", "contact@acme.fr"]
    assert scrape_emails(page, "lesjoiesducode.fr") == ["contact@lesjoiesducode.fr", "marie.durand@gmail.com"]


# --------------------------------------------------------------------------- ranking


def test_rank_contacts_orders_filters_and_dedupes():
    contacts = [
        ContactEmail(email="contact@acme.fr", confidence=0.8, kind="generic"),
        ContactEmail(email="Recrutement@acme.fr", confidence=0.9, kind="recruitment"),
        ContactEmail(email="noreply@acme.fr", kind="generic"),
        ContactEmail(email="webmaster@acme.fr", kind="generic"),
        ContactEmail(email="dpo@acme.fr", kind="generic"),
        ContactEmail(email="factures@acme.fr", kind="generic"),
        ContactEmail(email="support@acme.fr", kind="generic"),
        ContactEmail(email="votre@email.com", kind="generic"),
        ContactEmail(email="paul.martin@acme.fr", confidence=0.4, source="pattern", kind="leader"),
        ContactEmail(email="julie.rh@acme.fr", confidence=0.7, source="hunter", kind="hr", verified=True),
        ContactEmail(email="luc.verdier@acme.fr", confidence=0.4, source="pattern", kind="leader", verified=True),
        ContactEmail(email="contact@acme.fr", confidence=0.5, source="hunter", kind="generic", verified=True),
    ]
    ranked = rank_contacts(contacts)
    assert [c.email for c in ranked] == [
        "recrutement@acme.fr", "julie.rh@acme.fr", "luc.verdier@acme.fr", "contact@acme.fr", "paul.martin@acme.fr",
    ]
    merged = ranked[3]
    assert merged.verified and merged.confidence == 0.8 and merged.source == "scraped"
    assert not is_sendable(ranked[-1]) and is_sendable(ranked[2])


# --------------------------------------------------------------------------- patterns / MX / Hunter


def test_guess_patterns_order_and_normalisation():
    got = guess_patterns("JEAN PIERRE MARIE", "DE LA FONTAINE", "acme.fr")
    assert got[:4] == ["jean.delafontaine@acme.fr", "jean.de-la-fontaine@acme.fr", "j.delafontaine@acme.fr",
                       "jdelafontaine@acme.fr"]
    assert guess_patterns("Élodie", "Müller", "x.fr")[0] == "elodie.muller@x.fr"
    assert guess_patterns("", "X", "x.fr") == [] and guess_patterns("A", "B", "") == []


class _Answer:
    def __init__(self, exchange: str):
        self.exchange = exchange


class _FakeResolver:
    def __init__(self, mx=None, mx_error=None, a_ok=True):
        self.mx, self.mx_error, self.a_ok = mx, mx_error, a_ok

    def resolve(self, name, rtype):
        if rtype == "MX":
            if self.mx_error:
                raise self.mx_error
            return [_Answer(x) for x in self.mx]
        if not self.a_ok:
            raise dns.resolver.NXDOMAIN()
        return ["1.2.3.4"]


@pytest.mark.parametrize(("resolver", "expected"), [
    (_FakeResolver(mx=["aspmx.l.google.com."]), True),
    (_FakeResolver(mx=["."]), False),  # RFC 7505 null MX
    (_FakeResolver(mx_error=dns.resolver.NXDOMAIN()), False),
    (_FakeResolver(mx_error=dns.resolver.NoAnswer()), True),  # implicit MX via A record
    (_FakeResolver(mx_error=dns.resolver.NoAnswer(), a_ok=False), False),
    (_FakeResolver(mx_error=dns.resolver.YXDOMAIN()), False),
])
def test_has_mx(monkeypatch, resolver, expected):
    monkeypatch.setattr(_mailcheck, "_make_resolver", lambda: resolver)
    assert has_mx("acme.fr") is expected


def test_has_mx_rejects_garbage():
    assert has_mx("") is False and has_mx("localhost") is False


@respx.mock
def test_hunter_domain_search_maps_and_keeps_key_out_of_url():
    route = respx.get(f"{_mailcheck.HUNTER_BASE}/domain-search").mock(return_value=httpx.Response(200, json={"data": {
        "domain": "acme.fr",
        "emails": [
            {"value": "rh@acme.fr", "type": "generic", "confidence": 95, "verification": {"status": "valid"}},
            {"value": "julie.martin@acme.fr", "type": "personal", "confidence": 88, "first_name": "Julie",
             "last_name": "Martin", "position": "Chargée de recrutement", "department": "hr",
             "verification": {"status": "accept_all"}},
            {"value": "ceo@acme.fr", "type": "personal", "confidence": 60, "department": "executive"},
            {"value": "old@acme.fr", "type": "personal", "confidence": 90, "verification": {"status": "invalid"}},
            {"value": "noreply@acme.fr", "type": "generic", "confidence": 90},
        ],
    }}))
    found = hunter_domain_search("acme.fr", "secret-key")
    assert [(c.email, c.kind, c.verified) for c in found] == [
        ("rh@acme.fr", "recruitment", True), ("julie.martin@acme.fr", "hr", False), ("ceo@acme.fr", "leader", False),
    ]
    assert found[0].confidence == 0.85 and found[1].person_name == "Julie Martin" and found[0].source == "hunter"
    request = route.calls[0].request
    assert request.headers["x-api-key"] == "secret-key" and "secret-key" not in str(request.url)


@respx.mock
def test_hunter_errors_return_empty():
    route = respx.get(f"{_mailcheck.HUNTER_BASE}/domain-search").mock(return_value=httpx.Response(401, json={}))
    assert hunter_domain_search("acme.fr", "bad") == []
    route.mock(side_effect=httpx.ConnectError("down"))
    assert hunter_domain_search("acme.fr", "k") == []
    assert hunter_domain_search("acme.fr", "") == []


@respx.mock
def test_hunter_verify_polls_and_handles_claimed():
    route = respx.get(f"{_mailcheck.HUNTER_BASE}/email-verifier").mock(side_effect=[
        httpx.Response(202, json={}), httpx.Response(200, json={"data": {"status": "valid"}}),
        httpx.Response(451, json={}), httpx.Response(429, json={}),
    ])
    sleeps = []
    assert _mailcheck.hunter_verify("a@acme.fr", "k", sleep=sleeps.append) == "valid" and sleeps == [3.0]
    assert _mailcheck.hunter_verify("b@acme.fr", "k", sleep=sleeps.append) == "claimed"
    assert _mailcheck.hunter_verify("c@acme.fr", "k", sleep=sleeps.append) == "error"
    assert _mailcheck.hunter_verify("d@acme.fr", "", sleep=sleeps.append) == "error"
    route.mock(return_value=httpx.Response(202, json={}))
    assert _mailcheck.hunter_verify("e@acme.fr", "k", sleep=sleeps.append) == "unknown"


# --------------------------------------------------------------------------- crawl


def _mock_yaakadev():
    base = "https://yaakadev.com"
    respx.get(f"{base}/").mock(return_value=httpx.Response(200, text=_fx("yaakadev_home.html"), headers=HTML))
    respx.get(f"{base}/robots.txt").mock(return_value=httpx.Response(
        200, text="User-agent: *\nDisallow: /equipe\n", headers={"content-type": "text/plain"}))
    respx.get(f"{base}/contact").mock(return_value=httpx.Response(200, text=_fx("yaakadev_contact.html"), headers=HTML))
    respx.get(f"{base}/mentions-legales").mock(
        return_value=httpx.Response(200, text=_fx("yaakadev_mentions.html"), headers=HTML))
    equipe = respx.get(f"{base}/equipe").mock(return_value=httpx.Response(200, text="x", headers=HTML))
    respx.get(url__startswith=f"{base}/").mock(return_value=httpx.Response(404))
    return equipe


@respx.mock
def test_crawl_site_on_live_fixture_honours_robots():
    equipe = _mock_yaakadev()
    crawl = crawl_site("yaakadev.com", max_pages=12)
    assert [c.email for c in crawl.contacts] == ["secretariat@yaakadev.com"]
    assert crawl.contacts[0].kind == "generic" and crawl.contacts[0].source == "scraped"
    assert crawl.sources["secretariat@yaakadev.com"] == "https://yaakadev.com/"
    assert crawl.contact_form_url == "https://yaakadev.com/contact" and crawl.pages >= 3
    assert not equipe.called  # disallowed by robots.txt
    assert crawl_site_emails("yaakadev.com")[0].email == "secretariat@yaakadev.com"


@respx.mock
def test_crawl_site_limits_requests():
    _mock_yaakadev()
    crawl_site("yaakadev.com", max_pages=2)
    site_calls = [c for c in respx.calls if not c.request.url.path.endswith("robots.txt")]
    assert len(site_calls) == 2


@respx.mock
def test_crawl_site_detects_ats_careers_link():
    base = "https://trustteam.eu"
    respx.get(f"{base}/").mock(return_value=httpx.Response(200, text=_fx("trustteam_home.html"), headers=HTML))
    respx.get(url__startswith=f"{base}/").mock(return_value=httpx.Response(404))
    crawl = crawl_site("trustteam.eu", max_pages=3)
    assert crawl.careers_url.startswith("https://trustteam.jobtoolz.com/fr")
    assert "contact@trustteam.fr" in [c.email for c in crawl.contacts]


@respx.mock
def test_crawl_site_unreachable_and_internal_careers_page():
    respx.get("https://down.fr/").mock(side_effect=httpx.ConnectError("x"))
    respx.get("https://www.down.fr/").mock(side_effect=httpx.ConnectError("x"))
    assert crawl_site("down.fr") == SiteCrawl(domain="down.fr")

    home = '<a href="/nous-rejoindre">Rejoignez-nous</a><a href="mailto:jobs@acme.fr">jobs</a>'
    respx.get("https://acme.fr/").mock(return_value=httpx.Response(200, text=home, headers=HTML))
    respx.get(url__startswith="https://acme.fr/").mock(return_value=httpx.Response(404))
    crawl = crawl_site("acme.fr", max_pages=2)
    assert crawl.careers_url == "https://acme.fr/nous-rejoindre"
    assert crawl.contacts[0].email == "jobs@acme.fr" and crawl.contacts[0].kind == "recruitment"
