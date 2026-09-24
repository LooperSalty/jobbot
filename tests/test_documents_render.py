"""documents.render: HTML, one-page PDF auto-fit (headless bundled Chromium), DOCX, letter PDF, parse-back."""

from __future__ import annotations

import asyncio
import io
import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader, PdfWriter

from jobbot import clock
from jobbot.config import Experience, Project
from jobbot.documents import _fit, _pdf, render
from jobbot.documents.letter import write_cover_letter
from jobbot.documents.models import DocumentError, TailoredCV
from jobbot.documents.tailor import tailor_cv
from jobbot.matching._doccheck import split_sections
from jobbot.matching.ats import score_document
from jobbot.matching.keywords import extract_keywords
from jobbot.models import JobOffer
from jobbot.textutil import stable_id

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
JOBS = json.loads((FIXTURES / "jobs.json").read_text(encoding="utf-8"))
LONG_BULLET = ("Conception et développement d'une application web full-stack (React, Node.js, PostgreSQL, Docker) : "
               "recueil des besoins, spécifications, développement, tests, mise en production et documentation.")


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 9, 23, 8, 0, tzinfo=UTC))


@pytest.fixture
def job() -> JobOffer:
    data = JOBS["alternance_fullstack_metz"]
    return JobOffer(id=stable_id(data["source"], data["external_id"]), found_at=clock.now(), **data)


@pytest.fixture
def cv(profile, job) -> TailoredCV:
    return tailor_cv(profile, extract_keywords(job, None), None, job=job)[0]


