"""FormAnswerer + deterministic rules + option coercion (no browser, no network)."""

from __future__ import annotations

import json

import pytest

from jobbot.apply import answer_rules_extra as extra
from jobbot.apply.answer_rules import clean_label, find_skill, identity_key, profile_vocab, rule_text
from jobbot.apply.answers import FormAnswer, FormAnswerer, FormQuestion, is_job_specific, memory_key
from jobbot.apply.coerce import (
    coerce,
    is_binary,
    is_placeholder,
    level_option,
    match_option,
    numeric_option,
    prefer_not_option,
    to_int_str,
    yes_no,
)
from jobbot.llm import FakeLLM, LLMError
from jobbot.models import ContractType


def _llm_json(answer: str | None, confidence: float = 0.9) -> str:
    return json.dumps({"answer": answer, "confidence": confidence})


@pytest.fixture
def answerer(profile, repo, make_job):
    job = make_job(source="linkedin", contract_type=ContractType.ALTERNANCE)
    return FormAnswerer(profile, FakeLLM(_llm_json(None, 0.0)), repo, job)


def ask(answerer: FormAnswerer, label: str, kind: str = "text", options=(), required=True) -> FormAnswer:
    return answerer.answer(FormQuestion(label=label, kind=kind, options=list(options), required=required))


# --------------------------------------------------------------------------- identity


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Prénom *", "Jean"),
        ("First name", "Jean"),
        ("Nom", "DUPONT"),
        ("Last name (required)", "DUPONT"),
        ("Nom complet", "Jean DUPONT"),
        ("Adresse e-mail", "jean.dupont@example.com"),
        ("Email address", "jean.dupont@example.com"),
        ("Numéro de téléphone mobile", "+33 6 12 34 56 78"),
        ("Ville", "Metz"),
        ("Code postal", "57000"),
        ("Profil LinkedIn", "https://www.linkedin.com/in/jean-dupont-exemple"),
        ("GitHub", "https://github.com/jean-dupont-exemple"),
        ("Portfolio / site web", "https://jean-dupont.example.com"),
        ("Pays", "France"),
    ],
)
def test_identity_fields(answerer, label, expected):
    ans = ask(answerer, label)
    assert ans.value == expected
    assert ans.source == "rule" and ans.confidence >= 0.9


def test_identity_select_and_country_code(answerer):
    ans = ask(answerer, "Indicatif du pays", "select", ["Sélectionner une option", "Belgique (+32)", "France (+33)"])
    assert ans.value == "France (+33)"
    email = ask(answerer, "Email", "select", ["Select an option", "autre@example.org", "jean.dupont@example.com"])
    assert email.value == "jean.dupont@example.com"


def test_identity_key_ignores_real_questions():
    assert identity_key(rule_text("Êtes-vous prêt à déménager dans une autre ville ?")) is None
    assert identity_key(rule_text("Nom de l'entreprise")) is None


# --------------------------------------------------------------------------- years of experience


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Combien d'années d'expérience avez-vous avec React ?", "1"),
        ("How many years of work experience do you have with TypeScript?", "2"),
        ("Combien d'années d'expérience avez-vous en Node ?", "1"),
        ("How many years of experience do you have with JS?", "2"),
        ("Combien d'années d'expérience avez-vous en .NET ?", "1"),
        ("How many years of work experience do you have with Kotlin?", "0"),
        ("Combien d'années d'expérience professionnelle avez-vous ?", "1"),
        ("Years of experience in software development", "1"),
        ("Combien d'années d'expérience avez-vous avec Tailwind CSS ?", "1"),
    ],
)
def test_years_of_experience(answerer, label, expected):
    assert ask(answerer, label).value == expected


