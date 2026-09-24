"""documents.letter: French cover letter + e-mail (template and FakeLLM paths)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobbot import clock
from jobbot.documents._letter_rules import OPT_OUT, make_letter_input
from jobbot.documents.letter import claim_issues, letter_issues, write_cover_letter
from jobbot.documents.models import letter_to_text
from jobbot.llm import FakeLLM, LLMError
from jobbot.matching._doccheck import count_words
from jobbot.matching.ats import score_document
from jobbot.matching.keywords import extract_keywords
from jobbot.models import JobOffer
from jobbot.textutil import stable_id

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
JOBS = json.loads((FIXTURES / "jobs.json").read_text(encoding="utf-8"))

GOOD_PARAGRAPHS = [
    "À partir d'octobre 2026, je prépare le titre « Expert en ingénierie du développement et en architecture "
    "logicielle » (Bac+5, RNCP 36469) à Exemple Digital School, en alternance sur 24 mois au rythme de 3 semaines en "
    "entreprise et 1 semaine en formation. Je souhaite effectuer cette alternance au sein de Lorraine Digital "
    "Services en tant que développeur web fullstack. Je suis disponible immédiatement.",
    "Votre portail de gestion des interventions, utilisé chaque jour par plus de 2 000 agents, illustre le type "
    "d'applications métier que je souhaite construire. Votre pôle Produits travaille avec React, TypeScript et "
    "Node.js, une stack que je pratique déjà et que je veux approfondir auprès d'une équipe expérimentée au "
    "service des collectivités et des hôpitaux du Grand Est.",
    "Titulaire d'un BUT Informatique obtenu à l'IUT Exemple, j'ai conçu seul, lors de mon stage chez ACME INDUSTRIE, "
    "une application intranet full-stack en Next.js, TypeScript, Node.js et PostgreSQL, mise en production avec "
    "Docker et utilisée quotidiennement par les équipes. J'ai aussi contribué en équipe à un logiciel .NET avec un "
    "workflow Git complet et 210 commits, en participant aux revues de code et à la documentation technique.",
    "Je vous apporterai mon autonomie, ma rigueur et ma capacité à livrer des fonctionnalités utiles et "
    "documentées, de l'analyse des besoins jusqu'au support des utilisateurs. J'attends de mon côté d'apprendre "
    "les bonnes pratiques d'architecture de vos équipes, de mieux structurer mes tests et de progresser sur des "
    "projets à fort impact pour vos clients publics et privés. Je me tiens à votre disposition pour un entretien.",
]


def load_job(key: str) -> JobOffer:
    data = JOBS[key]
    return JobOffer(id=stable_id(data["source"], data["external_id"]), found_at=clock.now(), **data)


def set_now(monkeypatch, *args) -> None:
    monkeypatch.setattr(clock, "now", lambda: datetime(*args, tzinfo=UTC))


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    set_now(monkeypatch, 2026, 9, 23, 8, 0)


@pytest.fixture
def alt_job() -> JobOffer:
    return load_job("alternance_fullstack_metz")


def body_words(letter) -> int:
    return sum(count_words(p) for p in letter.paragraphs)


def draft(paragraphs=None, pitch="Titulaire d'un BUT Informatique, je prépare un Bac+5 en alternance.") -> str:
    return json.dumps({"paragraphs": paragraphs or GOOD_PARAGRAPHS, "email_pitch": pitch}, ensure_ascii=False)


def assert_llm_body(letter) -> None:
    """The LLM paragraphs are kept; the offer's company has 45 employees, so the aid sentence is inserted before
    the last sentence of the last paragraph."""
    assert letter.paragraphs[:3] == GOOD_PARAGRAPHS[:3]
    last = letter.paragraphs[3]
    assert "2 000 € pour un diplôme Bac+5 dans une entreprise de moins de 250 salariés" in last
    assert last.startswith(GOOD_PARAGRAPHS[3][:60])
    assert last.endswith("Je me tiens à votre disposition pour un entretien.")


# --------------------------------------------------------------------------- template


def test_template_letter_for_an_alternance_offer(profile, alt_job):
    keywords = extract_keywords(alt_job, None)
    letter = write_cover_letter(profile, None, job=alt_job, keywords=keywords)
    first = letter.paragraphs[0]
    for fact in ("Exemple Digital School", "RNCP 36469", "24 mois", "3 semaines en entreprise", "octobre 2026",
                 "Disponible immédiatement", "Lorraine Digital Services"):
        assert fact in first, fact
    assert letter.subject == "Candidature – Développeur Web Fullstack en alternance"
    assert letter.email_subject.endswith("– Jean DUPONT")
    assert letter.greeting == "Madame, Monsieur,"
    assert "Madame, Monsieur, l'expression de mes salutations distinguées" in letter.closing
    assert letter.signature == "Jean DUPONT"
    assert len(letter.paragraphs) == 4 and 240 <= body_words(letter) <= 370
    assert letter_issues(letter.paragraphs, make_letter_input(profile, alt_job, None, keywords)) == []
    # "ESN messine de 45 collaborateurs" in the offer: SME → aid sentence with its amount
    assert "2 000 € pour un diplôme Bac+5" in letter.paragraphs[-1]
    report = score_document(letter_to_text(letter), keywords, alt_job.title, kind="letter")
    assert report.section_score == 100 and report.title_score == 100 and report.score >= 65


def test_template_email_is_short_and_signed(profile, alt_job):
    letter = write_cover_letter(profile, None, job=alt_job, keywords=extract_keywords(alt_job, None))
    body = letter.email_body
    assert count_words(body) <= 150
    assert body.startswith("Madame, Monsieur,")
    assert "ci-joint mon CV et ma lettre de motivation" in body
    assert "Bien cordialement,\nJean DUPONT\n+33 6 12 34 56 78 · jean.dupont@example.com" in body
    assert OPT_OUT not in body  # only for spontaneous e-mails
    assert "au poste de développeur web fullstack en alternance au sein de Lorraine Digital Services." in body


@pytest.mark.parametrize(("title", "expected"), [
    ("Développeur Full-Stack Web & Logiciel", "développeur full-stack web & logiciel"),
    ("Développeur .NET / C#", "développeur .NET / C#"),
    ("Développeur React Next.js", "développeur React Next.js"),
    ("Développeur IA", "développeur IA"),
    ("Concepteur Développeur d'Applications", "concepteur développeur d'applications"),
    ("", ""),
])
def test_title_in_sentence(title, expected):
    from jobbot.documents._cv_rules import title_in_sentence

    assert title_in_sentence(title) == expected


def test_template_letter_for_a_cdi_offer(profile):
    job = load_job("cdi_dotnet_nancy")
    letter = write_cover_letter(profile, None, job=job, keywords=extract_keywords(job, None))
    text = " ".join(letter.paragraphs)
    assert letter.subject == "Candidature – Développeur .NET (CDI)"
    assert "Exemple Digital School" not in text and "disponible immédiatement" in text
    assert "Basé à Metz, je peux travailler sur Metz, Nancy" in text
    assert 230 <= body_words(letter) <= 370


def test_cdi_mobility_respects_location_contract_types(profile):
    from jobbot.config import LocationPref
    from jobbot.models import ContractType

    job = load_job("cdi_dotnet_nancy")
    locations = [LocationPref(city="Metz"), LocationPref(city="Luxembourg", contract_types=[ContractType.CDI])]
    custom = profile.model_copy(update={"target": profile.target.model_copy(update={"locations": locations})})
    cdi = " ".join(write_cover_letter(custom, None, job=job).paragraphs)
    assert "sur Metz et Luxembourg" in cdi
    cdd = job.model_copy(update={"contract_type": ContractType.CDD})
    assert "travailler sur Metz." in " ".join(write_cover_letter(custom, None, job=cdd).paragraphs)


def test_spontaneous_letter_with_aid_and_opt_out(profile, make_company):
    company = make_company(naf_label="Programmation informatique", headcount_range="10 à 19 salariés")
    letter = write_cover_letter(profile, None, company=company, keywords=[])
    text = " ".join(letter.paragraphs)
    assert letter.subject.startswith("Candidature spontanée – Alternance")
    assert "(Bac+5, 24 mois) dès octobre 2026" in letter.subject
    assert "programmation informatique" in text and "Acme Logiciels" in text
    assert "2 000 € pour un diplôme Bac+5" in text
    assert OPT_OUT in letter.email_body
    assert "ouvre droit à l'aide de l'État" in letter.email_body
    assert "candidature spontanée pour une alternance" in letter.email_body
    assert count_words(letter.email_body) <= 150


def test_aid_sentence_rules(profile, make_company, monkeypatch):
    big = make_company(headcount_range="1 000 à 1 999 salariés")
    letter = write_cover_letter(profile, None, company=big, keywords=[])
    assert "2 000 €" not in " ".join(letter.paragraphs)  # ≥ 250 employees: no amount
    assert "ouvre droit" in letter.email_body
    set_now(monkeypatch, 2027, 1, 5, 9, 0)
    late = write_cover_letter(profile, None, company=make_company(headcount_range="3 à 5 salariés"), keywords=[])
    assert "aide de l'État" not in letter_to_text(late) + late.email_body  # never promise 2027 amounts
    assert "Depuis octobre 2026" in late.paragraphs[0]
    assert "dès octobre" not in late.subject


@pytest.mark.parametrize(("description", "expected"), [
    ("ESN messine de 45 collaborateurs, utilisée par plus de 2 000 agents.", 45),
    ("Groupe international de 1 200 salariés.", 1200),
    ("Entreprise de 10 à 49 employés basée à Metz 57000.", 49),
    ("Notre application est utilisée par 3 000 personnes.", None),
    ("", None),
])
def test_company_size_read_from_the_offer(profile, alt_job, description, expected):
    from jobbot.documents._letter_rules import company_size

    job = alt_job.model_copy(update={"description": description})
    assert company_size(make_letter_input(profile, job, None, [])) == expected


def test_offer_without_size_or_from_a_big_company_gets_no_aid_amount(profile, alt_job):
    for description in ("Alternance en développement web à Metz.", "Groupe de 3 500 collaborateurs."):
        job = alt_job.model_copy(update={"description": description})
        letter = write_cover_letter(profile, None, job=job)
        assert "2 000 €" not in " ".join(letter.paragraphs)
        assert "aide de l'État" not in letter.email_body  # never in an offer e-mail


def test_letter_without_job_or_company(profile):
    letter = write_cover_letter(profile, None)
    assert "au sein de votre entreprise" in letter.paragraphs[0]
    assert letter.subject.startswith("Candidature spontanée")


# --------------------------------------------------------------------------- LLM


def test_llm_letter_is_used_when_valid(profile, alt_job):
    llm = FakeLLM(draft())
    letter = write_cover_letter(profile, llm, job=alt_job, keywords=extract_keywords(alt_job, None))
    assert_llm_body(letter)
    assert "Titulaire d'un BUT Informatique, je prépare un Bac+5 en alternance." in letter.email_body
    prompt, system, fast = llm.calls[0]
    assert "vous / moi / nous" in prompt and "Exemple Digital School" in prompt and "Kubernetes" in prompt
    assert "n'inventer" in system or "sans jamais inventer" in system
    assert fast is False and len(llm.calls) == 1


def test_llm_letter_gets_aid_sentence_for_small_company(profile, make_company):
    paragraphs = [p.replace("Lorraine Digital Services", "Acme Logiciels") for p in GOOD_PARAGRAPHS]
    llm = FakeLLM(draft(paragraphs))
    company = make_company(headcount_range="20 à 49 salariés", description="Éditeur de logiciels pour 2 000 agents.")
    letter = write_cover_letter(profile, llm, company=company)
    last = letter.paragraphs[-1]
    assert "2 000 € pour un diplôme Bac+5" in last
    assert last.endswith("Je me tiens à votre disposition pour un entretien.")
    prompt = llm.calls[0][0]
    assert "20 à 49 salariés" not in prompt  # the head count is only used by code (aid), never paraphrased
    assert "ne cite pas l'effectif" in prompt and "je maîtrise l'ensemble" in prompt


def test_llm_letter_with_invented_claims_is_retried_then_cleaned(profile, alt_job):
    bad = list(GOOD_PARAGRAPHS)
    bad[2] = bad[2] + " Je maîtrise Kubernetes et NestJS depuis 5 ans."
    llm = FakeLLM([draft(bad), draft(bad)])
    letter = write_cover_letter(profile, llm, job=alt_job, keywords=extract_keywords(alt_job, None))
    assert len(llm.calls) == 2
    feedback = llm.calls[1][0].split("Corrige ta version précédente")[1]
    assert "Kubernetes" in feedback and "au sujet de l'entreprise" in feedback
    assert_llm_body(letter)  # the unbacked sentence was removed from the last draft


def test_llm_letter_unusable_after_cleaning_uses_template(profile, alt_job):
    bad = ["Je maîtrise Kubernetes. " + p for p in GOOD_PARAGRAPHS[:3]]
    bad = [p.replace(". ", " avec Kubernetes. ") for p in bad]
    llm = FakeLLM([draft(bad), draft(bad)])
    letter = write_cover_letter(profile, llm, job=alt_job, keywords=extract_keywords(alt_job, None))
    assert "Kubernetes" not in " ".join(letter.paragraphs)
    assert letter.paragraphs[0].startswith("À partir d'octobre 2026, je prépare le titre")


def test_llm_letter_fixed_on_second_try(profile, alt_job):
    short = ["Trop court.", "Vraiment.", "Oui."]
    llm = FakeLLM([draft(short), draft()])
    letter = write_cover_letter(profile, llm, job=alt_job)
    assert_llm_body(letter)
    assert "mots" in llm.calls[1][0]


def test_pitch_sentences_repeating_the_application_are_dropped(profile, alt_job):
    pitch = ("Diplômé du BUT Informatique, je prépare un Bac+5 en alternance. "
             "Je candidate au poste de développeur web chez Lorraine Digital Services.")
    llm = FakeLLM(draft(pitch=pitch))
    letter = write_cover_letter(profile, llm, job=alt_job)
    assert "Diplômé du BUT Informatique, je prépare un Bac+5 en alternance." in letter.email_body
    assert "Je candidate" not in letter.email_body
    assert "ne répète ni le poste ni l'entreprise" in llm.calls[0][0]


def test_llm_error_and_bad_pitch_fall_back(profile, alt_job):
    def boom(prompt, system, fast):
        raise LLMError("hors service")

    letter = write_cover_letter(profile, FakeLLM(boom), job=alt_job)
    assert letter.paragraphs[0].startswith("À partir d'octobre 2026")
    long_pitch = "mot " * 90
    letter = write_cover_letter(profile, FakeLLM(draft(pitch=long_pitch)), job=alt_job)
    assert "mot mot mot" not in letter.email_body


def test_claim_issues_allow_offer_facts_but_not_first_person_claims(profile, alt_job):
    inp = make_letter_input(profile, alt_job, None, [])
    assert claim_issues("Votre équipe utilise Kubernetes au quotidien.", inp) == []
    assert claim_issues("Je maîtrise Kubernetes.", inp) == ["compétence non justifiée par le profil : Kubernetes"]
    assert claim_issues("Je souhaite découvrir Kubernetes à vos côtés.", inp) == []
    assert claim_issues("Je souhaite découvrir Terraform.", inp)  # not even in the offer
    assert claim_issues("J'ai réduit de 70 % les coûts.", inp)
    assert claim_issues("Un contrat avant le 1er janvier 2027 donne droit à 2 000 €.", inp) == []
    issues = letter_issues(["{entreprise} est dynamique et motivé XXX"], inp)
    assert any("paragraphe" in i for i in issues) and any("champ" in i for i in issues)
    assert any("cliché" in i for i in issues) and any("mots" in i for i in issues)
