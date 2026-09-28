"""outreach.people — named people (registry, team pages, Hunter, search titles), ranking (no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from jobbot.config import Secrets
from jobbot.http import HostThrottle
from jobbot.outreach import domains, people
from jobbot.outreach._brave import BraveEngine
from jobbot.outreach._mailcheck import HUNTER_BASE
from jobbot.outreach._people_parse import looks_like_name, split_name
from jobbot.outreach.people import (
    Person,
    display_name,
    find_people,
    find_people_detailed,
    is_small_company,
    merge_people,
    people_from_html,
    person_from_title,
    rank_people,
    registry_people,
    role_category,
    role_rank,
)

FIX = Path(__file__).parent / "fixtures" / "outreach"
HTML = {"content-type": "text/html; charset=utf-8"}
API = "https://recherche-entreprises.api.gouv.fr/search"


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(domains, "_SITE_THROTTLE", HostThrottle(0))
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    monkeypatch.setattr(domains, "_DDG", domains._DdgBreaker())
    waits: list[float] = []
    monkeypatch.setattr(domains, "_sleep", waits.append)
    return waits


@pytest.fixture
def bromaker(make_company):
    return make_company(
        name="BROMAKER", siren="800065146", domain="bromaker.fr", contacts=[], leaders=["Luc VERDIER"],
        raw={"leaders_detail": [{"first": "Luc", "last": "VERDIER", "role": "Président de SAS"},
                                {"first": "Yann", "last": "AUDIT", "role": "Commissaire aux comptes suppléant"}],
             "tranche_effectif_salarie": "03", "categorie_entreprise": "PME", "names": ["BROMAKER"]},
    )


def _secrets(**keys) -> Secrets:
    return Secrets(_env_file=None, **keys)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- names & roles


@pytest.mark.parametrize(("text", "expected"), [
    ("Julie Martin", True), ("Jean-Pierre DE LA FONTAINE", True), ("Karim BENALI", True),
    ("Notre équipe", False), ("Acme SAS", False), ("J. Martin", False), ("julie martin", False), ("Julie", False),
    ("Julie Martin 2024", False), ("Un nom beaucoup trop long pour être celui d'une personne", False),
])
def test_looks_like_name(text, expected):
    assert looks_like_name(text) is expected


def test_split_and_display_names():
    assert split_name("MARTIN Julie") == ("Julie", "MARTIN")
    assert split_name("Luc VERDIER") == ("Luc", "VERDIER")
    assert split_name("Jean de La Fontaine") == ("Jean", "de La Fontaine")
    assert split_name("Cher") == ("Cher", "")
    assert display_name("JEAN-PIERRE DE LA FONTAINE") == "Jean-Pierre de La Fontaine"
    assert display_name("Luc VERDIER") == "Luc Verdier" and display_name("Julie Martin") == "Julie Martin"


@pytest.mark.parametrize(("role", "expected"), [
    ("Chargée de recrutement", "hr"), ("DRH", "hr"), ("Talent Acquisition Manager", "hr"),
    ("CTO – directeur technique", "tech"), ("Lead developer", "tech"), ("Responsable technique", "tech"),
    ("Président de SAS", "leader"), ("Gérant", "leader"), ("Co-fondateur & CEO", "leader"),
    ("Développeuse front-end", "other"), ("Chef de projet", "other"), ("Nos valeurs", None), ("", None),
])
def test_role_category(role, expected):
    assert role_category(role) == expected


# --------------------------------------------------------------------------- team pages & search titles


def test_people_from_team_page():
    found = people_from_html(_fx("team_page.html"), "https://bromaker.fr/equipe", "bromaker.fr")
    assert [(p.name, p.category, p.email) for p in found] == [
        ("Luc Verdier", "leader", None),  # JSON-LD founder (the CMS author without jobTitle is ignored)
        ("Julie Martin", "hr", "julie.martin@bromaker.fr"),  # mailto of her own card only
        ("Karim BENALI", "tech", None),
        ("Sophie Leroy", "other", None),
        ("Thomas Petit", "other", None),  # "Nom, Poste" line
    ]
    assert all(p.source == "website" and p.source_url == "https://bromaker.fr/equipe" for p in found)
    assert found[2].first == "Karim" and found[2].last == "BENALI"


def test_people_from_html_ignores_testimonials_home_pages_and_foreign_addresses():
    home = _fx("home_testimonials.html")
    assert people_from_html(home, "https://bromaker.fr/", "bromaker.fr") == []  # home page: no card parsing
    assert people_from_html(home, "https://bromaker.fr/qui-sommes-nous", "bromaker.fr") == []  # testimonial
    page = ('<h3>Marc Durand</h3><p>Directeur général, Transports Durand</p>'
            '<div><h3>Anne Morel</h3><p>Responsable RH</p><a href="mailto:anne@gmail-like.com">x</a></div>')
    assert [(p.name, p.email) for p in people_from_html(page, "https://x.fr/equipe", "bromaker.fr")] == [
        ("Anne Morel", None)]
    forced = people_from_html("<h3>Anne Morel</h3><p>DRH</p>", "https://x.fr/nous", "x.fr", team_page=True)
    assert [p.name for p in forced] == ["Anne Morel"]


def test_person_from_search_titles():
    names = ["BROMAKER"]
    claire = person_from_title("Claire Dubois - Chargée de recrutement - Bromaker | LinkedIn", names)
    assert (claire.name, claire.category, claire.source, claire.source_url) == ("Claire Dubois", "hr", "search", None)
    karim = person_from_title("Karim Benali – CTO chez Bromaker | LinkedIn", names)
    assert (karim.role, karim.category) == ("CTO", "tech")
    strong = person_from_title("<strong>Luc Verdier</strong> - Gérant - <strong>Bromaker</strong> | LinkedIn", names)
    assert strong.category == "leader"
    for title in ("Paul Roux - Développeur full-stack - Bromaker | LinkedIn",  # not a contact role
                  "Anne Morel - Responsable RH - Autre Société | LinkedIn",  # another company
                  "Bromaker - Agence web à Metz", "Claire Dubois - Bromaker | LinkedIn", "", "Seul"):
        assert person_from_title(title, names) is None


# --------------------------------------------------------------------------- registry


def test_registry_people_from_discovery_data(bromaker):
    found = registry_people(bromaker)
    assert [(p.name, p.role, p.category, p.source) for p in found] == [
        ("Luc VERDIER", "Président de SAS", "leader", "registry")]  # statutory auditor excluded


@respx.mock
def test_registry_people_fetched_by_siren_when_unknown(make_company):
    route = respx.get(API).mock(return_value=httpx.Response(200, json={"results": [{
        "siren": "800065146",
        "dirigeants": [
            {"nom": "VERDIER", "prenoms": "LUC PIERRE", "qualite": "Gérant", "type_dirigeant": "personne physique",
             "date_de_naissance": "1980-01"},
            {"nom": "AUDIT", "prenoms": "YANN", "qualite": "Commissaire aux comptes titulaire",
             "type_dirigeant": "personne physique"},
            {"denomination": "HOLDING X", "qualite": "Président", "type_dirigeant": "personne morale"},
        ],
    }]}))
    lba = make_company(siren="800065146", source="lba_recruteur", leaders=[], raw={})
    found = registry_people(lba)
    assert [(p.name, p.role) for p in found] == [("Luc VERDIER", "Gérant")]
    assert route.calls[0].request.url.params["q"] == "800065146"
    route.mock(return_value=httpx.Response(500))
    fallback = make_company(siren="800065146", leaders=["Anne MOREL"], raw={})
    assert [(p.first, p.last) for p in registry_people(fallback)] == [("Anne", "MOREL")]


# --------------------------------------------------------------------------- ranking


def _p(name: str, role: str, category: str, source: str = "website", **extra) -> Person:
    first, last = split_name(name)
    return Person(name=name, first=first, last=last, role=role, category=category, source=source, **extra)


def test_rank_people_by_preferred_roles_and_company_size():
    crowd = [
        _p("Luc Verdier", "Gérant", "leader", "registry"), _p("Karim Benali", "CTO", "tech"),
        _p("Sophie Leroy", "Développeuse", "other"), _p("Claire Dubois", "Chargée de recrutement", "hr", "search"),
        _p("Julie Martin", "Responsable RH", "hr"),
    ]
    assert [p.name for p in rank_people(crowd)] == [
        "Julie Martin", "Claire Dubois", "Karim Benali", "Luc Verdier", "Sophie Leroy"]
    assert [p.name for p in rank_people(crowd, small_company=False)][-1] == "Luc Verdier"
    assert [p.name for p in rank_people(crowd, ["cto", "gérant"])][:2] == ["Karim Benali", "Luc Verdier"]
    assert role_rank("Directrice technique", ["rh", "directeur technique"]) == 1
    assert role_rank("Rhône logistique", ["rh"]) == 1  # "rh" is a word, not a prefix


def test_merge_people_keeps_best_role_registry_names_and_address():
    merged = merge_people([
        _p("Luc VERDIER", "Président de SAS", "leader", "registry"),
        _p("Luc Verdier", "CTO & co-fondateur", "tech", email="luc@bromaker.fr"),
        _p("Luc Verdier", "Gérant", "leader", "hunter", email="l.verdier@bromaker.fr", verified=True),
    ])
    assert len(merged) == 1
    luc = merged[0]
    assert (luc.category, luc.role, luc.last, luc.email, luc.verified) == (
        "tech", "CTO & co-fondateur", "VERDIER", "l.verdier@bromaker.fr", True)


def test_is_small_company(make_company):
    assert is_small_company(make_company(raw={}))
    assert is_small_company(make_company(raw={"tranche_effectif_salarie": "12"}))
    assert not is_small_company(make_company(raw={"tranche_effectif_salarie": "21"}))
    assert not is_small_company(make_company(raw={"categorie_entreprise": "GE", "tranche_effectif_salarie": "NN"}))


# --------------------------------------------------------------------------- find_people (all sources)


def _mock_site():
    home = '<a href="/equipe">Notre équipe</a><a href="/contact">Contact</a><a href="https://x.com/equipe">x</a>'
    respx.get("https://bromaker.fr/").mock(return_value=httpx.Response(200, text=home, headers=HTML))
    respx.get("https://bromaker.fr/equipe").mock(
        return_value=httpx.Response(200, text=_fx("team_page.html"), headers=HTML))


def _hunter_payload():
    return {"data": {"domain": "bromaker.fr", "pattern": "{first}.{last}", "emails": [
        {"value": "rh@bromaker.fr", "type": "generic", "confidence": 90, "verification": {"status": "valid"}},
        {"value": "julie.martin@bromaker.fr", "type": "personal", "confidence": 92, "first_name": "Julie",
         "last_name": "Martin", "position": "Responsable RH", "department": "hr",
         "verification": {"status": "valid"}},
        {"value": "anne.morel@bromaker.fr", "type": "personal", "confidence": 70, "first_name": "Anne",
         "last_name": "Morel", "department": "executive"},
        {"value": "old@bromaker.fr", "type": "personal", "first_name": "Old", "last_name": "Timer",
         "verification": {"status": "invalid"}},
    ]}}


@respx.mock
def test_find_people_detailed_combines_every_source(bromaker, repo):
    _mock_site()
    hunter = respx.get(f"{HUNTER_BASE}/domain-search").mock(return_value=httpx.Response(200, json=_hunter_payload()))
    brave = respx.get(domains.BRAVE_URL).mock(return_value=httpx.Response(200, json={"web": {"results": [
        {"title": "Claire Dubois - Chargée de recrutement - Bromaker | LinkedIn", "url": "https://fr.linkedin.com/in/c"},
        {"title": "Karim Benali – CTO chez Bromaker | LinkedIn", "url": "https://fr.linkedin.com/in/k"},
    ]}}))
    found = find_people_detailed(bromaker, secrets=_secrets(hunter_api_key="hk"),
                                 brave=BraveEngine(api_key="bk", repo=repo, monthly_cap=5))

    assert found.sources == ["registry", "website", "hunter", "search"]
    assert found.pattern == "{first}.{last}"
    assert [c.email for c in found.contacts] == ["rh@bromaker.fr", "julie.martin@bromaker.fr", "anne.morel@bromaker.fr"]
    names = [p.name for p in found.people]
    assert names[:4] == ["Julie Martin", "Claire Dubois", "Karim BENALI", "Luc Verdier"]
    assert "Old Timer" in names and len(names) == len(set(n.lower() for n in names))  # merged duplicates
    julie = found.people[0]
    assert julie.email == "julie.martin@bromaker.fr" and julie.verified
    luc = next(p for p in found.people if p.name == "Luc Verdier")
    assert luc.last == "VERDIER" and luc.category == "leader"  # registry split kept
    assert hunter.calls[0].request.headers["x-api-key"] == "hk"
    brave_request = brave.calls[0].request
    assert brave_request.headers["x-subscription-token"] == "bk"
    assert 'site:linkedin.com/in "BROMAKER"' in brave_request.url.params["q"]


@respx.mock
def test_find_people_uses_duckduckgo_politely_and_stops_on_bot_flag(bromaker, _offline):
    ddg = respx.get(domains.DDG_URL).mock(return_value=httpx.Response(200, text=_fx("ddg_linkedin.html")))
    kwargs = {"pages": [], "use_hunter": False}
    first = find_people(bromaker, **kwargs)
    assert [p.name for p in first] == ["Claire Dubois", "Karim Benali", "Luc VERDIER"]
    find_people(bromaker, **kwargs)
    # the sleep is 25 s + jitter minus the time already elapsed since the previous query (≈ ms here)
    assert len(ddg.calls) == 2 and _offline and min(_offline) >= domains.DDG_MIN_INTERVAL_S - 1

    ddg.mock(return_value=httpx.Response(202, text="<html>anomaly.js?cc=botnet</html>"))
    assert [p.name for p in find_people(bromaker, **kwargs)] == ["Luc VERDIER"]
    find_people(bromaker, **kwargs)
    assert len(ddg.calls) == 3  # blocked for the rest of the run: no further query


@respx.mock
def test_find_people_skips_brave_once_its_monthly_budget_is_spent(bromaker, repo):
    brave = respx.get(domains.BRAVE_URL).mock(return_value=httpx.Response(200, json={}))
    ddg = respx.get(domains.DDG_URL).mock(return_value=httpx.Response(200, text=_fx("ddg_linkedin.html")))
    found = find_people_detailed(bromaker, pages=[], use_hunter=False,
                                 brave=BraveEngine(api_key="bk", repo=repo, monthly_cap=0))
    assert not brave.called and ddg.called  # free last resort instead of a paid query
    assert "Claire Dubois" in [p.name for p in found.people]


@respx.mock
def test_find_people_isolates_failing_sources(bromaker, repo):
    respx.get("https://bromaker.fr/").mock(side_effect=httpx.ConnectError("down"))
    respx.get("https://www.bromaker.fr/").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{HUNTER_BASE}/domain-search").mock(side_effect=httpx.ConnectError("down"))
    respx.get(domains.BRAVE_URL).mock(return_value=httpx.Response(429, json={}))
    found = find_people_detailed(bromaker, secrets=_secrets(hunter_api_key="hk"),
                                 brave=BraveEngine(api_key="bk", repo=repo, monthly_cap=5))
    assert [p.name for p in found.people] == ["Luc VERDIER"] and found.pattern is None and found.contacts == []


def test_find_people_without_site_or_search(make_company):
    company = make_company(domain=None, website=None, raw={"leaders_detail": []}, leaders=[])
    found = find_people_detailed(company, use_search=False, secrets=_secrets(hunter_api_key="hk"))
    assert found.people == [] and found.sources == ["registry"]


def test_people_module_exports_parse_helpers():
    assert people.people_from_titles(["Claire Dubois - DRH - Bromaker | LinkedIn"], ["Bromaker"])[0].category == "hr"
