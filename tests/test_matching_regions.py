"""Region-level / big-city / country-only offer places (real LinkedIn cards seen on the server), no network.

Fictitious candidate (tests/fixtures/profile.yaml): Metz 40 km, Nancy 25 km, Luxembourg (CDI only), remote_ok,
willing_to_relocate = false.
"""

from __future__ import annotations

import pytest

from jobbot import clock
from jobbot.config import LocationPref
from jobbot.matching import geo, regions
from jobbot.matching import match as match_mod
from jobbot.matching import target_rules as tr
from jobbot.matching.match import THIN_REASON, match_job
from jobbot.models import ContractType

NO_PLACE = {"postal_code": None, "latitude": None, "longitude": None}
# ≥ 400 characters, no technical skill, no remote / contract / experience cue
NEUTRAL = ("Au sein d'une équipe à taille humaine, tu participeras à la vie du produit, des ateliers avec les "
           "utilisateurs jusqu'à la mise à disposition des nouvelles fonctionnalités. Nous privilégions la "
           "bienveillance, l'entraide et le partage de connaissances entre collègues. Les locaux sont faciles d'accès "
           "et un restaurant d'entreprise est à disposition. Le processus de recrutement comprend un échange avec "
           "l'équipe puis un entretien avec la direction.")
STACK = " Stack : React, TypeScript, Node.js, Next.js, PostgreSQL, Docker, Git."
IDF_EXCLUDED = "Écartée : trop loin : Île-de-France (hors de tes zones)"


def with_target(profile, **updates):
    return profile.model_copy(update={"target": profile.target.model_copy(update=updates)})


def card(make_job, title, location, description="", **overrides):
    """A LinkedIn guest search card: no coordinates, no postal code, contract unknown, remote unknown."""
    base = dict(source="linkedin", external_id=overrides.pop("external_id", "4300000001"), title=title,
                company="Studio Exemple", location=location, description=description,
                contract_type=ContractType.OTHER, remote=None, published_at=clock.now(), **NO_PLACE)
    return make_job(**{**base, **overrides})


# --------------------------------------------------------------------------- whole regions outside the zones


@pytest.mark.parametrize("title", ["Développeur Front React Audiovisuel H/F", "Ingénieur Full Stack"])
def test_real_ile_de_france_cards_are_filtered(profile, make_job, title):
    score, reasons = match_job(card(make_job, title, "Île-de-France, France"), profile)
    assert score == 0
    assert reasons == [IDF_EXCLUDED]


@pytest.mark.parametrize(("location", "label"), [
    ("Ile de France", "Ile de France"),
    ("Région parisienne", "Région parisienne"),
    ("Paris et périphérie", "Paris et périphérie"),
    ("Greater Paris Metropolitan Region", "Greater Paris Metropolitan Region"),
    ("Région Île-de-France, France", "Région Île-de-France"),
    ("Hauts-de-France, France", "Hauts-de-France"),
    ("Auvergne-Rhône-Alpes, France", "Auvergne-Rhône-Alpes"),
    ("Occitanie", "Occitanie"),
    ("Provence-Alpes-Côte d'Azur, France", "Provence-Alpes-Côte d'Azur"),
    ("Nouvelle-Aquitaine", "Nouvelle-Aquitaine"),
    ("Bretagne, France", "Bretagne"),
])
def test_regions_outside_every_zone_are_too_far(profile, make_job, location, label):
    job = card(make_job, "Développeur Full-Stack React", location, description=NEUTRAL + STACK)
    score, reasons = match_job(job, profile)
    assert score == 0
    assert reasons == [f"Écartée : trop loin : {label} (hors de tes zones)"]


