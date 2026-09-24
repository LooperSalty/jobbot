"""Rendering: CV → HTML (Jinja2) → one-page tagged A4 PDF (Playwright Chromium), CV → DOCX, letter → PDF,
and PDF → text (pypdf) for the ATS parse-back.

Auto-fit: the page height is measured in Chromium and the airiest density that fits is found by bisection on a
continuous scale (font 11 → 10 pt, line height, spacing, margins); when even the tightest one overflows, the
least relevant content is dropped (see ``_fit``) until the CV holds on exactly one A4 page. The generated
PDF's page count is checked too.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from jobbot import clock
from jobbot.config import Profile
from jobbot.documents import _pdf
from jobbot.documents._docx import build_cv_docx
from jobbot.documents._fit import CV_DENSITY, LETTER_DENSITY, trim_cv
from jobbot.documents._format import ContactItem, contact_items, emphasis_html
from jobbot.documents.models import CoverLetter, DocumentError, TailoredCV, cv_sections
from jobbot.matching import lexicon

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
MAX_FIT_ROUNDS = 40
NBSP = "\u00a0"
MAX_PDF_BYTES = 1_000_000
MAX_GLUE_CHARS = 18


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined, trim_blocks=False, lstrip_blocks=False,
    )


def _gluable(term: str) -> bool:
    return " " in term.strip() and len(term) <= MAX_GLUE_CHARS


@lru_cache(maxsize=1)
def _lexicon_terms() -> tuple[str, ...]:
    return tuple(name for name in lexicon.SKILLS if _gluable(name))


def _glue_pattern(cv: TailoredCV) -> re.Pattern[str] | None:
    """Short multi-word skill names are kept on one line (non-breaking spaces) so PDF text extraction never
    splits them across lines ("API REST", "Tailwind CSS", "Next.js 15"); long phrases may wrap (justified text
    would otherwise get wide gaps)."""
    items = {i for g in cv.skills for i in g.items if _gluable(i)}
    terms = sorted({t for t in (*items, *_lexicon_terms()) if not re.search(r"[<>&\"]", t)}, key=len, reverse=True)
    if not terms:
        return None
    body = "|".join(r"\s+".join(re.escape(w) for w in t.split()) for t in terms)
    return re.compile(rf"(?<![\w]){'(?:' + body + ')'}(?![\w])", re.IGNORECASE)


def _emphasis(pattern: re.Pattern[str] | None) -> Any:
    def em(text: str) -> Markup:
        out = emphasis_html(text)
        if pattern is not None:
            out = pattern.sub(lambda m: re.sub(r"\s+", NBSP, m.group(0)), out)
        return Markup(out)

    return em


def render_cv_html(cv: TailoredCV) -> str:
    """Standalone HTML of the CV (single column, French headings, contact block in the body)."""
    return _env().get_template("cv.html.j2").render(
        title=f"CV – {cv.identity.full_name}",
        name=cv.identity.full_name,
        headline=cv.headline,
        contacts=contact_items(cv.identity),
        sections=cv_sections(cv),
        em=_emphasis(_glue_pattern(cv)),
    )


# --------------------------------------------------------------------------- CV PDF


def _fit_cv(page: Any, cv: TailoredCV) -> tuple[TailoredCV, bool]:
    current = cv
    for _ in range(MAX_FIT_ROUNDS):
        _pdf.load_html(page, render_cv_html(current))
        if _pdf.fit_density(page, CV_DENSITY) is not None:
            return current, True
        trimmed = trim_cv(current)
        if trimmed is None:
            break
        current = trimmed
    log.warning("CV : le contenu dépasse une page même au plus compact")
    return current, False


def _render_cv(cv: TailoredCV, out_path: Path) -> tuple[Path, TailoredCV]:
    data = b""
    with _pdf.chromium_page() as page:
        current = cv
        for _ in range(MAX_FIT_ROUNDS):
            current, _fits = _fit_cv(page, current)
            data = _pdf.print_pdf(page)
            if _pdf.page_count(data) <= 1:
                break
            trimmed = trim_cv(current)
            if trimmed is None:
                log.warning("CV de %d pages : impossible de réduire davantage", _pdf.page_count(data))
                break
            current = trimmed
    return _write(out_path, data), current


def _write(out_path: Path, data: bytes) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    if len(data) > MAX_PDF_BYTES:
        log.warning("%s pèse %d octets (> 1 Mo)", out_path.name, len(data))
    return out_path


def render_cv_pdf_fitted(cv: TailoredCV, out_path: Path) -> tuple[Path, TailoredCV]:
    """Render the CV on one A4 page; returns the PDF path and the CV actually printed (after trimming)."""
    return _pdf.run_isolated(_render_cv, cv, Path(out_path))


def render_cv_pdf(cv: TailoredCV, out_path: Path) -> Path:
    """Tagged one-page A4 PDF of the CV (real selectable text, no header/footer)."""
    return render_cv_pdf_fitted(cv, out_path)[0]


def render_cv_docx(cv: TailoredCV, out_path: Path) -> Path:
    try:
        return build_cv_docx(cv, Path(out_path))
    except (OSError, ValueError, KeyError) as exc:
        raise DocumentError(f"Création du CV Word impossible : {exc}") from exc


# --------------------------------------------------------------------------- letter PDF


def _sender(profile: Profile) -> list[ContactItem]:
    identity = profile.identity
    place = " ".join(x for x in (identity.postal_code, identity.city) if x)
    items = [ContactItem(place)] if place else []
    permit = identity.driving_license
    items += [i for i in contact_items(identity)[1:] if not (permit and i.text == permit)]
    return [i for i in items if i.text]


def render_letter_html(letter: CoverLetter, profile: Profile, *, recipient: str | None = None) -> str:
    identity = profile.identity
    return _env().get_template("letter.html.j2").render(
        title=f"Lettre de motivation – {identity.full_name}",
        name=identity.full_name,
        sender=_sender(profile),
        recipient=[line.strip() for line in (recipient or "").splitlines() if line.strip()],
        place_date=f"{identity.city or 'Metz'}, le {clock.french_date()}",
        subject=letter.subject,
        greeting=letter.greeting,
        paragraphs=list(letter.paragraphs),
        closing=letter.closing,
        signature=letter.signature or identity.full_name,
    )


def _render_letter(page_html: str, out_path: Path) -> Path:
    with _pdf.chromium_page() as page:
        _pdf.load_html(page, page_html)
        if _pdf.fit_density(page, LETTER_DENSITY) is None:
            log.warning("Lettre de motivation : plus d'une page même au plus compact")
        data = _pdf.print_pdf(page)
    return _write(out_path, data)


def render_letter_pdf(letter: CoverLetter, profile: Profile, out_path: Path, *, recipient: str | None = None) -> Path:
    """Classic French one-page letter: sender, recipient, "Metz, le …", Objet, body, closing, signature."""
    return _pdf.run_isolated(_render_letter, render_letter_html(letter, profile, recipient=recipient), Path(out_path))


# --------------------------------------------------------------------------- parse-back


def pdf_text(path: Path) -> str:
    """Text layer of a PDF as an ATS extracts it (pypdf), pages joined by newlines."""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except (OSError, PdfReadError, ValueError) as exc:
        raise DocumentError(f"Lecture du PDF impossible ({Path(path).name}) : {exc}") from exc


def pdf_pages(path: Path) -> int:
    try:
        return len(PdfReader(str(path)).pages)
    except (OSError, PdfReadError, ValueError) as exc:
        raise DocumentError(f"Lecture du PDF impossible ({Path(path).name}) : {exc}") from exc

