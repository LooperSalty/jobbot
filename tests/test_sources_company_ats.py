"""Company career boards (source ``company_ats``): ATS detection, provider mapping, board discovery in the
database, kv refresh / cap, zone filtering, details. Offline (respx), fictitious employers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.config import LocationPref
from jobbot.models import ApplyMethod, ContractType, JobStatus
from jobbot.sources import ats_fetch, ats_mapping, ats_reader, ats_targets, public_page
from jobbot.sources.ats_detect import AtsBoard, ats_board, board_url, detect_ats
from jobbot.sources.base import SearchQuery
from jobbot.sources.company_ats import CompanyATSSource

FIX = Path(__file__).parent / "fixtures" / "sources_new"
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
METZ = LocationPref(city="Metz", postal_code="57000", radius_km=40, latitude=49.1193, longitude=6.1757)
NANCY = LocationPref(city="Nancy", postal_code="54000", radius_km=25, latitude=48.6921, longitude=6.1844)
LUX = LocationPref(city="Luxembourg", postal_code="1111", radius_km=25, country="LU",
                   contract_types=[ContractType.CDI])
LEVER = "https://api.lever.co/v0/postings/acme-logiciels"
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/lorrainedigital/jobs"
RECRUITEE = "https://nancy-web-studio.recruitee.com/api/offers/"
SMARTR = "https://api.smartrecruiters.com/v1/companies/MetzServicesGroupe/postings"
WORKABLE = "https://apply.workable.com/api/v3/accounts/flutter-metz/jobs"
TEAMTAILOR = "https://moselle-numerique.teamtailor.com/jobs.rss"
CAREERS = "https://www.thionville-soft.example/carrieres"


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def load(name: str):
    return json.loads(text(name))


def q(**kw) -> SearchQuery:
    base = dict(keywords="développeur", location=METZ, contract_types=[], max_age_days=30)
    base.update(kw)
    return SearchQuery(**base)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ats_fetch, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    monkeypatch.setattr("jobbot.clock.now", lambda: NOW)
    monkeypatch.setattr(public_page, "resolves_public", lambda host: True)  # fictitious .example hosts


def fetcher(countries=frozenset({"FR", "LU"})) -> ats_fetch.AtsFetcher:
    return ats_fetch.AtsFetcher(httpx.Client(), countries=countries)


# ----------------------------------------------------------------------------- detection


@pytest.mark.parametrize(("url", "expected"), [
    ("https://jobs.lever.co/acme/3f2a9c10-0b1e", ("lever", "acme")),
    ("https://jobs.eu.lever.co/acme/", ("lever", "acme")),
    ("https://api.lever.co/v0/postings/acme?mode=json", ("lever", "acme")),
    ("https://boards.greenhouse.io/acme/jobs/4400001", ("greenhouse", "acme")),
    ("https://job-boards.greenhouse.io/embed/job_app?for=acme&token=1", ("greenhouse", "acme")),
    ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", ("greenhouse", "acme")),
    ("https://acme.recruitee.com/o/developpeur", ("recruitee", "acme")),
    ("https://jobs.smartrecruiters.com/AcmeGroupe/744000100000001-dev", ("smartrecruiters", "AcmeGroupe")),
    ("https://careers.smartrecruiters.com/AcmeGroupe", ("smartrecruiters", "AcmeGroupe")),
    ("https://www.smartr.me/oneclick-ui/company/AcmeGroupe/publication/0b1c", ("smartrecruiters", "AcmeGroupe")),
    ("https://apply.workable.com/acme/j/A1B2C3D4E5/", ("workable", "acme")),
    ("https://apply.workable.com/api/v3/accounts/acme/jobs", ("workable", "acme")),
    ("https://acme.workable.com/", ("workable", "acme")),
    ("https://acme.teamtailor.com/jobs/8400001-dev", ("teamtailor", "acme")),
    ("https://www.welcometothejungle.com/fr/companies/acme/jobs/dev_METZ", ("wttj", "acme")),
    ("https://www.acme.example/carrieres/?gh_jid=4400001", None),
    ("https://www.teamtailor.com/fr/", None),
    ("https://www.greenhouse.io/fr/customers", None),
    ("https://apply.workable.com/j/A1B2C3D4E5", None),
    ("https://jobs.lever.co/acme%0A", None),
    ("https://jobs.lever.co/ac%20me", None),
    ("mailto:rh@acme.example", None),
    ("", None),
    (None, None),
])
def test_detect_ats(url, expected):
    assert detect_ats(url) == expected


def test_board_keys_and_urls():
    eu = ats_board("https://jobs.eu.lever.co/acme")
    assert eu == AtsBoard("lever", "acme", "eu") and eu.key == "lever:eu/acme"
    assert board_url(eu) == "https://jobs.eu.lever.co/acme"
    assert board_url(AtsBoard("teamtailor", "acme")) == "https://acme.teamtailor.com/jobs"


# ----------------------------------------------------------------------------- providers


@respx.mock
def test_lever_greenhouse_recruitee_mapping():
    respx.get(LEVER).mock(return_value=httpx.Response(200, json=load("lever_postings.json")))
    respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, json=load("greenhouse_jobs.json")))
    respx.get(RECRUITEE).mock(return_value=httpx.Response(200, json=load("recruitee_offers.json")))
    f = fetcher()
    lever = f.fetch_board(AtsBoard("lever", "acme-logiciels"), "Acme Logiciels")
    alt = lever[0]
    assert alt.source == "company_ats"
    assert alt.external_id == "lever:acme-logiciels:3f2a9c10-0b1e-4c7d-9a55-5d1f2e3a4b01"
    assert alt.company == "Acme Logiciels" and alt.contract_type == ContractType.ALTERNANCE and alt.location == "Metz"
    assert alt.apply_url.endswith("/apply") and alt.apply_method == ApplyMethod.EXTERNAL
    assert "Tes missions" in alt.description and "React / Node.js" in alt.description
    assert alt.published_at == datetime(2025, 9, 22, 6, tzinfo=UTC) and alt.raw["country_code"] == "FR"
    assert lever[1].remote is True and lever[1].contract_type == ContractType.CDI
    gh = f.fetch_board(AtsBoard("greenhouse", "lorrainedigital"))
    lux, nancy, new_york = gh
    assert lux.company == "Lorraine Digital" and lux.location == "Luxembourg, LU" and lux.raw["country_code"] == "LU"
    assert lux.contract_type == ContractType.CDI and "Spring Boot" in lux.description and "&lt;" not in lux.description
    assert nancy.contract_type == ContractType.ALTERNANCE and nancy.raw["country_code"] == "FR"
    assert new_york.raw["country_code"] == "NY"  # abroad: never matches a French / Luxembourg query
    stage, backend = f.fetch_board(AtsBoard("recruitee", "nancy-web-studio"))
    assert stage.contract_type == ContractType.STAGE and stage.location == "Nancy (54)" and stage.postal_code == "54000"
    assert stage.apply_url.endswith("/c/new") and stage.remote is None and "Storybook" in stage.description
    assert backend.remote is True and backend.contract_type == ContractType.CDI
    assert backend.salary == "32 000 – 38 000 € / an"


@respx.mock
def test_smartrecruiters_workable_teamtailor_mapping():
    sr = respx.get(url__startswith=SMARTR).mock(
        return_value=httpx.Response(200, json=load("smartrecruiters_postings.json")))
    wk = respx.post(WORKABLE).mock(side_effect=[httpx.Response(200, json=load("workable_jobs_p1.json")),
                                                httpx.Response(200, json=load("workable_jobs_p2.json"))])
    respx.get(TEAMTAILOR).mock(return_value=httpx.Response(200, text=text("teamtailor_jobs.rss")))
    f = fetcher(frozenset({"FR"}))
    (java,) = f.fetch_board(AtsBoard("smartrecruiters", "MetzServicesGroupe"))  # director posting dropped
    assert sr.calls[0].request.url.params["country"] == "fr" and java.raw["needs_details"] is True
    assert java.contract_type == ContractType.ALTERNANCE and (java.latitude, java.longitude) == (49.1193, 6.1757)
    assert java.url == "https://jobs.smartrecruiters.com/MetzServicesGroupe/744000100000001"
    flutter, go = f.fetch_board(AtsBoard("workable", "flutter-metz"), "Flutter Metz")
    assert json.loads(wk.calls[1].request.content)["token"] == "eyJvZmZzZXQiOjF9"
    assert flutter.remote is True and flutter.url == "https://apply.workable.com/flutter-metz/j/A1B2C3D4E5/"
    assert flutter.apply_url.endswith("/A1B2C3D4E5/apply/") and flutter.company == "Flutter Metz"
    assert go.contract_type == ContractType.CDD and go.location == "Thionville"
    fullstack, sales = f.fetch_board(AtsBoard("teamtailor", "moselle-numerique"))
    assert fullstack.company == "MOSELLE NUMERIQUE" and fullstack.location == "Metz (57)"
    assert fullstack.contract_type == ContractType.ALTERNANCE and fullstack.remote is None
    assert fullstack.external_id.endswith(":8400001") and sales.remote is False
    assert fullstack.published_at == datetime(2026, 9, 17, 14, 6, 1, tzinfo=UTC)


# ----------------------------------------------------------------------------- source over the database


def _routes():
    respx.get(LEVER).mock(return_value=httpx.Response(200, json=load("lever_postings.json")))
    respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, json=load("greenhouse_jobs.json")))
    respx.get(url__startswith=SMARTR).mock(return_value=httpx.Response(200, json=load("smartrecruiters_postings.json")))
    respx.get(CAREERS).mock(return_value=httpx.Response(200, text=text("careers_page.html")))
    respx.get("https://thionville-soft.teamtailor.com/jobs.rss").mock(return_value=httpx.Response(404))


@pytest.fixture
def seeded(repo, make_company, make_job):
    repo.upsert_company(make_company(siren="100000001", name="Acme Logiciels",
                                     careers_url="https://jobs.lever.co/acme-logiciels"))
    repo.upsert_company(make_company(siren="100000002", name="Thionville Soft", careers_url=CAREERS))
    repo.upsert_company(make_company(siren="100000003", name="Lorraine Digital", careers_url=None,
                                     raw={"ats_url": "https://boards.greenhouse.io/lorrainedigital"}))
    repo.upsert_company(make_company(siren="100000004", name="Spam SA", status="blacklisted",
                                     careers_url="https://jobs.lever.co/spam"))
    repo.upsert_company(make_company(siren="100000005", name="Sans Page", careers_url=None, website=None))
    repo.upsert_job(make_job(source="wttj", external_id="org/dev", company="Metz Services Groupe",
                             apply_url="https://jobs.smartrecruiters.com/MetzServicesGroupe/744000100000009-dev"))
    return repo


def source(repo, **kw) -> CompanyATSSource:
    return CompanyATSSource(repo, max_boards=kw.pop("max_boards", 80), client=httpx.Client(),
                            geocoder=kw.pop("geocoder", lambda place: None), wttj_source=object(), **kw)


def test_discover_targets(seeded):
    targets = ats_targets.discover_targets(seeded)
    keys = {t.key: t for t in targets}
    assert set(keys) == {"lever:acme-logiciels", "page:thionville-soft.example/carrieres",
                         "greenhouse:lorrainedigital", "smartrecruiters:MetzServicesGroupe"}
    assert keys["smartrecruiters:MetzServicesGroupe"].origin == "job"
    assert keys["page:thionville-soft.example/carrieres"].board is None
    for url in ("http://127.0.0.1:8501/", "http://localhost/jobs", "https://[::1]/jobs", "http://intranet/jobs",
                "https://www.linkedin.com/company/acme/jobs", "ftp://acme.example/jobs", None):
        assert not ats_targets.is_public_page(url)  # never an internal address nor a job board
    assert ats_targets.is_public_page("https://www.acme.example/carrieres")


@respx.mock
def test_search_filters_zone_dev_and_contract(seeded, profile):
    _routes()
    src = source(seeded, profile=profile)  # profile areas: Metz, Nancy, Luxembourg (CDI)
    metz = list(src.search(q()))
    titles = {j.title for j in metz}
    assert titles == {"Développeur Full-Stack (alternance)", "Développeur Java (F/H/X)",
                      "Développeur C# / .NET junior"}  # Thionville is 27 km from Metz; senior / sales dropped
    assert list(src.search(q())) == []  # each offer is given once per run (the registry stores it once)
    assert {j.title for j in src.search(q(location=NANCY))} == {"Développeur web"}
    assert {j.title for j in src.search(q(location=LUX))} == {"Software Engineer (Java)"}
    assert {j.title for j in src.search(q(location=None, remote=True))} == {"Développeur Python"}
    assert _stamped(seeded) == {"lever:acme-logiciels", "page:thionville-soft.example/carrieres",
                                "teamtailor:thionville-soft", "greenhouse:lorrainedigital",
                                "smartrecruiters:MetzServicesGroupe"}  # a 404 board is remembered too
    assert list(source(seeded).search(q(location=LUX))) == []  # boards read < 20 h ago are skipped

    def fresh(**kw) -> CompanyATSSource:  # boards read again (refresh_hours=0)
        return source(seeded, refresh_hours=0, **kw)

    assert list(fresh(profile=profile).search(q(contract_types=[ContractType.STAGE]))) == []
    cdi = {j.title for j in fresh(profile=profile).search(q(contract_types=[ContractType.CDI]))}
    assert cdi == {"Développeur C# / .NET junior"}
    capped = fresh(profile=profile)
    assert len(list(capped.search(q(max_results=1)))) == 1 and len(list(capped.search(q()))) == 2
    france_only = fresh()  # no profile: France only
    assert list(france_only.search(q(location=LUX))) == [] and len(list(france_only.search(q()))) == 3


@respx.mock
def test_search_area_without_coordinates_is_geocoded(seeded, profile):
    _routes()
    hayange = LocationPref(city="Hayange-sur-Exemple", postal_code="57700", radius_km=40)
    places = []
    src = source(seeded, profile=profile, geocoder=lambda place: places.append(place) or (49.1193, 6.1757))
    assert len(list(src.search(q(location=hayange)))) == 3  # the area got Metz-like coordinates
    assert places[0] == "57700 Hayange-sur-Exemple"


BOARD_KEYS = ("lever:acme-logiciels", "page:thionville-soft.example/carrieres", "teamtailor:thionville-soft",
              "greenhouse:lorrainedigital", "smartrecruiters:MetzServicesGroupe", "lever:spam")


def _stamped(repo) -> set[str]:
    return {key for key in BOARD_KEYS if ats_targets.last_fetch(repo, key) == NOW}


@respx.mock
def test_refresh_window_cap_and_errors(seeded, monkeypatch):
    lever = respx.get(LEVER).mock(return_value=httpx.Response(500))
    gh = respx.get(GREENHOUSE).mock(return_value=httpx.Response(404, json={"status": 404, "error": "Job not found"}))
    respx.get(url__startswith=SMARTR).mock(side_effect=httpx.ConnectError("réseau"))
    respx.get(CAREERS).mock(return_value=httpx.Response(200, text="<html><body>Rien</body></html>"))
    assert list(source(seeded).search(q())) == []
    assert lever.call_count == 2 and gh.call_count == 1  # 500 retried once, 404 is final
    assert _stamped(seeded) == set(BOARD_KEYS[:5]) - {"teamtailor:thionville-soft"}  # every attempt is dated
    assert ats_targets.failures(seeded, "lever:acme-logiciels") == 1
    assert ats_targets.failures(seeded, "greenhouse:lorrainedigital") == 0
    targets = ats_targets.discover_targets(seeded)
    assert ats_targets.select_targets(seeded, targets, limit=80, refresh_hours=20) == []
    day = ats_targets.select_targets(seeded, targets, limit=80, refresh_hours=20, now=NOW + timedelta(hours=21))
    assert [t.key for t in day] == ["page:thionville-soft.example/carrieres", "greenhouse:lorrainedigital"]
    later = NOW + timedelta(hours=41)  # failing boards wait twice as long
    assert len(ats_targets.select_targets(seeded, targets, limit=80, refresh_hours=20, now=later)) == 4
    capped = ats_targets.select_targets(seeded, targets, limit=1, refresh_hours=20, now=later)
    assert [t.key for t in capped] == ["lever:acme-logiciels"]  # oldest attempt, then discovery order
    assert ats_targets.select_targets(seeded, targets, limit=0, refresh_hours=20, now=later) == []
    monkeypatch.setattr("jobbot.clock.now", lambda: later)
    respx.get(LEVER).mock(return_value=httpx.Response(200, json=load("lever_postings.json")))
    one_board = source(seeded, max_boards=1)
    assert {j.title for j in one_board.search(q())} == {"Développeur Full-Stack (alternance)"}
    assert ats_targets.last_fetch(seeded, "lever:acme-logiciels") == later
    assert ats_targets.failures(seeded, "lever:acme-logiciels") == 0  # a success resets the back-off


def test_select_keeps_half_of_the_cap_for_rereads(repo):
    targets = [ats_targets.BoardTarget(AtsBoard("lever", f"b{i}"), None, f"E{i}", "company") for i in range(6)]
    for target in targets[3:]:
        ats_targets.mark_fetched(repo, target.key, NOW - timedelta(days=2))
    chosen = ats_targets.select_targets(repo, targets, limit=4, refresh_hours=20)
    assert [t.key for t in chosen] == ["lever:b0", "lever:b1", "lever:b3", "lever:b4"]
    assert ats_targets.mark_failed(repo, "lever:b0") == 1 and ats_targets.mark_failed(repo, "lever:b0") == 2
    assert ats_targets.refresh_delay(repo, "lever:b0", 20) == timedelta(hours=80)


@respx.mock
def test_time_budget_and_database_errors_keep_what_was_read(seeded, monkeypatch):
    _routes()
    reader = ats_reader.BoardReader(seeded, lambda: fetcher(), refresh_hours=20, time_budget_s=-1)
    assert list(reader.read_all(ats_targets.discover_targets(seeded), 30)) == []  # budget spent at once
    monkeypatch.setattr(seeded, "kv_set", lambda key, value: (_ for _ in ()).throw(RuntimeError("database is locked")))
    src = source(seeded)
    assert len(list(src.search(q()))) == 3  # offers kept although nothing could be recorded


def test_source_without_repository_is_available_and_empty():
    src = CompanyATSSource()
    assert src.available() == (True, "") and src.name == "company_ats"
    assert list(src.search(q())) == []


# ----------------------------------------------------------------------------- details


@respx.mock
def test_fetch_details_smartrecruiters_workable_jsonld_and_expired(seeded):
    detail_url = f"{SMARTR}/744000100000001"
    respx.get(detail_url).mock(return_value=httpx.Response(200, json=load("smartrecruiters_detail.json")))
    respx.get("https://apply.workable.com/api/v2/accounts/flutter-metz/jobs/A1B2C3D4E5").mock(
        return_value=httpx.Response(200, json=load("workable_detail.json")))
    respx.get("https://apply.workable.com/api/v2/accounts/flutter-metz/jobs/GONE").mock(
        return_value=httpx.Response(404))
    respx.get("https://www.thionville-soft.example/carrieres/developpeur-dotnet").mock(
        return_value=httpx.Response(200, text=text("careers_page.html")))
    src = source(seeded)
    listing = load("smartrecruiters_postings.json")["content"][0]
    java = ats_mapping.to_offer(AtsBoard("smartrecruiters", "MetzServicesGroupe"),
                                ats_mapping.smartrecruiters_posting(listing, "MetzServicesGroupe"), needs_details=True)
    done = src.fetch_details(java)
    assert "Spring Boot" in done.description and "Rythme" in done.description
    assert done.apply_url.endswith("?oga=true") and done.url.endswith("-developpeur-java-f-h-x-")
    assert done.raw["needs_details"] is False and done.raw["language"] == "fr" and done.status == JobStatus.NEW
    flutter = ats_mapping.to_offer(AtsBoard("workable", "flutter-metz"), ats_mapping.workable_posting(
        load("workable_jobs_p1.json")["results"][0], "flutter-metz"), needs_details=True)
    assert "Flutter (Dart)" in src.fetch_details(flutter).description
    gone = flutter.model_copy(update={"raw": {**flutter.raw, "posting_id": "GONE"}})
    assert src.fetch_details(gone).status == JobStatus.EXPIRED
    page_job = ats_mapping.to_offer(AtsBoard("jsonld", "x"), ats_mapping.Posting(
        posting_id="p1", title="Développeur C# / .NET junior",
        url="https://www.thionville-soft.example/carrieres/developpeur-dotnet"))
    assert "SQL Server" in src.fetch_details(page_job).description
    lever_job = ats_mapping.to_offer(AtsBoard("lever", "acme"),
                                     ats_mapping.Posting(posting_id="1", title="Dev", url="u"))
    assert src.fetch_details(lever_job) is lever_job  # full text already in the listing


# ----------------------------------------------------------------------------- live (opt-in: -m live)


@pytest.mark.live
@pytest.mark.parametrize("provider", ["lever", "greenhouse", "recruitee", "smartrecruiters", "workable"])
def test_live_unknown_board_is_handled(request, provider):
    """One polite GET per provider on a board that cannot exist: 404 → BoardNotFound, or an empty list."""
    if "live" not in (expr := request.config.getoption("markexpr") or "") or "not live" in expr:
        pytest.skip("test réseau : lancer explicitement avec -m live")
    f = ats_fetch.AtsFetcher(httpx.Client(timeout=20), countries=frozenset({"FR"}))
    try:
        assert f.fetch_board(AtsBoard(provider, "jobbot-tableau-inexistant-4242")) == []
    except ats_fetch.BoardNotFound:
        pass