@pytest.mark.parametrize(("location", "label", "city"), [
    ("Lyon et périphérie", "Lyon et périphérie", "lyon"),
    ("Grand Lyon", "Grand Lyon", "lyon"),
    ("Région lyonnaise", "Région lyonnaise", "lyon"),
    ("Toulouse Métropole", "Toulouse Métropole", "toulouse"),
    ("Métropole Européenne de Lille, France", "Métropole Européenne de Lille", "lille"),
    ("Strasbourg et périphérie", "Strasbourg et périphérie", "strasbourg"),
    ("Lyon", "Lyon", "lyon"),
    ("Marseille, Provence-Alpes-Côte d'Azur, France", "Marseille, Provence-Alpes-Côte d'Azur", "marseille"),
    ("Rouen, Normandie, France", "Rouen, Normandie", "rouen"),
])
def test_big_city_areas_are_placed_at_the_city(profile, make_job, location, label, city):
    job = card(make_job, "Développeur Full-Stack React", location, description=NEUTRAL + STACK)
    score, reasons = match_job(job, profile)
    assert score == 0
    assert reasons[0].startswith(f"Écartée : trop loin : {label} (hors de tes zones) — ")
    distance = int(reasons[0].split(" — ")[1].split(" km")[0])
    lat, lon = geo.KNOWN_PLACES[city]
    nearest = min(geo.haversine_km(lat, lon, loc.latitude, loc.longitude) for loc in profile.target.locations)
    assert distance >= nearest - 1


def test_far_reasons_share_one_wording(profile, make_job):
    paris = make_job(location="Paris (75)", postal_code="75002", latitude=48.8566, longitude=2.3522)
    reason = match_job(paris, profile)[1][0]
    assert reason.startswith("Écartée : trop loin : Paris (75) (hors de tes zones) — ")
    assert reason.endswith("km de Metz, rayon 40 km")
    villeurbanne = make_job(location="Villeurbanne (69)", **NO_PLACE)
    assert match_job(villeurbanne, profile)[1] == [
        "Écartée : trop loin : Villeurbanne (69) (hors de tes zones) — département 69"]
    hybrid = card(make_job, "Développeur React", "Île-de-France, France",
                  description=NEUTRAL + " Deux jours de télétravail par semaine.")
    assert match_job(hybrid, profile)[1] == [f"{IDF_EXCLUDED} — télétravail partiel seulement"]


# --------------------------------------------------------------------------- remote


def test_full_remote_offer_in_ile_de_france_is_kept(profile, make_job):
    remote = card(make_job, "Développeur Full-Stack React", "Île-de-France, France",
                  description=NEUTRAL + STACK + " Poste en full remote, depuis toute la France.")
    score, reasons = match_job(remote, profile)
    assert score >= 60
    assert "Télétravail complet (Île-de-France)" in reasons
    strict = with_target(profile, remote_ok=False)
    assert match_job(remote, strict)[1] == [IDF_EXCLUDED]


def test_remote_flag_is_trusted_only_from_boards_where_it_means_full_remote(profile, make_job):
    linkedin = card(make_job, "Développeur Full-Stack React", "Île-de-France, France", NEUTRAL + STACK, remote=True)
    assert "Télétravail complet (Île-de-France)" in match_job(linkedin, profile)[1]
    france_travail = card(make_job, "Développeur Full-Stack React", "Île-de-France", NEUTRAL + STACK, remote=True,
                          source="france_travail", external_id="201XKLM")  # its flag also covers hybrid work
    assert match_job(france_travail, profile)[1] == [f"{IDF_EXCLUDED} — télétravail partiel seulement"]


# --------------------------------------------------------------------------- regions containing a zone


@pytest.mark.parametrize(("location", "label"), [
    ("Grand Est, France", "Grand Est"), ("Grand Est", "Grand Est"), ("Région Grand Est", "Région Grand Est"),
    ("Lorraine, France", "Lorraine"),
])
def test_region_containing_a_zone_is_kept_with_a_penalty(profile, make_job, location, label):
    description = NEUTRAL + STACK
    region = card(make_job, "Développeur Full-Stack React", location, description)
    unknown_town = card(make_job, "Développeur Full-Stack React", "Quelque-Part", description)  # same 0.5 location
    score, reasons = match_job(region, profile)
    assert f"Localisation : lieu imprécis ({label}), -10 points" in reasons
    assert score >= 45
    assert match_job(unknown_town, profile)[0] - score == pytest.approx(10.0, abs=0.1)


