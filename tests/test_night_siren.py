"""night.siren / night.siren_match — employers of the stored offers resolved to a SIREN: acronyms (sigle, initials),
local establishment of a group, ambiguous names, punctuation, zone, negative cache expiry, per-night cap, throttle
(respx, realistic recherche-entreprises JSON of fictitious companies, no network)."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from jobbot import clock
from jobbot.http import HostThrottle
from jobbot.matching.geo import KNOWN_PLACES
from jobbot.night import hiring, siren_match
from jobbot.night.budget import Deadline
from jobbot.night.store import LOCAL_KEY, merge_company, store_candidates
from jobbot.outreach import companies
from jobbot.textutil import normalize_company, stable_id
from tests.night_fakes import SEARCH_URL, offer, page, registry_item

FIXTURES = Path(__file__).parent / "fixtures" / "night"
LATER = Deadline(clock.now() + timedelta(hours=2))
ZONE = siren_match.Zone(departments=frozenset({"57", "54"}))
SHARED_THROTTLE = companies._THROTTLE  # the real one, before the autouse fixture swaps it


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(companies, "_THROTTLE", HostThrottle(0))
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)


def fx(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _group(name: str, **kw) -> siren_match.OfferGroup:
    base = dict(name=name, key=normalize_company(name), country="FR", department="57", postal_code="57000",
                city="Metz", latitude=49.1193, longitude=6.1757, domain=None, signals=())
    return siren_match.OfferGroup(**{**base, **kw})


def _answer(located: dict[str, dict], free: dict[str, dict] | None = None):
    """/search answered by its ``q`` — ``located``: queries with a department / postal code, ``free``: without."""
    def answer(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        table = located if ("departement" in params or "code_postal" in params) else (free or {})
        return httpx.Response(200, json=table.get(params["q"], page([])))
    return answer


def _kv(ctx, name: str) -> dict | None:
    raw = ctx.repo.kv_get(f"night:siren:fr:{normalize_company(name)}")
    return json.loads(raw) if raw else None


# --------------------------------------------------------------------------- names


def test_query_variants_and_comparable_names():
    assert siren_match.query_variants("Q&Z I.T. (H/F)") == ["q&z it", "qz it"]
    assert siren_match.query_variants("Groupe Exemple France") == ["exemple"]
    assert siren_match.query_variants("Exemple Conseil Nord") == ["exemple conseil"]
    assert siren_match.query_variants("Exemple Logiciels") == []  # same as the name: no extra query
    assert siren_match.words("Exemple S.A.S.") == "exemple"
    assert siren_match.pair_score(siren_match.words("Q&Z I.T."), siren_match.words("QZ IT")) == 100
    initials = siren_match.words("SOCIETE D'INGENIERIE ET DE MAINTENANCE XYLO")
    assert siren_match.pair_score("simx", initials) == siren_match.ACRONYM_SCORE


def test_acronym_matched_through_its_sigle_never_the_homonym():
    homonym, group_item = fx("registry_sigle.json")["results"]
    match = siren_match.pick(_group("SIMX"), ZONE, [homonym, group_item])
    assert match.siren == "900100001" and match.place["siret"] == "90010000100051"  # Metz agency, not Paris HQ
    no_sigle = {**group_item, "sigle": None, "nom_complet": group_item["nom_raison_sociale"]}
    assert siren_match.pick(_group("SIMX"), ZONE, [homonym, no_sigle]).siren == "900100001"  # initials
    assert siren_match.pick(_group("SIMX"), ZONE, [homonym]) is None  # short name, plumber: the place can't decide


def test_ambiguous_names_are_rejected():
    items = fx("registry_ambiguous.json")["results"]
    assert siren_match.pick(_group("Exemple Digital"), ZONE, items) is None
    far = {**items[1], "matching_etablissements": [], "siege": {**items[1]["siege"], "code_postal": "57100",
                                                                "libelle_commune": "THIONVILLE",
                                                                "latitude": "49.3580", "longitude": "6.1680"}}
    assert siren_match.pick(_group("Exemple Digital"), ZONE, [items[0], far]) is None  # 5 points apart: no margin


def test_outside_the_zone_is_never_a_candidate():
    lyon, nancy = fx("registry_zone.json")["results"]
    assert siren_match.pick(_group("Exemple Studio Web"), ZONE, [lyon]) is None
    assert siren_match.pick(_group("Exemple Studio Web"), ZONE, [lyon, nancy]).siren == "900500002"


# --------------------------------------------------------------------------- resolution through the registry


@respx.mock
def test_group_is_stored_at_its_local_establishment_not_its_head_office(ctx):
    data = fx("registry_group_local.json")
    ctx.repo.upsert_job(offer("1", "Exemple Numérique Conseil", "Développeur Java (H/F)"))
    head_office = companies.company_from_result({**data["results"][0], "matching_etablissements": []}, set())
    ctx.repo.upsert_company(head_office)  # stored earlier at its Paris head office
    route = respx.get(SEARCH_URL).mock(side_effect=_answer({"Exemple Numérique Conseil": data}))
    [company] = hiring.hiring_companies(ctx, LATER)
    assert company.siren == "900200001" and company.id == stable_id("company", "900200001")  # group SIREN
    assert (company.city, company.postal_code, company.raw["siret"]) == ("Metz", "57070", "90020000100043")
    assert company.latitude == pytest.approx(49.104) and company.raw[LOCAL_KEY] is True
    assert route.calls[0].request.url.params["departement"] == "57" and route.call_count == 1
    store_candidates(ctx, [company], LATER)
    stored = ctx.repo.get_company(company.id)
    assert (stored.city, stored.address, stored.raw["siret"]) == ("Metz", "12 RUE DE L'EXEMPLE 57070 METZ",
                                                                   "90020000100043")
    entry = _kv(ctx, "Exemple Numérique Conseil")
    assert entry["siren"] == "900200001" and entry["strategy"] == "departement"
    assert entry["place"]["postal_code"] == "57070"
    [again] = hiring.hiring_companies(ctx, LATER)
    assert route.call_count == 1 and again.city == "Metz"  # known employer: no registry query


def test_store_keeps_the_local_establishment_over_a_head_office():
    local = companies.company_from_result(registry_item("900000001", "Exemple", cp="57070", lat=49.10, lon=6.22),
                                          set())
    local = local.model_copy(update={"raw": {**local.raw, LOCAL_KEY: True}})
    head = companies.company_from_result(registry_item("900000001", "Exemple", cp="75008", city="PARIS",
                                                       lat=48.87, lon=2.31), set())
    head = head.model_copy(update={"raw": {**head.raw, "siret": "90000000100029"}})
    for merged in (merge_company(local, head), merge_company(head, local)):
        assert (merged.postal_code, merged.latitude, merged.raw["siret"]) == ("57070", 49.10, "90000000100011")
        assert merged.raw[LOCAL_KEY] is True


@respx.mock
def test_ambiguous_employer_is_cached_as_unknown_after_every_query(ctx):
    ctx.repo.upsert_job(offer("1", "Exemple Digital", "Développeur web"))
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=fx("registry_ambiguous.json")))
    assert hiring.hiring_companies(ctx, LATER) == []
    assert route.call_count == 3
    assert route.calls[1].request.url.params["code_postal"] == "57000"  # narrowed: two companies answered
    entry = _kv(ctx, "Exemple Digital")
    assert entry["siren"] is None and entry["tried"] == ["departement", "code_postal", "sans_lieu"]


@respx.mock
def test_punctuation_variants_find_the_company(ctx):
    ctx.repo.upsert_job(offer("1", "Q&Z I.T.", "Développeur web"))
    route = respx.get(SEARCH_URL).mock(side_effect=_answer({"qz it": fx("registry_punctuation.json")}))
    [company] = hiring.hiring_companies(ctx, LATER)
    assert company.siren == "900400001" and company.city == "Metz"
    assert [c.request.url.params["q"] for c in route.calls] == ["Q&Z I.T.", "q&z it", "qz it"]
    assert all(c.request.url.params["departement"] == "57" for c in route.calls)
    assert _kv(ctx, "Q&Z I.T.")["strategy"] == "variante:qz it"


@respx.mock
def test_query_without_place_keeps_an_establishment_inside_the_zone(ctx):
    ctx.repo.upsert_job(offer("1", "Exemple Studio Web", "Développeur web"))
    route = respx.get(SEARCH_URL).mock(side_effect=_answer({}, {"Exemple Studio Web": fx("registry_zone.json")}))
    [company] = hiring.hiring_companies(ctx, LATER)
    assert company.siren == "900500002" and company.city == "Nancy"
    assert (company.latitude, company.longitude) == KNOWN_PLACES["nancy"]  # no registry coordinates: its city
    last = route.calls[-1].request.url.params
    assert "departement" not in last and "code_postal" not in last


# --------------------------------------------------------------------------- cache, cap, throttle


@respx.mock
def test_negative_cache_expires_after_seven_days(ctx):
    ctx.repo.upsert_job(offer("1", "Inconnue Totale", "Développeur web"))
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page([])))
    hiring.hiring_companies(ctx, LATER)
    per_lookup = route.call_count
    entry = _kv(ctx, "Inconnue Totale")
    assert per_lookup == 2 and entry["siren"] is None and entry["tried"] == ["departement", "sans_lieu"]
    assert clock.parse_dt(entry["retry_after"]) - clock.parse_dt(entry["at"]) == timedelta(days=7)
    hiring.hiring_companies(ctx, LATER)
    assert route.call_count == per_lookup  # not looked up again within 7 days
    key = f"night:siren:fr:{normalize_company('Inconnue Totale')}"
    past = clock.now() - timedelta(days=8)
    ctx.repo.kv_set(key, json.dumps({**entry, "at": clock.iso(past), "retry_after": clock.iso(past + timedelta(7))}))
    hiring.hiring_companies(ctx, LATER)
    assert route.call_count == 2 * per_lookup  # expired: looked up again
    ctx.repo.kv_set(key, json.dumps({"siren": None, "at": clock.iso(clock.now())}))  # older single-query resolver
    hiring.hiring_companies(ctx, LATER)
    assert route.call_count == 3 * per_lookup  # retried at once with every query


@respx.mock
def test_registry_queries_are_capped_per_night(ctx):
    assert ctx.settings.night.max_siren_lookups == 200
    night = ctx.settings.night.model_copy(update={"max_siren_lookups": 3})
    capped = dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={"night": night}))
    for i, (name, offers) in enumerate([("Inconnue Alpha", 3), ("Inconnue Beta", 2), ("Inconnue Gamma", 1)]):
        for n in range(offers):
            ctx.repo.upsert_job(offer(f"{i}-{n}", name, f"Développeur web {n}"))
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page([])))
    assert hiring.hiring_companies(capped, LATER) == []
    assert [c.request.url.params["q"] for c in route.calls] == ["Inconnue Alpha", "Inconnue Alpha", "Inconnue Beta"]
    assert _kv(ctx, "Inconnue Alpha")["tried"] == ["departement", "sans_lieu"]
    assert _kv(ctx, "Inconnue Beta") is None and _kv(ctx, "Inconnue Gamma") is None  # cut short: next night
    hiring.hiring_companies(capped, LATER)
    assert route.call_count == 6 and _kv(ctx, "Inconnue Beta")["siren"] is None


@respx.mock
def test_registry_queries_share_the_four_per_second_throttle(ctx, monkeypatch):
    assert SHARED_THROTTLE.min_interval_s >= 0.25
    waits: list[str] = []

    class Counting(HostThrottle):
        def wait(self, url: str) -> None:
            waits.append(url)

    monkeypatch.setattr(companies, "_THROTTLE", Counting(0))
    ctx.repo.upsert_job(offer("1", "Inconnue Totale", "Développeur web"))
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page([])))
    hiring.hiring_companies(ctx, LATER)
    assert len(waits) == route.call_count == 2
    assert all("recherche-entreprises.api.gouv.fr" in url for url in waits)
