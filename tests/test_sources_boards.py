"""Tests for sources/hellowork.py, sources/linkedin.py and sources/alerts.py (no network except -m live)."""

from __future__ import annotations

import base64
import gzip
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import respx

from jobbot import clock
from jobbot.config import LocationPref
from jobbot.http import HostThrottle
from jobbot.models import ApplyMethod, ContractType, JobStatus
from jobbot.sources import alerts
from jobbot.sources import hellowork as hw
from jobbot.sources import linkedin as li
from jobbot.sources.base import SearchQuery, SourceUnavailable

FIX = Path(__file__).parent / "fixtures" / "sources_boards"
METZ = LocationPref(city="Metz", postal_code="57000", radius_km=30)
LI_END = "<!DOCTYPE html>\n\n<!---->"  # guest search past the last page (26 bytes)


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("jobbot.http.time.sleep", lambda _s: None)


@pytest.fixture
def live(request):
    expr = request.config.getoption("-m") or ""
    if "live" not in expr or "not live" in expr:
        pytest.skip("test réseau : lancer avec -m live")


def hw_source() -> hw.HelloWorkSource:
    src = hw.HelloWorkSource(httpx.Client())
    src._throttle = HostThrottle(0)
    return src


def li_source(easy: bool = True) -> li.LinkedInSource:
    src = li.LinkedInSource(httpx.Client(), easy_apply_only=easy)
    src._throttle, src._sleep = HostThrottle(0), (lambda _s: None)
    return src


# =========================================================================== HelloWork


def test_hw_parse_search_page_real_fixture():
    total, cards = hw.parse_search_page(fixture("hw_search.html"))
    assert total == 293 and len(cards) == 8
    first = hw.card_to_job(cards[0], clock.now())
    assert (first.external_id, first.company, first.location) == ("78273485", "Utigroup", "Nancy - 54")
    assert first.title == "Concepteur Développeur . Net H/F"
    assert first.url == "https://www.hellowork.com/fr-fr/emplois/78273485.html"
    assert first.contract_type is ContractType.CDI and first.apply_method is ApplyMethod.HELLOWORK
    assert first.salary == "38 000 - 42 000 € / an" and first.published_at is not None
    assert first.raw["tags"] == ["Télétravail partiel"] and first.raw["needs_details"] is True
    methods = {c["id"]: hw.card_to_job(c, clock.now()).apply_method for c in cards}
    assert methods["74578079"] is ApplyMethod.EXTERNAL  # UrlWithForm
    assert methods["83571317"] is ApplyMethod.HELLOWORK  # Smartapply_v2
    alternance = hw.card_to_job(next(c for c in cards if c["id"] == "82502103"), clock.now())
    assert alternance.contract_type is ContractType.ALTERNANCE and alternance.raw["tags"] == ["2 ans"]


def test_hw_build_search_params():
    q = SearchQuery(keywords="développeur", location=METZ, contract_types=[ContractType.ALTERNANCE, ContractType.CDI],
                    max_age_days=7)
    params = hw.build_search_params(q)
    assert ("l_autocomplete", hw.LOCATION_URIS["metz"]) in params and ("ray", "50") in params
    assert ("c", "Alternance") in params and ("c", "CDI") in params and ("p", "1") in params
    assert ("d", "w") in params and ("f", "Informatique_dev") in params and ("t", "Complet") not in params
    remote = hw.build_search_params(SearchQuery(keywords="dev", remote=True, max_age_days=90,
                                                location=LocationPref(city="Lille", radius_km=500)))
    assert ("t", "Complet") in remote and ("d", "all") in remote and ("ray", "100") in remote
    assert not any(k == "l_autocomplete" for k, _ in remote)
    assert [k for k, _ in hw.build_search_params(SearchQuery(keywords="x"))].count("l") == 0


@pytest.mark.parametrize("label,expected", [
    ("Alternance", ContractType.ALTERNANCE), ("CDI", ContractType.CDI), ("CDD", ContractType.CDD),
    ("Stage", ContractType.STAGE), ("Intérim", ContractType.INTERIM), ("Travail_temp", ContractType.INTERIM),
    ("Indépendant", ContractType.FREELANCE), ("Contrat de professionnalisation", ContractType.ALTERNANCE),
    ("", ContractType.OTHER), (None, ContractType.OTHER),
])
def test_hw_contract_from_label(label, expected):
    assert hw.contract_from_label(label) is expected


