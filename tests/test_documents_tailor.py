"""documents.tailor: truth base, truthfulness checks, deterministic and LLM tailoring (FakeLLM)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobbot import clock
from jobbot.config import Education, Experience, Language, SkillGroup
from jobbot.documents import _cv_rules
from jobbot.documents._cv_llm import CVPlan, build_prompt, plan_to_cv
from jobbot.documents._truth import (
    build_truth,
    number_claims,
    phrase_supported,
    unsupported_claims,
    unsupported_degrees,
)
from jobbot.documents.models import cv_to_text
from jobbot.documents.tailor import _Attempt, _pick_best, check_truthfulness, tailor_cv, truthful_missing
from jobbot.llm import FakeLLM, LLMError
from jobbot.matching.keywords import extract_keywords
from jobbot.models import ATSReport, JobOffer, KeywordHit
from jobbot.textutil import stable_id

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
JOBS = json.loads((FIXTURES / "jobs.json").read_text(encoding="utf-8"))


def load_job(key: str) -> JobOffer:
    data = JOBS[key]
    return JobOffer(id=stable_id(data["source"], data["external_id"]), found_at=clock.now(), **data)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 9, 23, 8, 0, tzinfo=UTC))


@pytest.fixture
def alt_job() -> JobOffer:
    return load_job("alternance_fullstack_metz")


@pytest.fixture
def alt_keywords(alt_job) -> list[KeywordHit]:
    return extract_keywords(alt_job, None)


def kw(keyword: str, weight: float = 1.0, category: str = "hard") -> KeywordHit:
    return KeywordHit(keyword=keyword, weight=weight, category=category)


def plan(**overrides) -> str:
    base = {
        "headline": "Développeur Web Fullstack — Alternance Bac+5 (24 mois) dès octobre 2026",
        "summary": ("Titulaire d'un BUT Informatique, je développe des applications **React**, Node.js et "
                    "PostgreSQL ; j'ai conçu seul une application intranet utilisée quotidiennement. Autonome et "
                    "rigoureux."),
        "skills": [
            {"category": "Langages", "items": ["TypeScript", "JavaScript", "SQL"]},
            {"category": "Front-end", "items": ["React", "Next.js"]},
            {"category": "Back-end", "items": ["Node.js", "API REST"]},
            {"category": "Outils", "items": ["Git", "Docker", "CI/CD"]},
        ],
        "experiences": [
            {"ref": 0, "bullets": [
                "Conception d'une application web intranet (Next.js, TypeScript, React, Node.js, PostgreSQL) "
                "utilisée quotidiennement.",
                "Mise en production avec **Docker** et rédaction de la documentation technique.",
            ]},
            {"ref": 1, "bullets": ["Création de sites web Next.js pour des TPE, du cahier des charges au suivi."]},
        ],
        "projects": [{"ref": 0, "description": "Application mobile Flutter avec un back-end Python (API REST)."}],
        "interests": ["Veille technologique", "street workout"],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


# --------------------------------------------------------------------------- truth base


def test_truth_base_reflects_the_profile(profile):
    truth = build_truth(profile)
    assert {"React", "Next.js", "JavaScript", "PostgreSQL", "SQL", "Docker", "C#", ".NET"} <= truth.skills
    assert "Kubernetes" not in truth.skills and "Scrum" not in truth.skills
    assert {"Autonomie", "Rigueur", "Travail en équipe"} <= truth.soft
    assert truth.languages == {"Anglais"}
    assert {"210", "450", "23", "2026", "24", "36469"} <= truth.numbers
    assert "fullstack" in truth.families
    assert number_claims("Réduit de 30 % en 2 ans, 2 000 agents") == {"30", "30%", "2", "2an", "2000"}
    assert phrase_supported("maintenance évolutive", truth)
    assert not phrase_supported("comptabilité analytique", truth)


def test_degree_names_must_come_from_the_profile(profile):
    truth = build_truth(profile)
    assert unsupported_degrees("Titulaire d'un Doctorat et d'un MBA, titre d'ingénieur", truth) == [
        "Doctorat", "MBA", "diplôme d'ingénieur"]
    assert unsupported_degrees("BUT Informatique, DUT obtenu en 2025", truth) == []
    assert "diplôme « Doctorat »" in unsupported_claims("Doctorat en informatique", truth)


def test_truthful_missing_keeps_only_backed_keywords(profile):
    missing = [kw("React"), kw("Scrum"), kw("Kubernetes"), kw("Autonomie", 0.9, "soft"),
               kw("Anglais", 0.5, "language"), kw("Allemand", 0.5, "language"), kw("maintenance évolutive"),
               kw("Développeur Web Fullstack", 0.5, "title"), kw("Comptable", 0.5, "title")]
    kept = {k.keyword for k in truthful_missing(missing, profile)}
    assert kept == {"React", "Autonomie", "Anglais", "maintenance évolutive", "Développeur Web Fullstack"}


# --------------------------------------------------------------------------- truthfulness


def test_rule_cv_is_truthful(profile, alt_job, alt_keywords):
    cv, _, warnings = tailor_cv(profile, alt_keywords, None, job=alt_job)
    assert warnings == []
    assert check_truthfulness(cv, profile) == []


def test_check_truthfulness_flags_every_kind_of_invention(profile, alt_job, alt_keywords):
    cv, _, _ = tailor_cv(profile, alt_keywords, None, job=alt_job)
    first = cv.experiences[0]
    forged = cv.model_copy(update={
        "headline": "Développeur Kubernetes",
        "experiences": [
            first.model_copy(update={"bullets": ["Réduction de 40 % du temps de chargement avec Scrum."],
                                     "start": "2025-01", "title": "Lead developer"}),
            Experience(title="Développeur", company="Google", start="2024", end="2025"),
        ],
        "education": [*cv.education, Education(degree="Master Informatique", school="Université de Paris")],
        "skills": [SkillGroup(category="Cloud", items=["AWS", "React"])],
        "languages": [Language(name="Allemand", level="C1")],
        "identity": cv.identity.model_copy(update={"phone": "0600000000"}),
    })
    text = " | ".join(check_truthfulness(forged, profile))
    for expected in ("Kubernetes", "40", "Scrum", "Dates modifiées chez ACME INDUSTRIE", "Intitulé de poste modifié",
                     "Employeur absent du profil : Google", "Diplôme absent du profil : Master Informatique", "AWS",
                     "Langue absente du profil : Allemand", "Coordonnées"):
        assert expected in text, expected
    assert "React" not in text


# --------------------------------------------------------------------------- deterministic tailoring


def test_rule_tailoring_mirrors_title_and_orders_by_relevance(profile, alt_job, alt_keywords):
    cv, report, _ = tailor_cv(profile, alt_keywords, None, job=alt_job)
    assert cv.headline == "Développeur Web Fullstack — Alternance Bac+5 (24 mois) dès octobre 2026"
    assert "Compétences clés pour ce poste" in cv.summary
    assert cv.alternance == profile.alternance
    assert cv.experiences[0].bullets[0].startswith("Conception et développement")  # most relevant bullet first
    assert all(len(e.bullets) <= 4 for e in cv.experiences)
    assert len(cv.experiences[-1].bullets) == 1  # seasonal job capped
    assert cv.skills[0].items[0] == "TypeScript"
    assert report.score >= 80 and report.title_score == 100
    assert tailor_cv(profile, alt_keywords, None, job=alt_job)[0] == cv  # deterministic


def test_rule_tailoring_for_a_cdi_offer(profile):
    job = load_job("cdi_dotnet_nancy")
    cv, report, warnings = tailor_cv(profile, extract_keywords(job, None), None, job=job)
    assert cv.alternance is None
    assert cv.headline.startswith("Développeur .NET — Junior")
    assert "alternance" not in cv.summary.lower() and "en CDI" in cv.summary
    assert all("Exemple Digital School" not in e.school for e in cv.education)
    assert warnings == [] and report.score > 70


def test_rule_tailoring_spontaneous_uses_profile_headline(profile, make_company):
    keywords = [kw("TypeScript", 0.8), kw("développeur full-stack", 0.5, "title")]
    cv, _, warnings = tailor_cv(profile, keywords, None, company=make_company())
    assert cv.headline.startswith("Développeur Full-Stack Web & Logiciel — Alternance Bac+5")
    assert cv.summary.endswith("Compétences clés : TypeScript.")
    assert warnings == []


def test_headline_after_programme_start_says_available(profile, alt_job, alt_keywords, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 11, 5, 8, 0, tzinfo=UTC))
    cv, _, _ = tailor_cv(profile, alt_keywords, None, job=alt_job)
    assert cv.headline.endswith("Alternance Bac+5 (24 mois) disponible immédiatement")


def test_unrelated_offer_title_keeps_profile_title(profile, alt_job):
    job = alt_job.model_copy(update={"title": "Technicien de maintenance industrielle"})
    cv, _, _ = tailor_cv(profile, [kw("React")], None, job=job)
    assert cv.headline.startswith("Développeur Full-Stack Web & Logiciel")


# --------------------------------------------------------------------------- LLM tailoring


def test_plan_is_sanitized_against_the_profile(profile, alt_job, alt_keywords):
    bad = CVPlan.model_validate_json(plan(
        headline="Expert DevSecOps Kubernetes",
        summary="Expert Kubernetes avec 5 ans d'expérience.",
        skills=[{"category": "Tech", "items": ["React", "Kubernetes", "React", "recueil des besoins", "Comptabilité"]},
                {"category": "Outils", "items": ["Git", "Docker"]}],
        experiences=[
            {"ref": 0, "bullets": ["Animation des cérémonies Scrum et tests unitaires Jest.",
                                   "Mise en production avec **Docker** et documentation technique."]},
            {"ref": 99, "bullets": ["Référence invalide"]},
            {"ref": 2, "bullets": ["Réduction de 40 % du temps de chargement du site e-commerce."]},
        ],
        projects=[{"ref": 0, "description": "Détection Kubernetes."}, {"ref": 7, "description": "x"}],
        interests=["Golf", "pêche"],
    ))
    inp = _cv_rules.make_input(profile, alt_keywords, alt_job, None)
    rule = _cv_rules.build_rule_cv(inp)
    cv, fixes = plan_to_cv(bad, inp, rule)
    text = cv_to_text(cv)
    assert "Kubernetes" not in text and "Scrum" not in text and "40 %" not in text and "Comptabilité" not in text
    assert check_truthfulness(cv, profile) == []
    assert cv.headline == rule.headline and cv.summary == rule.summary
    assert [g.items for g in cv.skills] == [["React", "recueil des besoins"], ["Git", "Docker"]]
    assert [e.company for e in cv.experiences] == ["ACME INDUSTRIE", "Éditions Exemple"]
    assert cv.experiences[0].bullets[1] == "Mise en production avec **Docker** et documentation technique."
    assert cv.experiences[0].bullets[0] in profile.experiences[0].bullets  # replaced by an original bullet
    assert cv.experiences[1].bullets == profile.experiences[2].bullets
    assert cv.projects == [profile.projects[0]]
    assert cv.interests == ["pêche"]
    assert len(fixes) >= 6 and any("Kubernetes" in f for f in fixes)


def test_plan_never_has_more_bullets_than_the_profile(profile, alt_job, alt_keywords):
    padded = CVPlan.model_validate_json(plan(experiences=[{"ref": 1, "bullets": [
        "Création et maintenance de sites web pour des artisans et TPE.",
        "Réalisation complète du site de Rénov'Exemple en Next.js.",
        "Déploiement et suivi technique des sites en production.",
    ]}]))
    inp = _cv_rules.make_input(profile, alt_keywords, alt_job, None)
    cv, _ = plan_to_cv(padded, inp, _cv_rules.build_rule_cv(inp))
    assert len(profile.experiences[1].bullets) == 2
    assert cv.experiences[0].bullets == [
        "Création et maintenance de sites web pour des artisans et TPE.",
        "Réalisation complète du site de Rénov'Exemple en Next.js.",
    ]
    assert "jamais plus de puces que le profil" in build_prompt(inp)


def test_empty_plan_falls_back_section_by_section(profile, alt_job, alt_keywords):
    inp = _cv_rules.make_input(profile, alt_keywords, alt_job, None)
    rule = _cv_rules.build_rule_cv(inp)
    cv, _ = plan_to_cv(CVPlan(headline="", summary=""), inp, rule)
    assert cv.skills == rule.skills and cv.experiences == rule.experiences and cv.interests == rule.interests
    assert cv.projects == [] and cv.headline == rule.headline


def test_llm_plan_is_used(profile, alt_job, alt_keywords):
    llm = FakeLLM(plan())
    cv, report, warnings = tailor_cv(profile, alt_keywords, llm, job=alt_job, target_score=0)
    assert warnings == [] and len(llm.calls) == 1
    assert llm.calls[0][2] is False  # quality model, not the fast one
    assert cv.skills[1] == SkillGroup(category="Front-end", items=["React", "Next.js"])
    assert cv.summary.startswith("Titulaire d'un BUT Informatique")
    assert [len(e.bullets) for e in cv.experiences] == [2, 1]
    assert report.score >= 75


def test_llm_prompt_lists_truthful_and_forbidden_keywords(profile, alt_job, alt_keywords):
    llm = FakeLLM(plan())
    tailor_cv(profile, alt_keywords, llm, job=alt_job, target_score=0)
    prompt, system, _ = llm.calls[0]
    assert "n'inventes jamais rien" in system
    assert "N'invente RIEN" in prompt and "[E0]" in prompt and "[P0]" in prompt
    forbidden = prompt.split("NE possède PAS (interdits)")[1].split("##")[0]
    assert "Kubernetes" in forbidden and "Scrum" in forbidden and "React" not in forbidden
    assert "Alternant Développeur Web Fullstack" in prompt


def test_llm_iterates_on_truthful_missing_keywords_and_keeps_best(profile, alt_job, alt_keywords):
    weak = plan(skills=[{"category": "Langages", "items": ["TypeScript"]}],
                experiences=[{"ref": 3, "bullets": ["Travail en équipe sur 3 étés consécutifs."]}],
                summary="Développeur junior motivé par le web.", projects=[])
    llm = FakeLLM([weak, plan()])
    cv, report, _ = tailor_cv(profile, alt_keywords, llm, job=alt_job, target_score=99, max_iterations=2)
    assert len(llm.calls) == 2
    second = llm.calls[1][0]
    assert "Ta version précédente" in second and "Mots-clés encore absents" in second
    assert "Node.js" in second.split("Mots-clés encore absents")[1]
    assert cv.experiences[0].company == "ACME INDUSTRIE"  # the better second version was kept
    assert report.score >= 80


def test_llm_stops_when_target_reached(profile, alt_job, alt_keywords):
    llm = FakeLLM([plan(), plan()])
    tailor_cv(profile, alt_keywords, llm, job=alt_job, target_score=50, max_iterations=3)
    assert len(llm.calls) == 1


def test_llm_failure_falls_back_to_rules(profile, alt_job, alt_keywords):
    def boom(prompt, system, fast):
        raise LLMError("indisponible")

    rule_cv, _, _ = tailor_cv(profile, alt_keywords, None, job=alt_job)
    cv, _, warnings = tailor_cv(profile, alt_keywords, FakeLLM(boom), job=alt_job)
    assert cv == rule_cv and warnings == []
    invalid = FakeLLM("pas de json")
    assert tailor_cv(profile, alt_keywords, invalid, job=alt_job)[0] == rule_cv
    assert len(invalid.calls) == 2  # one repair attempt by complete_json


def test_pick_best_rules(profile, alt_job, alt_keywords):
    cv, _, _ = tailor_cv(profile, alt_keywords, None, job=alt_job)

    def attempt(score: float, warnings: list[str] | None = None) -> _Attempt:
        report = ATSReport(score=score, keyword_score=0, title_score=0, section_score=0, format_score=0)
        return _Attempt(cv, report, warnings or [])

    rule = attempt(80)
    assert _pick_best([], rule) is rule
    llm_ok = attempt(78)
    assert _pick_best([llm_ok], rule) is llm_ok  # LLM preferred when close
    assert _pick_best([attempt(60)], rule) is rule  # rules clearly better
    assert _pick_best([attempt(95, ["x"])], rule) is rule  # untruthful LLM version never wins
    best = attempt(85)
    assert _pick_best([attempt(82), best], rule) is best
    on_target = attempt(81)
    assert _pick_best([on_target], attempt(95), target=80) is on_target  # target reached: LLM wording kept


def test_key_skills_use_catalogue_spelling(profile, alt_job):
    inp = _cv_rules.make_input(profile, [kw("API"), kw("Next.js"), kw("Kubernetes")], alt_job, None)
    assert _cv_rules.key_skills(inp, "") == ["API REST", "Next.js 15"]
    assert _cv_rules.join_fr(["a"]) == "a" and _cv_rules.join_fr(["a", "b", "c"]) == "a, b et c"
