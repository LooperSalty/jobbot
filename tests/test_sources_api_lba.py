"""Tests for sources/lba.py (+ lba_mapping): La Bonne Alternance path B (site JSON) and path A (official API).

Fixtures in tests/fixtures/sources_api/: lba_search_*.json and lba_detail_*.json are live captures of
2026-09-23 (trimmed, apply tokens redacted); lba_api_search.json is schema-derived (no API key available).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.config import LocationPref, Secrets
from jobbot.http import HostThrottle
from jobbot.models import ApplyMethod, ContractType, JobStatus
from jobbot.sources import lba
from jobbot.sources.base import SearchQuery
from jobbot.textutil import stable_id

FIX = Path(__file__).parent / "fixtures" / "sources_api"
METZ = LocationPref(city="Metz", postal_code="57000", radius_km=40, latitude=49.1193, longitude=6.1757)
LBA_ALLOWED = {"q", "mode", "is_algo_company", "smart_apply", "level", "contract_type", "sort", "latitude",
               "longitude", "radius", "admin_area", "page", "hitsPerPage"}


def fx(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def lba_source(**kw) -> lba.LaBonneAlternanceSource:
    return lba.LaBonneAlternanceSource(Secrets(_env_file=None, lba_api_token=kw.get("lba_api_token")))  # type: ignore[call-arg]


def q(**kw) -> SearchQuery:
    return SearchQuery(**{"keywords": "développeur full-stack", "max_results": 50, **kw})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch, request):
    if request.node.get_closest_marker("live") is None:
        monkeypatch.setattr("jobbot.http.time.sleep", lambda _s: None)
        monkeypatch.setattr(lba, "_sleep", lambda _s: None)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def test_lba_partner_offer_is_enriched_and_routed(router):
    search = router.get(lba.SITE_SEARCH_URL).respond(json=fx("lba_search_offers.json"))
    router.get(lba.SITE_DETAIL_URL.format(sub_type=lba.SUB_PARTNER, id="6a064773687b65f199a02f5f")).respond(
        json=fx("lba_detail_partner.json"))
    src = lba_source()
    assert src.available() == (True, "")
    jobs = list(src.search(q(keywords="développeur full-stack alternance", location=METZ,
                             contract_types=[ContractType.ALTERNANCE])))
    params = search.calls[0].request.url.params
    assert set(params.keys()) <= LBA_ALLOWED and params["q"] == "développeur full-stack"
    assert (params["mode"], params["is_algo_company"], params["latitude"], params["radius"]) == (
        "emplois", "false", "49.1193", "40")
    (job,) = jobs
    assert (job.apply_method, job.contract_type) == (ApplyMethod.HELLOWORK, ContractType.ALTERNANCE)
    assert job.apply_url.startswith("https://www.hellowork.com/fr-fr/emplois/79006307.html")
    assert (job.latitude, job.longitude, job.postal_code) == (49.108385, 6.194897, "57000")
    assert job.raw["source"] == lba.SUB_PARTNER and job.raw["id"] == "6a064773687b65f199a02f5f"
    assert job.raw["hasEmail"] is False and job.raw["application_count"] == 0
    assert job.raw["hellowork_id"] == "79006307" and "token" not in json.dumps(job.raw)
    assert "Vue.js" in job.description and "CHAUSSEA" in job.description
    assert job.url.startswith(f"{lba.LBA_SITE}/emploi/offres_emploi_partenaires/6a064773687b65f199a02f5f/")


def test_lba_smart_apply_offers_and_admin_area(router):
    search = router.get(lba.SITE_SEARCH_URL).respond(json=fx("lba_search_smart.json"))
    detail = router.get(url__startswith=f"{lba.LBA_SITE}/api/_private/jobs/")
    loc = LocationPref(city="Metz", postal_code="57000")
    jobs = list(lba_source().search(q(keywords="", location=loc, max_results=3)))
    params = search.calls[0].request.url.params
    assert params["admin_area"] == "departement:57" and "q" not in params and params["hitsPerPage"] == "3"
    assert len(jobs) == 3 and all(j.apply_method == ApplyMethod.LBA and j.apply_url is None for j in jobs)
    assert jobs[0].raw["source"] == lba.SUB_LBA and jobs[0].raw["smart_apply"] is True
    assert detail.call_count == 0 and search.call_count == 1


def test_lba_ignores_non_alternance_and_default_areas(router):
    search = router.get(lba.SITE_SEARCH_URL).respond(json={"hits": [], "nbPages": 0})
    assert list(lba_source().search(q(contract_types=[ContractType.CDI]))) == [] and search.call_count == 0
    assert list(lba_source().search(q())) == []
    assert [c.request.url.params["admin_area"] for c in search.calls] == ["departement:57", "departement:54"]


def test_lba_site_error_raises(router):
    router.get(lba.SITE_SEARCH_URL).respond(400, json={"message": 'Unrecognized key: "foo"'})
    with pytest.raises(RuntimeError, match="HTTP 400"):
        list(lba_source().search(q()))


def test_lba_official_api_when_token(router):
    api = router.get(lba.API_SEARCH_URL).respond(json=fx("lba_api_search.json"))
    router.get(lba.SITE_SEARCH_URL).respond(json={"hits": [], "nbPages": 0})
    src = lba_source(lba_api_token="api-key")
    jobs = {j.external_id: j for j in src.search(q(location=METZ))}
    assert api.calls[0].request.headers["Authorization"] == "Bearer api-key"
    assert api.calls[0].request.url.params["romes"].startswith("M1855")
    assert set(jobs) == {"6a064773687b65f199a02f5f", "6b1111111111111111111111", "france-travail-212ABCD"}
    hw, native, ftj = jobs.values()
    assert (hw.apply_method, hw.remote, hw.raw["source"]) == (ApplyMethod.HELLOWORK, False, lba.SUB_PARTNER)
    assert (native.apply_method, native.remote) == (ApplyMethod.LBA, True)
    assert native.raw["recipient_id"] == "recruiters_6b1111111111111111111111" and native.company_domain == "efluid.fr"
    assert (ftj.apply_method, ftj.raw["source"], ftj.remote) == (ApplyMethod.EXTERNAL, None, True)
    list(src.search(q(location=METZ)))
    assert api.call_count == 1  # cached: the API has no keyword filter


def test_lba_official_api_rejected_falls_back_to_site(router):
    api = router.get(lba.API_SEARCH_URL).respond(401, json={"message": "Impossible de déchiffrer la clé d'API"})
    router.get(lba.SITE_SEARCH_URL).respond(json=fx("lba_search_smart.json"))
    src = lba_source(lba_api_token="bad")
    assert len(list(src.search(q(max_results=2)))) == 2
    assert len(list(src.search(q(max_results=2)))) == 2 and api.call_count == 1


def test_lba_api_rate_limit_419_is_retried(router):
    router.get(lba.API_SEARCH_URL).mock(side_effect=[
        httpx.Response(419, headers={"retry-after": "2"}), httpx.Response(200, json={"jobs": [], "recruiters": []}),
    ])
    src = lba_source(lba_api_token="k")
    assert src._api_search([("romes", "M1855")]) == {"jobs": [], "recruiters": []}


def test_lba_search_recruiters(router):
    page1 = fx("lba_search_recruiters.json")
    page2 = {"hits": page1["hits"][:1], "nbHits": 11, "page": 1, "nbPages": 2}
    search = router.get(lba.SITE_SEARCH_URL).mock(side_effect=[httpx.Response(200, json=page1),
                                                              httpx.Response(200, json=page2)])
    companies = lba_source().search_recruiters(49.1193, 6.1757, 30, ["M1855", "Z9999"])
    params = search.calls[0].request.url.params
    assert (params["is_algo_company"], params["q"], params["radius"]) == ("true", "développeur web", "30")
    assert search.call_count == 2 and len(companies) == 10
    lorca = next(c for c in companies if c.name == "LORCA SERVICES")
    assert (lorca.siren, lorca.city, lorca.postal_code, lorca.source) == ("851900969", "LEMUD", "57580", "lba_recruteur")
    assert lorca.id == stable_id("company", "851900969") and lorca.naf_label == "EDITION DE LOGICIELS APPLICATIFS"
    assert lorca.raw["hasEmail"] is True and lorca.raw["recipient_id"] == "partners_6a003c199a6be614e489356f"
    assert (lorca.raw["source"], lorca.raw["id"], lorca.raw["application_count"]) == ("recruteurs_lba", "85190096900011", 38)
    assert round(lorca.latitude, 3) == 49.038


def test_lba_search_recruiters_merges_official_api(router):
    router.get(lba.SITE_SEARCH_URL).respond(json={**fx("lba_search_recruiters.json"), "nbPages": 1})
    api = router.get(lba.API_SEARCH_URL).respond(json=fx("lba_api_search.json"))
    companies = {c.name: c for c in lba_source(lba_api_token="k").search_recruiters(49.1, 6.1, 500, [])}
    assert api.calls[0].request.url.params["radius"] == "200"
    efluid = companies["EFLUID"]
    assert (efluid.website, efluid.domain, efluid.naf_code, efluid.headcount_range) == (
        "https://www.efluid.fr", "efluid.fr", "58.29C", "250-499")
    assert efluid.raw["phone"] == "0387000000" and efluid.raw["application_count"] == 67
    assert companies["NANCY CODE"].raw["hasEmail"] is False and len(companies) == 11


def test_lba_search_recruiters_survives_site_error(router):
    router.get(lba.SITE_SEARCH_URL).respond(503)
    assert lba_source().search_recruiters(49.1, 6.1, 30, ["M1805"]) == []


def test_lba_fetch_details(router, make_job):
    native_id, partner_id = "6aaa5a821124e472788457aa", "6a064773687b65f199a02f5f"
    router.get(lba.SITE_DETAIL_URL.format(sub_type=lba.SUB_LBA, id=native_id)).respond(json=fx("lba_detail_lba_offer.json"))
    cancelled = {**fx("lba_detail_partner.json"), "job": {**fx("lba_detail_partner.json")["job"], "status": "Cancelled"}}
    router.get(lba.SITE_DETAIL_URL.format(sub_type=lba.SUB_PARTNER, id=partner_id)).respond(json=cancelled)
    router.get(lba.SITE_DETAIL_URL.format(sub_type=lba.SUB_PARTNER, id="gone")).respond(404)
    router.get(lba.SITE_DETAIL_URL.format(sub_type=lba.SUB_PARTNER, id="boom")).respond(500)
    src = lba_source()

    def job(ext, sub):
        return make_job(source="lba", external_id=ext, description="x", apply_method=ApplyMethod.EXTERNAL,
                        raw={"source": sub, "id": ext})

    native = src.fetch_details(job(native_id, lba.SUB_LBA))
    assert native.apply_method == ApplyMethod.LBA and native.raw["recipient_id"] == f"recruiters_{native_id}"
    assert "Profil recherché" in native.description and native.status == JobStatus.NEW
    assert src.fetch_details(job(partner_id, lba.SUB_PARTNER)).status == JobStatus.EXPIRED
    assert src.fetch_details(job("gone", lba.SUB_PARTNER)).status == JobStatus.EXPIRED
    assert src.fetch_details(job("boom", lba.SUB_PARTNER)).description == "x"
    other = make_job(source="lba", raw={"source": "recruteurs_lba", "id": "1"})
    assert src.fetch_details(other) is other
    no_relay = {**fx("lba_detail_lba_offer.json"), "contact": {"hasEmail": False, "url": f"{lba.LBA_SITE}/emploi/x"}}
    was_lba = job(native_id, lba.SUB_LBA).model_copy(update={"apply_method": ApplyMethod.LBA})
    downgraded = lba.apply_detail(was_lba, no_relay)
    assert (downgraded.apply_method, downgraded.apply_url) == (ApplyMethod.EXTERNAL, was_lba.url)


def test_lba_mapping_helpers():
    assert lba.search_text("développeur (alternance) web") == "développeur web"
    assert lba.geo_point({"coordinates": [6.1, 49.2]}) == (49.2, 6.1) and lba.geo_point(None) == (None, None)
    assert lba.split_address("2B RUE ARDANT DU PICQ 57000 METZ") == ("57000", "METZ")
    assert lba.split_address("") == (None, "")
    assert lba.safe_map(lba.hit_to_job, {"title": "sans id"}) is None
    assert lba.api_recruiter_to_company({"workplace": {}}) is None



# --------------------------------------------------------------------------- live (opt-in: -m live)


@pytest.mark.live
def test_live_lba_offers_and_recruiters():
    src = lba_source()
    src._site_throttle = HostThrottle(2.5)  # polite: >= 2 s between live requests
    jobs = list(src.search(q(keywords="développeur", location=METZ, contract_types=[ContractType.ALTERNANCE],
                             max_results=5)))
    assert all(j.source == "lba" and j.raw.get("source") and j.raw.get("id") for j in jobs)
    companies = src.search_recruiters(49.1193, 6.1757, 20, ["M1855"])
    assert companies and all(c.source == "lba_recruteur" and len(c.siren or "") == 9 for c in companies)