def test_hw_small_helpers():
    assert hw.remote_from_label("Télétravail complet") is True
    assert hw.remote_from_label("Télétravail partiel") is None
    assert hw.remote_from_label("Pas de télétravail") is False and hw.remote_from_label("CDI") is None
    assert hw.apply_method_for("Smartapply_v3") is ApplyMethod.HELLOWORK
    assert hw.apply_method_for(None, True) is ApplyMethod.HELLOWORK
    assert hw.apply_method_for(None, True, "Postuler sur le site du recruteur") is ApplyMethod.EXTERNAL
    assert hw.apply_method_for(None) is ApplyMethod.EXTERNAL
    assert [hw.radius_param(r) for r in (0, 3, 20, 21, 80, 300)] == [0, 5, 20, 50, 100, 100]
    assert [hw.date_param(d) for d in (1, 2, 7, 30, 31, 60)] == ["h", "d", "w", "m", "m", "all"]
    now = clock.now()
    assert hw._relative_date("il y a 3 jours", now) == now - 3 * hw._REL_UNITS["jour"]
    assert hw._relative_date("Hier", now) is not None and hw._relative_date("Aujourd'hui", now) == now
    assert hw._relative_date("bientôt", now) is None


def test_hw_parse_offer_page_in_site():
    job = hw.parse_offer_page("79006307", fixture("hw_detail_79006307.html"))
    assert job.title == "Alternant - Developpeur Web Fullstack H/F" and job.company == "Chausséa"
    assert (job.location, job.postal_code) == ("Metz", "57000")
    assert job.apply_method is ApplyMethod.HELLOWORK and job.contract_type is ContractType.ALTERNANCE
    assert job.published_at.isoformat().startswith("2026-09-14") and job.salary is None
    assert "Les missions du poste" in job.description and "Compétences : Force de proposition" in job.description
    assert job.raw["response_type"] == "ATS" and job.raw["company_type"] == "entreprises"
    assert job.raw["needs_details"] is False and "Vue JS" in job.raw["skills"]


def test_hw_parse_offer_page_external_partner():
    job = hw.parse_offer_page("81303268", fixture("hw_detail_81303268.html"))
    assert job.apply_method is ApplyMethod.EXTERNAL and job.raw["apply_label"].endswith("site du partenaire")
    assert job.salary == "492 - 1 823 € / mois" and job.raw["skills"] == ["Respect des délais"]
    assert job.raw["industry"] == ["Services aux Entreprises"] and "company_type" not in job.raw


def test_hw_parse_offer_page_html_fallback_and_garbage():
    html = """<h1 id="main-content"><span data-cy="jobTitle">Développeur React H/F</span>
      <a href="/fr-fr/entreprises/acme.html">Acme</a></h1><ul><li>Metz - 57</li><li>CDI</li></ul>
      <div id="offer-panel"><p>Stack React, TypeScript.</p></div>
      <a data-cy="applyButtonHeader" href="#postuler">Postuler</a>"""
    job = hw.parse_offer_page("12345678", html)
    assert (job.title, job.company, job.location) == ("Développeur React H/F", "Acme", "Metz - 57")
    assert job.contract_type is ContractType.CDI and "TypeScript" in job.description
    assert job.apply_method is ApplyMethod.EXTERNAL  # no response_type / directApply known
    assert hw.parse_offer_page("12345678", "<html><body>Erreur</body></html>") is None
    broken = '<script type="application/ld+json">{oops</script><script>window.dataLayer.push({bad});</script>'
    assert hw.parse_offer_page("1", broken) is None


@respx.mock
def test_hw_search_uses_page_one_and_respects_max_results():
    route = respx.get(hw.BASE + hw.SEARCH_PATH).mock(return_value=httpx.Response(200, text=fixture("hw_search.html")))
    jobs = list(hw_source().search(SearchQuery(keywords="développeur", location=METZ, max_results=3)))
    assert len(jobs) == 3 and all(j.source == "hellowork" and j.status is JobStatus.NEW for j in jobs)
    assert route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["p"] == "1" and params["k"] == "développeur" and params["f"] == "Informatique_dev"


@respx.mock
def test_hw_blocked_suspends_source():
    route = respx.get(hw.BASE + hw.SEARCH_PATH).mock(return_value=httpx.Response(403, text="403 Forbidden"))
    src = hw_source()
    with pytest.raises(SourceUnavailable, match="bloque"):
        list(src.search(SearchQuery(keywords="dev")))
    ok, reason = src.available()
    assert ok is False and "HTTP 403" in reason
    with pytest.raises(SourceUnavailable):
        src.job_from_id("79006307")
    assert route.call_count == 1  # no more requests once blocked


