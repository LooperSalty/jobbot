"""Tests for the extra job sources: WTTJ (Algolia), Indeed (JobSpy), Free-Work, APEC. No network by default."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.config import LocationPref
from jobbot.models import ApplyMethod, ContractType, JobStatus
from jobbot.sources import apec, free_work, jobspy_source, wttj
from jobbot.sources.base import SearchQuery, SourceUnavailable

FIX = Path(__file__).parent / "fixtures" / "sources_extra"
METZ = LocationPref(city="Metz", postal_code="57000", radius_km=40, latitude=49.1193, longitude=6.1757)
NANCY = LocationPref(city="Nancy", postal_code="54000", radius_km=25, latitude=48.6921, longitude=6.1844)
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)
ALGOLIA = f"https://csekhvms53-dsn.algolia.net/1/indexes/{wttj.JOBS_INDEX}/query"
WTTJ_API = "https://api.welcometothejungle.com/api/v1/organizations"


def load(name: str):
    text = (FIX / name).read_text(encoding="utf-8")
    return text if name.endswith(".js") else json.loads(text)


def q(**kw) -> SearchQuery:
    base = dict(keywords="développeur", location=METZ, contract_types=[ContractType.CDI], max_age_days=3650)
    base.update(kw)
    return SearchQuery(**base)


@pytest.fixture(autouse=True)
def offline_speedups(request, monkeypatch):
    """Frozen clock + no politeness delays — except for live tests, which must stay polite."""
    if request.node.get_closest_marker("live"):
        return
    for mod in (wttj, free_work, apec, jobspy_source):
        monkeypatch.setattr(mod, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    monkeypatch.setattr("jobbot.clock.now", lambda: NOW)


# ----------------------------------------------------------------------------- WTTJ


def test_wttj_body_contract_geo_and_date_filters():
    body = wttj._build_search_body(q(keywords="développeur fullstack alternance",
                                     contract_types=[ContractType.ALTERNANCE], max_age_days=30), 0, 50, now=NOW)
    assert body["query"] == "développeur fullstack"
    assert "contract_type:apprenticeship" in body["filters"]
    assert body["aroundLatLng"] == "49.1193,6.1757" and body["aroundRadius"] == 40000
    assert body["numericFilters"] == [f"published_at_timestamp>={int((NOW - timedelta(days=30)).timestamp())}"]
    remote = wttj._build_search_body(q(remote=True, location=None, contract_types=[]), 1, 10, now=NOW)
    assert "remote:fulltime" in remote["filters"] and "offices.country_code:FR" in remote["filters"]
    assert "aroundLatLng" not in remote and remote["page"] == 1
    city_only = wttj._build_search_body(q(location=LocationPref(city="Thionville")), 0, 10, now=NOW)
    assert 'offices.city:"Thionville"' in city_only["filters"]


@pytest.mark.parametrize(("value", "title", "expected"), [
    ("apprenticeship", "", ContractType.ALTERNANCE), ("full_time", "", ContractType.CDI),
    ("temporary", "", ContractType.CDD), ("internship", "", ContractType.STAGE),
    ("freelance", "", ContractType.FREELANCE), ("vie", "", ContractType.OTHER),
    ("other", "Développeur web en alternance", ContractType.ALTERNANCE), (None, "", ContractType.OTHER),
])
def test_wttj_contract_mapping(value, title, expected):
    assert wttj._map_contract(value, title) == expected


@respx.mock
def test_wttj_search_maps_hits_and_enriches_with_detail():
    respx.get(wttj.ENV_URL).mock(return_value=httpx.Response(200, text=load("wttj_env.js")))
    algolia = respx.post(ALGOLIA).mock(return_value=httpx.Response(200, json=load("wttj_search_cdi.json")))
    respx.get(url__startswith=f"{WTTJ_API}/atos/").mock(return_value=httpx.Response(200, json=load("wttj_detail.json")))
    respx.get(url__startswith=WTTJ_API).mock(return_value=httpx.Response(404, json={}))
    src = wttj.WelcomeToTheJungleSource(client=httpx.Client())
    atos, lux, cm = list(src.search(q(max_results=3)))
    req = algolia.calls[0].request
    assert req.headers["Referer"] == wttj.REFERER and req.headers["X-Algolia-Application-Id"] == "CSEKHVMS53"
    assert atos.source == "wttj" and atos.external_id == "atos/developpeur-java-f-h-strasbourg_nancy_ATOS_Zw2WrJ3"
    assert atos.url == f"{wttj.SITE_URL}/fr/companies/{atos.external_id.replace('/', '/jobs/')}"
    assert atos.apply_url == "https://atos.contactrh.com/jobs/10425/44454330" and src.available() == (True, "")
    assert atos.apply_method == ApplyMethod.EXTERNAL and atos.contract_type == ContractType.CDI
    assert (atos.postal_code, atos.location, atos.latitude) == ("54000", "Nancy (54)", 48.68956)
    assert atos.salary == "35 000 – 45 000 € / an" and atos.company == "Atos"
    assert atos.company_domain == "atos.net" and atos.status == JobStatus.NEW
    assert "Le poste en quelques mots" in atos.description and "Outils : Java" in atos.description
    assert atos.raw["ats"] == "external" and atos.published_at == datetime(2026, 9, 19, 14, 20, 13, tzinfo=UTC)
    assert lux.location == "Luxembourg, Luxembourg" and lux.remote is False and lux.apply_url is None
    assert "Missions" in lux.description and cm.company == "Crédit Mutuel Alliance Fédérale"


@respx.mock
def test_wttj_env_failure_falls_back_and_max_results_limits_pages():
    respx.get(wttj.ENV_URL).mock(return_value=httpx.Response(500))
    algolia = respx.post(ALGOLIA).mock(return_value=httpx.Response(200, json=load("wttj_search_apprenticeship.json")))
    respx.get(url__startswith=WTTJ_API).mock(side_effect=httpx.ConnectError("boom"))
    src = wttj.WelcomeToTheJungleSource(client=httpx.Client())
    jobs = list(src.search(q(contract_types=[ContractType.ALTERNANCE], max_results=1)))
    assert len(jobs) == 1 and algolia.call_count == 1
    assert jobs[0].contract_type == ContractType.ALTERNANCE and jobs[0].location == "Strasbourg"
    assert algolia.calls[0].request.headers["X-Algolia-API-Key"] == wttj.DEFAULT_API_KEY


@respx.mock
def test_wttj_errors_raise_source_unavailable_and_details_fail_soft(make_job):
    respx.get(wttj.ENV_URL).mock(return_value=httpx.Response(200, text=load("wttj_env.js")))
    respx.post(ALGOLIA).mock(return_value=httpx.Response(403, json={"message": "Method not allowed with this referer"}))
    src = wttj.WelcomeToTheJungleSource(client=httpx.Client())
    results = src.search(q())
    with pytest.raises(SourceUnavailable, match="WTTJ"):
        list(results)
    job = make_job(source="wttj", raw={"org_slug": "acme", "slug": "dev"})
    respx.get(f"{WTTJ_API}/acme/jobs/dev").mock(return_value=httpx.Response(500))
    assert src.fetch_details(job) == job
    bare = make_job(source="wttj", raw={})
    assert src.fetch_details(bare) == bare


def _alert_job(make_job, source: str, external_id: str, raw: dict):
    """Minimal offer as built by sources.alerts from a job-alert e-mail link."""
    return make_job(source=source, external_id=external_id, title="", company="", location="", postal_code=None,
                    latitude=None, longitude=None, description="", contact_email=None, raw=raw,
                    apply_method=ApplyMethod.EXTERNAL, contract_type=ContractType.OTHER)


@respx.mock
def test_wttj_fetch_details_completes_alert_offer(make_job):
    slug = "developpeur-java-f-h-strasbourg_nancy_ATOS_Zw2WrJ3"
    respx.get(f"{WTTJ_API}/atos/jobs/{slug}").mock(return_value=httpx.Response(200, json=load("wttj_detail.json")))
    job = _alert_job(make_job, "wttj", f"atos/{slug}", {"organization_slug": "atos", "job_slug": slug})
    full = wttj.WelcomeToTheJungleSource(client=httpx.Client()).fetch_details(job)
    assert (full.title, full.company, full.location) == ("Développeur Java (F/H) - Strasbourg", "Atos", "Nancy (54)")
    assert full.contract_type == ContractType.CDI and full.apply_url.startswith("https://atos.contactrh.com/")
    assert full.published_at == datetime(2026, 9, 19, 14, 20, 13, tzinfo=UTC) and len(full.description) > 400
    no_raw = _alert_job(make_job, "wttj", f"atos/{slug}", {})
    assert wttj.WelcomeToTheJungleSource(client=httpx.Client()).fetch_details(no_raw).company == "Atos"


# ----------------------------------------------------------------------------- Free-Work


@pytest.mark.parametrize(("loc", "expected"), [
    (METZ, "fr~grand-est~moselle~metz"), (NANCY, "fr~grand-est~meurthe-et-moselle~nancy"),
    (LocationPref(city="Ajaccio", postal_code="20000"), "fr~corse~corse-du-sud~ajaccio"),
    (LocationPref(city="Luxembourg", postal_code="1150"), "lu~~~"), (LocationPref(city="Metz"), None),
])
def test_free_work_location_key(loc, expected):
    assert free_work._location_key(loc) == expected


def test_free_work_params_and_contract_choice():
    query = q(keywords="développeur web CDI", contract_types=[ContractType.CDI, ContractType.ALTERNANCE])
    params = free_work._build_params(query, 1, 50, "fr~grand-est~moselle~metz", 40)
    assert params == {"page": 1, "itemsPerPage": 50, "searchKeywords": "développeur web",
                      "contracts": "apprenticeship,permanent", "locationKeys": "fr~grand-est~moselle~metz",
                      "locationRadius": 40}
    assert free_work._pick_contract(["contractor", "permanent"], ()) == ContractType.CDI
    assert free_work._pick_contract(["contractor", "fixed-term"], (ContractType.FREELANCE,)) == ContractType.FREELANCE
    assert free_work._pick_contract([], ()) == ContractType.OTHER


def _fw_members(level: str = "junior") -> dict:
    data = load("freework_search.json")
    data["hydra:member"] = [{**m, "experienceLevel": level} for m in data["hydra:member"]]
    return data


@respx.mock
def test_free_work_search_maps_members():
    route = respx.get(free_work.API_URL).mock(return_value=httpx.Response(200, json=_fw_members()))
    jobs = list(free_work.FreeWorkSource(client=httpx.Client()).search(q()))
    keys = [c.request.url.params["locationKeys"] for c in route.calls]
    assert keys == ["fr~grand-est~moselle~metz"]  # Luxembourg: only through the profile's LU area queries
    assert len(jobs) == 3
    esch, nancy = jobs[0], jobs[2]
    assert esch.source == "free_work" and esch.external_id == "developpeur-talend-data-integration-engineer-h-f"
    assert esch.url == ("https://www.free-work.com/fr/tech-it/job-mission/data-engineer/"
                        "developpeur-talend-data-integration-engineer-h-f")
    assert esch.contract_type == ContractType.CDI and esch.remote is False and esch.apply_url is None
    assert esch.location == "Esch-sur-Alzette, Luxembourg" and esch.postal_code == "4361"
    assert esch.latitude == pytest.approx(49.500956) and esch.apply_method == ApplyMethod.EXTERNAL
    assert esch.published_at == datetime(2026, 9, 21, 22, 45, 33, tzinfo=UTC)
    assert nancy.location == "Nancy (54)" and nancy.company == "Raedy" and "Flutter" in nancy.description


@respx.mock
def test_free_work_filters_senior_remote_and_alternance_skips_luxembourg():
    route = respx.get(free_work.API_URL).mock(return_value=httpx.Response(200, json=_fw_members("senior")))
    src = free_work.FreeWorkSource(client=httpx.Client())
    assert list(src.search(q(location=NANCY))) == [] and route.call_count == 1
    remote = _fw_members()
    remote["hydra:member"][0] = {**remote["hydra:member"][0], "remoteMode": "full",
                                 "applicationType": "url", "applicationUrl": "https://ats.example/apply/1"}
    route.mock(return_value=httpx.Response(200, json=remote))
    jobs = list(src.search(q(remote=True, location=None, contract_types=[ContractType.ALTERNANCE])))
    assert [j.remote for j in jobs] == [True] and jobs[0].apply_url == "https://ats.example/apply/1"
    assert "locationKeys" not in route.calls[-1].request.url.params


@respx.mock
def test_free_work_errors_and_fetch_details(make_job):
    respx.get(free_work.API_URL).mock(return_value=httpx.Response(503))
    src = free_work.FreeWorkSource(client=httpx.Client())
    results = src.search(q())
    with pytest.raises(SourceUnavailable, match="Free-Work"):
        list(results)
    detail = {**load("freework_detail.json"), "applicationType": "url", "applicationUrl": "https://x.example/a"}
    respx.get(f"{free_work.API_URL}/abc").mock(return_value=httpx.Response(200, json=detail))
    job = make_job(source="free_work", external_id="abc", description="court", raw={"slug": "abc", "job_slug": "d"})
    enriched = src.fetch_details(job)
    assert enriched.apply_url == "https://x.example/a" and len(enriched.description) > 1000
    assert enriched.id == job.id and enriched.found_at == job.found_at
    respx.get(f"{free_work.API_URL}/zzz").mock(return_value=httpx.Response(404))
    missing = make_job(source="free_work", raw={"slug": "zzz"})
    assert src.fetch_details(missing) == missing
    slug = "developpeur-talend-data-integration-engineer-h-f"
    respx.get(f"{free_work.API_URL}/{slug}").mock(return_value=httpx.Response(200, json=load("freework_detail.json")))
    alert = _alert_job(make_job, "free_work", slug, {"job_slug": "data-engineer"})
    full = src.fetch_details(alert)
    assert (full.title, full.company) == ("Développeur Talend / Data Integration Engineer H/F", "CONSORT GROUP")
    assert full.contract_type == ContractType.CDI and full.postal_code == "4361" and full.published_at


# ----------------------------------------------------------------------------- APEC


def test_apec_body_contracts_and_geo():
    body = apec._build_body(q(keywords="développeur CDI"), ["582374"], (49.1193, 6.1757, 40), 20, 20)
    assert body["motsCles"] == "développeur" and body["typesContrat"] == ["101888"]
    assert body["lieux"] == ["582374"] and body["pagination"] == {"range": 20, "startIndex": 20}
    assert body["pointGeolocDeReference"] == {"distance": 40, "latitude": 49.1193, "longitude": 6.1757}
    alt = apec._build_body(q(contract_types=[ContractType.ALTERNANCE]), ["57"], None, 0, 20)
    assert alt["typesContrat"] == ["20053", "597137", "597139", "597138", "597140"]
    assert alt["pointGeolocDeReference"] == {"distance": 0}


@respx.mock
def test_apec_search_maps_results_and_filters_non_it():
    lieu = respx.get(apec.LIEU_URL).mock(return_value=httpx.Response(200, json=load("apec_lieu.json")))
    search = respx.post(apec.SEARCH_URL).mock(return_value=httpx.Response(200, json=load("apec_search.json")))
    src = apec.ApecSource(client=httpx.Client())
    jobs = list(src.search(q()))
    sent = json.loads(search.calls[0].request.content)
    assert sent["lieux"] == ["582374"] and sent["pointGeolocDeReference"]["distance"] == 40
    titles = [j.title for j in jobs]
    assert "Développeur Foncier Est F/H" not in titles and "Responsable commercial F/H" not in titles
    java = next(j for j in jobs if j.external_id == "179429390W")
    assert java.url == "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/179429390W"
    assert java.location == "Metz (57)" and java.company == "GESER BEST" and java.contract_type == ContractType.CDI
    assert java.salary == "35 - 43 k€ brut annuel" and java.latitude == pytest.approx(49.1082122)
    assert java.published_at == datetime(2026, 9, 17, 13, 26, 47, tzinfo=UTC)
    assert java.apply_method == ApplyMethod.EXTERNAL and java.raw["description_truncated"] is True
    list(src.search(q()))
    assert lieu.call_count == 1  # place id cached


@respx.mock
def test_apec_datadome_block_remote_skip_and_fetch_details(make_job):
    respx.get(apec.LIEU_URL).mock(return_value=httpx.Response(500))
    search = respx.post(apec.SEARCH_URL).mock(return_value=httpx.Response(
        403, json={"url": "https://geo.captcha-delivery.com/captcha"}, headers={"X-DataDome": "protected"}))
    src = apec.ApecSource(client=httpx.Client())
    assert list(src.search(q(remote=True, location=None))) == [] and search.call_count == 0
    results = src.search(q())
    with pytest.raises(SourceUnavailable, match="DataDome"):
        list(results)
    assert json.loads(search.calls[0].request.content)["lieux"] == ["57"]  # fallback on the department
    assert src.available()[0] is False and "APEC" in src.available()[1]
    job = make_job(source="apec")
    assert src.fetch_details(job) == job


# ----------------------------------------------------------------------------- JobSpy / Indeed


@pytest.mark.parametrize(("title", "desc", "job_type", "expected"), [
    ("STAGE - Développeur F/H", "", None, ContractType.STAGE),
    ("Alternant développeur web", "", None, ContractType.ALTERNANCE),
    ("Développeur Java", "Poste en CDI à Metz", None, ContractType.CDI),
    ("Développeur", "Contrat d'apprentissage de 24 mois", None, ContractType.ALTERNANCE),
    ("Développeur IA", "apprentissage automatique et CDD de 6 mois", None, ContractType.CDD),
    ("Développeur", "", "internship", ContractType.STAGE), ("Développeur", "", "fulltime", ContractType.OTHER),
])
def test_jobspy_contract_detection(title, desc, job_type, expected):
    assert jobspy_source._detect_contract(title, desc, job_type) == expected


def test_jobspy_kwargs():
    kw = jobspy_source._build_scrape_kwargs(q(contract_types=[ContractType.ALTERNANCE], max_age_days=7), "france")
    assert kw["search_term"] == "développeur alternance" and kw["location"] == "Metz"
    assert kw["distance"] == 25 and kw["hours_old"] == 168 and kw["site_name"] == ["indeed"]
    assert kw["country_indeed"] == "france" and "is_remote" not in kw
    remote = jobspy_source._build_scrape_kwargs(q(remote=True, location=None), "france")
    assert remote["is_remote"] is True and "hours_old" not in remote and remote["location"] == "France"
    assert jobspy_source._build_scrape_kwargs(q(remote=True), "luxembourg") is None
    lux = jobspy_source._build_scrape_kwargs(q(location=NANCY), "luxembourg")
    assert lux["location"] == "Luxembourg" and lux["country_indeed"] == "luxembourg"


def test_jobspy_availability(monkeypatch, tmp_path):
    monkeypatch.setattr(jobspy_source.importlib.util, "find_spec", lambda name: None)
    monkeypatch.delenv("JOBSPY_PYTHON", raising=False)
    monkeypatch.setattr(jobspy_source, "_default_python", lambda: sys.executable)
    src = jobspy_source.JobSpySource()
    ok, reason = src.available()
    assert ok is False and "python-jobspy" in reason and src.name == "indeed"
    results = src.search(q())
    with pytest.raises(SourceUnavailable):
        list(results)
    fake_py = tmp_path / "python.exe"  # dedicated interpreter (JOBSPY_PYTHON) checked with "import jobspy"
    fake_py.write_text("")
    monkeypatch.setenv("JOBSPY_PYTHON", str(fake_py))
    calls = []
    monkeypatch.setattr(jobspy_source.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or type("P", (), {"returncode": 0})())
    assert jobspy_source.JobSpySource().available() == (True, "") and calls[0][0] == str(fake_py)


@pytest.fixture
def jobspy_ready(monkeypatch):
    monkeypatch.setattr(jobspy_source.JobSpySource, "available", lambda self: (True, ""))


def test_jobspy_search_maps_rows(monkeypatch, jobspy_ready):
    rows = load("jobspy_indeed.json")
    old = {**rows[1], "id": "in-old", "date_posted": "2019-04-18T00:00:00.000"}
    seen_kwargs = []
    monkeypatch.setattr(jobspy_source, "_run_worker",
                        lambda py, kw, timeout_s: seen_kwargs.append(kw) or [*rows, rows[0], old])
    src = jobspy_source.JobSpySource(countries=("france", "luxembourg"))
    jobs = list(src.search(q(max_age_days=30, max_results=10)))
    assert [j.external_id for j in jobs] == ["4b736c815495879f", "47219f4b33002a09", "ab47b78341556b21"]
    stage = jobs[0]
    assert stage.source == "indeed" and stage.contract_type == ContractType.STAGE
    assert stage.url == "https://fr.indeed.com/viewjob?jk=4b736c815495879f"
    assert stage.apply_url.startswith("https://www.handicap-job.com/") and stage.apply_method == ApplyMethod.EXTERNAL
    assert stage.location == "Vantoux" and stage.company_domain == "groupe-talentshandicap.com"
    assert stage.published_at == datetime(2026, 9, 20, tzinfo=UTC) and stage.remote is False
    assert len(seen_kwargs) == 2 and seen_kwargs[1]["country_indeed"] == "luxembourg"
    list(src.search(q(location=NANCY, max_age_days=30, max_results=10)))
    assert len(seen_kwargs) == 3  # the Luxembourg search is location-independent → not repeated


def test_jobspy_failures(monkeypatch, jobspy_ready):
    def boom(py, kw, timeout_s):
        raise jobspy_source.JobSpyError("délai dépassé")

    monkeypatch.setattr(jobspy_source, "_run_worker", boom)
    results = jobspy_source.JobSpySource().search(q())
    with pytest.raises(SourceUnavailable, match="délai dépassé"):
        list(results)
    rows = [{"id": None, "title": None}, {"id": "in-1", "title": "Dev", "job_url": "https://fr.indeed.com/viewjob?jk=1",
                                         "salary_source": None, "min_amount": 30000, "max_amount": 35000,
                                         "currency": "EUR", "interval": "yearly", "emails": "rh@acme.fr"}]
    monkeypatch.setattr(jobspy_source, "_run_worker", lambda py, kw, timeout_s: rows)
    jobs = list(jobspy_source.JobSpySource().search(q()))
    assert len(jobs) == 1 and jobs[0].salary == "30 000 – 35 000 € / an" and jobs[0].contact_email == "rh@acme.fr"


def test_jobspy_worker_subprocess(monkeypatch):
    """The real child-process path, with fake jobspy packages from the fixtures (no network)."""
    monkeypatch.setenv("PYTHONPATH", str(FIX / "fake_jobspy_ok"))
    rows = jobspy_source._run_worker(sys.executable, {"search_term": "dév"}, 60)
    assert rows == [{"id": "in-x", "title": "dév"}]
    failing = {"search_term": "boom"}
    with pytest.raises(jobspy_source.JobSpyError, match="cassé"):
        jobspy_source._run_worker(sys.executable, failing, 60)
    monkeypatch.setenv("PYTHONPATH", str(FIX / "fake_jobspy_slow"))
    with pytest.raises(jobspy_source.JobSpyError, match="délai"):
        jobspy_source._run_worker(sys.executable, {}, 2)


# ----------------------------------------------------------------------------- live (opt-in: -m live)


@pytest.mark.live
@pytest.mark.parametrize(("factory", "query", "url_prefix"), [
    (wttj.WelcomeToTheJungleSource, q(keywords="developpeur", max_results=1, max_age_days=60),
     "https://www.welcometothejungle.com/fr/companies/"),
    (free_work.FreeWorkSource, q(location=NANCY, max_results=3, max_age_days=365),
     "https://www.free-work.com/fr/tech-it/job-mission/"),
    (apec.ApecSource, q(max_results=3, max_age_days=60), "https://www.apec.fr/candidat/"),
    (jobspy_source.JobSpySource, q(max_results=3, max_age_days=30), "https://fr.indeed.com/viewjob?jk="),
], ids=["wttj", "free_work", "apec", "indeed"])
def test_live_search(request, factory, query, url_prefix):
    if "live" not in (expr := request.config.getoption("markexpr") or "") or "not live" in expr:
        pytest.skip("test réseau : lancer explicitement avec -m live")  # conftest does not skip them by default
    src = factory()
    ok, reason = src.available()
    if not ok:
        pytest.skip(reason)
    jobs = list(src.search(query))
    assert all(j.url.startswith(url_prefix) and j.title and j.apply_method == ApplyMethod.EXTERNAL for j in jobs)
