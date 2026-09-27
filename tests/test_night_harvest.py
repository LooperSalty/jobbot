"""night.harvest / night.hiring / night.store — registry around every area, LBA recruiters, employers of the stored
offers, dedupe / merge and the relevance gate (respx, fictitious companies, no network)."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from jobbot import clock
from jobbot.http import HostThrottle
from jobbot.models import Company, ContactEmail
from jobbot.night import hiring
from jobbot.night.budget import Deadline
from jobbot.night.harvest import harvest, harvest_nafs, near_point
from jobbot.night.store import combine, merge_signals, store_candidates
from jobbot.outreach import companies
from jobbot.textutil import stable_id
from tests.night_fakes import NEAR_URL, SEARCH_URL, offer, page, registry_item

LATER = Deadline(clock.now() + timedelta(hours=2))


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(companies, "_THROTTLE", HostThrottle(0))
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    monkeypatch.setitem(sys.modules, "jobbot.sources.lba", None)  # LBA absent unless a test installs a fake


def _deadline(hours: float = 2) -> Deadline:
    return Deadline(clock.now() + timedelta(hours=hours))


# --------------------------------------------------------------------------- registry around a point


def test_harvest_nafs_profile_codes_then_it_list(profile):
    custom = profile.model_copy(update={"target": profile.target.model_copy(update={"naf_codes": ["73.11Z", "62"]})})
    nafs = harvest_nafs(custom)
    assert nafs[0] == "73.11Z" and "62" not in nafs
    assert {"62.01Z", "58.29C", "63.11Z", "61.10Z", "72.19Z"} <= set(nafs)


@respx.mock
def test_near_point_pages_and_filters(ctx):
    first = [registry_item(f"9000000{i:02d}", f"Exemple Code {i}") for i in range(25)]
    second = [registry_item("900000099", "Exemple Dernier"), registry_item("900000098", "Solo", sole_trader=True),
              registry_item("900000097", "Fermee", active=False)]
    route = respx.get(NEAR_URL).mock(side_effect=[httpx.Response(200, json=page(first, total=28)),
                                                  httpx.Response(200, json=page(second, total=28))])
    metz = ctx.profile.target.locations[0]
    with httpx.Client() as client:
        found, stopped = near_point(client, metz, 50, ["62.01Z", "62.02A"], LATER)
    assert not stopped and len(found) == 26  # sole trader and closed company dropped
    params = route.calls[0].request.url.params
    assert params["lat"] == str(metz.latitude) and params["long"] == str(metz.longitude)
    assert params["radius"] == "50" and params["activite_principale"] == "62.01Z,62.02A"
    assert params["per_page"] == "25" and route.calls[1].request.url.params["page"] == "2"


@respx.mock
def test_near_point_splits_by_naf_beyond_the_api_limit(ctx):
    def answer(request):
        nafs = request.url.params["activite_principale"]
        if "," in nafs:
            return httpx.Response(200, json=page([], total=12_000))
        return httpx.Response(200, json=page([registry_item(f"91000000{len(nafs) % 10}", f"Exemple {nafs}")]))

    route = respx.get(NEAR_URL).mock(side_effect=answer)
    with httpx.Client() as client:
        found, _ = near_point(client, ctx.profile.target.locations[0], 50, ["62.01Z", "58.29C"], LATER)
    assert [c.name for c in found] == ["Exemple 62.01Z", "Exemple 58.29C"]
    assert route.call_count == 3


@respx.mock
def test_a_failing_page_keeps_the_pages_already_read(ctx):
    first = [registry_item(f"9000001{i:02d}", f"Exemple {i}") for i in range(25)]
    respx.get(NEAR_URL).mock(side_effect=[httpx.Response(200, json=page(first, total=60)),
                                          httpx.Response(400, json={"erreur": "page invalide"})])
    with httpx.Client() as client:
        found, stopped = near_point(client, ctx.profile.target.locations[0], 50, ["62.01Z"], LATER)
    assert len(found) == 25 and not stopped


@respx.mock
def test_near_point_stops_at_the_deadline(ctx):
    respx.get(NEAR_URL).mock(return_value=httpx.Response(200, json=page([registry_item("900000001", "A")],
                                                                        total=100)))
    with httpx.Client() as client:
        found, stopped = near_point(client, ctx.profile.target.locations[0], 50, ["62.01Z"],
                                    Deadline(clock.now() - timedelta(seconds=1)))
    assert stopped and len(found) == 1  # the first page only


# --------------------------------------------------------------------------- store: dedupe, merge, gate


def _registry(siren: str, name: str, **kw) -> Company:
    return companies.company_from_result(registry_item(siren, name, **kw), set())


def test_merge_signals_dedupes_and_keeps_the_latest():
    old = {"title": "Développeur web", "source": "lba", "date": "2026-09-01T00:00:00+00:00"}
    new = {"title": "Développeur Web", "source": "lba", "date": "2026-09-20T00:00:00+00:00"}
    other = {"title": "Ingénieur DevOps", "source": "indeed", "date": "2026-09-10T00:00:00+00:00"}
    assert merge_signals([old], [new, other]) == [new, other]


def test_store_dedupes_merges_and_applies_the_relevance_gate(ctx):
    kept = _registry("900000001", "Exemple Logiciels")
    ctx.repo.upsert_company(kept.model_copy(update={
        "status": "enriched", "contacts": [ContactEmail(email="rh@exemple-logiciels.fr", kind="recruitment")],
        "domain": "exemple-logiciels.fr"}))
    waiting_off = _registry("900000002", "Exemple Plomberie", naf="43.22A").model_copy(update={"status": "new"})
    ctx.repo.upsert_company(waiting_off)
    fresh = [
        _registry("900000001", "Exemple Logiciels", tranche="12"),  # refresh of the stored one
        _registry("900000003", "Nouvelle Appli"), _registry("900000003", "Nouvelle Appli"),  # twice (two areas)
        _registry("900000002", "Exemple Plomberie", naf="43.22A"),  # outside the target
        _registry("900000004", "Exemple Maconnerie", naf="43.99C"),  # outside the target, never stored
    ]
    result = store_candidates(ctx, fresh, LATER)
    assert (result.found, result.new, result.relevant, result.irrelevant) == (4, 1, 2, 2)
    stored = ctx.repo.get_company(kept.id)
    assert stored.status == "enriched" and stored.contacts and stored.domain == "exemple-logiciels.fr"
    assert stored.raw["tranche_effectif_salarie"] == "12"
    assert ctx.repo.get_company(waiting_off.id).status == "irrelevant"
    assert ctx.repo.get_company(stable_id("company", "900000004")) is None


def test_combine_keeps_one_candidate_per_company():
    a = _registry("900000001", "Exemple").model_copy(update={"raw": {"hiring_signal": [
        {"title": "Développeur web", "source": "lba", "date": "2026-09-01"}]}})
    b = _registry("900000001", "Exemple")
    assert len(combine([a, b])) == 1 and combine([a, b])[0].raw["hiring_signal"]


# --------------------------------------------------------------------------- hiring signals


def _jobs(ctx, *jobs):
    for job in jobs:
        ctx.repo.upsert_job(job)


def test_offer_eligibility():
    assert hiring.eligible(offer("1", "Exemple Logiciels"))
    assert not hiring.eligible(offer("2", "Entreprise confidentielle"))
    assert not hiring.eligible(offer("3", "Ecole du Code Exemple"))  # school / training organisation
    assert not hiring.eligible(offer("4", "Exemple Ventes", "Business developer (H/F)"))
    assert not hiring.eligible(offer("5", "Exemple Boulangerie", "Vendeur en boulangerie"))


def test_job_zone(ctx):
    assert hiring.job_zone(ctx, offer("1", "A")) == "FR"
    assert hiring.job_zone(ctx, offer("2", "B", location="Paris (75)", postal_code="75001", lat=48.86,
                                      lon=2.35)) is None
    assert hiring.job_zone(ctx, offer("3", "C", location="Nancy (54)", postal_code="54000", lat=None,
                                      lon=None)) == "FR"
    assert hiring.job_zone(ctx, offer("4", "D", location="Luxembourg, Luxembourg", postal_code=None, lat=49.61,
                                      lon=6.13)) == "LU"


def _group(name: str = "Exemple Logiciels", **kw) -> hiring.OfferGroup:
    base = dict(name=name, key=name.lower(), country="FR", department="57", postal_code="57000", city="Metz",
                latitude=None, longitude=None, domain=None, signals=())
    return hiring.OfferGroup(**{**base, **kw})


def test_best_match_accepts_only_high_confidence():
    exact = registry_item("900000001", "EXEMPLE LOGICIELS SAS")
    other = registry_item("900000002", "EXEMPLE CONSTRUCTION")
    assert hiring.best_match(_group(), [other, exact])["siren"] == "900000001"
    assert hiring.best_match(_group("Exemple"), [other, registry_item("900000003", "EXEMPLE LOGISTIQUE")]) is None
    twins = [registry_item("900000004", "EXEMPLE LOGICIELS"), registry_item("900000005", "EXEMPLE LOGICIEL")]
    assert hiring.best_match(_group(), twins) is None  # ambiguous: never guess
    near_name = registry_item("900000006", "EXEMPLE LOGICIELS METZ", cp="57000")
    assert hiring.best_match(_group(), [near_name])["siren"] == "900000006"  # same postal code: 85+ is enough
    assert hiring.best_match(_group(postal_code="54000", city="Nancy"), [near_name]) is None


@respx.mock
def test_hiring_companies_resolve_cache_and_luxembourg(ctx):
    _jobs(ctx, offer("1", "Exemple Logiciels", "Développeur React en alternance"),
          offer("2", "Exemple Logiciels", "Développeur Python (H/F)", days_ago=20),
          offer("3", "Inconnue Totale", "Développeur web"),
          offer("4", "Exemple Luxembourg Tech", "Software developer", location="Luxembourg, Luxembourg",
                postal_code=None, lat=49.61, lon=6.13, company_domain="exemple-lu.example"))

    def answer(request):
        if request.url.params["q"] == "Exemple Logiciels":
            return httpx.Response(200, json=page([registry_item("900000001", "EXEMPLE LOGICIELS")]))
        return httpx.Response(200, json=page([registry_item("900000009", "SANS RAPPORT")]))

    route = respx.get(SEARCH_URL).mock(side_effect=answer)
    found = {c.name: c for c in hiring.hiring_companies(ctx, LATER)}
    assert set(found) == {"EXEMPLE LOGICIELS", "Exemple Luxembourg Tech"}
    french = found["EXEMPLE LOGICIELS"]
    assert french.siren == "900000001" and [s["title"] for s in french.raw["hiring_signal"]] == [
        "Développeur React en alternance", "Développeur Python (H/F)"]
    assert route.calls[0].request.url.params["departement"] == "57"
    lux = found["Exemple Luxembourg Tech"]
    assert lux.siren is None and lux.raw["country"] == "LU" and lux.website == "exemple-lu.example"
    ctx.repo.upsert_company(french)  # what store_candidates does
    calls = route.call_count
    again = hiring.hiring_companies(ctx, LATER)
    assert route.call_count == calls  # resolutions cached (unresolved employer retried after 30 days)
    assert {c.name for c in again} == {"EXEMPLE LOGICIELS", "Exemple Luxembourg Tech"}
    cached = json.loads(ctx.repo.kv_get("night:siren:fr:inconnue totale"))
    assert cached["siren"] is None


@respx.mock
def test_harvest_stores_everything_through_the_gate(ctx, monkeypatch):
    lba_calls: list[tuple] = []

    class FakeLba:
        def __init__(self, secrets):
            pass

        def search_recruiters(self, lat, lon, radius, romes):
            lba_calls.append((lat, radius))
            return [Company(id="lba-1", name="Exemple Recrute Alternants", siren="900000050",
                            naf_label="Programmation informatique", source="lba_recruteur",
                            found_at=clock.now(), raw={"siret": "90000005000011"})]

    monkeypatch.setitem(sys.modules, "jobbot.sources.lba", types.SimpleNamespace(LaBonneAlternanceSource=FakeLba))
    respx.get(NEAR_URL).mock(return_value=httpx.Response(200, json=page([
        registry_item("900000001", "Exemple Logiciels"), registry_item("900000002", "Exemple Donnees", naf="63.11Z"),
    ])))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page([
        registry_item("900000001", "EXEMPLE LOGICIELS")])))
    _jobs(ctx, offer("9", "Exemple Logiciels", "Développeur full-stack"))
    irrelevant = _registry("900000060", "Exemple Banque", naf="64.19Z").model_copy(update={"status": "irrelevant"})
    ctx.repo.upsert_company(irrelevant)
    result = harvest(ctx, _deadline())
    assert result.new == 3 and result.irrelevant == 0 and not result.errors
    assert [radius for _, radius in lba_calls] == [50, 45]  # Metz 40+20 (max 50), Nancy 25+20; never Luxembourg
    stored = ctx.repo.get_company(stable_id("company", "900000001"))
    assert stored.raw["hiring_signal"][0]["title"] == "Développeur full-stack"  # merged with the registry data
    assert ctx.repo.get_company(stable_id("company", "900000050")).source == "lba_recruteur"


def test_irrelevant_company_is_reopened_by_a_developer_offer(ctx):
    bank = _registry("900000070", "Exemple Banque Regionale", naf="64.19Z").model_copy(update={
        "status": "irrelevant", "raw": {"irrelevant_reason": "secteur hors numérique"}})
    ctx.repo.upsert_company(bank)
    signal = bank.model_copy(update={"raw": {"hiring_signal": [
        {"title": "Développeur Java en alternance", "source": "lba", "date": clock.iso(clock.now())}]}})
    result = store_candidates(ctx, [signal], LATER)
    stored = ctx.repo.get_company(bank.id)
    assert result.relevant == 1 and stored.status == "new" and "irrelevant_reason" not in stored.raw


def test_deadline_type_is_timezone_aware():
    assert isinstance(LATER.end, datetime) and LATER.end.tzinfo is not None