@respx.mock
@pytest.mark.parametrize("response,match", [
    (httpx.Response(200, text="<html><body>maintenance</body></html>"), "méconnaissable"),
    (httpx.Response(500), "HTTP 500"),
    (httpx.ConnectError("boom"), "injoignable"),
])
def test_hw_search_errors(response, match):
    route = respx.get(hw.BASE + hw.SEARCH_PATH)
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    src = hw_source()
    assert src.available() == (True, "")
    with pytest.raises(SourceUnavailable, match=match):
        list(src.search(SearchQuery(keywords="dev")))


@respx.mock
def test_hw_job_from_id_statuses():
    respx.get(hw.offer_url("79006307")).mock(return_value=httpx.Response(200, text=fixture("hw_detail_79006307.html")))
    respx.get(hw.offer_url("50000000")).mock(return_value=httpx.Response(410, text="Erreur 410"))
    respx.get(hw.offer_url("60000000")).mock(return_value=httpx.Response(404))
    respx.get(hw.offer_url("70000000")).mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get(hw.offer_url("80000000")).mock(return_value=httpx.Response(502))
    src = hw_source()
    assert src.job_from_id(" 79006307 ").apply_method is ApplyMethod.HELLOWORK
    assert src.job_from_id("50000000") is None and src.job_from_id("60000000") is None
    for bad_id, error in (("70000000", SourceUnavailable), ("80000000", SourceUnavailable), ("abc", ValueError)):
        with pytest.raises(error):
            src.job_from_id(bad_id)


@respx.mock
def test_hw_fetch_details_merges_and_expires(make_job):
    respx.get(hw.offer_url("79006307")).mock(return_value=httpx.Response(200, text=fixture("hw_detail_79006307.html")))
    respx.get(hw.offer_url("50000000")).mock(return_value=httpx.Response(410))
    src = hw_source()
    card = make_job(source="hellowork", external_id="79006307", description="", title="Alternant",
                    apply_method=ApplyMethod.EXTERNAL, status=JobStatus.SHORTLISTED,
                    raw={"needs_details": True, "x": 1})
    full = src.fetch_details(card)
    assert full.id == card.id and full.status is JobStatus.SHORTLISTED and full.found_at == card.found_at
    assert full.title.startswith("Alternant - Developpeur") and full.apply_method is ApplyMethod.HELLOWORK
    assert full.raw["x"] == 1 and full.raw["needs_details"] is False and len(full.description) > 400
    gone = src.fetch_details(make_job(source="hellowork", external_id="50000000", status=JobStatus.SHORTLISTED))
    assert gone.status is JobStatus.EXPIRED and gone.raw["expired"] is True
    applied = src.fetch_details(make_job(source="hellowork", external_id="50000000", status=JobStatus.APPLIED))
    assert applied.status is JobStatus.APPLIED
    other = make_job()  # france_travail job: untouched, no request
    assert src.fetch_details(other) is other


# =========================================================================== LinkedIn


def test_li_parse_search_cards_real_fixture():
    cards = li.parse_search_cards(fixture("li_search_easy.html"))
    assert len(cards) == 10
    job = li.card_to_job(cards[1], clock.now(), easy_apply=True)
    assert (job.external_id, job.title, job.company, job.location) == (
        "4310551940", "Développeur Java confirmé F/H", "OPEN", "Metz")
    assert job.url == "https://www.linkedin.com/jobs/view/4310551940/"
    assert job.apply_method is ApplyMethod.LINKEDIN_EASY_APPLY and job.raw["easy_apply"] is True
    assert job.published_at.date().isoformat() == "2026-09-11" and job.raw["insight"] == "Recrutement actif"
    assert li.card_to_job(cards[0], clock.now(), easy_apply=False).apply_method is ApplyMethod.EXTERNAL
    assert li.parse_search_cards(LI_END) == []


def test_li_build_search_params():
    p = li.build_search_params(SearchQuery(keywords="alternance développeur", location=METZ, max_age_days=7), True)
    assert p == {"f_TPR": "r604800", "geoId": "103167155", "distance": "25",
                 "keywords": "alternance développeur", "f_AL": "true"}
    lux = li.build_search_params(SearchQuery(keywords="dev", location=LocationPref(city="Luxembourg")), False)
    assert lux["geoId"] == "104042105" and "distance" not in lux and "f_AL" not in lux
    lille = li.build_search_params(SearchQuery(keywords="dev", location=LocationPref(city="Lille")), False)
    assert lille["location"] == "Lille" and "geoId" not in lille
    remote = li.build_search_params(SearchQuery(keywords="développeur", remote=True), True)
    assert remote["geoId"] == li.FRANCE_GEO_ID and remote["keywords"] == "développeur télétravail"
    assert [li.distance_param(km) for km in (0, 8, 16, 40, 80, 200)] == [0, 5, 10, 25, 50, 50]


