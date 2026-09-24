"""outreach.emails — enrich_company / enrich_companies (domain, crawl, people, patterns, guesses, summary; no network)."""

from __future__ import annotations

import dataclasses
import json
import socket
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.http import HostThrottle
from jobbot.llm import FakeLLM, LLMError
from jobbot.models import ContactEmail
from jobbot.outreach import _nominative, domains, emails, people
from jobbot.outreach._mailcheck import HUNTER_MONTHLY_CAPS, hunter_month_key
from jobbot.outreach._patterns import KV_PREFIX
from jobbot.outreach.domains import DomainMatch
from jobbot.outreach.emails import EnrichReport, SiteCrawl, enrich_companies, enrich_company

FIX = Path(__file__).parent / "fixtures" / "outreach"
HTML = {"content-type": "text/html; charset=utf-8"}
SUMMARY = ("Bromaker conçoit des applications web et mobiles sur mesure pour les PME du Grand Est, "
           "avec React et Node.js.")


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Nothing may leave the machine: sockets and DNS fail loudly; people search and MX are stubbed."""
    def refuse(*args, **kwargs):
        raise OSError("réseau interdit pendant les tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "sendto", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(domains, "_SITE_THROTTLE", HostThrottle(0))
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    monkeypatch.setattr(people, "_search_titles", lambda client, company, secrets, search_url=None: [])
    monkeypatch.setattr(people, "_fetch_registry_leaders", lambda siren, client: [])
    monkeypatch.setattr(_nominative, "has_mx", lambda domain: True)


def _mock_yaakadev():
    base = "https://yaakadev.com"
    respx.get(f"{base}/").mock(return_value=httpx.Response(200, text=_fx("yaakadev_home.html"), headers=HTML))
    respx.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{base}/contact").mock(return_value=httpx.Response(200, text=_fx("yaakadev_contact.html"), headers=HTML))
    respx.get(f"{base}/mentions-legales").mock(
        return_value=httpx.Response(200, text=_fx("yaakadev_mentions.html"), headers=HTML))
    respx.get(url__startswith=f"{base}/").mock(return_value=httpx.Response(404))


@pytest.fixture
def small_company(make_company):
    return make_company(
        name="BROMAKER", siren="800065146", domain=None, website=None, contacts=[], leaders=["Luc VERDIER"],
        raw={"leaders_detail": [{"first": "Luc", "last": "VERDIER", "role": "Président de SAS"},
                                {"first": "Yann", "last": "AUDIT", "role": "Commissaire aux comptes"}],
             "categorie_entreprise": "PME", "outreach_score": 5},
    )


def _with(ctx, *, hunter: bool = False, llm=None, **outreach):
    secrets = ctx.secrets.model_copy(update={"hunter_api_key": "hk"}) if hunter else ctx.secrets
    settings = ctx.settings.model_copy(update={"outreach": ctx.settings.outreach.model_copy(update=outreach)})
    return dataclasses.replace(ctx, secrets=secrets, settings=settings, llm=llm or ctx.llm)


def _patch_site(monkeypatch, crawl: SiteCrawl | None, match: DomainMatch | None = None, mx: bool = True):
    match = match or DomainMatch(domain="bromaker.fr", confidence=1.0, method="clearbit", verified_by="siren")
    monkeypatch.setattr(domains, "find_domain_verified",
                        lambda company, client=None, secrets=None, contact=None, search_url=None: match)
    monkeypatch.setattr(emails, "crawl_site", lambda domain, client=None, max_pages=8: crawl)
    monkeypatch.setattr(emails, "has_mx", lambda domain: mx)
    monkeypatch.setattr(_nominative, "has_mx", lambda domain: mx)


def _no_hunter(monkeypatch):
    monkeypatch.setattr(people, "hunter_domain_data", lambda *a, **k: pytest.fail("Hunter inattendu"))
    monkeypatch.setattr(_nominative, "hunter_verify", lambda *a, **k: pytest.fail("vérification inattendue"))


# --------------------------------------------------------------------------- site addresses & leaders


def test_enrich_company_with_site_emails_tags_leader_and_learns_pattern(ctx, small_company, monkeypatch):
    crawl = SiteCrawl(
        domain="bromaker.fr", pages=3, careers_url="https://bromaker.teamtailor.com/jobs",
        contact_form_url="https://bromaker.fr/contact",
        contacts=[ContactEmail(email="contact@bromaker.fr", confidence=0.8, kind="generic"),
                  ContactEmail(email="luc.verdier@bromaker.fr", confidence=0.6, kind="named")],
        sources={"contact@bromaker.fr": "https://bromaker.fr/contact",
                 "luc.verdier@bromaker.fr": "https://bromaker.fr/equipe"},
    )
    _patch_site(monkeypatch, crawl)
    _no_hunter(monkeypatch)
    out = enrich_company(ctx, small_company)
    assert out.status == "enriched" and out.domain == "bromaker.fr" and out.website == "https://bromaker.fr"
    assert out.careers_url == "https://bromaker.teamtailor.com/jobs"
    assert [(c.email, c.kind) for c in out.contacts] == [("luc.verdier@bromaker.fr", "leader"),
                                                         ("contact@bromaker.fr", "generic")]
    assert out.contacts[0].person_name == "Luc VERDIER"  # no guess: the leader's address is known
    assert out.raw["domain_verified_by"] == "siren" and out.raw["email_sources"]["contact@bromaker.fr"].endswith("/contact")
    assert out.raw["contact_form_url"] and small_company.status == "new"  # original untouched
    assert out.raw["email_pattern"] == "{first}.{last}" and ctx.repo.kv_get(f"{KV_PREFIX}bromaker.fr") == "{first}.{last}"
    assert out.raw["people"] == [{"name": "Luc VERDIER", "role": "Président de SAS", "category": "leader",
                                  "source": "registry"}]


def test_enrich_company_name_match_scales_confidence_and_drops_no_mx(ctx, small_company, monkeypatch):
    crawl = SiteCrawl(domain="bromaker.fr", contacts=[ContactEmail(email="rh@bromaker.fr", confidence=0.9,
                                                                   kind="recruitment")])
    match = DomainMatch(domain="bromaker.fr", confidence=0.8, method="duckduckgo", verified_by="name")
    _patch_site(monkeypatch, crawl, match)
    monkeypatch.setattr(people, "_search_titles", lambda *a: pytest.fail("recherche inutile : adresse RH connue"))
    out = enrich_company(_with(ctx, hunter=True), small_company)  # recruitment address: no Hunter credit spent
    assert out.contacts[0].email == "rh@bromaker.fr" and out.contacts[0].confidence == 0.72
    assert out.raw["domain_confidence"] == 0.8
    assert [c.email for c in out.contacts[1:]] == ["luc.verdier@bromaker.fr", "l.verdier@bromaker.fr"]  # fallbacks
    assert ctx.repo.kv_get(hunter_month_key("search")) is None and ctx.repo.kv_get(hunter_month_key("verify")) is None

    _patch_site(monkeypatch, crawl, match, mx=False)
    assert enrich_company(ctx, small_company).status == "no_email"


# --------------------------------------------------------------------------- Hunter


def test_enrich_company_uses_hunter_people_and_pattern(ctx, small_company, monkeypatch):
    _patch_site(monkeypatch, SiteCrawl(domain="bromaker.fr"))
    calls = []

    def fake_data(domain, key, client=None):
        calls.append((domain, key))
        return {"pattern": "{f}{last}", "emails": [
            {"value": "rh@bromaker.fr", "type": "generic", "confidence": 80, "department": "hr"},
            {"value": "cdubois@bromaker.fr", "type": "personal", "confidence": 75, "first_name": "Claire",
             "last_name": "Dubois", "position": "Chargée de recrutement", "verification": {"status": "valid"}},
        ]}

    monkeypatch.setattr(people, "hunter_domain_data", fake_data)
    monkeypatch.setattr(_nominative, "hunter_verify", lambda email, key, client=None: "accept_all")
    out = enrich_company(_with(ctx, hunter=True), small_company)
    assert calls == [("bromaker.fr", "hk")] and out.status == "enriched"
    assert int(ctx.repo.kv_get(hunter_month_key("search"))) == 1
    assert out.raw["email_sources"]["rh@bromaker.fr"] == "hunter"
    assert ctx.repo.kv_get(f"{KV_PREFIX}bromaker.fr") == "{f}{last}"
    claire = next(c for c in out.contacts if c.email == "cdubois@bromaker.fr")
    assert (claire.kind, claire.person_name, claire.verified) == ("hr", "Claire Dubois", True)
    luc = next(c for c in out.contacts if c.person_name == "Luc VERDIER")
    assert (luc.email, luc.source, luc.verified) == ("lverdier@bromaker.fr", "pattern", False)  # learned pattern
    assert out.raw["email_sources"]["lverdier@bromaker.fr"] == "pattern:registry"


def test_enrich_company_never_guesses_an_address_hunter_knows_invalid(ctx, small_company, monkeypatch):
    _patch_site(monkeypatch, SiteCrawl(domain="bromaker.fr"))
    invalid = {"value": "luc.verdier@bromaker.fr", "first_name": "Luc", "last_name": "Verdier",
               "position": "Gérant", "verification": {"status": "invalid"}}
    monkeypatch.setattr(people, "hunter_domain_data", lambda *a, **k: {"emails": [invalid]})
    monkeypatch.setattr(_nominative, "hunter_verify", lambda email, key, client=None: "unknown")
    out = enrich_company(_with(ctx, hunter=True), small_company)
    assert [c.email for c in out.contacts] == ["l.verdier@bromaker.fr", "lverdier@bromaker.fr"]
    with_pattern = {"pattern": "{first}.{last}", "emails": [invalid]}
    monkeypatch.setattr(people, "hunter_domain_data", lambda *a, **k: with_pattern)
    assert enrich_company(_with(ctx, hunter=True), small_company).contacts == []  # dead under the domain pattern


def test_enrich_company_guess_verification_by_hunter(ctx, small_company, monkeypatch):
    _patch_site(monkeypatch, SiteCrawl(domain="bromaker.fr"))
    monkeypatch.setattr(people, "hunter_domain_data", lambda *a, **k: {})
    verdicts = {"luc.verdier@bromaker.fr": "accept_all", "l.verdier@bromaker.fr": "valid"}
    checked = []

    def fake_verify(email, key, client=None):
        checked.append(email)
        return verdicts.get(email, "invalid")

    monkeypatch.setattr(_nominative, "hunter_verify", fake_verify)
    out = enrich_company(_with(ctx, hunter=True), small_company)
    assert checked == ["luc.verdier@bromaker.fr", "l.verdier@bromaker.fr"]  # auditor never guessed
    assert [(c.email, c.source, c.verified, c.confidence) for c in out.contacts] == [
        ("l.verdier@bromaker.fr", "pattern", True, 0.4)]
    assert out.status == "enriched" and out.raw["email_sources"]["l.verdier@bromaker.fr"] == "pattern:registry"

    verdicts["l.verdier@bromaker.fr"] = "unknown"  # unverifiable → both kept as unverified best guesses
    kept = enrich_company(_with(ctx, hunter=True), small_company).contacts
    assert [(c.email, c.verified, c.confidence) for c in kept] == [
        ("luc.verdier@bromaker.fr", False, 0.3), ("l.verdier@bromaker.fr", False, 0.2)]
    assert enrich_company(_with(ctx, hunter=True, allow_unverified_guesses=False), small_company).contacts == []

    verdicts["luc.verdier@bromaker.fr"] = "claimed"  # opted out of Hunter: never written to
    assert enrich_company(_with(ctx, hunter=True), small_company).contacts == []


def test_enrich_company_hunter_budget_is_capped(ctx, small_company, monkeypatch):
    _patch_site(monkeypatch, SiteCrawl(domain="bromaker.fr"))
    ctx.repo.kv_set(hunter_month_key("search"), str(HUNTER_MONTHLY_CAPS["search"]))
    ctx.repo.kv_set(hunter_month_key("verify"), str(HUNTER_MONTHLY_CAPS["verify"]))
    _no_hunter(monkeypatch)
    assert enrich_company(_with(ctx, hunter=True, allow_unverified_guesses=False), small_company).status == "no_email"
    guessed = enrich_company(_with(ctx, hunter=True), small_company)  # no credit: unverified guesses only
    assert guessed.status == "enriched" and not any(c.verified for c in guessed.contacts)


# --------------------------------------------------------------------------- settings.outreach


def test_enrich_company_guesses_unverified_when_allowed_else_never(ctx, small_company, monkeypatch):
    _patch_site(monkeypatch, SiteCrawl(domain="bromaker.fr"))
    _no_hunter(monkeypatch)
    out = enrich_company(ctx, small_company)
    assert out.status == "enriched"
    assert [(c.email, c.kind, c.person_name) for c in out.contacts] == [
        ("luc.verdier@bromaker.fr", "leader", "Luc VERDIER"), ("l.verdier@bromaker.fr", "leader", "Luc VERDIER")]
    assert enrich_company(_with(ctx, allow_unverified_guesses=False), small_company).status == "no_email"
    big = small_company.model_copy(update={"raw": {**small_company.raw, "categorie_entreprise": "GE"}})
    assert enrich_company(ctx, big).status == "no_email"  # the leader of a large company is not a target


def test_enrich_company_with_nominative_contacts_disabled(ctx, small_company, monkeypatch):
    crawl = SiteCrawl(domain="bromaker.fr", contacts=[ContactEmail(email="luc.verdier@bromaker.fr", kind="named")])
    _patch_site(monkeypatch, crawl)
    monkeypatch.setattr(_nominative, "find_people_detailed", lambda *a, **k: pytest.fail("pas de recherche"))
    monkeypatch.setattr(_nominative, "hunter_domain_search", lambda domain, key, client=None: [
        ContactEmail(email="rh@bromaker.fr", confidence=0.8, source="hunter", kind="recruitment")])
    out = enrich_company(_with(ctx, hunter=True, allow_nominative=False), small_company)
    assert out.status == "enriched" and out.contacts[0].email == "rh@bromaker.fr"
    alone = enrich_company(_with(ctx, allow_nominative=False), small_company)
    assert alone.status == "no_email" and [c.email for c in alone.contacts] == ["luc.verdier@bromaker.fr"]


def test_enrich_company_finds_team_and_search_people(ctx, small_company, monkeypatch):
    crawl = SiteCrawl(
        domain="bromaker.fr", contacts=[ContactEmail(email="contact@bromaker.fr", kind="generic"),
                                        ContactEmail(email="julie.martin@bromaker.fr", confidence=0.6, kind="named")],
        sources={"julie.martin@bromaker.fr": "https://bromaker.fr/equipe"},
        page_html={"https://bromaker.fr/equipe": _fx("team_page.html")},
        site_text="Bromaker, agence web à Metz : applications sur mesure en React et Node.js. " * 4,
    )
    _patch_site(monkeypatch, crawl)
    _no_hunter(monkeypatch)
    monkeypatch.setattr(people, "_search_titles", lambda client, company, secrets, search_url=None: [
        "Claire Dubois - Chargée de recrutement - BROMAKER | LinkedIn"])
    llm = FakeLLM(lambda prompt, system, fast: SUMMARY if fast else "")
    out = enrich_company(_with(ctx, llm=llm), small_company)

    by_email = {c.email: c for c in out.contacts}
    assert by_email["julie.martin@bromaker.fr"].kind == "hr"
    assert by_email["julie.martin@bromaker.fr"].person_name == "Julie Martin"
    guesses = [(c.email, c.kind, c.confidence) for c in out.contacts if c.source == "pattern"]
    assert guesses == [("claire.dubois@bromaker.fr", "hr", 0.35), ("karim.benali@bromaker.fr", "tech", 0.35),
                       ("luc.verdier@bromaker.fr", "leader", 0.35)]  # learned first.last, one guess each
    sources = out.raw["email_sources"]
    assert sources["claire.dubois@bromaker.fr"] == "pattern:search"
    assert sources["karim.benali@bromaker.fr"] == "pattern:website"
    assert [p["name"] for p in out.raw["people"]][:3] == ["Julie Martin", "Claire Dubois", "Karim BENALI"]
    assert out.description == SUMMARY and llm.calls and llm.calls[0][2] is True  # fast model


def test_enrich_company_without_employee_search(ctx, small_company, monkeypatch):
    crawl = SiteCrawl(domain="bromaker.fr", contacts=[ContactEmail(email="contact@bromaker.fr", kind="generic")],
                      page_html={"https://bromaker.fr/equipe": _fx("team_page.html")})
    _patch_site(monkeypatch, crawl)
    _no_hunter(monkeypatch)
    monkeypatch.setattr(people, "_search_titles", lambda *a: pytest.fail("recherche désactivée"))
    out = enrich_company(_with(ctx, allow_employee_search=False), small_company)
    assert {c.person_name for c in out.contacts if c.person_name} == {"Luc VERDIER"}  # registry leader only


# --------------------------------------------------------------------------- description


def test_description_falls_back_to_meta_and_is_never_overwritten(ctx, small_company, monkeypatch):
    crawl = SiteCrawl(domain="bromaker.fr", contacts=[ContactEmail(email="rh@bromaker.fr", kind="recruitment")],
                      meta_description="Agence web à Metz : applications sur mesure.", site_text="x" * 300)

    def broken(prompt, system, fast):
        raise LLMError("quota")

    _patch_site(monkeypatch, crawl)
    assert enrich_company(_with(ctx, llm=FakeLLM(broken)), small_company).description.startswith("Agence web")
    refusal = FakeLLM("Désolé, je ne peux pas résumer ce texte.")
    assert enrich_company(_with(ctx, llm=refusal), small_company).description.startswith("Agence web")
    known = small_company.model_copy(update={"description": "Déjà décrite."})
    assert enrich_company(_with(ctx, llm=FakeLLM(broken)), known).description == "Déjà décrite."
    _patch_site(monkeypatch, SiteCrawl(domain="bromaker.fr", meta_description="Meta."), mx=False)
    assert enrich_company(ctx, small_company).description is None  # nobody to write to: no summary


# --------------------------------------------------------------------------- misc


def test_enrich_company_blacklisted_or_without_site(ctx, small_company, monkeypatch):
    monkeypatch.setattr(domains, "find_domain_verified", lambda company, client=None, secrets=None, contact=None, search_url=None: None)
    out = enrich_company(ctx, small_company)
    assert out.status == "no_email" and out.raw["enrich_note"]
    ctx.repo.blacklist_add("bromaker")
    assert enrich_company(ctx, small_company).status == "blacklisted"


def test_enrich_company_reuses_verified_domain_and_keeps_contacted_status(ctx, small_company, monkeypatch):
    known = small_company.model_copy(update={
        "domain": "bromaker.fr", "status": "contacted",
        "raw": {**small_company.raw, "domain_verified_by": "siren", "domain_confidence": 1.0},
    })
    monkeypatch.setattr(domains, "find_domain_verified", lambda *a, **k: pytest.fail("domain already verified"))
    monkeypatch.setattr(emails, "crawl_site", lambda domain, client=None, max_pages=8: SiteCrawl(
        domain=domain, contacts=[ContactEmail(email="jobs@bromaker.fr", kind="recruitment")]))
    monkeypatch.setattr(emails, "has_mx", lambda d: True)
    out = enrich_company(ctx, known)
    assert out.status == "contacted" and out.contacts[0].email == "jobs@bromaker.fr"


def test_enrich_companies_batch_isolation(ctx, make_company, monkeypatch):
    for i, score in enumerate((1, 9, 5)):
        ctx.repo.upsert_company(make_company(siren=f"10000000{i}", name=f"Boite {i}", raw={"outreach_score": score}))
    ctx.repo.upsert_company(make_company(siren="200000000", name="Déjà faite", status="enriched"))
    order = []

    def fake_enrich(ctx_, company):
        order.append(company.name)
        if company.name == "Boite 2":
            raise RuntimeError("site en panne")
        status = "enriched" if company.name == "Boite 1" else "no_email"
        return company.model_copy(update={"status": status})

    monkeypatch.setattr(emails, "enrich_company", fake_enrich)
    report = enrich_companies(ctx, limit=10)
    assert order == ["Boite 1", "Boite 2", "Boite 0"]  # best score first, only status "new"
    assert report == EnrichReport(processed=3, with_email=1, without_email=2)
    failed = next(c for c in ctx.repo.list_companies() if c.name == "Boite 2")
    assert failed.status == "new" and failed.raw["enrich_attempts"] == 1 and "panne" in failed.raw["enrich_error"]

    for _ in range(2):
        enrich_companies(ctx, limit=1)  # Boite 2 is the only "new" one left → fails twice more
    failed = next(c for c in ctx.repo.list_companies() if c.name == "Boite 2")
    assert failed.status == "no_email" and failed.raw["enrich_attempts"] == 3
    assert enrich_companies(ctx, limit=0) == EnrichReport(processed=0, with_email=0, without_email=0)


def test_enrich_company_end_to_end_with_live_fixtures(ctx, make_company, monkeypatch):
    """Real HTML (yaakadev.com) through domain verification and crawl, network mocked."""
    monkeypatch.setattr(domains, "_resolves", lambda d: False)
    monkeypatch.setattr(emails, "has_mx", lambda d: True)
    company = make_company(name="YAAKADEV", siren="812991172", domain=None, website=None, contacts=[],
                           raw={"names": ["YAAKADEV"]})
    with respx.mock:
        respx.get(domains.CLEARBIT_URL).mock(return_value=httpx.Response(200, text=_fx("clearbit_yaakadev.json")))
        _mock_yaakadev()
        out = enrich_company(ctx, company)
    assert out.domain == "yaakadev.com" and out.status == "enriched"
    assert [c.email for c in out.contacts] == ["secretariat@yaakadev.com"]
    assert out.description.startswith("Yaakadev digitalise vos processus métiers")  # meta (FakeLLM answers "{}")
    assert json.dumps(out.raw)  # serialisable for the repository
    ctx.repo.upsert_company(out)
    stored = ctx.repo.get_company(out.id)
    assert stored.contacts[0].email == "secretariat@yaakadev.com" and stored.description == out.description
