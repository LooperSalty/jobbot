"""Live check with the real LLM (claude -p): tailored CV + letter for a realistic offer, copied to output/samples/.

Run explicitly: .venv\\Scripts\\python.exe -m pytest tests/test_documents_live.py -m live -s
Then open output/samples/<cas>/*.pdf to check them visually (1 page, clean, accents, no overflow).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jobbot import clock
from jobbot.config import load_settings
from jobbot.context import AppContext
from jobbot.documents.render import pdf_pages, pdf_text
from jobbot.documents.service import prepare_documents
from jobbot.llm import ClaudeCLIClient, LLMError
from jobbot.matching._doccheck import count_words, letter_paragraphs
from jobbot.models import Company, JobOffer
from jobbot.textutil import stable_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "documents"
SAMPLES = PROJECT_ROOT / "output" / "samples"

pytestmark = [pytest.mark.live, pytest.mark.timeout(1800)]


@pytest.fixture
def live_ctx(project_root: Path) -> AppContext:
    try:
        llm = ClaudeCLIClient(load_settings(PROJECT_ROOT).llm)
    except LLMError as exc:
        pytest.skip(f"LLM indisponible : {exc}")
    ctx = AppContext.build(project_root, dry_run=True, llm=llm)
    yield ctx
    ctx.repo.close()


def _publish(docs, name: str) -> Path:
    out = SAMPLES / name
    out.mkdir(parents=True, exist_ok=True)
    for path in (docs.cv_pdf, docs.cv_docx, docs.letter_pdf):
        if path:
            shutil.copy(path, out / Path(path).name)
    (out / "rapport.json").write_text(json.dumps({
        "cv_ats": docs.cv_ats.model_dump(mode="json") if docs.cv_ats else None,
        "letter_ats": docs.letter_ats.model_dump(mode="json") if docs.letter_ats else None,
        "truth_warnings": docs.truth_warnings, "email_subject": docs.email_subject, "email_body": docs.email_body,
        "letter_text": docs.letter_text,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _report(docs, out: Path) -> None:
    """ASCII only: the console may be cp1252 when the output is redirected."""
    print(f"\nCV ATS {docs.cv_ats.score} | lettre {docs.letter_ats.score} -> {out}")


def _check(docs) -> None:
    assert pdf_pages(Path(docs.cv_pdf)) == 1
    assert pdf_pages(Path(docs.letter_pdf)) == 1
    text = pdf_text(Path(docs.cv_pdf))
    assert "Expérience professionnelle".upper() in text.upper() and "é" in text
    assert docs.truth_warnings == []
    assert docs.cv_ats.score >= 75
    body = [p for p in letter_paragraphs(docs.letter_text) if not p.startswith("Je vous prie")]
    assert 200 <= sum(count_words(p) for p in body) <= 380
    assert count_words(docs.email_body) <= 150


def test_live_alternance_offer(live_ctx):
    data = json.loads((FIXTURES / "jobs.json").read_text(encoding="utf-8"))["alternance_fullstack_metz"]
    job = JobOffer(id=stable_id(data["source"], data["external_id"]), found_at=clock.now(), **data)
    live_ctx.repo.upsert_job(job)
    docs = prepare_documents(live_ctx, job=job)
    out = _publish(docs, "alternance_fullstack_metz")
    _report(docs, out)
    _check(docs)
    assert "Exemple Digital School" in docs.letter_text and "24 mois" in docs.letter_text


def test_live_spontaneous_application(live_ctx):
    company = Company(
        id=stable_id("company", "812345678"), name="Numéria Solutions", siren="812345678", naf_code="62.01Z",
        naf_label="Programmation informatique", headcount_range="20 à 49 salariés", city="Metz",
        postal_code="57000", description="Éditeur messin de logiciels SaaS (React, Node.js, PostgreSQL) pour les "
        "collectivités locales du Grand Est.", found_at=clock.now(),
    )
    docs = prepare_documents(live_ctx, company=company)
    out = _publish(docs, "candidature_spontanee")
    _report(docs, out)
    _check(docs)
    assert "STOP" in docs.email_body