@pytest.mark.parametrize("title,desc,etype,level,expected", [
    ("Alternance - Développeur web", "", None, None, ContractType.ALTERNANCE),
    ("APPRENTICESHIP ANALYST DEVELOPER", "", None, None, ContractType.ALTERNANCE),
    ("Stage développeur", "", None, None, ContractType.STAGE),
    ("Développeur CDI", "alternance possible", None, None, ContractType.CDI),
    ("Développeur web", "Poste en alternance, rythme 3 semaines / 1 semaine", None, None, ContractType.ALTERNANCE),
    ("Développeur web", "L'équipe compte 3 alternants et 5 devs.", None, None, ContractType.OTHER),
    ("Développeur", "", "Stage / Alternance", None, ContractType.ALTERNANCE),
    ("Développeur", "stage de fin d'études", None, "Stagiaire / Alternant", ContractType.STAGE),
    ("Développeur", "Contrat : CDD de 6 mois", None, None, ContractType.CDD),
    ("Développeur freelance", "", None, None, ContractType.FREELANCE),
    ("Développeur", "mission en intérim", None, None, ContractType.INTERIM),
])
def test_li_guess_contract_type(title, desc, etype, level, expected):
    assert li.guess_contract_type(title, desc, etype, level) is expected


def test_li_detect_remote():
    assert li.detect_remote("Poste en full remote") is True
    assert li.detect_remote("Télétravail complet possible") is True
    assert li.detect_remote("Hybride, 2 jours de télétravail") is None


def test_li_parse_job_detail_and_merge():
    easy = li.parse_job_detail(fixture("li_detail_4457696481.html"))
    assert easy["apply_mode"] == "easy_apply" and easy["company_id"] == "3234841"
    assert easy["criteria"]["Type d'emploi"] == "Temps plein" and easy["closed"] is False
    assert easy["poster"]["profile_url"] == "https://fr.linkedin.com/in/prenom-nom-00000000"
    ext = li.parse_job_detail(fixture("li_detail_4469506752.html"))
    assert ext["apply_mode"] == "external" and ext["poster"] is None and ext["location"] == "Florange"
    card = li.card_to_job(li.parse_search_cards(fixture("li_search_all.html"))[0], clock.now(), easy_apply=False)
    merged = li.merge_detail(card, ext)
    assert merged.contract_type is ContractType.ALTERNANCE and merged.apply_method is ApplyMethod.EXTERNAL
    assert merged.company == "ArcelorMittal Aços Longos" and "Responsibilities" in merged.description
    assert merged.raw["needs_details"] is False and merged.raw["company_id"] == "35461482"
    assert li.parse_job_detail("")["apply_mode"] == "unknown"


def _li_search_route(*responses):
    return respx.get(li.SEARCH_URL).mock(side_effect=list(responses))


@respx.mock
def test_li_search_paginates_and_stops():
    route = _li_search_route(httpx.Response(200, text=fixture("li_search_easy.html")), httpx.Response(200, text=LI_END))
    jobs = list(li_source().search(SearchQuery(keywords="développeur", location=METZ, max_results=100)))
    assert len(jobs) == 10 and route.call_count == 2
    first, second = (c.request.url.params for c in route.calls)
    assert first["start"] == "0" and second["start"] == "10" and first["f_AL"] == "true"
    assert all(j.apply_method is ApplyMethod.LINKEDIN_EASY_APPLY for j in jobs)


@respx.mock
def test_li_search_max_results_and_duplicates():
    route = _li_search_route(*[httpx.Response(200, text=fixture("li_search_all.html"))] * 3)
    assert len(list(li_source(easy=False).search(SearchQuery(keywords="dev", max_results=4)))) == 4
    assert route.call_count == 1
    route.reset()
    jobs = list(li_source(easy=False).search(SearchQuery(keywords="dev", max_results=30)))
    assert len(jobs) == 10 and route.call_count == 2  # page 2 only repeats page 1 → stop
    assert "f_AL" not in route.calls.last.request.url.params


