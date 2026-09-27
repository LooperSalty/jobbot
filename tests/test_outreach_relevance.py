"""outreach.relevance — the single « is this employer plausible for a developer apprentice? » gate (pure rules)."""

from __future__ import annotations

import pytest

from jobbot import clock
from jobbot.models import Company
from jobbot.outreach.relevance import (
    IRRELEVANT_STATUS,
    REASON_KEY,
    headcount_min,
    is_relevant_employer,
    report_lines,
    split_relevant,
)
from jobbot.sources.lba_mapping import recruiter_detail_facts


def _co(name: str = "Entreprise", **fields) -> Company:
    return Company(**{"id": f"id-{name}", "name": name, "found_at": clock.now(), **fields})


def _with_nafs(profile, codes: list[str]):
    return profile.model_copy(update={"target": profile.target.model_copy(update={"naf_codes": codes})})


# --------------------------------------------------------------------------- irrelevant employers


@pytest.mark.parametrize(("company", "reason"), [
    (_co("TRAVAUX EXEMPLE TP", naf_code="42.11Z", headcount_range="10 à 19 salariés", raw={"tranche_effectif_salarie": "11"}),
     "secteur hors numérique (NAF 42.11Z, 10 à 19 salariés)"),
    (_co("CLIM EXEMPLE", naf_code="43.22B", headcount_range="3-5"),
     "secteur hors numérique (NAF 43.22B, 3-5 salariés)"),
    (_co("RESEAUX ELECTRIQUES EXEMPLE", naf_code="43.21A", headcount_range="100-199"),
     "secteur hors numérique (NAF 43.21A, 100-199 salariés)"),
    (_co("INDUS EXEMPLE SAS", naf_code="25.62B", raw={"tranche_effectif_salarie": "12"}),
     "secteur hors numérique (NAF 25.62B, 20 à 49 salariés)"),
    (_co("Maçonnerie Dupont", naf_label="Travaux de maçonnerie générale et gros œuvre de bâtiment"),
     "secteur hors numérique (Travaux de maçonnerie générale et gros œuvre de bâtiment, effectif inconnu)"),
    (_co("Boulangerie Martin"), "secteur inconnu et nom sans rapport avec le numérique"),
])
def test_out_of_sector_small_companies_are_rejected_with_a_reason(profile, company, reason):
    assert is_relevant_employer(company, profile) == (False, reason)


@pytest.mark.parametrize("company", [
    _co("BATI EXEMPLE", naf_code="42.11Z", headcount_range="1 000 à 1 999 salariés"),  # large, but public works
    _co("CLIMATECH", naf_code="43.22B", headcount_range="6 à 9 salariés"),  # « tech » alone proves nothing
    _co("CONSEIL GESTION EST", naf_code="70.22Z", headcount_range="10 à 19 salariés"),
    _co("DEV COMMERCIAL", naf_label="Activités des sièges sociaux",
        raw={"rome_labels": ["Développeur / Développeuse commercial", "Chargé / Chargée d'affaires"]}),
])
def test_size_or_loose_words_never_rescue_a_field_or_small_non_it_company(profile, company):
    relevant, reason = is_relevant_employer(company, profile)
    assert not relevant and reason.startswith("secteur hors numérique")


# --------------------------------------------------------------------------- relevant employers


@pytest.mark.parametrize(("company", "start"), [
    (_co("SNCF VOYAGEURS", naf_code="49.10Z", raw={"tranche_effectif_salarie": "53"}), "grande structure"),
    (_co("CHAUSSEA", naf_code="47.72A", headcount_range="2 000 à 4 999 salariés"), "grande structure"),
    (_co("CHU EST", naf_code="86.10Z", headcount_range="250-499"), "grande structure"),
    (_co("GROUPE ETI", naf_code="64.19Z", raw={"tranche_effectif_salarie": "NN", "categorie_entreprise": "ETI"}),
     "grande structure"),
    (_co("TELCO", naf_code="61.10Z"), "secteur numérique (NAF 61.10Z)"),
    (_co("LABO R&D", naf_code="72.19Z"), "secteur numérique (NAF 72.19Z)"),
    (_co("CONSEIL SI", naf_code="70.22Z", headcount_range="50 à 99 salariés"), "conseil / ingénierie"),
    (_co("BUREAU SYNTEC", naf_code="71.12B", raw={"idcc": ["1486"]}), "conseil / ingénierie"),
    (_co("ATIXYS", naf_label="PROGRAMMATION INFORMATIQUE"), "secteur numérique (PROGRAMMATION INFORMATIQUE)"),
    (_co("HOLDING", naf_label="Activités des sièges sociaux",
         raw={"rome_labels": ["Responsable Commercial", "Développeur / Développeuse web"]}), "La Bonne Alternance"),
    (_co("Solutions Informatiques de l'Est"), "nom ou activité"),
    (_co("ACME IT"), "nom ou activité"),
    (_co("Studio Nova", description="Agence web : sites et applications mobiles sur mesure."), "nom ou activité"),
    (_co("LORRAINE INFORMATIQUE", naf_code="47.41Z", headcount_range="3 à 5 salariés"), "nom ou activité"),
])
def test_it_employers_and_large_organisations_are_accepted(profile, company, start):
    relevant, reason = is_relevant_employer(company, profile)
    assert relevant and reason.startswith(start), reason


