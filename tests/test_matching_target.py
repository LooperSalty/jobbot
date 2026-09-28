"""matching v2 rules: per-country / per-location contracts, relocation, salary floor (no network)."""

from __future__ import annotations

import pytest

from jobbot.config import LocationPref
from jobbot.matching import target_rules as tr
from jobbot.matching.match import match_job
from jobbot.models import ContractType

NO_COORDS = dict(postal_code=None, latitude=None, longitude=None)
PARIS = dict(location="Paris (75)", postal_code="75002", latitude=48.8566, longitude=2.3522)
STRASBOURG = dict(location="Strasbourg (67)", postal_code="67000", latitude=48.5734, longitude=7.7521)
CDI_DESC = "CDI. Stack : TypeScript, React, Next.js, Node.js, PostgreSQL, Docker, Git. Débutant accepté."


def with_target(profile, **updates):
    return profile.model_copy(update={"target": profile.target.model_copy(update=updates)})


def cdi(make_job, **overrides):
    base = dict(external_id="cdi-1", title="Développeur Full-Stack React / Node.js (H/F)",
                contract_type=ContractType.CDI, description=CDI_DESC)
    return make_job(**{**base, **overrides})


def locations(profile, **per_city):
    """Profile locations with some fields replaced, per city (e.g. Nancy={"contract_types": [...]})."""
    return [loc.model_copy(update=per_city.get(loc.city, {})) for loc in profile.target.locations]


# --------------------------------------------------------------------------- country of an offer


@pytest.mark.parametrize(("location", "postal", "raw", "expected"), [
    ("Luxembourg", None, {}, "LU"),
    ("Esch-sur-Alzette, Luxembourg", "4361", {}, "LU"),
    ("Kirchberg, LU", None, {}, "LU"),
    ("Arlon, Belgique", None, {}, "BE"),
    ("Sarrebruck, Allemagne", None, {}, "DE"),
    ("99 - ESPAGNE", None, {}, "ES"),
    ("99 - Groenland", None, {}, "ZZ"),
    ("Metz, Grand Est, France", None, {}, "FR"),
    ("57 - METZ", None, {}, "FR"),
    ("Talange", "57525", {}, "FR"),
    ("Remote", None, {}, None),
    ("", None, {"office_country": "LU"}, "LU"),
])
def test_offer_country(make_job, location, postal, raw, expected):
    job = make_job(location=location, postal_code=postal, latitude=None, longitude=None, raw=raw)
    assert tr.offer_country(job) == expected


# --------------------------------------------------------------------------- Luxembourg = CDI only


def test_alternance_in_luxembourg_is_filtered_with_a_clear_reason(profile, make_job):
    job = make_job(location="Luxembourg", **NO_COORDS)  # the default job is an alternance
    score, reasons = match_job(job, profile)
    assert score == 0 and len(reasons) == 1
    assert reasons[0] == "Écartée : offre en alternance au Luxembourg — contrats recherchés dans ce pays : CDI"


def test_luxembourg_rule_applies_even_inside_a_french_radius(profile, make_job):
    wide = with_target(profile, locations=locations(profile, Metz={"radius_km": 80}))
    job = make_job(location="Luxembourg", latitude=49.6116, longitude=6.1319, postal_code=None)
    assert "au Luxembourg" in match_job(job, wide)[1][0]


def test_cdi_in_luxembourg_within_radius_is_kept(profile, make_job):
    esch = cdi(make_job, location="Esch-sur-Alzette, Luxembourg", postal_code="4361",
               latitude=49.500956, longitude=5.98)
    score, reasons = match_job(esch, profile)
    assert score >= 60 and any("km de Luxembourg" in r for r in reasons)
    unknown = cdi(make_job, location="Bascharage, Luxembourg", **NO_COORDS)  # not in geo.KNOWN_PLACES
    score, reasons = match_job(unknown, profile)
    assert score >= 45 and any(r.startswith("Au Luxembourg — lieu précis à vérifier") for r in reasons)
    kirchberg = cdi(make_job, location="Kirchberg, Luxembourg", **NO_COORDS)  # known offline: distance checked
    assert any("km de Luxembourg" in r for r in match_job(kirchberg, profile)[1])


def test_cdi_far_away_in_luxembourg_is_still_too_far(profile, make_job):
    clervaux = cdi(make_job, location="Clervaux, Luxembourg", postal_code=None, latitude=50.0547, longitude=6.0314)
    score, reasons = match_job(clervaux, profile)
    assert score == 0 and "trop loin" in reasons[0]