@respx.mock
def test_li_search_start_limit_and_changed_site():
    _li_search_route(httpx.Response(400))
    assert list(li_source().search(SearchQuery(keywords="dev"))) == []
    respx.get(li.SEARCH_URL).mock(return_value=httpx.Response(200, text="<ul><li>nouvelle mise en page</li></ul>"))
    with pytest.raises(SourceUnavailable, match="méconnaissables"):
        list(li_source().search(SearchQuery(keywords="dev")))


@respx.mock
def test_li_429_backoff_then_success_then_suspension():
    slept: list[float] = []
    src = li_source()
    src._sleep = slept.append
    _li_search_route(httpx.Response(429), httpx.Response(200, text=LI_END))
    assert list(src.search(SearchQuery(keywords="dev"))) == []
    assert 30.0 in slept and src.available() == (True, "")
    _li_search_route(httpx.Response(429), httpx.Response(429))
    with pytest.raises(SourceUnavailable, match="429"):
        list(src.search(SearchQuery(keywords="dev")))
    assert src.available()[0] is False
    with pytest.raises(SourceUnavailable):
        src.fetch_details(li.card_to_job(li.parse_search_cards(fixture("li_search_easy.html"))[0], clock.now(), True))


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(999), httpx.Response(403), httpx.Response(500),
    httpx.Response(200, text='<script>window.location.href = "https://www.linkedin.com/authwall?trk=qf"</script>'),
])
def test_li_blocking_responses(response):
    respx.get(li.SEARCH_URL).mock(return_value=response)
    with pytest.raises(SourceUnavailable):
        list(li_source().search(SearchQuery(keywords="dev")))


