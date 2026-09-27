"""night.siren_match / siren_prefer / siren_remote — the operating IT company of a group over its holdings, ties
between group entities, acronyms with many namesakes, the regional sibling closer to the offer, common names, and IT
companies without any local agency kept at their head office but working where the offer is (respx, realistic
recherche-entreprises JSON of fictitious companies, no network)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from jobbot import clock
from jobbot.http import HostThrottle
from jobbot.models import ContactEmail
from jobbot.night import hiring, siren_match, siren_remote
from jobbot.night.budget import Deadline
from jobbot.night.siren_prefer import HOLDING, IT, Traits, compare, winner
from jobbot.night.store import LOCAL_KEY, merge_company, store_candidates
from jobbot.outreach import companies
from jobbot.outreach.priority import company_priority
from jobbot.textutil import normalize_company
from tests.night_fakes import SEARCH_URL, offer, registry_item

FIXTURES = Path(__file__).parent / "fixtures" / "night"
LATER = Deadline(clock.now() + timedelta(hours=2))
ZONE = siren_match.Zone(departments=frozenset({"57", "54"}))
NO_AGENCY = "Pas d'agence locale enregistrée : candidature au siège (recrute pour Metz)"
THIONVILLE = dict(cp="57100", city="THIONVILLE", lat=49.358, lon=6.168)


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


def _answer(located: dict[str, dict], free: dict[str, dict]):
    """/search answered by its ``q`` — ``located``: queries with a department / postal code, ``free``: without."""
    def answer(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        table = located if ("departement" in params or "code_postal" in params) else free
        return httpx.Response(200, json=table.get(params["q"], {"results": [], "total_results": 0}))
    return answer


def _kv(ctx, name: str) -> dict | None:
    raw = ctx.repo.kv_get(f"night:siren:fr:{normalize_company(name)}")
    return json.loads(raw) if raw else None


def _remote(name: str, fixture: str, **group) -> siren_match.Match | None:
    data = fx(fixture)
    return siren_remote.decide_remote(_group(name, **group), ZONE, data["results"], data["total_results"])


# --------------------------------------------------------------------------- operating company vs holdings


def test_holding_never_wins_over_the_operating_it_company():
    syntec = {"est_entrepreneur_individuel": False, "liste_idcc": ["1486"]}  # holdings of IT groups often apply it
    holding = {**registry_item("900000011", "EXEMPLE NUMERIX", naf="70.10Z", categorie="GE"), "complements": syntec}
    operating = registry_item("900000012", "EXEMPLE NUMERIX FRANCE", **THIONVILLE)
    assert siren_match.pick(_group("Exemple Numerix"), ZONE, [holding, operating]).siren == "900000012"
    assert siren_match.pick(_group("Exemple Numerix"), ZONE, [holding]).siren == "900000011"  # alone: as before
    items = fx("registry_holding.json")["results"]  # « VEXLINK » holding and « VEXLINK FRANCE », both near Paris
    assert _remote("Vexlink", "registry_holding.json").siren == "900600002"
    assert siren_remote.decide_remote(_group("Vexlink"), ZONE, items[:1]) is None  # a holding without agency: never


def test_group_entities_tie_goes_to_the_operating_it_company():
    items = fx("registry_group_tie.json")["results"]
    ranked = siren_match.rank(_group("Zorgano Groupe"), ZONE, items)
    assert ranked[0].siren == "900900001" and ranked[0].score - ranked[1].score < siren_match.MARGIN  # no margin
    match = siren_match.pick(_group("Zorgano Groupe"), ZONE, items)
    assert match.siren == "900900002" and match.place["siret"] == "90090000200044"  # its Nancy agency


def test_tie_between_it_companies_goes_to_the_larger_one():
    big = {**registry_item("900000021", "EXEMPLE LOGICIELS"), "nombre_etablissements_ouverts": 12}
    small = {**registry_item("900000022", "EXEMPLE LOGICIELS", **THIONVILLE), "nombre_etablissements_ouverts": 1}
    assert siren_match.pick(_group("Exemple Logiciels"), ZONE, [big, small]).siren == "900000021"
    assert siren_match.pick(_group("Exemple Logiciels"), ZONE, [{**small, "tranche_effectif_salarie": "22"},
                                                                {**big, "nombre_etablissements_ouverts": 2}]
                            ).siren == "900000022"  # 100-199 employees against 10-19
    unknown = [registry_item("900000021", "EXEMPLE LOGICIELS"), registry_item("900000022", "EXEMPLE LOGICIELS",
                                                                              **THIONVILLE)]
    assert siren_match.pick(_group("Exemple Logiciels"), ZONE, unknown) is None  # nothing tells them apart


def test_tie_breaks_never_decide_on_unknown_or_close_values():
    base = Traits(tier=IT, name=100.0)
    assert compare(replace(base, establishments=12), base) == 0  # unknown count
    assert compare(replace(base, establishments=3), replace(base, establishments=2)) == 0
    assert compare(replace(base, establishments=4), replace(base, establishments=2)) == 1
    assert compare(replace(base, tranche=6), replace(base, tranche=5)) == 0
    assert compare(replace(base, tranche=8), replace(base, tranche=5)) == 1
    assert compare(base, replace(base, name=97.0)) == 0
    assert compare(base, replace(base, tier=HOLDING)) == 1
    assert compare(replace(base, hq_in_region=True), base) == 1
    near = replace(base, name=88.0, sibling=True, regional=True, hq_km=130.0)
    far = replace(base, sibling=True, hq_km=280.0)
    assert compare(near, far) == 1  # a group sibling closer to the offer beats the exact name
    assert compare(replace(near, sibling=False), far) == -1  # not a sibling: the name decides
    assert winner([base, base]) is None


# --------------------------------------------------------------------------- acronyms, regional siblings, common names


def test_acronym_with_many_namesakes_goes_to_its_sigle_with_the_most_establishments():
    match = _remote("KZB", "registry_acronym.json")
    assert match.siren == "900800002" and match.remote  # « SOCIETE POUR L'INFORMATIQUE KZB (KZB) », 30 establishments
    assert match.place["siret"] == "90080000200018" and match.notes == (NO_AGENCY,)
    data = fx("registry_acronym.json")
    plain = [{**i, "sigle": None, "nom_complet": i["nom_raison_sociale"]} for i in data["results"]
             if i["siren"] != "900800002"]  # « SAS K.Z.B. » no longer shows KZB as its sigle
    assert siren_remote.decide_remote(_group("KZB"), ZONE, plain, data["total_results"]) is None


def test_regional_sibling_closer_to_the_offer_wins_over_the_literal_qualifier():
    match = _remote("Kalido Nord", "registry_regional.json")
    assert match.siren == "900700003"  # KALIDO EST (Strasbourg area) for an offer in Metz, not KALIDO NORD (Lille)
    assert match.notes == (NO_AGENCY, "Entité du groupe la plus proche de l'offre : KALIDO EST (Schiltigheim) "
                                      "plutôt que KALIDO NORD (Lille)")
    lille = _group("Kalido Nord", department="59", postal_code="59000", city="Lille", latitude=50.63, longitude=3.06)
    assert siren_match.pick(lille, ZONE, fx("registry_regional.json")["results"]).siren == "900700002"  # its own


def test_regional_entity_preferred_over_the_national_one_closer_to_the_offer():
    holding, north, east = fx("registry_regional.json")["results"]
    national = {**holding, "activite_principale": "62.02A", "nombre_etablissements_ouverts": 40,
                "tranche_effectif_salarie": "41", "categorie_entreprise": "ETI"}
    match = siren_remote.decide_remote(_group("Kalido"), ZONE, [national, north, east])
    assert match.siren == "900700003" and match.notes[-1].endswith("KALIDO EST (Schiltigheim) plutôt que "
                                                                   "KALIDO (Paris)")


def test_common_name_needs_a_local_establishment_or_its_sigle():
    data = fx("registry_common.json")  # five « EXEMPLE SERVICES » in five activities, 143 results
    items, total = data["results"], data["total_results"]
    assert siren_remote.decide_remote(_group("Exemple Services"), ZONE, items, total) is None
    assert siren_remote.decide_remote(_group("Exemple Services"), ZONE, items[:3], total) is None
    assert siren_remote.decide_remote(_group("Exemple Services"), ZONE, items[:2], 2).siren == "901000001"
    metz = {"siret": "90100000100099", "code_postal": "57000", "libelle_commune": "METZ", "latitude": "49.1190",
            "longitude": "6.1760", "etat_administratif": "A", "adresse": "1 RUE DE L'EXEMPLE 57000 METZ"}
    local = {**items[0], "matching_etablissements": [metz]}
    assert siren_match.pick(_group("Exemple Services"), ZONE, [local, *items[1:]]).siren == "901000001"


# --------------------------------------------------------------------------- resolution, storage, priority


@respx.mock
def test_it_company_without_local_agency_works_where_the_offer_is(ctx):
    ctx.repo.upsert_job(offer("1", "Vexlink", "Développeur Java (H/F)"))
    route = respx.get(SEARCH_URL).mock(side_effect=_answer({}, {"Vexlink": fx("registry_holding.json")}))
    [company] = hiring.hiring_companies(ctx, LATER)
    assert company.siren == "900600002" and company.address == "3 ESPLANADE DE L'EXEMPLE 92800 PUTEAUX"
    assert company.raw["siret"] == "90060000200013"  # the head office
    assert (company.city, company.postal_code, company.latitude, company.longitude) == ("Metz", "57000", 49.12, 6.18)
    assert company.raw["agency"] == "absente" and company.raw["agency_notes"] == [NO_AGENCY]
    assert [c.request.url.params["q"] for c in route.calls] == ["Vexlink", "Vexlink"]
    entry = _kv(ctx, "Vexlink")
    assert entry["strategy"] == "siege" and entry["place"]["agency"] == "absente"
    store_candidates(ctx, [company], LATER)
    stored = ctx.repo.get_company(company.id)
    assert (stored.city, stored.address, stored.raw["agency"]) == ("Metz", company.address, "absente")
    [again] = hiring.hiring_companies(ctx, LATER)
    assert route.call_count == 2 and again.city == "Metz" and again.raw["agency_notes"] == [NO_AGENCY]
    ready = stored.model_copy(update={"status": "enriched", "contacts": [
        ContactEmail(email="recrutement@vexlink.example", confidence=0.9, kind="recruitment")]})
    _, reasons = company_priority(ready, ctx.profile, ctx.repo)
    assert NO_AGENCY in reasons and any(r.startswith("À 0 km de Metz") for r in reasons)


@respx.mock
def test_a_local_it_namesake_forbids_looking_further(ctx):
    ctx.repo.upsert_job(offer("1", "Exemple Digital", "Développeur web"))
    remote = {**registry_item("900000041", "EXEMPLE DIGITAL", cp="75011", city="PARIS", lat=48.86, lon=2.38),
              "matching_etablissements": [], "nombre_etablissements_ouverts": 30}
    respx.get(SEARCH_URL).mock(side_effect=_answer({"Exemple Digital": fx("registry_ambiguous.json")},
                                                   {"Exemple Digital": {"results": [remote], "total_results": 1}}))
    assert hiring.hiring_companies(ctx, LATER) == []  # two local companies answer: never the one from Paris
    assert _kv(ctx, "Exemple Digital")["tried"] == ["departement", "code_postal", "sans_lieu"]


def test_a_real_local_establishment_wins_over_the_offer_place():
    remote = companies.company_from_result(registry_item("900000031", "Exemple", cp="75008", city="PARIS",
                                                         lat=48.87, lon=2.31), set())
    remote = remote.model_copy(update={"city": "Metz", "postal_code": "57000", "latitude": 49.12, "longitude": 6.18,
                                       "raw": {**remote.raw, LOCAL_KEY: True, "agency": "absente",
                                               "agency_notes": [NO_AGENCY]}})
    local = companies.company_from_result(registry_item("900000031", "Exemple", cp="57070", lat=49.10, lon=6.22),
                                          set())
    local = local.model_copy(update={"raw": {**local.raw, LOCAL_KEY: True}})
    for merged in (merge_company(local, remote), merge_company(remote, local)):
        assert (merged.postal_code, merged.latitude) == ("57070", 49.10)
        assert "agency" not in merged.raw and "agency_notes" not in merged.raw
