"""DOCX version of the CV (python-docx) for ATS that prefer Word (Taleo, SuccessFactors — research §2.2).

Built-in "Heading 1" section titles, "List Bullet" bullets, everything in body paragraphs: no table, no text box,
no image, nothing in the page header/footer (research §5, §6.4).
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor

from jobbot.documents._format import SEPARATOR, contact_line, strip_emphasis
from jobbot.documents.models import Section, TailoredCV, cv_sections

FONT = "Arial"
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
MUTED = RGBColor(0x54, 0x5C, 0x68)
BODY_PT = 10.5


def _style_document(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(BODY_PT)
    normal.paragraph_format.space_after = Pt(1)
    normal.paragraph_format.space_before = Pt(0)
    heading = doc.styles["Heading 1"]
    heading.font.name = FONT
    heading.font.size = Pt(11.5)
    heading.font.bold = True
    heading.font.color.rgb = ACCENT
    heading.paragraph_format.space_before = Pt(8)
    heading.paragraph_format.space_after = Pt(2)
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Cm(1.3)
        section.left_margin = section.right_margin = Cm(1.5)


def _paragraph(doc: Document, text: str, *, bold: bool = False, italic: bool = False, size: float | None = None,
               color: RGBColor | None = None, style: str | None = None) -> None:
    para = doc.add_paragraph(style=style)
    run = para.add_run(strip_emphasis(text))
    run.bold, run.italic = bold, italic
    if size:
        run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color
    if style is None and not bold:
        para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY


def _section(doc: Document, section: Section) -> None:
    doc.add_heading(section.title, level=1)
    for text in section.paragraphs:
        _paragraph(doc, text)
    for label, items in section.pairs:
        para = doc.add_paragraph()
        para.add_run(f"{label} : ").bold = True
        para.add_run(strip_emphasis(items))
    for entry in section.entries:
        if section.key == "projects":
            para = doc.add_paragraph()
            para.add_run(f"{entry.title} — ").bold = True
            para.add_run(strip_emphasis(entry.note))
            continue
        _paragraph(doc, entry.title, bold=True)
        if entry.meta:
            _paragraph(doc, entry.meta, italic=True, size=BODY_PT - 0.5, color=MUTED)
        for bullet in entry.bullets:
            _paragraph(doc, bullet, style="List Bullet")
        if entry.note:
            _paragraph(doc, entry.note, size=BODY_PT - 0.5)
    if section.inline:
        _paragraph(doc, section.inline)


def build_cv_docx(cv: TailoredCV, out_path: Path) -> Path:
    doc = Document()
    _style_document(doc)
    _paragraph(doc, cv.identity.full_name, bold=True, size=18)
    _paragraph(doc, cv.headline, bold=True, size=11.5, color=ACCENT)
    _paragraph(doc, contact_line(cv.identity).replace(SEPARATOR, " · "), size=BODY_PT - 0.5, color=MUTED)
    for section in cv_sections(cv):
        _section(doc, section)
    props = doc.core_properties
    props.author = cv.identity.full_name
    props.title = f"CV – {cv.identity.full_name} – {strip_emphasis(cv.headline).split(' — ')[0]}"
    props.subject = ""
    props.keywords = ""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path
