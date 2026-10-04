"""matching.ats: deterministic, explainable ATS score of CVs and cover letters."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobbot.matching.ats import keyword_coverage, score_document
from jobbot.models import KeywordHit

FIXTURES = Path(__file__).parent / "fixtures" / "matching"
# private (git-ignored): text of a real CV and cover letter, kept off the public repo
if not (FIXTURES / "cv_candidat.txt").exists() or not (FIXTURES / "lettre_candidat.txt").exists():
    pytest.skip("CV / lettre de test privés absents (non publiés)", allow_module_level=True)
CV = (FIXTURES / "cv_candidat.txt").read_text(encoding="utf-8")
LETTER = (FIXTURES / "lettre_candidat.txt").read_text(encoding="utf-8")
TITLE = "Développeur Full-Stack React / Node.js en alternance (H/F)"


def kw(keyword: str, weight: float = 1.0, category: str = "hard") -> KeywordHit:
    return KeywordHit(keyword=keyword, weight=weight, category=category)


KEYWORDS = [
    kw("React"), kw("Node.js"), kw("TypeScript"), kw("PostgreSQL", 0.9), kw("Docker", 0.5, "tool"),
    kw("Git", 0.9, "tool"), kw("Scrum", 0.5), kw("Tests unitaires", 0.5), kw("Autonomie", 0.8, "soft"),
    kw("Anglais", 0.5, "language"), kw("Développeur Full-Stack", 0.5, "title"),
]


def test_cv_fixture_scores_well_and_explains():
    report = score_document(CV, KEYWORDS, TITLE, kind="cv")
    assert report.score >= 75
    assert report.section_score == 100
    assert report.title_score >= 85
    matched = {k.keyword for k in report.matched}
    assert {"React", "Node.js", "TypeScript", "PostgreSQL", "Docker", "Git", "Autonomie", "Anglais"} <= matched
    assert {k.keyword for k in report.missing} == {"Scrum", "Tests unitaires"}
    assert all(k.found for k in report.matched) and not any(k.found for k in report.missing)
    assert report.word_count > 200
    assert score_document(CV, KEYWORDS, TITLE) == report  # deterministic


def test_synonyms_parent_child_and_related_credit():
    cv = "Compétences\nPostgres, ReactJS, GitHub Actions\nExpérience professionnelle\nAPI avec NodeJS et MySQL"
    matched, missing = keyword_coverage(
        cv, [kw("PostgreSQL"), kw("React"), kw("CI/CD"), kw("Node.js"), kw("SQL"), kw("MariaDB"), kw("Vue.js")]
    )
    assert {k.keyword for k in matched} == {"PostgreSQL", "React", "CI/CD", "Node.js", "SQL"}
    assert {k.keyword for k in missing} == {"MariaDB", "Vue.js"}  # siblings only → partial credit, still missing
    report = score_document(cv, [kw("MariaDB")], "Développeur")
    assert 0 < report.keyword_score < 70
    assert any("MariaDB" in w and "MySQL" in w for w in report.warnings)


def test_parent_only_gives_partial_credit_and_warning():
    report = score_document("Compétences : SQL\nExpérience : requêtes SQL", [kw("PostgreSQL")], "Développeur")
    assert report.keyword_score == pytest.approx(50.0)
    assert any("PostgreSQL" in w and "SQL" in w for w in report.warnings)


def test_skills_section_only_is_worth_less_than_evidence_in_experience():
    listed = "Compétences\nReact, Docker\nExpérience professionnelle\nDéveloppement web"
    evidenced = "Compétences\nReact, Docker\nExpérience professionnelle\nApplication React déployée avec Docker"
    keywords = [kw("React"), kw("Docker")]
    assert score_document(evidenced, keywords, "").keyword_score == 100
    assert score_document(listed, keywords, "").keyword_score == pytest.approx(80.0)


def test_fuzzy_multiword_phrases_and_single_word_stems():
    text = "J'ai géré la maintenance corrective d'applications métier et la gestion de la production."
    matched, _ = keyword_coverage(text, [kw("gestion production"), kw("applications métiers")])
    assert {k.keyword for k in matched} == {"gestion production", "applications métiers"}
    _, missing = keyword_coverage(text, [kw("comptabilité analytique")])
    assert missing and missing[0].keyword == "comptabilité analytique"


def test_keyword_stuffing_is_not_rewarded_but_penalized():
    base = CV
    stuffed = CV + "\nReact React React React React React React React"
    normal = score_document(base, KEYWORDS, TITLE)
    spam = score_document(stuffed, KEYWORDS, TITLE)
    assert spam.keyword_score == normal.keyword_score  # frequency capped
    assert spam.score < normal.score
    assert any("bourrage" in w for w in spam.warnings)


def test_cv_without_contact_dates_or_sections_gets_warnings():
    report = score_document("Développeur React\nJ'aime coder en React et Node.js.", [kw("React")], "Développeur React")
    assert report.format_score < 30
    text = " ".join(report.warnings)
    assert "e-mail" in text and "téléphone" in text and "date" in text.lower()
    assert "Expérience professionnelle" in text and "Formation" in text
    assert report.title_score == 100


def test_format_detects_year_only_dates_and_bad_glyphs():
    icon = chr(0xF0E0)  # Font Awesome envelope glyph (private-use area)
    cv = "\n".join(
        ["Jean DUPONT", f"{icon} jean@example.com · 06 12 34 56 78", "Expérience", "Développeur 2024 - 2025", "Stage 2023"]
    )
    report = score_document(cv, [], "")
    joined = " ".join(report.warnings)
    assert "non lisibles" in joined
    assert "année seule" in joined
    assert report.keyword_score == 50  # neutral without keywords
    assert "Aucun mot-clé" in joined


def test_letter_fixture_structure_and_title():
    report = score_document(LETTER, KEYWORDS, TITLE, kind="letter")
    assert report.title_score == 100
    assert report.section_score == 100  # greeting + closing + 4 body paragraphs
    assert report.score >= 70
    assert {"React", "TypeScript", "PostgreSQL"} <= {k.keyword for k in report.matched}


def test_letter_problems_are_reported():
    letter = "Bonjour,\n\nJe suis dynamique et motivé. {entreprise} XXX\n\nMerci."
    report = score_document(letter, [kw("React")], "Développeur web", kind="letter")
    joined = " ".join(report.warnings)
    assert "politesse" in joined and "paragraphe" in joined and "Champ à compléter" in joined
    assert "Clichés" in joined and "courte" in joined
    assert "n'apparaît pas" in joined
    assert report.score < 30


def test_long_letter_and_long_cv_warnings():
    long_text = "Madame, Monsieur,\n\n" + ("Développement React et Node.js au quotidien. " * 160)
    letter = score_document(long_text, [kw("React")], "", kind="letter")
    assert any("longue" in w for w in letter.warnings)
    cv = score_document(long_text, [kw("React")], "")
    assert any("trop long" in w for w in cv.warnings)