def test_foreign_offer_without_search_area_in_that_country(profile, make_job):
    belgium = cdi(make_job, location="Arlon, Belgique", **NO_COORDS)
    assert match_job(belgium, profile)[1] == ["Écartée : offre en Belgique — aucune zone de recherche dans ce pays"]
    no_lu = with_target(profile, locations=[loc for loc in profile.target.locations if loc.country != "LU"])
    lux = cdi(make_job, location="Luxembourg", **NO_COORDS)
    assert "aucune zone de recherche" in match_job(lux, no_lu)[1][0]
    unknown = cdi(make_job, location="99 - Groenland", **NO_COORDS)
    assert "à l'étranger (99 - Groenland)" in match_job(unknown, profile)[1][0]


def test_old_profile_without_country_field_still_counts_luxembourg(profile, make_job):
    legacy = with_target(profile, locations=locations(profile, Luxembourg={"country": "FR"}))
    job = make_job(location="Luxembourg", **NO_COORDS)
    assert "au Luxembourg" in match_job(job, legacy)[1][0]


def test_unknown_contract_in_luxembourg_is_not_filtered_on_the_contract(profile, make_job):
    job = cdi(make_job, title="Développeur React", contract_type=ContractType.OTHER, description="React, Node.js.",
              location="Luxembourg", **NO_COORDS)
    assert match_job(job, profile)[0] > 0


# --------------------------------------------------------------------------- per-location contracts


def test_contract_only_wanted_in_luxembourg(profile, make_job):
    lu_cdd = with_target(profile, locations=locations(
        profile, Luxembourg={"contract_types": [ContractType.CDI, ContractType.CDD]}))
    cdd_lu = cdi(make_job, contract_type=ContractType.CDD, location="Luxembourg", **NO_COORDS)
    assert match_job(cdd_lu, lu_cdd)[0] > 0
    cdd_metz = cdi(make_job, contract_type=ContractType.CDD)
    assert match_job(cdd_metz, lu_cdd)[1] == ["Écartée : contrat CDD recherché uniquement au Luxembourg"]
    assert "non recherché (CDD)" in match_job(cdd_metz, profile)[1][0]
    stage = make_job(title="Stage développeur web", contract_type=ContractType.STAGE, location="Luxembourg",
                     **NO_COORDS)
    assert "non recherché (stage)" in match_job(stage, lu_cdd)[1][0]


def test_french_zone_restricted_to_some_contracts(profile, make_job):
    nancy_cdi = with_target(profile, locations=locations(profile, Nancy={"contract_types": [ContractType.CDI]}))
    nancy = dict(location="Nancy (54)", postal_code="54000", latitude=48.6921, longitude=6.1844)
    score, reasons = match_job(make_job(**nancy), nancy_cdi)
    assert score == 0
    assert reasons == [
        "Écartée : offre en alternance dans la zone de Nancy — contrats recherchés dans cette zone : CDI"]
    assert match_job(cdi(make_job, **nancy), nancy_cdi)[0] > 45
    assert match_job(make_job(), nancy_cdi)[0] > 45  # alternance in Metz is still fine
    by_department = make_job(location="Vandœuvre (54)", **NO_COORDS)
    assert "zone de Nancy" in match_job(by_department, nancy_cdi)[1][0]


def test_empty_target_contracts_accept_every_contract(profile, make_job):
    anything = with_target(profile, contract_types=[])
    assert match_job(cdi(make_job, contract_type=ContractType.CDD), anything)[0] > 0


# --------------------------------------------------------------------------- alternance only

STACK_DESC = "Stack : TypeScript, React, Next.js, Node.js, PostgreSQL, Docker, Git. " * 7  # full text, no contract


def alternance_only(profile):
    return with_target(profile, contract_types=[ContractType.ALTERNANCE],
                       locations=locations(profile, Luxembourg={"contract_types": None}))


def unknown_contract(make_job, **overrides):
    base = dict(source="linkedin", title="Développeur Full-Stack React / Node.js (H/F)",
                contract_type=ContractType.OTHER, description=STACK_DESC)
    return make_job(**{**base, **overrides})


def test_alternance_only_filters_an_unknown_contract_read_in_full(profile, make_job):
    score, reasons = match_job(unknown_contract(make_job), alternance_only(profile))
    assert score == 0 and reasons == ["Écartée : contrat non précisé (seule l'alternance est recherchée)"]