def test_years_select_ranges(answerer):
    opts = ["Sélectionner", "Moins d'un an", "1 à 2 ans", "3 à 5 ans", "Plus de 5 ans"]
    assert ask(answerer, "Années d'expérience en Kotlin", "select", opts).value == "Moins d'un an"
    assert ask(answerer, "Années d'expérience en TypeScript", "radio", opts).value == "1 à 2 ans"
    assert ask(answerer, "Years of experience with React", "number").value == "1"


def test_skill_yes_no(answerer):
    assert ask(answerer, "Avez-vous de l'expérience avec Docker ?", "radio", ["Oui", "Non"]).value == "Oui"
    assert ask(answerer, "Do you have experience with Kubernetes?", "radio", ["Yes", "No"]).value == "No"


def test_find_skill_boundaries(profile):
    vocab = profile_vocab(profile)
    assert find_skill("experience en postgresql", vocab) == "postgresql"
    assert find_skill("c est une question", vocab) is None
    assert find_skill("maitrisez vous next.js", vocab) == "next.js"
    assert find_skill("github actions", vocab) == "github"


# --------------------------------------------------------------------------- common FR/EN questions


def test_salary_depends_on_contract(profile, repo, make_job):
    alt = FormAnswerer(profile, None, repo, make_job(contract_type=ContractType.ALTERNANCE))
    cdi = FormAnswerer(profile, None, repo, make_job(contract_type=ContractType.CDI))
    assert "grille" in ask(alt, "Prétentions salariales").value
    assert ask(cdi, "Salary expectations (EUR, gross/year)", "number").value == "32000"
    assert ask(alt, "Salaire annuel souhaité", "number").source in ("none", "llm")


@pytest.mark.parametrize(
    ("label", "kind", "options", "expected"),
    [
        ("Quel est votre préavis ?", "text", (), "Disponible immédiatement"),
        ("Notice period (days)", "number", (), "0"),
        ("Délai de préavis", "select", ("Immédiatement", "1 mois", "3 mois"), "Immédiatement"),
        ("Date de début souhaitée", "text", (), "Octobre 2026"),
        ("Êtes-vous disponible pour démarrer en octobre 2026 ?", "radio", ("Oui", "Non"), "Oui"),
        ("Are you legally authorized to work in France?", "radio", ("Yes", "No"), "Yes"),
        ("Êtes-vous autorisé(e) à travailler en France ?", "radio", ("Oui", "Non"), "Oui"),
        ("Will you now or in the future require visa sponsorship?", "radio", ("Yes", "No"), "No"),
        ("Êtes-vous prêt(e) à déménager ?", "radio", ("Oui", "Non"), "Non"),
        ("Are you comfortable working on-site in Metz?", "radio", ("Yes", "No"), "Yes"),
        ("Quel est votre niveau d'anglais ?", "select",
         ("Sélectionner", "Aucune", "Notions", "Professionnel", "Bilingue ou langue maternelle"), "Professionnel"),
        ("What is your level of proficiency in English?", "select",
         ("None", "Conversational", "Professional", "Native or bilingual"), "Professional"),
        ("Niveau d'anglais", "text", (), "B2"),
        ("Parlez-vous anglais ?", "radio", ("Oui", "Non"), "Oui"),
        ("Are you fluent in English?", "radio", ("Yes", "No"), "No"),
        ("Parlez-vous allemand ?", "radio", ("Oui", "Non"), "Non"),
        ("Niveau de français", "select", ("Débutant", "Courant", "Langue maternelle"), "Langue maternelle"),
        ("Niveau d'études", "select", ("Bac", "Bac+2", "Bac+3", "Bac+5"), "Bac+3"),
        ("Highest level of education", "select",
         ("High School", "Associate's Degree", "Bachelor's Degree", "Master's Degree"), "Bachelor's Degree"),
        ("Avez-vous obtenu le diplôme suivant : Master ?", "radio", ("Oui", "Non"), "Non"),
        ("Have you completed the following level of education: Bachelor's Degree?", "radio", ("Yes", "No"), "Yes"),
        ("Avez-vous le permis B ?", "radio", ("Oui", "Non"), "Oui"),
        ("Avez-vous plus de 18 ans ?", "radio", ("Oui", "Non"), "Oui"),
        ("Rythme de l'alternance et code RNCP", "textarea", (),
         "3 semaines en entreprise / 1 semaine en formation — RNCP 36469"),
        ("Durée du contrat d'alternance (mois)", "number", (), "24"),
        ("Nom de votre école", "text", (), "Exemple Digital School"),
        ("Êtes-vous inscrit dans une école ?", "radio", ("Oui", "Non"), "Oui"),
        ("Quelle formation préparez-vous ?", "text", (),
         "Expert en ingénierie du développement et en architecture logicielle (Bac+5, RNCP 36469)"),
        ("Comment avez-vous connu cette offre ?", "select", ("Indeed", "LinkedIn", "Autre"), "LinkedIn"),
        ("Entreprise actuelle", "text", (), "Indépendant"),
        ("Genre", "select", ("Homme", "Femme", "Je préfère ne pas répondre"), "Je préfère ne pas répondre"),
        ("Télétravail souhaité", "text", (), "Hybride ou sur site"),
    ],
)
def test_common_questions(answerer, label, kind, options, expected):
    ans = ask(answerer, label, kind, options)
    assert ans.value == expected, (label, ans)
    assert ans.source in ("rule", "profile")


