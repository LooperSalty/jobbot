"""Tailored documents: the CV and cover-letter models, their section view and plain-text forms.

``cv_sections`` is the single description of what a CV shows, in which order; the HTML template, the DOCX
renderer and ``cv_to_text`` all consume it, so the text scored by the ATS is the text that is rendered.
Section order for a junior / alternance CV (research §6.5): Profil → Compétences → Expérience professionnelle
→ Projets → Formation → Langues → Centres d'intérêt.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from jobbot.config import AlternanceInfo, Education, Experience, Identity, Language, Project, SkillGroup
from jobbot.documents._format import (
    SEPARATOR,
    clean_spaces,
    contact_line,
    format_period,
    merge_unbalanced,
    strip_emphasis,
)
from jobbot.textutil import normalize

SECTION_TITLES = {
    "profile": "Profil",
    "skills": "Compétences",
    "experience": "Expérience professionnelle",
    "projects": "Projets",
    "education": "Formation",
    "languages": "Langues",
    "interests": "Centres d'intérêt",
}


class DocumentError(RuntimeError):
    """A document could not be produced (rendering, reading a PDF…). Message in French."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TailoredCV(_Frozen):
    identity: Identity
    headline: str
    summary: str
    experiences: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    alternance: AlternanceInfo | None = None


class CoverLetter(_Frozen):
    subject: str  # without the "Objet :" prefix
    greeting: str  # "Madame, Monsieur,"
    paragraphs: list[str]
    closing: str  # "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées."
    email_subject: str
    email_body: str
    signature: str = ""  # candidate's full name, printed under the closing


# --------------------------------------------------------------------------- section view


@dataclass(frozen=True)
class Entry:
    title: str
    meta: str = ""
    bullets: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    paragraphs: tuple[str, ...] = ()
    entries: tuple[Entry, ...] = ()
    pairs: tuple[tuple[str, str], ...] = ()
    inline: str = ""


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _join(*parts: str, sep: str = SEPARATOR) -> str:
    return sep.join(p for p in (clean_spaces(x) for x in parts) if p)


def _experience_entry(exp: Experience) -> Entry:
    bullets = tuple(b for b in (clean_spaces(x) for x in exp.bullets) if b)
    return Entry(title=exp.title, meta=_join(exp.company, exp.location, format_period(exp.start, exp.end)),
                 bullets=bullets)


def _project_entry(project: Project) -> Entry:
    description = clean_spaces(project.description)
    seen = normalize(strip_emphasis(description))
    extra = [s for s in project.skills if normalize(s) and normalize(s) not in seen]
    note = f"{description} ({', '.join(extra)})" if extra and description else description or ", ".join(extra)
    return Entry(title=project.name, note=note)


def _is_programme(education: Education, alternance: AlternanceInfo | None) -> bool:
    if alternance is None or not alternance.school:
        return False
    return normalize(alternance.school) in normalize(education.school)


def _programme_note(alt: AlternanceInfo) -> str:
    """'Titre RNCP niveau 7 · alternance de 24 mois · rythme : …' (the start date is in the meta line)."""
    return _join(
        f"Titre RNCP niveau {alt.rncp_level}" if alt.rncp_level else "",
        f"alternance de {alt.duration_months} mois" if alt.duration_months else "",
        f"rythme : {alt.rhythm}" if alt.rhythm else "",
    )


def _education_entry(education: Education, alternance: AlternanceInfo | None) -> Entry:
    start, note = education.start, ""
    if alternance is not None and _is_programme(education, alternance):
        start = alternance.start_date or start
        note = _programme_note(alternance)
    meta = _join(_join(education.school, education.location, sep=", "), format_period(start, education.end))
    note = _join(clean_spaces(education.details), note)
    return Entry(title=education.degree, meta=meta, note=note)


def cv_sections(cv: TailoredCV) -> list[Section]:
    """Non-empty sections of ``cv`` in display order."""
    sections = [
        Section("profile", SECTION_TITLES["profile"], paragraphs=(clean_spaces(cv.summary),)) if cv.summary else None,
        Section("skills", SECTION_TITLES["skills"],
                pairs=tuple((g.category, ", ".join(g.items)) for g in cv.skills if g.items)) if cv.skills else None,
        Section("experience", SECTION_TITLES["experience"],
                entries=tuple(_experience_entry(e) for e in cv.experiences)) if cv.experiences else None,
        Section("projects", SECTION_TITLES["projects"],
                entries=tuple(_project_entry(p) for p in cv.projects)) if cv.projects else None,
        Section("education", SECTION_TITLES["education"],
                entries=tuple(_education_entry(e, cv.alternance) for e in cv.education)) if cv.education else None,
        Section("languages", SECTION_TITLES["languages"],
                inline=SEPARATOR.join(f"{x.name} : {x.level}" for x in cv.languages)) if cv.languages else None,
        Section("interests", SECTION_TITLES["interests"],
                inline=SEPARATOR.join(_capitalize(i) for i in merge_unbalanced(cv.interests)))
        if cv.interests else None,
    ]
    return [s for s in sections if s is not None]


# --------------------------------------------------------------------------- plain text


def _section_lines(section: Section) -> list[str]:
    lines = [section.title]
    lines.extend(section.paragraphs)
    lines.extend(f"{label} : {items}" for label, items in section.pairs)
    for entry in section.entries:
        if section.key == "projects":
            lines.append(_join(entry.title, entry.note, sep=" — "))
            continue
        lines.extend(x for x in (entry.title, entry.meta) if x)
        lines.extend(f"- {b}" for b in entry.bullets)
        if entry.note:
            lines.append(entry.note)
    if section.inline:
        lines.append(section.inline)
    return lines


def cv_to_text(cv: TailoredCV) -> str:
    """Plain text of the CV as an ATS reads it (same content and order as the rendered PDF)."""
    lines = [cv.identity.full_name, clean_spaces(cv.headline), contact_line(cv.identity)]
    for section in cv_sections(cv):
        lines.append("")
        lines.extend(_section_lines(section))
    return strip_emphasis_lines(lines)


def strip_emphasis_lines(lines: list[str]) -> str:
    return "\n".join(strip_emphasis(line) if line else "" for line in lines).strip()


def letter_to_text(letter: CoverLetter) -> str:
    """Plain text of the letter (subject, greeting, paragraphs, closing, signature), pasted into forms."""
    blocks = [f"Objet : {letter.subject}" if letter.subject else "", letter.greeting]
    blocks += [clean_spaces(p) for p in letter.paragraphs]
    blocks += [letter.closing, letter.signature]
    return "\n\n".join(b.strip() for b in blocks if b and b.strip())