def test_alternance_only_keeps_what_says_alternance(profile, make_job):
    strict = alternance_only(profile)
    assert match_job(make_job(), strict)[0] > 45
    in_text = unknown_contract(make_job, description=STACK_DESC + " Poste à pourvoir en alternance, 24 mois.")
    assert match_job(in_text, strict)[0] > 45
    in_title = unknown_contract(make_job, title="Alternant développeur React (H/F)")
    assert match_job(in_title, strict)[0] > 45


@pytest.mark.parametrize("wording", [
    "Type de contrat : Alternance (24 mois)", "Contrat : Apprentissage", "Poste à pourvoir en apprentissage",
    "Alternance 3 semaines entreprise / 1 semaine école", "#alternance", "Contrat pro de 12 mois",
])
def test_alternance_only_keeps_any_loose_mention_of_alternance(profile, make_job, wording):
    job = unknown_contract(make_job, source="indeed", description=f"{STACK_DESC} {wording}")
    assert match_job(job, alternance_only(profile))[0] > 45


def test_alternance_only_waits_for_the_full_text_of_a_search_card(profile, make_job):
    card = unknown_contract(make_job, description="React, Node.js.")
    assert match_job(card, alternance_only(profile))[0] > 0  # judged again once the offer is read in full
    snippet = unknown_contract(make_job, source="adzuna", raw={"description_truncated": True})
    assert match_job(snippet, alternance_only(profile))[0] > 0  # cut by its source: « alternance » may follow
    pending = unknown_contract(make_job, raw={"needs_details": True})
    assert match_job(pending, alternance_only(profile))[0] > 0


def test_alternance_only_trusts_the_school_relayed_offers(profile, make_job):
    relayed = unknown_contract(make_job, source="classroom", raw={"relayed_by_school": True})
    assert match_job(relayed, alternance_only(profile))[0] > 0


def test_alternance_only_drops_cdi_everywhere(profile, make_job):
    strict = alternance_only(profile)
    assert "non recherché (CDI)" in match_job(cdi(make_job), strict)[1][0]
    esch = cdi(make_job, location="Esch-sur-Alzette, Luxembourg", postal_code="4361",
               latitude=49.500956, longitude=5.98)
    assert "non recherché (CDI)" in match_job(esch, strict)[1][0]


def test_unknown_contract_still_kept_when_other_contracts_are_wanted(profile, make_job):
    assert match_job(unknown_contract(make_job), profile)[0] > 0  # alternance + CDI searched


# --------------------------------------------------------------------------- relocation


def test_relocation_anywhere_in_france(profile, make_job):
    job = cdi(make_job, **PARIS)
    assert "trop loin" in match_job(job, profile)[1][0]
    mobile = with_target(profile, willing_to_relocate=True)
    score, reasons = match_job(job, mobile)
    assert score > 45 and "Hors des zones de recherche, mais déménagement accepté (toute la France)" in reasons
    linkedin = cdi(make_job, location="Lyon, Auvergne-Rhône-Alpes, France", **NO_COORDS)
    assert match_job(linkedin, mobile)[0] > 0


def test_relocation_limited_to_regions(profile, make_job):
    grand_est = with_target(profile, willing_to_relocate=True, relocation_regions=["Grand Est"])
    score, reasons = match_job(cdi(make_job, **STRASBOURG), grand_est)
    assert score > 45 and any("déménagement accepté (Grand Est)" in r for r in reasons)
    assert "trop loin" in match_job(cdi(make_job, **PARIS), grand_est)[1][0]
    alsace = with_target(profile, willing_to_relocate=True, relocation_regions=["alsace", "75"])
    assert match_job(cdi(make_job, **STRASBOURG), alsace)[0] > 0
    assert match_job(cdi(make_job, **PARIS), alsace)[0] > 0
    reims = cdi(make_job, location="Reims (51)", postal_code="51100", latitude=49.2583, longitude=4.0317)
    assert match_job(reims, alsace)[0] == 0
    ara = with_target(profile, willing_to_relocate=True, relocation_regions=["Auvergne-Rhône-Alpes"])
    linkedin = cdi(make_job, location="Lyon, Auvergne-Rhône-Alpes, France", **NO_COORDS)
    assert any("Auvergne-Rhône-Alpes" in r for r in match_job(linkedin, ara)[1])


def test_relocation_never_applies_abroad_or_without_french_evidence(profile, make_job):
    mobile = with_target(profile, willing_to_relocate=True)
    geneva = cdi(make_job, location="Genève", postal_code=None, latitude=46.2044, longitude=6.1432)
    assert "trop loin" in match_job(geneva, mobile)[1][0]
    lux_alt = make_job(location="Luxembourg", **NO_COORDS)
    assert "au Luxembourg" in match_job(lux_alt, mobile)[1][0]
    far_lu = cdi(make_job, location="Clervaux, Luxembourg", postal_code=None, latitude=50.0547, longitude=6.0314)
    assert "trop loin" in match_job(far_lu, mobile)[1][0]


