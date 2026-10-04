"""documents.importer: existing CV (PDF) → profile dict → config/profile.yaml."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobbot.config import Profile, load_profile
from jobbot.documents import importer
from jobbot.documents._import_rules import parse_cv_text, parse_period
from jobbot.documents.models import DocumentError
from jobbot.llm import FakeLLM, LLMError

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
CV_FILE = FIXTURES / "cv_candidat_original.txt"  # private (git-ignored): text of a real CV, kept off the public repo
if not CV_FILE.exists():
    pytest.skip("CV de test privé absent (non publié)", allow_module_level=True)
CV_TEXT = CV_FILE.read_text(encoding="utf-8")

LLM_PROFILE = {
    "identity": {"first_name": "Jean", "last_name": "DUPONT", "email": "jean.dupont@example.com",
                 "phone": "+33 6 12 34 56 78", "city": "Metz", "postal_code": "57000",
                 "github": "https://github.com/jean-dupont-exemple", "extra": "ignored"},
    "headline": "Développeur Full-Stack Web & Logiciel — Recherche alternance Bac+5",
    "summary": "Diplômé du BUT Informatique.",
    "experiences": [{"title": "Développeur Full-Stack — Stage", "company": "ACME INDUSTRIE", "start": "2026-02",
                     "end": "2026-06", "kind": "internship", "bullets": ["Conception d'une application intranet."],
                     "skills": ["Next.js", "PostgreSQL"]}],
    "education": [{"degree": "BUT Informatique", "school": "IUT Exemple", "start": "2023", "end": "2026"}],
    "skills": [{"category": "Langages", "items": ["TypeScript", "Python"]}],
    "languages": [{"name": "Anglais", "level": "B2"}],
    "interests": ["Veille technologique"],
}


@pytest.fixture
def fake_pdf(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "cv.pdf"
    path.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(importer, "pdf_text", lambda p: CV_TEXT)
    return path


def test_parse_period():
    assert parse_period("Février – Juin 2026") == ("2026-02", "2026-06")
    assert parse_period("Depuis 2025") == ("2025", "")
    assert parse_period("Étés 2022 – 2025") == ("2022", "2025")
    assert parse_period("Septembre 2023 à aujourd'hui") == ("2023-09", "")
    assert parse_period("sans date") == ("", "")


def test_rules_parse_a_real_cv_text():
    data = parse_cv_text(CV_TEXT)
    identity = data["identity"]
    assert (identity["first_name"], identity["last_name"]) == ("Jean", "DUPONT")
    assert identity["email"] == "jean.dupont@example.com" and identity["phone"] == "+33 6 12 34 56 78"
    assert (identity["city"], identity["postal_code"]) == ("Metz", "57000")
    assert identity["driving_license"] == "Permis B — véhicule personnel"
    assert data["headline"].startswith("Développeur Full-Stack Web & Logiciel")
    assert data["summary"].startswith("Diplômé du BUT Informatique")
    exps = data["experiences"]
    assert [e["kind"] for e in exps] == ["internship", "freelance", "internship", "seasonal"]
    assert (exps[0]["company"], exps[0]["start"], exps[0]["end"]) == ("ACME INDUSTRIE", "2026-02", "2026-06")
    assert len(exps[0]["bullets"]) == 4 and exps[0]["bullets"][0].endswith("gestion fine des droits.")
    assert exps[1]["start"] == "2025" and exps[1]["end"] == ""
    assert data["education"][1]["school"] == "IUT Exemple, Université Exemple"
    groups = {g["category"]: g["items"] for g in data["skills"]}
    assert groups["Langages"] == ["TypeScript", "JavaScript", "Python", "C#", "PHP", "C", "SQL"]
    assert "Linux (Arch, Debian)" in groups["DevOps & outils"]
    assert data["languages"] == [{"name": "Anglais", "level": "B2"}]
    assert "développement de jeux vidéo (Roblox/Luau, Unity)" in data["interests"]


def test_import_cv_with_llm(fake_pdf):
    llm = FakeLLM(json.dumps(LLM_PROFILE, ensure_ascii=False))
    data = importer.import_cv(fake_pdf, llm)
    profile = Profile.model_validate(data)
    assert profile.identity.github == "https://github.com/jean-dupont-exemple"
    assert profile.experiences[0].skills == ["Next.js", "PostgreSQL"]
    assert profile.target.job_titles == ["Développeur Full-Stack Web & Logiciel"]
    assert profile.target.locations[0].city == "Metz"
    assert "Texte du CV" in llm.calls[0][0] and CV_TEXT[:40] in llm.calls[0][0]


def test_import_cv_falls_back_to_rules(fake_pdf):
    def boom(prompt, system, fast):
        raise LLMError("quota")

    data = importer.import_cv(fake_pdf, FakeLLM(boom))
    profile = Profile.model_validate(data)
    assert profile.identity.full_name == "Jean DUPONT"
    assert len(profile.experiences) == 4
    assert importer.import_cv(fake_pdf, None) == data


def test_import_cv_rejects_scanned_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "pdf_text", lambda p: "  ")
    with pytest.raises(DocumentError, match="scanné"):
        importer.import_cv(tmp_path / "scan.pdf", None)


def test_invalid_import_raises(monkeypatch):
    bad = importer.ImportedCV.model_validate({"identity": {"first_name": "A", "last_name": "B"},
                                              "languages": [{"name": "Anglais"}]})
    monkeypatch.setattr(importer, "_target_defaults", lambda data: {"contract_types": ["inconnu"]})
    with pytest.raises(DocumentError, match="invalide"):
        importer.to_profile_dict(bad)


def test_write_profile_yaml_round_trip_and_backup(fake_pdf, tmp_path):
    data = importer.import_cv(fake_pdf, None)
    out = tmp_path / "config" / "profile.yaml"
    assert importer.write_profile_yaml(data, out) == out
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Profil maître") and "Développeur Full-Stack" in text  # UTF-8, no escapes
    loaded = load_profile(out)
    assert loaded.identity.email == "jean.dupont@example.com"
    assert loaded.experiences[0].company == "ACME INDUSTRIE"
    importer.write_profile_yaml({"identity": {"first_name": "X"}}, out)  # incomplete: written, warned
    assert (tmp_path / "config" / "profile.yaml.bak").read_text(encoding="utf-8") == text


@pytest.mark.browser
def test_import_a_cv_rendered_by_jobbot(profile, tmp_path):
    from jobbot.documents.render import render_cv_pdf
    from jobbot.documents.tailor import tailor_cv

    cv, _, _ = tailor_cv(profile, [], None)
    pdf = render_cv_pdf(cv, tmp_path / "cv.pdf")
    data = importer.import_cv(pdf, None)
    assert data["identity"]["email"] == profile.identity.email
    assert any(e["company"] == "ACME INDUSTRIE" for e in data["experiences"])