def test_region_is_not_filtered_when_a_zone_department_is_unknown(profile, make_job):
    odd = LocationPref(city="Hameau Perdu", radius_km=30, latitude=48.9, longitude=2.3)  # no postal code
    target = with_target(profile, locations=[odd])
    job = card(make_job, "Développeur Full-Stack React", "Île-de-France, France", NEUTRAL + STACK)
    assert "Localisation : lieu imprécis (Île-de-France), -10 points" in match_job(job, target)[1]


# --------------------------------------------------------------------------- country only


def test_real_nextjs_card_located_in_france_only(profile, make_job):
    nextjs = card(make_job, "Développeur(se) Fullstack Nextjs", "France")  # server: 85 with « Next.js (1/1) »
    score, reasons = match_job(nextjs, profile)
    assert "Localisation : lieu non précisé (France), -15 points" in reasons
    assert THIN_REASON in reasons
    assert score < 60
    in_metz = card(make_job, "Développeur(se) Fullstack Nextjs", "Metz, Grand Est, France")
    assert match_job(in_metz, profile)[0] - score >= 15


def test_country_only_place_is_fine_when_the_offer_is_full_remote(profile, make_job):
    remote = card(make_job, "Développeur Full-Stack React", "France",
                  NEUTRAL + STACK + " Poste 100 % télétravail.")
    score, reasons = match_job(remote, profile)
    assert "Télétravail complet" in reasons
    assert not any("lieu non précisé" in r for r in reasons)
    flagged = card(make_job, "Développeur Full-Stack React", "France", NEUTRAL + STACK, remote=True)
    assert "Télétravail complet" in match_job(flagged, profile)[1]
    nowhere = card(make_job, "Développeur Full-Stack React", "", NEUTRAL + STACK)
    assert "Localisation : lieu non précisé, -15 points" in match_job(nowhere, profile)[1]
    penalised = match_job(card(make_job, "Développeur Full-Stack React", "France", NEUTRAL + STACK), profile)[0]
    assert score - penalised == pytest.approx(15 + 15 * (0.9 - 0.5), abs=0.1)


# --------------------------------------------------------------------------- still fine


def test_luxembourg_cdi_metz_and_nancy_still_fit(profile, make_job):
    lux = card(make_job, "Développeur Full-Stack (CDI)", "Luxembourg, Luxembourg", NEUTRAL + STACK,
               contract_type=ContractType.CDI)
    score, reasons = match_job(lux, profile)
    assert score >= 60 and "À 0 km de Luxembourg" in reasons
    belval = card(make_job, "Développeur Full-Stack (CDI)", "Belval, Luxembourg", NEUTRAL + STACK,
                  contract_type=ContractType.CDI)
    assert match_job(belval, profile)[0] >= 45
    for place, city in (("Metz, Grand Est, France", "Metz"), ("Nancy, Grand Est, France", "Nancy"),
                        ("Metz Métropole", "Metz"), ("Grand Nancy", "Nancy"), ("Nancy et environs", "Nancy")):
        score, reasons = match_job(card(make_job, "Développeur Full-Stack React", place, NEUTRAL + STACK), profile)
        assert score >= 70, place
        assert f"À 0 km de {city}" in reasons, place


# --------------------------------------------------------------------------- relocation


def test_relocation_regions_for_region_level_offers(profile, make_job):
    job = card(make_job, "Développeur Full-Stack React", "Île-de-France, France", NEUTRAL + STACK)
    anywhere = with_target(profile, willing_to_relocate=True)
    assert "Hors des zones de recherche, mais déménagement accepté (toute la France)" in match_job(job, anywhere)[1]
    idf = with_target(profile, willing_to_relocate=True, relocation_regions=["Île-de-France"])
    score, reasons = match_job(job, idf)
    assert score >= 45 and "Hors des zones de recherche, mais déménagement accepté (Île-de-France)" in reasons
    parisian = card(make_job, "Développeur Full-Stack React", "Région parisienne", NEUTRAL + STACK)
    assert match_job(parisian, with_target(profile, willing_to_relocate=True, relocation_regions=["idf"]))[0] > 0
    grand_est = with_target(profile, willing_to_relocate=True, relocation_regions=["Grand Est"])
    assert match_job(job, grand_est)[1] == [IDF_EXCLUDED]
    paris_only = with_target(profile, willing_to_relocate=True, relocation_regions=["75"])
    assert match_job(job, paris_only)[1] == [IDF_EXCLUDED]  # the whole region is not accepted
    lyon = card(make_job, "Développeur Full-Stack React", "Lyon et périphérie", NEUTRAL + STACK)
    ara = with_target(profile, willing_to_relocate=True, relocation_regions=["Auvergne-Rhône-Alpes"])
    assert "Hors des zones de recherche, mais déménagement accepté (Auvergne-Rhône-Alpes)" in match_job(lyon, ara)[1]