def test_relocation_does_not_override_a_zone_contract_restriction(profile, make_job):
    target = dict(willing_to_relocate=True,
                  locations=locations(profile, Nancy={"contract_types": [ContractType.CDI]}))
    nancy = make_job(location="Nancy (54)", postal_code="54000", latitude=48.6921, longitude=6.1844)
    assert "zone de Nancy" in match_job(nancy, with_target(profile, **target))[1][0]


def test_region_helpers():
    assert tr.region_of("57") == "Grand Est" and tr.region_of("2A") == "Corse" and tr.region_of("99") is None
    assert tr.region_of(None) is None and tr.region_of("974") == "La Réunion"
    accepted = tr.accepted_departments(["Lorraine", "sud", "2a", "Moselle", "inconnu"])
    assert accepted.keys() >= {"54", "57", "13", "2A"} and accepted["54"] == "Lorraine" and accepted["13"] == "sud"
    assert "67" not in tr.accepted_departments(["Lorraine"])


# --------------------------------------------------------------------------- salary floor


@pytest.mark.parametrize(("text", "expected"), [
    ("Annuel de 28000.0 Euros à 30000.0 Euros sur 12.0 mois", (28000, 30000)),
    ("Mensuel de 2000.0 Euros sur 12.0 mois", (24000, 24000)),
    ("Mensuel de 2800 Euros sur 13 mois", (36400, 36400)),
    ("Mensuel de 1801.80 Euros — Selon profil", (21622, 21622)),
    ("Horaire de 11.88 Euros sur 12 mois", (19091, 19091)),
    ("30 000 – 35 000 € / an", (30000, 35000)),
    ("40 000,00 € - 50 000,00 €/an", (40000, 50000)),
    ("35 - 43 k€ brut annuel", (35000, 43000)),
    ("€40K/yr - €50K/yr", (40000, 50000)),
    ("45k-55k € / an ; 400-500 € / jour", (45000, 55000)),
    ("2 000 € / mois", (24000, 24000)),
    ("38000", (38000, 38000)),
    ("2500", (30000, 30000)),
    ("Selon profil", None),
    ("$50,000 - $60,000 a year", None),
    ("55 000 CHF / an", None),
    ("", None),
    (None, None),
    ("Mensuel de 5 Euros", None),
])
def test_annual_salary(text, expected):
    result = tr.annual_salary(text)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=1)


def test_salary_floor(profile, make_job):
    floor = with_target(profile, min_salary_eur=32000)
    low = cdi(make_job, salary="Annuel de 28000.0 Euros à 30000.0 Euros sur 12.0 mois")
    score, reasons = match_job(low, floor)
    assert score == 0
    assert reasons == ["Écartée : salaire annoncé inférieur au minimum souhaité (30 000 € par an < 32 000 €)"]
    assert match_job(cdi(make_job, salary="30 000 – 35 000 € / an"), floor)[0] > 0  # the range reaches the floor
    assert match_job(cdi(make_job, salary="Selon profil"), floor)[0] > 0  # unparsable → never filters
    assert match_job(cdi(make_job, salary="$20,000 a year"), floor)[0] > 0
    assert match_job(make_job(salary="Mensuel de 1000.0 Euros"), floor)[0] > 0  # never for an alternance
    other = cdi(make_job, title="Développeur React", contract_type=ContractType.OTHER, description="React",
                salary="Mensuel de 1400 Euros")
    assert match_job(other, floor)[0] > 0  # unknown contract (maybe an alternance) → not filtered
    assert match_job(low, profile)[0] > 0  # no floor configured
    assert tr.salary_reason(low, with_target(profile, min_salary_eur=0), ContractType.CDI) is None


def test_location_pref_country_helper():
    from jobbot.sources.countries import country_name, is_foreign, location_country

    assert location_country(None) == "FR" and not is_foreign(None)
    assert location_country(LocationPref(city="Luxembourg")) == "LU"
    assert location_country(LocationPref(city="Arlon", country="be")) == "BE" and is_foreign(
        LocationPref(city="Arlon", country="BE"))
    assert location_country(LocationPref(city="Metz", country=" ")) == "FR"
    assert country_name("lu") == "Luxembourg" and country_name("XX") == "XX"
