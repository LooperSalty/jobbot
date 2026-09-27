"""Tests for sources/france_travail.py (+ _mapping) and sources/registry.py (no network).

La Bonne Alternance tests: tests/test_sources_api_lba.py.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.config import LocationPref, Secrets
from jobbot.models import ApplyMethod, ContractType, JobStatus
from jobbot.sources import france_travail as ft
from jobbot.sources import registry
from jobbot.sources.base import SearchQuery, SourceUnavailable
from jobbot.textutil import stable_id

FIX = Path(__file__).parent / "fixtures" / "sources_api"
METZ = LocationPref(city="Metz", postal_code="57000", radius_km=40, latitude=49.1193, longitude=6.1757)
TOKEN = {"scope": ft.SCOPE, "expires_in": 1499, "token_type": "Bearer", "access_token": "tok-1"}


def fx(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def secrets(**kw) -> Secrets:
    base = {"france_travail_client_id": None, "france_travail_client_secret": None, "lba_api_token": None}
    return Secrets(_env_file=None, **{**base, **kw})  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("jobbot.http.time.sleep", lambda _s: None)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def ft_source() -> ft.FranceTravailSource:
    return ft.FranceTravailSource(secrets(france_travail_client_id="PAR_x", france_travail_client_secret="s3cret"))


def q(**kw) -> SearchQuery:
    return SearchQuery(**{"keywords": "développeur full-stack", "max_results": 50, **kw})


# --------------------------------------------------------------------------- France Travail helpers


@pytest.mark.parametrize(("text", "expected"), [
    ("Télétravail partiel possible", True), ("full remote", True), ("Poste hybride à Metz", True),
    ("Pas de télétravail", False), ("100 % sur site", False), ("Poste à Metz", None), ("", None),
])
def test_detect_remote(text, expected):
    assert ft.detect_remote(text) is expected


def test_query_helpers():
    assert ft.keywords_param("Développeur d'applications (H/F) en alternance C# .NET") == "developpeur,applications,c#,.net"
    assert ft.keywords_param("a b c d e f g h i j") == ""
    assert len(ft.keywords_param("un deux trois quatre cinq six sept huit neuf").split(",")) == 7
    assert [ft.published_since(d) for d in (0, 2, 7, 30, 90)] == [1, 3, 7, 31, 31]
    assert ft.parse_content_range("offres 0-149/320") == (0, 149, 320)
    assert ft.parse_content_range(None) is None
    assert [ft.department_from_postal(p) for p in ("57000", "20100", "20200", "97400", "", None)] == [
        "57", "2A", "2B", "974", None, None]
    assert ft.domain_of("www.acme.fr") == "acme.fr" and ft.domain_of("https://candidat.francetravail.fr/x") is None
    assert ft.usable_email("Pour postuler : https://candidat.francetravail.fr/offres/1") is None
    assert ft.pick_commune([{"nom": "Montigny", "code": "1", "population": 5}, {"nom": "Metz", "code": "57463"}], "Metz") == "57463"
    assert ft.pick_commune([{"nom": "A", "code": "1", "population": 5}, {"nom": "B", "code": "2", "population": 9}], "X") == "2"
    assert ft.contract_filters([ContractType.STAGE]) == []


def test_ft_available_requires_credentials():
    ok, reason = ft.FranceTravailSource(secrets()).available()
    assert not ok and "FRANCE_TRAVAIL_CLIENT_ID" in reason
    with pytest.raises(SourceUnavailable):
        list(ft.FranceTravailSource(secrets()).search(q()))


# --------------------------------------------------------------------------- France Travail search


def test_ft_alternance_search_paginates_and_maps(router):
    token = router.post(ft.TOKEN_URL).respond(json=TOKEN)
    geo = router.get(ft.GEO_COMMUNES_URL).respond(json=[{"nom": "Metz", "code": "57463", "population": 120000}])
    search = router.get(ft.SEARCH_URL).mock(side_effect=[
        httpx.Response(206, json=fx("ft_search_page1.json"), headers={"Content-Range": "offres 0-149/151"}),
        httpx.Response(200, json=fx("ft_search_page2.json"), headers={"Content-Range": "offres 150-150/151"}),
        httpx.Response(204, headers={"Content-Range": "offres */0"}),
    ])
    src = ft_source()
    jobs = list(src.search(q(location=METZ, contract_types=[ContractType.ALTERNANCE])))

    assert token.call_count == 1 and token.calls[0].request.url.params["realm"] == "/partenaire"
    first, second = (dict(c.request.url.params) for c in search.calls[:2])
    assert first["natureContrat"] == "E2,FS" and "typeContrat" not in first
    assert first["commune"] == "57463" and first["distance"] == "40" and "M1855" in first["codeROME"]
    assert first["motsCles"] == "developpeur,full-stack" and first["publieeDepuis"] == "31"
    assert (first["range"], second["range"]) == ("0-149", "150-299")
    assert search.calls[0].request.headers["Authorization"] == "Bearer tok-1"

    a, b, c = jobs
    assert a.id == stable_id("france_travail", "212ABCD") and a.contract_type == ContractType.ALTERNANCE
    assert (a.apply_method, a.apply_url) == (ApplyMethod.EXTERNAL, "https://careers.acme-logiciels.fr/jobs/42")
    assert a.contact_email is None and a.remote is True and a.postal_code == "57000"
    assert a.company_domain == "acme-logiciels.fr" and "1400" in a.salary
    assert "Compétences : Développer une application web" in a.description
    assert a.url == ft.OFFER_PAGE_URL.format(id="212ABCD") and a.published_at.year == 2026
    assert (b.apply_method, b.remote) == (ApplyMethod.HELLOWORK, False)
    assert b.apply_url.startswith("https://www.hellowork.com/")
    assert (c.apply_method, c.contact_email, c.remote) == (ApplyMethod.EMAIL, "recrutement@metz-digital.fr", False)
    assert c.postal_code == "54000"
    # token + commune cached: a second search neither re-authenticates nor re-geocodes
    assert list(src.search(q(location=METZ))) == [] and token.call_count == 1 and geo.call_count == 1


def test_ft_cdi_query_drops_alternance_and_keeps_real_email(router):
    router.post(ft.TOKEN_URL).respond(json=TOKEN)
    search = router.get(ft.SEARCH_URL).respond(200, json=fx("ft_search_cdi.json"))
    jobs = list(ft_source().search(q(contract_types=[ContractType.CDI, ContractType.STAGE])))
    params = dict(search.calls[0].request.url.params)
    assert params["typeContrat"] == "CDI" and "natureContrat" not in params
    assert params["departement"] == "57,54" and params["inclureLimitrophes"] == "true"
    assert [j.external_id for j in jobs] == ["213XYZ1"]
    assert (jobs[0].apply_method, jobs[0].contact_email) == (ApplyMethod.EMAIL, "rh@lorraine-soft.fr")
    assert jobs[0].contract_type == ContractType.CDI and jobs[0].remote is True


def test_ft_remote_query_is_national_and_filters_remote(router):
    router.post(ft.TOKEN_URL).respond(json=TOKEN)
    search = router.get(ft.SEARCH_URL).respond(200, json=fx("ft_search_cdi.json"))
    jobs = list(ft_source().search(q(remote=True, location=METZ, max_results=1)))
    params = dict(search.calls[0].request.url.params)
    assert not {"departement", "commune", "natureContrat", "typeContrat"} & params.keys()
    assert [j.external_id for j in jobs] == ["213XYZ1"]


def test_ft_401_refreshes_token_once(router):
    token = router.post(ft.TOKEN_URL).respond(json=TOKEN)
    router.get(ft.SEARCH_URL).mock(side_effect=[httpx.Response(401), httpx.Response(204)])
    assert list(ft_source().search(q())) == [] and token.call_count == 2


def test_ft_token_rejected_raises_without_leaking_secret(router):
    router.post(ft.TOKEN_URL).respond(400, json={"error": "invalid_client", "error_description": "Client authentication failed"})
    with pytest.raises(SourceUnavailable) as err:
        list(ft_source().search(q()))
    assert "invalid_client" in str(err.value) and "s3cret" not in str(err.value)


def test_ft_commune_400_falls_back_to_department(router):
    router.post(ft.TOKEN_URL).respond(json=TOKEN)
    router.get(ft.GEO_COMMUNES_URL).respond(json=[{"nom": "Metz", "code": "57463"}])
    search = router.get(ft.SEARCH_URL).mock(side_effect=[
        httpx.Response(400, json={"codeHttp": 400, "message": "Référentiel communes indisponible"}),
        httpx.Response(200, json=fx("ft_search_page2.json")),
    ])
    jobs = list(ft_source().search(q(location=METZ, contract_types=[ContractType.ALTERNANCE])))
    fallback = dict(search.calls[1].request.url.params)
    assert fallback["departement"] == "57" and "commune" not in fallback and len(jobs) == 1


def test_ft_geo_failure_and_server_error(router):
    router.post(ft.TOKEN_URL).respond(json=TOKEN)
    router.get(ft.GEO_COMMUNES_URL).mock(side_effect=httpx.ConnectError("boom"))
    search = router.get(ft.SEARCH_URL).respond(500)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        list(ft_source().search(q(location=LocationPref(city="Metz", postal_code="57000"))))
    assert dict(search.calls[0].request.url.params)["departement"] == "57"


def test_ft_fetch_details(router, make_job):
    router.post(ft.TOKEN_URL).respond(json=TOKEN)
    offer = fx("ft_search_page1.json")["resultats"][0]
    detail = router.get(ft.DETAIL_URL.format(id="212ABCD")).mock(
        side_effect=[httpx.Response(200, json=offer), httpx.Response(204)] + [httpx.Response(503)] * 4)
    job = make_job(external_id="212ABCD", description="court", status=JobStatus.SHORTLISTED, match_score=70.0)
    src = ft_source()
    fresh = src.fetch_details(job)
    assert fresh.id == job.id and fresh.status == JobStatus.SHORTLISTED and fresh.match_score == 70.0
    assert "TypeScript" in fresh.description
    gone = src.fetch_details(job)
    assert gone.status == JobStatus.EXPIRED and gone.raw == {"expired": True} and gone.contact_email is None
    assert src.fetch_details(job) is job and detail.call_count == 6  # 503 retried 3 times
    assert src.fetch_details(make_job(source="lba")).source == "lba"


# --------------------------------------------------------------------------- registry


class FakeSource:
    def __init__(self, name, jobs=(), error=None, ok=True):
        self.name, self._jobs, self._error, self._ok, self.fetched = name, list(jobs), error, ok, []

    def available(self):
        return (self._ok, "" if self._ok else "pas de clé")

    def search(self, query):
        if self._error:
            raise self._error
        yield from self._jobs

    def fetch_details(self, job):
        self.fetched.append(job.id)
        if self._error:
            raise self._error
        return job.model_copy(update={"description": "Description complète " * 30, "status": JobStatus.EXPIRED})


def test_build_queries(profile, settings):
    queries = registry.build_queries(profile, settings)
    titles = {qq.keywords for qq in queries}
    assert "développeur fullstack alternance" not in titles and "développeur full-stack" in titles
    # alternance: Metz, Nancy, remote — CDI: Metz, Nancy, Luxembourg (CDI only), remote
    assert len(queries) == 9 * (3 + 4)
    remote = [qq for qq in queries if qq.remote]
    assert len(remote) == 18 and all(qq.location is None for qq in remote)
    lux = [qq for qq in queries if qq.location is not None and qq.location.city == "Luxembourg"]
    assert len(lux) == 9 and all(qq.contract_types == [ContractType.CDI] for qq in lux)
    assert all(len(qq.contract_types) == 1 and qq.max_results == settings.search.max_results_per_query for qq in queries)
    bare = profile.model_copy(update={"target": profile.target.model_copy(update={
        "job_titles": [], "locations": [], "contract_types": [], "remote_ok": False})})
    (only,) = registry.build_queries(bare, settings)
    assert only.keywords == "TypeScript React Next.js" and only.location is None and only.contract_types == []


def test_build_sources_reports_problems(ctx, monkeypatch):
    fake_module = types.ModuleType("jobbot.sources.linkedin")
    fake_module.LinkedInSource = lambda: FakeSource("linkedin")
    monkeypatch.setitem(sys.modules, "jobbot.sources.linkedin", fake_module)
    monkeypatch.setitem(sys.modules, "jobbot.sources.apec", None)  # → ImportError
    broken = types.ModuleType("jobbot.sources.wttj")
    broken.WelcomeToTheJungleSource = lambda: (_ for _ in ()).throw(ValueError("cassé"))
    monkeypatch.setitem(sys.modules, "jobbot.sources.wttj", broken)
    names = ["LBA", "france-travail", "linkedin", "apec", "wttj", "nope", "jobspy"]
    monkeypatch.setitem(sys.modules, "jobbot.sources.jobspy_source", types.SimpleNamespace(
        JobSpySource=lambda: FakeSource("indeed", ok=False)))
    sources, problems = registry._build(ctx, names)
    assert [s.name for s in sources] == ["lba", "linkedin"]
    assert set(problems) == {"france_travail", "apec", "wttj", "nope", "indeed"}
    assert "FRANCE_TRAVAIL" in problems["france_travail"] and "cassé" in problems["wttj"]
    assert [s.name for s in registry.build_sources(ctx, ["lba"])] == ["lba"]


def test_collect_jobs(ctx, make_job):
    ok = make_job(external_id="a", company="Bonne Boite")
    excluded = make_job(external_id="b", company="ISCOD SAS")
    blacklisted = make_job(external_id="c", company="Spam Corp", contact_email="rh@spam.fr")
    ctx.repo.blacklist_add("spam.fr")
    target = ctx.profile.target.model_copy(update={"exclude_companies": ["Iscod"]})
    ctx = dataclasses.replace(ctx, profile=ctx.profile.model_copy(update={"target": target}))
    good = FakeSource("france_travail", jobs=[ok, excluded, blacklisted, ok])
    failing = FakeSource("hellowork", error=RuntimeError("site changé"))
    down = FakeSource("lba", error=SourceUnavailable("bloqué"))
    queries = [q(), q(keywords="développeur web")]
    report = registry.collect_jobs(ctx, [good, failing, down], queries)
    assert (report.new, report.updated) == (1, 0)
    assert report.per_source == {"france_travail": 1, "hellowork": 0, "lba": 0}
    assert "2 requêtes en échec" in report.errors["hellowork"] and report.errors["lba"] == "bloqué"
    assert ctx.repo.get_job(ok.id) is not None and ctx.repo.get_job(excluded.id) is None
    again = registry.collect_jobs(ctx, [good], queries[:1])
    assert (again.new, again.updated, again.errors) == (0, 1, {})


def test_collect_jobs_caps_queries_for_scraped_boards(ctx):
    class Counting(FakeSource):
        def search(self, query):
            self.fetched.append(query.keywords)
            yield from ()

    hw, api = Counting("hellowork"), Counting("france_travail")
    queries = [q(keywords=f"développeur {i}") for i in range(10)]
    registry.collect_jobs(ctx, [hw, api], queries)
    assert len(hw.fetched) == registry.SOURCE_QUERY_BUDGET["hellowork"] and len(api.fetched) == 10


def test_collect_jobs_builds_sources_and_queries(ctx, make_job, monkeypatch):
    fakes = {"lba": FakeSource("lba", jobs=[make_job(source="lba")]), "france_travail": FakeSource("france_travail", ok=False)}
    monkeypatch.setattr(registry, "_instantiate", lambda _ctx, name: fakes.get(name) or FakeSource(name, ok=False))
    report = registry.collect_jobs(ctx)
    assert report.new == 1 and report.per_source == {"lba": 1}
    assert report.errors["france_travail"] == "pas de clé"
    assert len(report.errors) == len(ctx.settings.search.sources) - 1  # every configured source but lba


def test_ensure_details(ctx, make_job, monkeypatch):
    fake = FakeSource("france_travail")
    built = []
    monkeypatch.setattr(registry, "_instantiate", lambda _ctx, name: built.append(name) or fake)
    long_job = make_job(description="y" * 500)
    assert registry.ensure_details(ctx, long_job) is long_job and fake.fetched == []
    short = make_job(external_id="s1", description="court", status=JobStatus.SHORTLISTED, match_score=80.0)
    ctx.repo.upsert_job(short)
    done = registry.ensure_details(ctx, short)
    assert done.id == short.id and done.status == JobStatus.EXPIRED and done.match_score == 80.0
    assert ctx.repo.get_job(short.id).description.startswith("Description complète")
    assert registry.ensure_details(ctx, make_job(external_id="s2", description="x")).description.startswith("Desc")
    assert built == ["france_travail"]  # source instance reused within the same context
    assert registry.ensure_details(ctx, make_job(source="alerts", description="x")).description == "x"
    fake._error = RuntimeError("réseau")
    assert registry.ensure_details(ctx, short).description == "court"
    unchanged = FakeSource("france_travail")
    unchanged.fetch_details = lambda job: job
    monkeypatch.setattr(registry, "_detail_cache", {"ctx": None, "sources": {}})
    monkeypatch.setattr(registry, "_instantiate", lambda _ctx, name: unchanged)
    assert registry.ensure_details(ctx, short) is short


# --------------------------------------------------------------------------- live (opt-in: -m live)


@pytest.mark.live
def test_live_france_travail():
    creds = Secrets(_env_file=Path(__file__).resolve().parents[1] / ".env")  # type: ignore[call-arg]
    src = ft.FranceTravailSource(creds)
    if not src.available()[0]:
        pytest.skip("identifiants France Travail absents")
    jobs = list(src.search(q(keywords="développeur", contract_types=[ContractType.ALTERNANCE], max_results=5)))
    assert all(j.source == "france_travail" and j.contract_type == ContractType.ALTERNANCE for j in jobs)


def test_france_only_sources_skip_foreign_locations(profile, settings):
    queries = registry.build_queries(profile, settings)
    lux = [qq for qq in queries if qq.location is not None and qq.location.country == "LU"]
    assert lux
    assert not any(qq.location is not None and qq.location.country == "LU" for qq in registry._budgeted("lba", queries))
    kept = registry._supported("linkedin", queries)
    assert kept == queries