@respx.mock
def test_li_redirect_to_authwall_and_network_error():
    respx.get(li.SEARCH_URL).mock(return_value=httpx.Response(302, headers={"Location": "https://www.linkedin.com/authwall?x=1"}))
    respx.get("https://www.linkedin.com/authwall").mock(return_value=httpx.Response(200, text="login"))
    src = li_source()
    with pytest.raises(SourceUnavailable, match="refuse"):
        list(src.search(SearchQuery(keywords="dev")))
    assert src.available()[0] is False
    respx.get(li.SEARCH_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(SourceUnavailable, match="injoignable"):
        list(li_source().search(SearchQuery(keywords="dev")))


@respx.mock
def test_li_fetch_details(make_job):
    detail_url = li.DETAIL_URL.format(job_id="4457696481")
    respx.get(detail_url).mock(return_value=httpx.Response(200, text=fixture("li_detail_4457696481.html")))
    respx.get(li.DETAIL_URL.format(job_id="1")).mock(return_value=httpx.Response(404))
    respx.get(li.DETAIL_URL.format(job_id="2")).mock(return_value=httpx.Response(500))
    respx.get(li.DETAIL_URL.format(job_id="3")).mock(return_value=httpx.Response(200, text="<html></html>"))
    closed = fixture("li_detail_4469506752.html") + (
        '<figcaption class="closed-job__flavor--closed">Les candidatures ne sont plus acceptées</figcaption>')
    respx.get(li.DETAIL_URL.format(job_id="4")).mock(return_value=httpx.Response(200, text=closed))
    src = li_source()
    base = make_job(source="linkedin", external_id="4457696481", description="", apply_method=ApplyMethod.EXTERNAL)
    full = src.fetch_details(base)
    assert full.apply_method is ApplyMethod.LINKEDIN_EASY_APPLY and full.company == "EFOR" and full.description
    assert full.raw["poster"]["name"] == "Prénom NOM" and full.id == base.id
    assert src.fetch_details(make_job(source="linkedin", external_id="1")).status is JobStatus.EXPIRED
    assert src.fetch_details(make_job(source="linkedin", external_id="4")).raw["closed"] is True
    for bad in ("2", "3"):
        with pytest.raises(SourceUnavailable):
            src.fetch_details(make_job(source="linkedin", external_id=bad))
    other = make_job(source="linkedin", external_id="not-a-number")
    assert src.fetch_details(other) is other


# =========================================================================== alerts


def test_alert_linkedin_html():
    jobs = alerts.jobs_from_alert_email("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
                                        "« alternance développeur » : ArcelorMittal recrute",
                                        fixture("alert_linkedin.html"), "")
    assert [j.external_id for j in jobs] == ["4469506752", "4457696481", "4310551940"]
    first, second, third = jobs
    assert (first.title, first.company, first.location) == (
        "Alternance - Développeur Full Stack (H/F)", "ArcelorMittal", "Florange, Grand Est, France")
    assert first.apply_method is ApplyMethod.EXTERNAL and first.contract_type is ContractType.ALTERNANCE
    assert second.title == "Apprenti Ingénieur en développement C#" and second.company == "EFOR"
    assert second.apply_method is ApplyMethod.LINKEDIN_EASY_APPLY and second.raw["easy_apply"] is True
    assert third.company == "OPEN" and third.location == "Nancy"
    assert first.url == "https://www.linkedin.com/jobs/view/4469506752/" and first.status is JobStatus.NEW
    assert first.raw["from_alert"] is True and first.raw["needs_details"] is True
    assert first.raw["alert_sender"] == "linkedin.com" and first.description == ""


def test_alert_linkedin_text_only_and_merge():
    jobs = alerts.jobs_from_alert_email("jobalerts-noreply@linkedin.com", "Nouvelles offres", "",
                                        fixture("alert_linkedin.txt"))
    by_id = {j.external_id: j for j in jobs}
    assert set(by_id) == {"4469506752", "4457696481", "4310551940"}
    assert by_id["4469506752"].company == "ArcelorMittal" and by_id["4469506752"].location.startswith("Florange")
    assert by_id["4457696481"].apply_method is ApplyMethod.LINKEDIN_EASY_APPLY
    assert (by_id["4310551940"].title, by_id["4310551940"].company, by_id["4310551940"].location) == (
        "Développeur Java junior F/H", "OPEN", "Nancy")
    bare_html = '<a href="https://www.linkedin.com/comm/jobs/view/4469506752/"><img src="x.png"></a>'
    merged = alerts.jobs_from_alert_email("jobalerts-noreply@linkedin.com", "Alerte", bare_html,
                                          fixture("alert_linkedin.txt"))
    assert merged[0].title == "Alternance - Développeur Full Stack (H/F)" and len(merged) == 3


def test_alert_hellowork_with_tracking_redirect():
    jobs = alerts.jobs_from_alert_email("HelloWork <alertes@emails.hellowork.com>", "3 nouvelles offres",
                                        fixture("alert_hellowork.html"), "")
    assert [j.external_id for j in jobs] == ["79006307", "83292557", "78262233"]
    assert all(j.source == "hellowork" and j.url == hw.offer_url(j.external_id) for j in jobs)
    assert (jobs[0].company, jobs[0].location) == ("Chausséa", "Metz - 57")
    assert jobs[0].contract_type is ContractType.ALTERNANCE
    assert jobs[1].title == "Développeur Full Stack en alternance H/F" and jobs[1].company == "MEWO campus"
    assert (jobs[2].company, jobs[2].location, jobs[2].contract_type) == ("Bloomind", "Metz", ContractType.CDI)


def _gz_token(text: str) -> str:
    return base64.urlsafe_b64encode(gzip.compress(text.encode())).decode().rstrip("=")


def test_alert_indeed_links_and_encoded_tracking():
    token = _gz_token('{"u":"https://fr.indeed.com/rc/clk/dl?jk=0123456789abcdef&from=ja"}')
    html = f"""
      <p><a href="https://fr.indeed.com/rc/clk/dl?jk=a1b2c3d4e5f6a7b8&amp;from=ja&amp;qd=x">Développeur web H/F</a></p>
      <p>Acme - Metz (57)</p>
      <p><a href="https://cts.indeed.com/v3/{token}">Alternance développeur full stack</a></p><p>Beta SAS</p>
      <p><a href="https://fr.indeed.com/pagead/clk/dl?mo=r&amp;ad=-6NYlbfkN0Ck">Offre sponsorisée</a></p>
      <p><a href="https://fr.indeed.com/viewjob?vjk=a1b2c3d4e5f6a7b8">Voir l'offre</a></p>"""
    jobs = alerts.jobs_from_alert_email("Indeed <alert@indeed.com>", "Nouveaux emplois", html, "")
    assert [(j.source, j.external_id) for j in jobs] == [("indeed", "a1b2c3d4e5f6a7b8"), ("indeed", "0123456789abcdef")]
    assert jobs[0].url == "https://fr.indeed.com/viewjob?jk=a1b2c3d4e5f6a7b8" and jobs[0].company == "Acme"
    assert jobs[1].contract_type is ContractType.ALTERNANCE and jobs[1].raw["jk"] == "0123456789abcdef"


@pytest.mark.parametrize("sender,url,source,external_id,canonical", [
    ("Welcome to the Jungle <hello@welcometothejungle.com>",
     "https://www.welcometothejungle.com/fr/companies/carrefour/jobs/dev-full-stack_massy?utm_source=alert",
     "wttj", "carrefour/dev-full-stack_massy",
     "https://www.welcometothejungle.com/fr/companies/carrefour/jobs/dev-full-stack_massy"),
    ("France Travail <ne-pas-repondre@francetravail.fr>",
     "https://candidat.francetravail.fr/offres/recherche/detail/201qxyz?at_medium=email",
     "france_travail", "201QXYZ", "https://candidat.francetravail.fr/offres/recherche/detail/201QXYZ"),
    ("Pôle emploi <ne-pas-repondre@pole-emploi.fr>", "https://candidat.pole-emploi.fr/offres/recherche/detail/6927986",
     "france_travail", "6927986", "https://candidat.francetravail.fr/offres/recherche/detail/6927986"),
    ("APEC <offres@diffusion.apec.fr>",
     "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/179469236W?xtor=EPR-1",
     "apec", "179469236W", "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/179469236W"),
    ("Free-Work <alertes@free-work.com>", "https://www.free-work.com/fr/tech-it/job-mission/developpeur/dev-java-h-f",
     "free_work", "dev-java-h-f", "https://www.free-work.com/fr/tech-it/job-mission/developpeur/dev-java-h-f"),
    ("Free-Work <alertes@free-work.com>", "https://www.free-work.com/fr/tech-it/developpeur/job-mission/dev-php",
     "free_work", "dev-php", "https://www.free-work.com/fr/tech-it/job-mission/developpeur/dev-php"),
    ("La bonne alternance <nepasrepondre@apprentissage.beta.gouv.fr>",
     "https://labonnealternance.apprentissage.beta.gouv.fr/emploi/offres_emploi_partenaires/6a0647/alternant-dev",
     "lba", "6a0647", "https://labonnealternance.apprentissage.beta.gouv.fr/emploi/offres_emploi_partenaires/6a0647/offre"),
    ("La bonne alternance <nepasrepondre@apprentissage.beta.gouv.fr>",
     "https://labonnealternance.apprentissage.beta.gouv.fr/recherche?type=matcha&itemId=abc123&display=list",
     "lba", "abc123", "https://labonnealternance.apprentissage.beta.gouv.fr/emploi/matcha/abc123/offre"),
])
def test_alert_other_boards(sender, url, source, external_id, canonical):
    html = f'<div><a href="{url}">Développeur web en alternance</a><p>Acme · Metz</p></div>'
    jobs = alerts.jobs_from_alert_email(sender, "Nouvelles offres pour vous", html, "")
    assert len(jobs) == 1
    job = jobs[0]
    assert (job.source, job.external_id, job.url) == (source, external_id, canonical)
    assert (job.title, job.company, job.location) == ("Développeur web en alternance", "Acme", "Metz")


@pytest.mark.parametrize("sender,subject,expected", [
    ("jobs-noreply@linkedin.com", "Votre candidature a été envoyée à Acme", 0),
    ("messages-noreply@linkedin.com", "Nouvelles offres", 0),
    ("contact@acme.fr", "Re: Candidature développeur", 0),
    ("contact@acme.fr", "Point sur le projet", 0),
    ("recruteur.123@indeedemail.com", "Question sur votre profil", 0),  # recruiter relay, not an alert
    ("jean.dupont@example.com", "Fwd: 3 nouvelles offres pour votre alerte", 1),  # forwarded alert
    ("jobalerts-noreply@linkedin.com", "Développeur : Acme recrute", 1),
])
def test_alert_recognition(sender, subject, expected):
    html = '<a href="https://www.linkedin.com/jobs/view/developpeur-at-acme-4012345678?trk=x">Développeur</a>'
    assert len(alerts.jobs_from_alert_email(sender, subject, html, "")) == expected


def test_alert_board_recognition_helpers():
    assert alerts.recognise_board("HelloWork <noreply@mail.hellowork.com>") == "hellowork"
    assert alerts.recognise_board("x@notlinkedin.com") is None and alerts.recognise_board("") is None
    assert alerts.is_alert_email("someone@example.org", "Job alert: 5 new jobs") is True
    assert alerts.is_alert_email("alert@indeed.com", "Mot de passe oublié") is False


HW_OFFER = "https://www.hellowork.com/fr-fr/emplois/79006307.html"


@pytest.mark.parametrize("wrapped", [
    f"https://www.google.com/url?q={HW_OFFER}&sa=D",
    f"https://eur01.safelinks.protection.outlook.com/?url={quote(HW_OFFER, safe='')}&data=x",
    "https://click.example.com/?u=" + quote(quote(HW_OFFER, safe=""), safe=""),
    f"https://abc.r.eu-west-1.awstrack.me/L0/{quote(HW_OFFER, safe=':')}/1/0102abc=",
    "https://links.example.com/c/" + base64.urlsafe_b64encode(HW_OFFER.encode()).decode(),
    f"{HW_OFFER}?xtor=EPR-3&amp;utm_source=alerte",
])
def test_alert_unwraps_tracking_links(wrapped):
    hit = alerts.match_offer_url(wrapped)
    assert hit is not None and (hit.source, hit.external_id) == ("hellowork", "79006307")


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/comm/jobs/search?keywords=dev", "mailto:rh@acme.fr", "https://www.hellowork.com/fr-fr/",
    "https://fr.indeed.com/viewjob?jk=zz", "https://example.org/emplois/79006307.html", "not a url", "",
    "https://links.example.com/c/" + "A" * 40,
])
def test_alert_non_offer_urls(url):
    assert alerts.match_offer_url(url) is None


