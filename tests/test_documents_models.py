"""documents.models / documents._format: section view, plain-text forms, formatting helpers."""

from __future__ import annotations

from datetime import datetime

from jobbot.clock import PARIS
from jobbot.config import Experience, Language, Project, SkillGroup
from jobbot.documents import _format as fmt
from jobbot.documents.models import CoverLetter, TailoredCV, cv_sections, cv_to_text, letter_to_text
from jobbot.matching._doccheck import split_sections


def _cv(profile, **overrides) -> TailoredCV:
    base = dict(
        identity=profile.identity,
        headline="Développeur Full-Stack — Alternance Bac+5 (24 mois) dès octobre 2026",
        summary="Développeur **full-stack** junior, autonome et rigoureux.",
        experiences=[Experience(title="Développeur Full-Stack — Stage", company="ACME INDUSTRIE", location="Metz (57)",
                                start="2026-02", end="2026-06", bullets=["Conception d'une **application** React."])],
        projects=[Project(name="Cartes", description="Application mobile Flutter.", skills=["Flutter", "Python"])],
        education=list(profile.education),
        skills=[SkillGroup(category="Langages", items=["TypeScript", "Python"])],
        languages=[Language(name="Anglais", level="B2")],
        interests=["Veille technologique", "jeux vidéo (Roblox/Luau", "Unity)"],
        alternance=profile.alternance,
    )
    base.update(overrides)
    return TailoredCV(**base)


def test_format_dates_and_periods():
    assert fmt.format_date("2026-02") == "Février 2026"
    assert fmt.format_date("02/2026") == "Février 2026"
    assert fmt.format_date("2025") == "2025"
    assert fmt.format_date("2026-13") == "2026-13"
    assert fmt.format_period("2026-02", "2026-06") == "Février 2026 – Juin 2026"
    assert fmt.format_period("2025", "") == "2025 – aujourd'hui"
    assert fmt.format_period("2025", "2025") == "2025"
    assert fmt.format_period("", "2022") == "2022"
    assert fmt.month_label("2026-10") == "octobre 2026"
    assert fmt.parse_year_month("2026-10") == (2026, 10)
    assert fmt.parse_year_month("2026") is None
    assert fmt.has_started("2026-10", datetime(2026, 11, 2, tzinfo=PARIS))
    assert not fmt.has_started("2026-10", datetime(2026, 9, 23, tzinfo=PARIS))


def test_contact_items_urls_and_helpers(profile):
    items = fmt.contact_items(profile.identity)
    texts = [i.text for i in items]
    assert texts[0] == "Metz (57)"
    assert "jean.dupont@example.com" in texts
    assert "linkedin.com/in/jean-dupont-exemple" in texts and "github.com/jean-dupont-exemple" in texts
    assert any(i.href == "mailto:jean.dupont@example.com" for i in items)
    assert any(i.href and i.href.startswith("tel:+336") for i in items)
    assert fmt.display_url("https://www.example.org/") == "example.org"
    assert fmt.department("97411") == "974" and fmt.department("x") == ""
    assert fmt.ascii_token("Hélène Marie") == "Helene-Marie" and fmt.ascii_token("") == "x"
    assert fmt.merge_unbalanced(["a (b", "c)", "d", ""]) == ["a (b, c)", "d"]
    assert fmt.merge_unbalanced(["x (y"]) == ["x (y"]


def test_emphasis_markers():
    assert fmt.strip_emphasis("Un **mot** clé ** seul") == "Un mot clé seul"
    assert fmt.emphasis_html("A **<b>** & c") == "A <strong>&lt;b&gt;</strong> &amp; c"


def test_sections_order_and_content(profile):
    cv = _cv(profile)
    keys = [s.key for s in cv_sections(cv)]
    assert keys == ["profile", "skills", "experience", "projects", "education", "languages", "interests"]
    education = next(s for s in cv_sections(cv) if s.key == "education")
    programme = education.entries[0]
    assert "Octobre 2026" in programme.meta  # start date of the apprenticeship programme
    assert programme.note == ("Titre RNCP niveau 7 · alternance de 24 mois · "
                              "rythme : 3 semaines en entreprise / 1 semaine en formation")
    interests = next(s for s in cv_sections(cv) if s.key == "interests")
    assert interests.inline == "Veille technologique · Jeux vidéo (Roblox/Luau, Unity)"  # consistent capitals
    empty = _cv(profile, summary="", skills=[], projects=[], languages=[], interests=[], experiences=[])
    assert [s.key for s in cv_sections(empty)] == ["education"]


def test_cv_to_text_is_ats_readable(profile):
    text = cv_to_text(_cv(profile))
    lines = text.splitlines()
    assert lines[0] == "Jean DUPONT"
    assert "jean.dupont@example.com" in lines[2] and "+33 6 12 34 56 78" in lines[2]
    assert "**" not in text
    assert "Février 2026 – Juin 2026" in text
    assert "Cartes — Application mobile Flutter. (Python)" in text  # skills not already in the description
    sections = split_sections(text)
    assert {"profile", "skills", "experience", "projects", "education", "languages", "interests"} <= set(sections)
    assert "Langages : TypeScript, Python" in sections["skills"]


def test_letter_to_text():
    letter = CoverLetter(subject="Candidature – Développeur", greeting="Madame, Monsieur,",
                         paragraphs=["Un.", "  Deux  mots. "], closing="Salutations.", email_subject="s",
                         email_body="b", signature="Jean DUPONT")
    assert letter_to_text(letter) == (
        "Objet : Candidature – Développeur\n\nMadame, Monsieur,\n\nUn.\n\nDeux mots.\n\nSalutations.\n\nJean DUPONT"
    )
    assert letter_to_text(letter.model_copy(update={"subject": "", "signature": ""})).startswith("Madame")
