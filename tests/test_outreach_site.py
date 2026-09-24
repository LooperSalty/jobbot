"""outreach._site (website summary) + crawl personalisation fields + Hunter raw data (no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from jobbot.http import HostThrottle
from jobbot.llm import FakeLLM, LLMError
from jobbot.models import ContactEmail
from jobbot.outreach import _mailcheck, domains
from jobbot.outreach._site import MAX_SUMMARY_CHARS, meta_description, site_text, summarize_site
from jobbot.outreach.emails import crawl_site, is_sendable

FIX = Path(__file__).parent / "fixtures" / "outreach"
HTML = {"content-type": "text/html; charset=utf-8"}
TEXT = "Bromaker conçoit des applications web sur mesure pour les PME du Grand Est. " * 5


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(domains, "_SITE_THROTTLE", HostThrottle(0))
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)


def test_meta_description_and_site_text():
    assert meta_description(_fx("yaakadev_home.html")).startswith("Yaakadev digitalise vos processus")
    assert meta_description('<meta property="og:description" content="Une agence web qui code à Metz.">') == \
        "Une agence web qui code à Metz."
    assert meta_description('<meta name="description" content="Trop court">') is None
    pages = [("https://acme.fr/", "<p>Accueil Acme</p>"), ("https://acme.fr/mentions-legales", "<p>SIREN</p>"),
             ("https://acme.fr/a-propos", "<p>Nous faisons du React</p>")]
    text = site_text(pages)
    assert "Accueil Acme" in text and "React" in text and "SIREN" not in text
    assert len(site_text([("https://acme.fr/", "<p>" + "x" * 5000 + "</p>")], limit=100)) == 100


def test_summarize_site_uses_fast_llm_and_cleans_answer():
    llm = FakeLLM('« Bromaker conçoit des applications web pour les PME.  Clients : collectivités. »')
    summary = summarize_site(llm, "Bromaker", TEXT, "meta")
    assert summary == "Bromaker conçoit des applications web pour les PME. Clients : collectivités."
    prompt, system, fast = llm.calls[0]
    assert fast is True and "Bromaker" in prompt and "N'invente rien" in prompt and system


@pytest.mark.parametrize("answer", ["{}", "Désolé, je ne peux pas résumer ce site.", "Trop court.", ""])
def test_summarize_site_falls_back_to_meta_on_bad_answers(answer):
    assert summarize_site(FakeLLM(answer), "Bromaker", TEXT, "Meta description.") == "Meta description."


def test_summarize_site_fallbacks_and_truncation():
    def broken(prompt, system, fast):
        raise LLMError("indisponible")

    assert summarize_site(FakeLLM(broken), "Bromaker", TEXT, None) is None
    silent = FakeLLM("jamais appelé")
    assert summarize_site(silent, "Bromaker", "trop peu de texte", "Meta.") == "Meta." and silent.calls == []
    assert summarize_site(None, "Bromaker", TEXT, "Meta.") == "Meta."
    long_answer = ("Bromaker développe des applications. " * 40).strip()
    cut = summarize_site(FakeLLM(long_answer), "Bromaker", TEXT, None)
    assert len(cut) <= MAX_SUMMARY_CHARS and cut.endswith(".")


@respx.mock
def test_crawl_site_keeps_pages_meta_and_text_for_personalisation():
    home = ('<html><head><meta name="description" content="Bromaker conçoit des applications web sur mesure.">'
            '</head><body><p>Bienvenue chez Bromaker</p><a href="/equipe">Notre équipe</a></body></html>')
    respx.get("https://bromaker.fr/").mock(return_value=httpx.Response(200, text=home, headers=HTML))
    respx.get("https://bromaker.fr/equipe").mock(
        return_value=httpx.Response(200, text=_fx("team_page.html"), headers=HTML))
    respx.get(url__startswith="https://bromaker.fr/").mock(return_value=httpx.Response(404))
    crawl = crawl_site("bromaker.fr", max_pages=3)
    assert crawl.meta_description == "Bromaker conçoit des applications web sur mesure."
    assert "Bienvenue chez Bromaker" in crawl.site_text and "Julie Martin" in crawl.site_text
    assert set(crawl.page_html) == {"https://bromaker.fr/", "https://bromaker.fr/equipe"}
    assert "page_html" not in crawl.model_dump()  # never serialised
    assert "julie.martin@bromaker.fr" in [c.email for c in crawl.contacts]


@respx.mock
def test_hunter_domain_data_returns_pattern_and_tech_kind():
    respx.get(f"{_mailcheck.HUNTER_BASE}/domain-search").mock(return_value=httpx.Response(200, json={"data": {
        "pattern": "{first}.{last}", "emails": [
            {"value": "karim.benali@acme.fr", "type": "personal", "confidence": 80, "first_name": "Karim",
             "last_name": "Benali", "position": "CTO", "department": "executive"}]}}))
    data = _mailcheck.hunter_domain_data("acme.fr", "k")
    assert data["pattern"] == "{first}.{last}"
    assert _mailcheck.hunter_contact(data["emails"][0]).kind == "tech"
    assert _mailcheck.hunter_contact({"value": "x.y@acme.fr", "confidence": "n/a"}).confidence == 0.0
    assert _mailcheck.hunter_domain_data("", "k") == {} and _mailcheck.hunter_domain_data("acme.fr", "") == {}


def test_hunter_credit_budget(repo):
    for _ in range(_mailcheck.HUNTER_MONTHLY_CAPS["search"]):
        assert _mailcheck.take_hunter_credit(repo, "search")
    assert not _mailcheck.take_hunter_credit(repo, "search")
    assert _mailcheck.take_hunter_credit(repo, "verify")


def test_is_sendable_with_unverified_guesses_allowed():
    guess = ContactEmail(email="a.b@acme.fr", source="pattern", kind="hr")
    assert not is_sendable(guess) and is_sendable(guess, allow_unverified=True)
    assert is_sendable(guess.model_copy(update={"verified": True}))