def test_rules_are_fault_tolerant(answerer, monkeypatch):
    def boom(_r):
        raise RuntimeError("bug")

    monkeypatch.setattr("jobbot.apply.answers.RULES", (boom, extra.adult))
    assert ask(answerer, "Avez-vous plus de 18 ans ?", "radio", ["Oui", "Non"]).value == "Oui"


# --------------------------------------------------------------------------- memory, profile, LLM


def test_memory_roundtrip(answerer, repo):
    q = "Numéro de sécurité sociale"
    assert ask(answerer, q).source == "none"
    answerer.remember(q + " *", "1 23 45")
    assert repo.kv_get(memory_key(q)) == "1 23 45"
    ans = ask(answerer, q)
    assert (ans.value, ans.source) == ("1 23 45", "memory")
    answerer.remember("", "x")  # ignored, no crash
    answerer.remember(q, "  ")


def test_attestation_checkbox_never_auto_ticked(answerer):
    label = "Je certifie l'exactitude des informations fournies"
    assert ask(answerer, label, "checkbox").value is None
    answerer.remember(label, "Oui")
    assert ask(answerer, label, "checkbox").value == "Oui"
    consent = "Do you agree to the processing of your personal data?"
    assert ask(answerer, consent, "radio", ["Yes", "No"]).value is None


def test_freeform_profile_answer(profile, repo):
    custom = profile.model_copy(update={"answers": {**profile.answers, "Avez-vous un véhicule personnel ?": "Oui"}})
    a = FormAnswerer(custom, None, repo)
    ans = a.answer(FormQuestion(label="Avez-vous un véhicule personnel ?", kind="radio", options=["Oui", "Non"]))
    assert (ans.value, ans.source) == ("Oui", "profile")


def test_llm_answers_open_question(profile, repo, make_job):
    llm = FakeLLM(_llm_json("Votre produit SaaS m'intéresse car il combine React et Node.js.", 0.95))
    a = FormAnswerer(profile, llm, repo, make_job(company="Acme"))
    ans = a.answer(FormQuestion(label="Pourquoi nous ?", kind="textarea", required=True))
    assert ans.source == "llm" and "React" in ans.value
    assert ans.confidence == 0.8  # capped
    prompt, system, fast = llm.calls[0]
    assert fast is True and "Pourquoi nous" in prompt and "Acme" in prompt and "n'invente" in system.lower()