def test_alert_raw_keys_expected_by_owning_sources():
    lba = alerts.match_offer_url(f"{alerts.LBA_BASE}/emploi/offres_emploi_partenaires/6a0647/alternant-dev")
    assert lba.extra == {"source": "offres_emploi_partenaires", "id": "6a0647"}  # sources/lba.py fetch_details
    wttj = alerts.match_offer_url("https://www.welcometothejungle.com/fr/companies/acme/jobs/dev_metz")
    assert wttj.extra == {"organization_slug": "acme", "job_slug": "dev_metz"}  # sources/wttj.py _slugs
    free_work = alerts.match_offer_url("https://www.free-work.com/fr/tech-it/job-mission/developpeur/dev-php")
    assert free_work.extra == {"job_slug": "developpeur"}  # sources/free_work.py fetch_details


def test_alert_generic_lines_do_not_eat_real_words():
    html = """<table><tr><td><a href="https://www.hellowork.com/fr-fr/emplois/12345678.html">Nouveau</a>
      <a href="https://www.hellowork.com/fr-fr/emplois/12345678.html">Manager de projet web</a>
      <p>Newton Consulting · Metz</p><p>Voir l'offre</p></td></tr></table>"""
    job = alerts.jobs_from_alert_email("alertes@hellowork.com", "Alerte", html, "")[0]
    assert (job.title, job.company, job.location) == ("Manager de projet web", "Newton Consulting", "Metz")


