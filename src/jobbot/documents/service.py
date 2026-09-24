"""prepare_documents: keywords → tailored CV → one-page PDF (+ DOCX) → parse-back ATS → letter → PDF → DB.

Files go to ``output/<yyyy-mm-dd>/<slug-company>-<slug-title>/`` as ``CV_<Prénom>_<NOM>.pdf|.docx`` and
``Lettre_de_motivation_<Prénom>_<NOM>.pdf`` (ASCII, never the company name — research §6.5). The final CV ATS
report is computed on the text extracted back from the PDF, i.e. what an ATS will actually read.
"""

from __future__ import annotations

import logging
from pathlib import Path

from jobbot import clock
from jobbot.context import AppContext
from jobbot.documents._format import ascii_token
from jobbot.documents.letter import write_cover_letter
from jobbot.documents.models import CoverLetter, DocumentError, TailoredCV, cv_to_text, letter_to_text
from jobbot.documents.render import pdf_pages, pdf_text, render_cv_docx, render_cv_pdf_fitted, render_letter_pdf
from jobbot.documents.tailor import tailor_cv
from jobbot.matching import lexicon
from jobbot.matching.ats import score_document
from jobbot.models import ATSReport, Company, JobOffer, JobStatus, KeywordHit, TailoredDocuments
from jobbot.textutil import slugify

log = logging.getLogger(__name__)

MAX_KEYWORDS = 30
COMPANY_SKILL_WEIGHT = 1.0
TARGET_KEYWORD_WEIGHT = 0.8
TITLE_WEIGHT = 0.5
MAX_BYTES = 1_000_000
KEEP_STATUSES = frozenset({JobStatus.APPROVED, JobStatus.APPLIED})  # never downgraded to DOCS_READY


# --------------------------------------------------------------------------- keywords


def _job_keywords(ctx: AppContext, job: JobOffer) -> list[KeywordHit]:
    from jobbot.matching.keywords import extract_keywords

    return extract_keywords(job, ctx.llm, max_keywords=MAX_KEYWORDS)


def spontaneous_title(ctx: AppContext) -> str:
    titles = ctx.profile.target.job_titles
    return titles[0] if titles else ctx.profile.headline.split(" — ")[0]


def company_keywords(ctx: AppContext, company: Company | None) -> list[KeywordHit]:
    """Spontaneous application: skills named in the company's activity/description, then the profile's target
    keywords, then the target title."""
    hits: dict[str, KeywordHit] = {}
    text = " ".join(x for x in ((company.naf_label, company.description) if company else ()) if x)
    for name in sorted(lexicon.find_skills(text)):
        hits.setdefault(name.lower(), KeywordHit(keyword=name, weight=COMPANY_SKILL_WEIGHT,
                                                 category=lexicon.skill_category(name)))
    for raw in ctx.profile.target.keywords:
        name = lexicon.canonicalize(raw)
        if name:
            hits.setdefault(name.lower(), KeywordHit(keyword=name, weight=TARGET_KEYWORD_WEIGHT,
                                                     category=lexicon.skill_category(name)))
    title = spontaneous_title(ctx)
    hits.setdefault(title.lower(), KeywordHit(keyword=title, weight=TITLE_WEIGHT, category="title"))
    return list(hits.values())[:MAX_KEYWORDS]


# --------------------------------------------------------------------------- files


def output_folder(ctx: AppContext, job: JobOffer | None, company: Company | None) -> Path:
    day = clock.to_paris(clock.now()).strftime("%Y-%m-%d")
    if job is not None:
        name = f"{slugify(job.company or 'entreprise', 40)}-{slugify(job.title, 50)}"
    else:
        name = f"{slugify(company.name if company else 'entreprise', 50)}-candidature-spontanee"
    folder = ctx.output_dir / day / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def file_names(ctx: AppContext) -> tuple[str, str, str]:
    identity = ctx.profile.identity
    who = f"{ascii_token(identity.first_name)}_{ascii_token(identity.last_name).upper()}"
    return f"CV_{who}.pdf", f"CV_{who}.docx", f"Lettre_de_motivation_{who}.pdf"