def test_llm_select_must_match_options(profile, repo):
    a = FormAnswerer(profile, FakeLLM(_llm_json("Peut-être")), repo)
    q = FormQuestion(label="Question étrange ?", kind="select", options=["Rouge", "Bleu"], required=True)
    assert a.answer(q).value is None
    b = FormAnswerer(profile, FakeLLM(_llm_json("bleu", 0.7)), repo)
    assert b.answer(q).value == "Bleu"
    assert b.answer(q, allow_llm=False).value is None


def test_llm_null_and_errors(profile, repo):
    q = FormQuestion(label="Numéro de sécurité sociale", kind="text", required=True)
    assert FormAnswerer(profile, FakeLLM(_llm_json(None)), repo).answer(q).value is None
    assert FormAnswerer(profile, FakeLLM(_llm_json("null")), repo).answer(q).value is None

    def raise_error(*_):
        raise LLMError("down")

    assert FormAnswerer(profile, FakeLLM(raise_error), repo).answer(q).source == "none"

    def raise_bug(*_):
        raise ValueError("bug")

    assert FormAnswerer(profile, FakeLLM(raise_bug), repo).answer(q).source == "none"


def test_motivation_fallback_without_llm(profile, repo, make_job):
    def raise_error(*_):
        raise LLMError("down")

    a = FormAnswerer(profile, FakeLLM(raise_error), repo, make_job(title="Dev Web", company="Acme"))
    ans = a.answer(FormQuestion(label="Pourquoi souhaitez-vous nous rejoindre ?", kind="textarea"))
    assert ans.source == "rule" and "Acme" in ans.value and "Dev Web" in ans.value


def test_job_specific_questions_skip_memory(answerer):
    answerer.remember("Pourquoi nous ?", "Réponse donnée à une autre entreprise")
    assert ask(answerer, "Pourquoi nous ?", "text").value != "Réponse donnée à une autre entreprise"
    assert is_job_specific("Why do you want to join us?")
    assert not is_job_specific("Ville")


def test_file_and_empty_questions(answerer):
    assert ask(answerer, "CV", "file").source == "none"
    assert ask(answerer, "  * ").source == "none"


# --------------------------------------------------------------------------- coercion helpers


def test_clean_label():
    assert clean_label("Phone Phone") == "Phone"
    assert clean_label("Téléphone * (obligatoire) :") == "Téléphone"
    assert clean_label("Email Required") == "Email"


def test_coercion_helpers():
    assert to_int_str("32 000 €") == "32000"
    assert to_int_str("2 ans") == "2"
    assert to_int_str("aucun") is None
    assert yes_no("Oui, bien sûr") is True and yes_no("No") is False and yes_no("Peut-être") is None
    assert is_binary(["Sélectionner", "Yes", "No"]) and not is_binary(["A", "B"])
    assert is_placeholder("Select an option") and is_placeholder("Sélectionner une option") and not is_placeholder("Oui")
    assert numeric_option(4, ["0-1 an", "1-3 ans", "3 ans et plus"]) == "3 ans et plus"
    assert numeric_option(0, ["Aucune", "1 an"]) == "Aucune"
    assert numeric_option(2, ["moins de 2 ans", "2 à 4 ans"]) == "2 à 4 ans"
    assert match_option("France", ["Belgique (+32)", "France (+33)"]) == "France (+33)"
    assert match_option("Louis", ["Oui", "Non"]) is None
    assert match_option("", ["Oui"]) is None
    assert coerce("checkbox", ["React", "Vue", "Angular"], ["React; Angular"]) == ["React", "Angular"]
    assert coerce("checkbox", [], ["yes"]) == "Oui"
    assert coerce("checkbox", [], ["maybe"]) is None
    assert coerce("file", [], ["x"]) is None
    assert prefer_not_option(["Homme", "Prefer not to say"]) == "Prefer not to say"
    assert level_option(["A", "B"], 3, lambda _o: None) is None
    assert level_option(["Débutant", "Bilingue"], 3, extra.lang_level) == "Débutant"
