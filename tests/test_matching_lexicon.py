"""matching.lexicon: canonical skills, FR/EN variants, ambiguity-safe detection."""

from __future__ import annotations

import pytest

from jobbot.matching import lexicon
from jobbot.matching.lexicon import (
    SKILLS,
    SOFT_SKILLS,
    canonicalize,
    count_skills,
    find_languages,
    find_skills,
    find_soft_skills,
)


def test_lexicon_size_and_shape():
    assert len(SKILLS) >= 150
    assert len(SOFT_SKILLS) >= 10
    assert all(isinstance(v, list) for v in SKILLS.values())
    assert all(isinstance(v, list) and v for v in SOFT_SKILLS.values())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("JS", "JavaScript"), ("javascript", "JavaScript"), ("ES6", "JavaScript"), ("JavaScript ES6+", "JavaScript"),
        ("Node", "Node.js"), ("NodeJS", "Node.js"), ("Node.js", "Node.js"), ("node js", "Node.js"),
        ("Postgres", "PostgreSQL"), ("PostgreSQL", "PostgreSQL"), ("C#", "C#"), ("csharp", "C#"),
        (".NET", ".NET"), ("dotnet", ".NET"), (".NET 8", ".NET"), ("CI/CD", "CI/CD"), ("CI-CD", "CI/CD"),
        ("intégration continue", "CI/CD"), ("K8s", "Kubernetes"), ("Kubernetes", "Kubernetes"),
        ("React.js", "React"), ("ReactJS", "React"), ("react", "React"), ("Next.js 15", "Next.js"),
        ("Vue", "Vue.js"), ("C", "C"), ("go", "Go"), ("golang", "Go"), ("serveurs MCP", "MCP"),
        ("programmation orientée objet", "POO"), ("YOLOv8", "YOLO"), ("esprit d'équipe", "Travail en équipe"),
        ("Anglais", "Anglais"), ("intégration de LLM", "LLM"), ("mise en production", "Déploiement"),
    ],
)
def test_canonicalize_variants(text, expected):
    assert canonicalize(text) == expected


def test_canonicalize_unknown_and_mixed_terms_are_cleaned_not_invented():
    assert canonicalize("  saisie   de données ") == "saisie de données"
    assert canonicalize("HTML/CSS") == "HTML/CSS"  # two skills → no single canonical
    assert canonicalize("") == ""
    assert lexicon.is_known("postgres") and not lexicon.is_known("pêche")


def test_find_skills_on_a_real_stack_line():
    text = "Stack : React, Node.js, PostgreSQL, Docker, CI/CD (GitHub Actions), JavaScript/TypeScript"
    assert find_skills(text) == {
        "React", "Node.js", "PostgreSQL", "Docker", "CI/CD", "GitHub Actions", "JavaScript", "TypeScript",
    }


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ("C'est une belle entreprise, Vitamine C et bâtiment C.", "C"),
        ("Nous travaillons avec Google et Let's Go !", "Go"),
        ("Pôle R&D, groupe R", "R"),
        ("Maîtrise de JavaScript et TypeScript", "Java"),
        ("En vue de renforcer l'équipe, point de vue client", "Vue.js"),
        ("Réactivité et rigueur ; profil réactif", "React"),
        ("Tickets restaurant, sous la supervision de ton tuteur", "Gestion de tickets"),
        ("Tu apprendras vite et bien", "Vite"),
        ("MySQL et PostgreSQL", "SQL"),
        ("React Native pour le mobile", "React"),
        ("Intégration en Tailwind CSS", "CSS"),
        ("Tableau de bord des ventes", "Tableau"),
    ],
)
def test_ambiguous_tokens_are_not_matched(text, forbidden):
    assert forbidden not in find_skills(text)


def test_ambiguous_tokens_match_in_a_tech_context():
    assert {"C", "C#", "Go", "R", "Python"} <= find_skills("Langages : C, C#, Python, Go, R")
    assert {"C", "C++"} <= find_skills("Développement embarqué en C/C++")
    assert "Vue.js" in find_skills("Front en Vue, React ou Angular")
    assert "Express.js" in find_skills("API Node.js / Express")
    assert "Go" in find_skills("Vous codez en langage Go")


def test_tech_safe_boundaries_for_symbols():
    assert find_skills("C# / .NET Core, C++ et ASP.NET Core") == {"C#", ".NET", "C++", "ASP.NET"}
    assert {"C#", ".NET"} <= find_skills("Développeur C#.NET")
    assert find_skills("Python 3.12, Next.js 15, HTML5/CSS3") == {"Python", "Next.js", "HTML", "CSS"}
    assert "Java" in find_skills("Java/JavaScript") and "JavaScript" in find_skills("Java/JavaScript")


def test_count_skills_counts_mentions_and_lenient_mode():
    counts = count_skills("React ici, React là, et encore React.js")
    assert counts["React"] == 3
    assert "C" not in count_skills("C")  # alone, no context
    assert "C" in count_skills("C", lenient=True)  # e.g. a CV skills section
    assert count_skills("") == {}


def test_soft_skills_and_languages():
    assert find_soft_skills("Autonome, rigoureuse, esprit d'équipe") == {"Autonomie", "Rigueur", "Travail en équipe"}
    assert find_languages("Anglais courant, allemand apprécié") == {"Anglais", "Allemand"}


def test_hierarchy_helpers():
    assert lexicon.ancestors("Next.js") == {"React", "JavaScript"}
    assert "PostgreSQL" in lexicon.descendants("SQL")
    assert "Vue.js" in lexicon.related("React")
    assert lexicon.expand_with_ancestors(["PostgreSQL"]) == {"PostgreSQL", "SQL"}
    assert lexicon.skill_category("Docker") == "tool"
    assert lexicon.skill_category("React") == "hard"
    assert lexicon.skill_category("Autonomie") == "soft"
    assert lexicon.skill_category("Anglais") == "language"


def test_profile_skills_are_all_recognised(profile):
    unknown = [s for s in profile.all_skills() if not lexicon.is_known(s)]
    # Only a handful of purely descriptive items may stay outside the lexicon.
    assert len(unknown) <= 4, unknown