def _recipient(job: JobOffer | None, company: Company | None) -> str:
    if job is not None:
        return "\n".join(x for x in (job.company, job.contact_name or "Service recrutement", job.location) if x)
    if company is None:
        return ""
    place = " ".join(x for x in (company.postal_code or "", company.city) if x)
    return "\n".join(x for x in (company.name, "Service recrutement", company.address or "", place) if x)


# --------------------------------------------------------------------------- steps


def _final_cv_ats(path: Path, fitted: TailoredCV, keywords: list[KeywordHit], title: str) -> ATSReport:
    """ATS report on the text extracted back from the PDF (fallback: the CV text), plus file checks."""
    extra: list[str] = []
    try:
        text = pdf_text(path)
        if pdf_pages(path) > 1:
            extra.append("Le CV dépasse une page")
    except DocumentError as exc:
        log.warning("Relecture du CV impossible : %s", exc)
        text = ""
    if len(text) < 200:
        extra.append("Texte du PDF illisible ou absent : score calculé sur le texte du CV")
        text = cv_to_text(fitted)
    if path.exists() and path.stat().st_size > MAX_BYTES:
        extra.append("Fichier CV supérieur à 1 Mo")
    report = score_document(text, keywords, title, kind="cv")
    return report.model_copy(update={"warnings": [*report.warnings, *extra]}) if extra else report


def _docx(cv: TailoredCV, path: Path) -> str | None:
    try:
        return str(render_cv_docx(cv, path).resolve())
    except DocumentError as exc:
        log.warning("%s", exc)
        return None


def _letter_files(ctx: AppContext, letter: CoverLetter, path: Path, recipient: str) -> str | None:
    try:
        return str(render_letter_pdf(letter, ctx.profile, path, recipient=recipient).resolve())
    except DocumentError as exc:
        log.warning("Lettre de motivation non générée en PDF : %s", exc)
        return None


def prepare_documents(ctx: AppContext, *, job: JobOffer | None = None,
                      company: Company | None = None) -> TailoredDocuments:
    """Tailored CV + cover letter for ``job`` (or a spontaneous application to ``company``), saved and recorded."""
    if job is None and company is None:
        raise ValueError("prepare_documents : une offre (job) ou une entreprise (company) est requise")
    keywords = _job_keywords(ctx, job) if job is not None else company_keywords(ctx, company)
    title = job.title if job is not None else spontaneous_title(ctx)
    thresholds = ctx.settings.thresholds
    cv, _, truth_warnings = tailor_cv(
        ctx.profile, keywords, ctx.llm, job=job, company=company,
        target_score=thresholds.target_ats_score, max_iterations=thresholds.max_tailor_iterations,
    )
    folder = output_folder(ctx, job, company)
    cv_name, docx_name, letter_name = file_names(ctx)
    cv_path, fitted = render_cv_pdf_fitted(cv, folder / cv_name)
    cv_ats = _final_cv_ats(cv_path, fitted, keywords, title)
    letter = write_cover_letter(ctx.profile, ctx.llm, job=job, company=company, keywords=keywords)
    letter_text = letter_to_text(letter)
    docs = TailoredDocuments(
        job_id=job.id if job is not None else None,
        company_id=company.id if company is not None else None,
        cv_pdf=str(cv_path.resolve()),
        cv_docx=_docx(fitted, folder / docx_name),
        letter_pdf=_letter_files(ctx, letter, folder / letter_name, _recipient(job, company)),
        letter_text=letter_text,
        email_subject=letter.email_subject,
        email_body=letter.email_body,
        cv_ats=cv_ats,
        letter_ats=score_document(letter_text, keywords, title, kind="letter"),
        truth_warnings=truth_warnings,
        created_at=clock.now(),
    )
    ctx.repo.save_documents(docs)
    if job is not None:
        _mark_docs_ready(ctx, job)
    log.info("Documents prêts (%s) : CV ATS %.0f/100, lettre %.0f/100", folder.name, cv_ats.score,
             docs.letter_ats.score if docs.letter_ats else 0)
    return docs


def _mark_docs_ready(ctx: AppContext, job: JobOffer) -> None:
    """DOCS_READY, except for a job already approved or applied to (regenerating documents keeps that decision)."""
    current = ctx.repo.get_job(job.id)
    if current is not None and current.status in KEEP_STATUSES:
        return
    ctx.repo.update_job_status(job.id, JobStatus.DOCS_READY)