def _pdf_bytes(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- HTML / fitting rules (no browser)


def test_cv_html_is_single_column_and_ats_safe(cv):
    html = render.render_cv_html(cv)
    lowered = html.lower()
    for forbidden in ("<table", "<img", "display: grid", "display:grid", "position: absolute", "<svg", "@font-face"):
        assert forbidden not in lowered
    assert "Expérience professionnelle" in html and "Compétences" in html and "Formation" in html
    assert 'href="mailto:jean.dupont@example.com"' in html
    assert "API REST" in html  # multi-word skills never split across lines
    assert "<strong>" not in render.render_cv_html(cv.model_copy(update={"summary": "Sans gras"}))
    assert "<strong>Docker</strong>" in render.render_cv_html(cv.model_copy(update={"summary": "Avec **Docker**"}))


def test_trim_order_drops_least_relevant_content_first(profile, cv):
    long_cv = cv.model_copy(update={
        "experiences": [cv.experiences[0].model_copy(update={"bullets": [LONG_BULLET] * 5}), *cv.experiences[1:]],
        "projects": list(profile.projects[:2]),
        "interests": ["a", "b", "c", "d", "e"],
    })
    steps = []
    current = long_cv
    while (current := _fit.trim_cv(current)) is not None:
        steps.append(([len(e.bullets) for e in current.experiences], len(current.projects), len(current.interests),
                      len(current.skills)))
    assert steps[0] == ([4, 2, 1, 1], 2, 5, len(cv.skills))
    assert steps[1][1] == 1 and steps[2][2] == 3
    assert steps[3][0] == [3, 2, 1, 1]
    assert steps[-1][0] == [1, 1] and steps[-1][3] == _fit.MIN_SKILL_GROUPS
    assert _fit.trim_cv(TailoredCV(identity=profile.identity, headline="h", summary="s")) is None


def test_run_isolated_moves_work_out_of_a_running_loop():
    assert _pdf.run_isolated(lambda: threading.current_thread().name) == threading.current_thread().name

    async def inside_loop() -> str:
        return _pdf.run_isolated(lambda: threading.current_thread().name)

    assert asyncio.run(inside_loop()).startswith("jobbot-pdf")


def test_fit_loop_with_a_fake_browser(cv, tmp_path, monkeypatch):
    class FakePage:
        def __init__(self):
            self.loads, self.pdfs = 0, [_pdf_bytes(2), _pdf_bytes(1)]

        def set_content(self, html, wait_until="load"):
            self.loads += 1

        def evaluate(self, script, level):
            return 1500.0 if self.loads == 1 else 900.0  # first content overflows at every density

        def pdf(self, **kwargs):
            assert kwargs["tagged"] is True and kwargs["format"] == "A4" and kwargs["display_header_footer"] is False
            return self.pdfs.pop(0)

    page = FakePage()

    @contextmanager
    def fake_page():
        yield page

    monkeypatch.setattr(_pdf, "chromium_page", fake_page)
    path, fitted = render.render_cv_pdf_fitted(cv, tmp_path / "cv.pdf")
    assert render.pdf_pages(path) == 1
    assert len(fitted.interests) == 3  # 1st trim (content overflowing at every density level)
    assert sum(len(e.bullets) for e in fitted.experiences) == sum(len(e.bullets) for e in cv.experiences) - 1
    assert page.loads == 3  # 2nd trim: the printed PDF still had 2 pages


def test_chromium_errors_become_document_errors(cv, tmp_path, monkeypatch):
    import playwright.sync_api as sync_api

    def broken():
        raise sync_api.Error("Executable doesn't exist")

    monkeypatch.setattr(sync_api, "sync_playwright", broken)
    with pytest.raises(DocumentError, match="playwright install chromium"):
        render.render_cv_pdf(cv, tmp_path / "cv.pdf")


def test_pdf_text_on_invalid_files(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(DocumentError):
        render.pdf_text(bad)
    with pytest.raises(DocumentError):
        render.pdf_pages(tmp_path / "missing.pdf")
    with pytest.raises(DocumentError):
        _pdf.page_count(b"nope")


# --------------------------------------------------------------------------- DOCX


def test_cv_docx_has_no_table_header_or_image(cv, tmp_path):
    path = render.render_cv_docx(cv, tmp_path / "cv.docx")
    doc = Document(str(path))
    assert not doc.tables and not doc.inline_shapes
    assert all(not p.text.strip() for s in doc.sections for p in s.header.paragraphs)
    texts = [p.text for p in doc.paragraphs]
    assert texts[0] == "Jean DUPONT" and "jean.dupont@example.com" in texts[2]
    headings = [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]
    assert headings[:3] == ["Profil", "Compétences", "Expérience professionnelle"]
    assert any(p.style.name == "List Bullet" for p in doc.paragraphs)
    assert doc.core_properties.author == "Jean DUPONT" and doc.core_properties.keywords == ""


def test_cv_docx_errors_are_reported(cv, tmp_path, monkeypatch):
    monkeypatch.setattr(render, "build_cv_docx", lambda *_: (_ for _ in ()).throw(OSError("disque plein")))
    with pytest.raises(DocumentError, match="disque plein"):
        render.render_cv_docx(cv, tmp_path / "cv.docx")


# --------------------------------------------------------------------------- real Chromium


@pytest.mark.browser
def test_cv_pdf_is_one_tagged_page_with_readable_text(cv, job, tmp_path):
    path = render.render_cv_pdf(cv, tmp_path / "cv.pdf")
    reader = PdfReader(str(path))
    assert len(reader.pages) == 1
    assert "/StructTreeRoot" in reader.trailer["/Root"]  # tagged PDF
    assert path.stat().st_size < 500_000
    text = render.pdf_text(path)
    lines = text.splitlines()
    assert lines[0] == "Jean DUPONT"
    assert any("jean.dupont@example.com" in line and "+33 6 12 34 56 78" in line for line in lines[:4])
    assert "jean-dupont.example.com" in text  # contact items never broken
    assert {"profile", "skills", "experience", "education", "languages"} <= set(split_sections(text))
    report = score_document(text, extract_keywords(job, None), job.title)
    assert report.score >= 80 and report.format_score == 100


@pytest.mark.browser
def test_overlong_cv_is_trimmed_to_one_page(profile, cv, tmp_path):
    bloated = cv.model_copy(update={
        "experiences": [e.model_copy(update={"bullets": [LONG_BULLET] * 5}) for e in cv.experiences],
        "projects": list(profile.projects),
    })
    path, fitted = render.render_cv_pdf_fitted(bloated, tmp_path / "cv.pdf")
    assert render.pdf_pages(path) == 1
    assert sum(len(e.bullets) for e in fitted.experiences) < 20
    assert fitted.experiences[0].bullets  # the key experience keeps bullets


@pytest.mark.browser
def test_letter_pdf_classic_layout(profile, job, tmp_path):
    letter = write_cover_letter(profile, None, job=job, keywords=extract_keywords(job, None))
    path = render.render_letter_pdf(letter, profile, tmp_path / "lettre.pdf",
                                    recipient="Lorraine Digital Services\nService recrutement\nMetz (57)")
    assert render.pdf_pages(path) == 1
    text = render.pdf_text(path)
    for piece in ("Jean DUPONT", "57000 Metz", "Lorraine Digital Services", "Metz, le 23 septembre 2026",
                  "Objet : Candidature – Développeur Web Fullstack en alternance", "Madame, Monsieur,",
                  "salutations distinguées"):
        assert piece in text, piece
    assert text.index("Lorraine Digital Services") < text.index("Objet")


@pytest.mark.browser
def test_long_letter_still_renders(profile, tmp_path):
    letter = write_cover_letter(profile, None)
    long_letter = letter.model_copy(update={"paragraphs": letter.paragraphs * 3})
    path = render.render_letter_pdf(long_letter, profile, tmp_path / "long.pdf")
    assert render.pdf_pages(path) >= 1


@pytest.mark.browser
def test_render_works_inside_a_running_event_loop(cv, tmp_path):
    async def inside() -> Path:
        return render.render_cv_pdf(cv.model_copy(update={"projects": [Project(name="P", description="d")],
                                                          "experiences": [Experience(title="t", company="c")]}),
                                    tmp_path / "loop.pdf")

    assert render.pdf_pages(asyncio.run(inside())) == 1