def test_alert_linkedin_current_job_id_and_dedupe():
    html = """<a href="https://www.linkedin.com/jobs/search/?currentJobId=4012345678&amp;x=1">Dev front</a>
              <a href="https://fr.linkedin.com/jobs/view/dev-front-at-acme-4012345678">Dev front</a>"""
    jobs = alerts.jobs_from_alert_email("jobs-listings@linkedin.com", "Offres recommandées pour vous", html, "")
    assert [j.external_id for j in jobs] == ["4012345678"]


def test_alert_never_raises(monkeypatch, caplog):
    def boom(_html):
        raise RuntimeError("parser crash")

    monkeypatch.setattr(alerts, "_hits_from_html", boom)
    assert alerts.jobs_from_alert_email("jobalerts-noreply@linkedin.com", "Alerte", "<p>x</p>", "") == []
    assert "illisible" in caplog.text
    monkeypatch.undo()
    assert alerts.jobs_from_alert_email(None, None, None, None) == []  # type: ignore[arg-type]
    assert alerts.jobs_from_alert_email("jobalerts-noreply@linkedin.com", "Alerte", "<<<>>", "\x00") == []


# =========================================================================== live (opt-in: -m live)


@pytest.mark.live
def test_live_hellowork_offer_page(live):
    job = hw.HelloWorkSource().job_from_id("79006307")
    assert job is None or (job.title and job.raw.get("response_type"))


@pytest.mark.live
def test_live_linkedin_guest_search_and_detail(live):
    src = li.LinkedInSource(easy_apply_only=True)
    jobs = list(src.search(SearchQuery(keywords="développeur", location=METZ, max_results=3, max_age_days=30)))
    assert jobs and all(j.external_id.isdigit() for j in jobs)
    full = src.fetch_details(jobs[0])
    assert full.description and full.apply_method in (ApplyMethod.LINKEDIN_EASY_APPLY, ApplyMethod.EXTERNAL)