# --------------------------------------------------------------------------- foreign regions, geocoding


@pytest.mark.parametrize(("location", "expected"), [
    ("Bruxelles, Région de Bruxelles-Capitale", "BE"), ("Wallonie", "BE"), ("Saarland", "DE"),
    ("Bade-Wurtemberg, Allemagne", "DE"), ("Suisse romande", "CH"), ("Île-de-France", "FR"),
    ("Lyon et périphérie", "FR"), ("Grand Est", "FR"), ("Sarre-Union (67)", "FR"),
])
def test_offer_country_of_regions(make_job, location, expected):
    assert tr.offer_country(make_job(location=location, **NO_PLACE)) == expected


def test_foreign_region_without_search_area(profile, make_job):
    brussels = card(make_job, "Développeur Full-Stack (CDI)", "Bruxelles, Région de Bruxelles-Capitale",
                    NEUTRAL + STACK, contract_type=ContractType.CDI)
    assert match_job(brussels, profile)[1] == ["Écartée : offre en Belgique — aucune zone de recherche dans ce pays"]


def test_a_region_is_never_geocoded(ctx, make_job, monkeypatch):
    monkeypatch.setattr(match_mod, "geocode", lambda query, client=None: pytest.fail(f"géocodage de {query}"))
    for location in ("Île-de-France, France", "Hauts-de-France", "Paris et périphérie"):
        job = card(make_job, "Développeur React", location)
        assert match_mod._locate(ctx, job) is job


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(("location", "label", "departments", "center"), [
    ("Île-de-France, France", "Île-de-France", regions.REGIONS["Île-de-France"], None),
    ("Paris et périphérie", "Paris et périphérie", regions.REGIONS["Île-de-France"], None),
    ("Grand Est", "Grand Est", regions.REGIONS["Grand Est"], None),
    ("Région Hauts-de-France", "Région Hauts-de-France", regions.REGIONS["Hauts-de-France"], None),
    ("Lorraine (région)", "Lorraine (région)", ("54", "55", "57", "88"), None),
    ("Lyon et périphérie, France", "Lyon et périphérie", ("69",), (45.7640, 4.8357)),
    ("Aix-Marseille Métropole", "Aix-Marseille Métropole", ("13",), (43.2965, 5.3698)),
    ("Bordeaux Métropole", "Bordeaux Métropole", ("33",), (44.8378, -0.5792)),
])
def test_vague_place(location, label, departments, center):
    assert regions.vague_place(location) == regions.VaguePlace(label, tuple(departments), center)


@pytest.mark.parametrize("location", [
    "Metz", "Metz, Grand Est, France", "Talange, Grand Est, France", "Paris 8e, Île-de-France, France", "France",
    "", None, "Moselle", "Centre", "Sud", "Grand-Charmont", "Villeurbanne et périphérie", "Remote",
])
def test_not_a_vague_place(location):
    assert regions.vague_place(location) is None


def test_place_label_and_country_only():
    assert regions.place_label("Île-de-France, France") == "Île-de-France"
    assert regions.place_label("Metz, Grand Est, France") == "Metz, Grand Est"
    assert regions.place_label("France") == "France" and regions.place_label(None) == ""
    for text in ("France", "", None, "France entière", "Toute la France", "FRANCE (Remote)"):
        assert regions.is_country_only(text), text
    for text in ("Metz", "Grand Est", "Télétravail", "Europe"):
        assert not regions.is_country_only(text), text
    assert tr.region_of("57") == "Grand Est" and tr.REGIONS is regions.REGIONS
