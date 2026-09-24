"""documents.service.prepare_documents: keywords → CV → PDF parse-back ATS → letter → files → DB."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobbot import clock
from jobbot.documents import service
from jobbot.documents.models import DocumentError, cv_to_text
from jobbot.documents.render import pdf_pages
from jobbot.models import JobOffer, JobStatus
from jobbot.textutil import stable_id

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
JOBS = json.loads((FIXTURES / "jobs.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 9, 23, 8, 0, tzinfo=UTC))


@pytest.fixture
def job() -> JobOffer:
    data = JOBS["alternance_fullstack_metz"]
    return JobOffer(id=stable_id(data["source"], data["external_id"]), found_at=clock.now(), **data)


@pytest.fixture
def fake_render(monkeypatch):
    """Replace Chromium/pypdf with fakes; the "extracted" PDF text is the CV text."""
    state: dict = {"texts": {}}

    def cv_pdf(cv, out_path):
        out_path.write_bytes(b"%PDF-fake")
        state["texts"][str(out_path)] = cv_to_text(cv)
        return out_path, cv

    def letter_pdf(letter, profile, out_path, *, recipient=None):
        state["recipient"] = recipient
        out_path.write_bytes(b"%PDF-fake")
        return out_path

    def docx(cv, out_path):
        out_path.write_bytes(b"PK")
        return out_path

    monkeypatch.setattr(service, "render_cv_pdf_fitted", cv_pdf)
    monkeypatch.setattr(service, "render_letter_pdf", letter_pdf)
    monkeypatch.setattr(service, "render_cv_docx", docx)
    monkeypatch.setattr(service, "pdf_text", lambda path: state["texts"][str(path)])
    monkeypatch.setattr(service, "pdf_pages", lambda path: 1)
    return state


def test_prepare_documents_for_an_offer(ctx, job, fake_render):
    ctx.repo.upsert_job(job)
    docs = service.prepare_documents(ctx, job=job)
    folder = ctx.output_dir / "2026-09-23" / "lorraine-digital-services-alternant-developpeur-web-fullstack-h-f"
    assert Path(docs.cv_pdf) == (folder / "CV_Jean_DUPONT.pdf").resolve()
    assert Path(docs.cv_docx) == (folder / "CV_Jean_DUPONT.docx").resolve()
    assert Path(docs.letter_pdf) == (folder / "Lettre_de_motivation_Jean_DUPONT.pdf").resolve()
    assert "Lorraine" not in Path(docs.cv_pdf).name  # never the company name in attachment names
    assert docs.job_id == job.id and docs.company_id is None
    assert docs.cv_ats is not None and docs.cv_ats.score >= 80 and docs.cv_ats.title_score == 100
    assert docs.letter_ats is not None and docs.letter_ats.section_score == 100
    assert docs.truth_warnings == []
    assert docs.letter_text.startswith("Objet : Candidature – Développeur Web Fullstack en alternance")
    assert docs.email_subject.endswith("– Jean DUPONT") and "Madame, Monsieur," in docs.email_body
    assert fake_render["recipient"] == "Lorraine Digital Services\nService recrutement\nMetz (57)"
    assert ctx.repo.get_job(job.id).status == JobStatus.DOCS_READY
    saved = ctx.repo.latest_documents(job_id=job.id)
    assert saved is not None and saved.cv_pdf == docs.cv_pdf and saved.cv_ats == docs.cv_ats


def test_regenerating_documents_keeps_an_approval(ctx, job, fake_render):
    ctx.repo.upsert_job(job)
    ctx.repo.update_job_status(job.id, JobStatus.APPROVED)
    service.prepare_documents(ctx, job=job)
    assert ctx.repo.get_job(job.id).status == JobStatus.APPROVED
    ctx.repo.update_job_status(job.id, JobStatus.PENDING_REVIEW)
    service.prepare_documents(ctx, job=job)
    assert ctx.repo.get_job(job.id).status == JobStatus.DOCS_READY


def test_prepare_documents_spontaneous(ctx, make_company, fake_render):
    company = make_company(naf_label="Programmation informatique", description="Éditeur SaaS React et Node.js",
                           headcount_range="10 à 19 salariés", address="1 rue Serpenoise")
    docs = service.prepare_documents(ctx, company=company)
    assert "acme-logiciels-candidature-spontanee" in docs.cv_pdf
    assert docs.company_id == company.id and docs.job_id is None
    assert docs.email_subject.startswith("Candidature spontanée – Alternance")
    assert "STOP" in docs.email_body
    assert fake_render["recipient"] == "Acme Logiciels\nService recrutement\n1 rue Serpenoise\n57000 Metz"
    assert ctx.repo.latest_documents(company_id=company.id) is not None


def test_company_keywords(ctx, make_company):
    company = make_company(naf_label="Programmation informatique", description="Plateforme React / PostgreSQL")
    keywords = service.company_keywords(ctx, company)
    names = [k.keyword for k in keywords]
    assert names[:2] == ["PostgreSQL", "React"] and keywords[0].weight == 1.0
    assert "TypeScript" in names and "C#" in names
    title = [k for k in keywords if k.category == "title"]
    assert title and title[0].keyword == "développeur full-stack"
    assert len({n.lower() for n in names}) == len(names)
    assert service.company_keywords(ctx, None)[-1].category == "title"


def test_prepare_documents_requires_a_target(ctx):
    with pytest.raises(ValueError):
        service.prepare_documents(ctx)


def test_unreadable_pdf_falls_back_to_cv_text(ctx, job, fake_render, monkeypatch):
    def broken(path):
        raise DocumentError("PDF illisible")

    monkeypatch.setattr(service, "pdf_text", broken)
    docs = service.prepare_documents(ctx, job=job)
    assert any("illisible ou absent" in w for w in docs.cv_ats.warnings)
    assert docs.cv_ats.score >= 80


def test_two_page_cv_and_missing_letter_pdf_are_reported(ctx, job, fake_render, monkeypatch):
    def no_letter(*args, **kwargs):
        raise DocumentError("Chromium indisponible")

    def no_docx(*args, **kwargs):
        raise DocumentError("Word indisponible")

    monkeypatch.setattr(service, "pdf_pages", lambda path: 2)
    monkeypatch.setattr(service, "render_letter_pdf", no_letter)
    monkeypatch.setattr(service, "render_cv_docx", no_docx)
    docs = service.prepare_documents(ctx, job=job)
    assert "Le CV dépasse une page" in docs.cv_ats.warnings
    assert docs.letter_pdf is None and docs.cv_docx is None
    assert docs.letter_text  # the letter text is still available for forms and e-mails


def test_keywords_come_from_the_matching_module(ctx, job, fake_render, monkeypatch):
    calls = []

    def fake_extract(offer, llm, *, max_keywords=30):
        calls.append((offer.id, llm, max_keywords))
        from jobbot.models import KeywordHit
        return [KeywordHit(keyword="React", weight=1.0)]

    import jobbot.matching.keywords as keywords_module
    monkeypatch.setattr(keywords_module, "extract_keywords", fake_extract)
    docs = service.prepare_documents(ctx, job=job)
    assert calls == [(job.id, ctx.llm, service.MAX_KEYWORDS)]
    assert [k.keyword for k in docs.cv_ats.matched] == ["React"]


@pytest.mark.browser
def test_prepare_documents_end_to_end_with_chromium(ctx, job):
    ctx.repo.upsert_job(job)
    docs = service.prepare_documents(ctx, job=job)
    assert pdf_pages(Path(docs.cv_pdf)) == 1 and pdf_pages(Path(docs.letter_pdf)) == 1
    assert Path(docs.cv_docx).stat().st_size > 5_000
    assert docs.cv_ats.score >= 80 and docs.cv_ats.format_score == 100 and docs.cv_ats.section_score == 100
    assert not any("illisible" in w for w in docs.cv_ats.warnings)
