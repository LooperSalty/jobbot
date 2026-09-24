"""matching.match: hard filters + deterministic relevance on realistic French offers and the fictitious test profile (tests/fixtures/profile.yaml)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from jobbot import clock
from jobbot.matching import filters
from jobbot.matching.match import MatchReport, match_job, title_similarity
from jobbot.models import ContractType, JobOffer
from jobbot.textutil import stable_id

FIXTURES = Path(__file__).parent / "fixtures" / "matching"
OFFERS = json.loads((FIXTURES / "offers.json").read_text(encoding="utf-8"))


def offer(key: str, **overrides) -> JobOffer:
    data = {**OFFERS[key], **overrides}
    return JobOffer(
        id=stable_id(data["source"], data["external_id"]), found_at=clock.now(),
        published_at=overrides.pop("published_at", clock.now() - timedelta(days=2)), **data,
    )


def test_fullstack_alternance_in_metz_scores_high(profile):
    score, reasons = match_job(offer("fullstack_alternance"), profile)
    assert score >= 85
    text = " | ".join(reasons)
    assert "Intitulé proche" in text and "Contrat : alternance" in text
    assert "Compétences en commun" in text and "React" in text and "TypeScript" in text
    assert "km de Metz" in text and "télétravail partiel" in text
    assert "Bac+5" in text and "24 mois" in text
    assert "Publiée il y a 2 jour(s)" in text


def test_dotnet_junior_in_nancy_is_shortlisted(profile):
    score, reasons = match_job(offer("dotnet_junior"), profile)
    assert score >= 70
    text = " | ".join(reasons)
    assert ".NET" in text and "C#" in text and "km de Nancy" in text and "Contrat : CDI" in text
    assert "Débutants / juniors acceptés" in text


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("school_iscod", "ISCOD"),
        ("school_disguised", "école"),
        ("senior_title", "senior"),
        ("senior_years", "4 an(s)"),
        ("business_developer", "commercial"),
        ("developpeur_commercial", "commercial"),
    ],
)
def test_hard_filters_return_zero_with_french_reason(profile, key, expected):
    score, reasons = match_job(offer(key), profile)
    assert score == 0.0
    assert len(reasons) == 1 and reasons[0].startswith("Écartée")
    assert expected in reasons[0]


def test_school_filters_on_company_name_delegation_and_weak_cues(profile, make_job):
    assert match_job(make_job(company="École Supérieure du Numérique"), profile)[0] == 0
    delegated = make_job(company="Garage Dupont", raw={"is_delegated": True})
    assert "CFA" in match_job(delegated, profile)[1][0]
    weak = make_job(description="Formation financée, titre RNCP niveau 6, places limitées. React, Node.js.")
    assert match_job(weak, profile)[0] == 0
    legit = make_job(description="Vous préparez un diplôme Bac+5 en alternance. Formation financée par l'OPCO. React.")
    assert match_job(legit, profile)[0] > 0


@pytest.mark.parametrize(
    ("title", "contract", "description", "expected"),
    [
        ("Apprenti développeur web", ContractType.OTHER, "", ContractType.ALTERNANCE),
        ("Développeur web", ContractType.CDD, "Contrat d'apprentissage de 24 mois", ContractType.ALTERNANCE),
        ("Développeur web", ContractType.OTHER, "Poste en contrat pro", ContractType.ALTERNANCE),
        ("Développeur web", ContractType.OTHER, "Machine learning et apprentissage automatique", ContractType.OTHER),
        ("Stage développeur web", ContractType.OTHER, "", ContractType.STAGE),
        ("Développeur web", ContractType.OTHER, "Poste en CDI à pourvoir", ContractType.CDI),
        ("Développeur web", ContractType.OTHER, "Mission freelance, TJM selon profil", ContractType.FREELANCE),
        ("Développeur web", ContractType.OTHER, "Mission d'intérim de 3 mois", ContractType.INTERIM),
        ("Développeur web", ContractType.OTHER, "CDD de 6 mois", ContractType.CDD),
        ("Développeur web", ContractType.CDI, "Nous accueillons aussi des alternants", ContractType.CDI),
        ("Développeur web en alternance", ContractType.CDI, "", ContractType.ALTERNANCE),
    ],
)
def test_contract_detection(make_job, title, contract, description, expected):
    job = make_job(title=title, contract_type=contract, description=description)
    assert filters.detect_contract(job) == expected


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("3 ans d'expérience", 3), ("3+ years of experience", 3), ("minimum 5 ans sur un poste similaire", 5),
        ("Expérience exigée de 2 An(s)", 2), ("3 à 5 ans d'expérience", 3), ("moins de 2 ans d'expérience", None),
        ("Fort de 20 ans d'expérience, notre cabinet recrute", None), ("deux ans d'expérience", 2),
        ("encadré par un développeur ayant 10 ans d'expérience", None), ("contrat de 2 ans minimum en alternance", None),
        ("At least 4 years", 4), ("5 years experience in Java", 5), ("Expérience : 3 ans", 3),
        ("expérience significative de 5 ans minimum", 5), ("Débutant accepté", None),
    ],
)
def test_required_experience_years(text, years):
    assert filters.required_experience_years(text) == years


def test_unwanted_contract_and_excluded_words_are_filtered(profile, make_job):
    assert "stage" in match_job(make_job(title="Stage développeur web", contract_type=ContractType.STAGE), profile)[1][0]
    cdd = make_job(title="Développeur web", contract_type=ContractType.CDD, description="React, Node.js.")
    assert "CDD" in match_job(cdd, profile)[1][0]
    secret = make_job(description="React. Habilitation secret défense requise.")
    assert "habilitation secret défense" in match_job(secret, profile)[1][0]
    assert "tech lead" in match_job(make_job(title="Tech Lead React (H/F)"), profile)[1][0]


def test_school_diploma_title_does_not_trigger_the_expert_exclusion(profile, make_job):
    ok = make_job(title="Alternance - Expert en ingénierie logicielle (H/F)")
    assert match_job(ok, profile)[0] > 0
    assert match_job(make_job(title="Expert Java (H/F)"), profile)[0] == 0


def test_experience_slightly_above_max_is_tolerated_for_alternance(profile, make_job):
    score, reasons = match_job(make_job(description="React, Node.js. 3 ans d'expérience souhaités."), profile)
    assert score > 0
    assert any("toléré" in r for r in reasons)
    cdi = make_job(contract_type=ContractType.CDI, title="Développeur React", description="3 ans d'expérience. React.")
    assert match_job(cdi, profile)[0] == 0


def test_location_rules(profile, make_job):
    paris = dict(location="Paris (75)", postal_code="75002", latitude=48.8566, longitude=2.3522)
    far = match_job(make_job(**paris), profile)
    assert far[0] == 0 and "trop loin" in far[1][0]
    hybrid = match_job(make_job(**paris, description="React. 2 jours de télétravail par semaine."), profile)
    assert "télétravail partiel seulement" in hybrid[1][0]
    remote = match_job(make_job(**paris, description="Poste en full remote. React, Node.js."), profile)
    assert remote[0] > 60 and any("Télétravail complet" in r for r in remote[1])
    no_coords = dict(latitude=None, longitude=None)
    talange = match_job(make_job(location="Talange (57)", postal_code=None, **no_coords), profile)
    assert any("département 57" in r for r in talange[1])
    lyon_area = match_job(make_job(location="Villeurbanne (69)", postal_code=None, **no_coords), profile)
    assert lyon_area[0] == 0 and "département 69" in lyon_area[1][0]
    unknown = match_job(make_job(location="Quelque-Part", postal_code=None, **no_coords), profile)
    assert any("à vérifier" in r for r in unknown[1])
    nowhere = match_job(make_job(location="", postal_code=None, **no_coords), profile)
    assert any("non précisée" in r for r in nowhere[1])
    linkedin = match_job(make_job(location="Metz, Grand Est, France", postal_code=None, **no_coords), profile)
    assert any("km de Metz" in r for r in linkedin[1])


def test_remote_does_not_bypass_distance_when_profile_refuses_remote(profile, make_job):
    strict = profile.model_copy(update={"target": profile.target.model_copy(update={"remote_ok": False})})
    job = make_job(location="Lyon", latitude=45.764, longitude=4.8357, description="Full remote. React.")
    assert match_job(job, strict)[0] == 0
    no_areas = profile.model_copy(update={"target": profile.target.model_copy(update={"locations": []})})
    assert any("Aucune zone" in r for r in match_job(make_job(), no_areas)[1])


def test_title_similarity_and_non_dev_titles(profile, make_job):
    best, target = title_similarity("Ingénieur logiciel C++ (H/F)", profile.target.job_titles)
    assert best >= 55 and target
    assert title_similarity("", profile.target.job_titles) == (50.0, None)
    data = make_job(title="Data Analyst (H/F)", description="Power BI, SQL, Python, Excel.")
    score, reasons = match_job(data, profile)
    assert score < 45 and "Intitulé hors développement logiciel" in reasons


def test_freshness_levels_and_unknown_skills(profile, make_job):
    old = match_job(make_job(published_at=clock.now() - timedelta(days=45)), profile)[1]
    assert any("offre ancienne" in r for r in old)
    unknown = match_job(make_job(published_at=None), profile)[1]
    assert "Date de publication inconnue" in unknown
    today = match_job(make_job(published_at=clock.now()), profile)[1]
    assert "Publiée aujourd'hui" in today
    bare = match_job(make_job(title="Développeur web", description="Poste polyvalent au sein de notre agence."), profile)[1]
    assert "Aucune compétence technique identifiée dans l'annonce" in bare


def test_alternance_level_and_duration_adjustments(profile, make_job):
    bts = make_job(description="Alternance BTS SIO sur 12 mois. React, Node.js.")
    score_bts, reasons = match_job(bts, profile)
    assert any("Bac+2" in r for r in reasons) and any("12 mois" in r for r in reasons)
    bac5 = make_job(description="Alternance Bac+5 sur 24 mois. React, Node.js.")
    assert match_job(bac5, profile)[0] > score_bts


def test_unknown_contract_is_neutral(profile, make_job):
    score, reasons = match_job(make_job(title="Développeur React", contract_type=ContractType.OTHER, description="React"),
                               profile)
    assert score > 0 and "Type de contrat non précisé" in reasons


def test_match_report_is_frozen():
    report = MatchReport(scored=3, shortlisted=2, filtered_out=1)
    with pytest.raises(ValidationError):
        report.scored = 5