def test_profile_naf_codes_are_targeted_exactly_or_by_prefix(profile):
    assert is_relevant_employer(_co(naf_code="73.11Z"), profile) == (True, "NAF ciblé (73.11Z)")  # fixture profile
    assert is_relevant_employer(_co(naf_code="62.01Z"), _with_nafs(profile, [])) == (
        True, "secteur numérique (NAF 62.01Z)")
    engineering = _co(naf_code="71.12B", headcount_range="10 à 19 salariés")
    assert not is_relevant_employer(engineering, profile)[0]
    assert is_relevant_employer(engineering, _with_nafs(profile, ["71"])) == (True, "NAF ciblé (71.12B)")


@pytest.mark.parametrize(("fields", "expected"), [
    ({"raw": {"tranche_effectif_salarie": "32"}}, 250),
    ({"raw": {"tranche_effectif_salarie": "NN"}, "headcount_range": "effectif non renseigné"}, None),
    ({"headcount_range": "1 000 à 1 999 salariés"}, 1000),
    ({"headcount_range": "10 000 salariés et plus"}, 10000),
    ({"headcount_range": "250-499"}, 250),
    ({"headcount_range": "0 salarié"}, 0),
    ({}, None),
])
def test_headcount_min(fields, expected):
    assert headcount_min(_co(**fields)) == expected


# --------------------------------------------------------------------------- bookkeeping & LBA detail


def test_split_sets_aside_waiting_companies_only(ctx):
    waiting = _co("TRAVAUX EXEMPLE TP", naf_code="42.11Z", status="enriched", raw={"outreach_score": 7})
    contacted = _co("CLIM EXEMPLE", naf_code="43.22B", status="contacted")
    fine = _co("EFLUID", naf_code="58.29C", status="enriched")
    for company in (waiting, contacted, fine):
        ctx.repo.upsert_company(company)

    relevant, off = split_relevant(ctx.repo, ctx.profile, [waiting, contacted, fine])

    assert [c.name for c in relevant] == ["EFLUID"] and [c.name for c, _ in off] == ["TRAVAUX EXEMPLE TP", "CLIM EXEMPLE"]
    stored = ctx.repo.get_company(waiting.id)
    assert stored.status == IRRELEVANT_STATUS and stored.raw["outreach_score"] == 7
    assert stored.raw[REASON_KEY].startswith("secteur hors numérique (NAF 42.11Z")
    assert ctx.repo.get_company(contacted.id).status == "contacted"  # history is never rewritten
    assert report_lines(off)[0].startswith("TRAVAUX EXEMPLE TP : hors cible, écartée (secteur hors numérique")
    many = [(_co(f"TP {n}"), "secteur inconnu") for n in range(12)]
    assert len(report_lines(many)) == 11 and report_lines(many)[-1] == "… et 2 autre(s) entreprise(s) hors cible écartée(s)"


def test_recruiter_detail_fills_missing_sector_and_size_only():
    site_hit = _co("ATIXYS", naf_label="PROGRAMMATION INFORMATIQUE")
    detail = {"nafs": [{"code": "42.11Z", "label": "Construction de routes et autoroutes"}],
              "company": {"size": "10-19"}}
    filled = recruiter_detail_facts(site_hit, detail)
    assert (filled.naf_code, filled.naf_label, filled.headcount_range) == (
        "42.11Z", "Construction de routes et autoroutes", "10-19")
    registry = _co("EFLUID", naf_code="58.29C", headcount_range="250 à 499 salariés")
    assert recruiter_detail_facts(registry, detail) is registry
    assert recruiter_detail_facts(site_hit, {}) is site_hit
