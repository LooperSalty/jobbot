"""Deterministic, explainable ATS-like score of a CV or cover letter against job keywords.

CV     = 60 % keywords + 15 % title + 15 % sections + 10 % format   (minus keyword-stuffing penalties)
Letter = 70 % keywords + 10 % title + 20 % structure (10 structure + 10 format)

Keyword credit (docs/research/ats-and-documents.md §6.3, step B): a lexicon skill found in the experience/projects
text AND the "Compétences" section = 1.0, experience only = 0.9, skills section only = 0.8; a narrower skill
satisfies a broader one (JD "SQL", CV "PostgreSQL" = full credit); only the broader one present = 0.5; only a
sibling (JD "Vue.js", CV "React") = 0.3. Other phrases: exact match, else rapidfuzz ``token_set_ratio`` over
sliding windows. Repeating a keyword never earns more; beyond a few mentions it costs points.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from typing import Literal

from rapidfuzz import fuzz, process

from jobbot.matching import _doccheck as dc
from jobbot.matching import lexicon
from jobbot.matching._text import family_of, norm_lines, normalize_title, title_norm
from jobbot.models import ATSReport, KeywordHit

CV_WEIGHTS = {"keywords": 60.0, "title": 15.0, "sections": 15.0, "format": 10.0}
LETTER_WEIGHTS = {"keywords": 70.0, "title": 10.0, "sections": 10.0, "format": 10.0}
CATEGORY_FACTOR = {"soft": 0.5, "title": 0.5, "language": 0.7}  # hard skills weigh most (research §6.3)
MATCH_THRESHOLD = 0.7  # credit at/above → keyword counted as matched
STUFFING_LIMIT = {"cv": 6, "letter": 3}  # mentions beyond this = stuffing (headline + bullets + skills is normal)
STUFFING_PENALTY, STUFFING_CAP = 2.0, 10.0
NEUTRAL_KEYWORD_SCORE = 50.0
HEAD_LINES = {"cv": 8, "letter": 15}
_STOPWORDS = frozenset(["de", "des", "du", "d", "la", "le", "les", "l", "et", "en", "a", "au", "aux", "un", "une", "pour", "par", "sur", "avec", "the", "of", "and", "in", "to", "for"])


@dataclass(frozen=True)
class _Doc:
    """Read-only view of the scored document; derived indexes are computed lazily once."""

    text: str
    kind: str
    sections: dict[str, str] = field(default_factory=dict)

    @cached_property
    def has_skill_section(self) -> bool:
        return self.kind == "cv" and "skills" in self.sections

    @cached_property
    def section_skills(self) -> Counter[str]:
        if not self.has_skill_section:
            return Counter()
        return lexicon.count_skills(self.sections.get("skills", ""), lenient=True)

    @cached_property
    def body_skills(self) -> Counter[str]:
        if not self.has_skill_section:
            return lexicon.count_skills(self.text)
        body = "\n".join(t for name, t in self.sections.items() if name != "skills")
        return lexicon.count_skills(body)

    @cached_property
    def all_skills(self) -> Counter[str]:
        return self.body_skills + self.section_skills

    @cached_property
    def soft(self) -> set[str]:
        return lexicon.find_soft_skills(self.text)

    @cached_property
    def languages(self) -> set[str]:
        return lexicon.find_languages(self.text)

    @cached_property
    def tnorm(self) -> str:
        return title_norm(self.text)

    @cached_property
    def tokens(self) -> list[str]:
        return _fuzzy_tokens(self.tnorm)

    @cached_property
    def head(self) -> list[str]:
        return [title_norm(line) for line in norm_lines(self.text)[: HEAD_LINES[self.kind]]]

    def windows(self, size: int) -> list[str]:
        toks = self.tokens
        if len(toks) <= size:
            return [" ".join(toks)] if toks else []
        return [" ".join(toks[i : i + size]) for i in range(len(toks) - size + 1)]


_ELISION = re.compile(r"\b(?:qu|[cdjlmnst])'")


def _fuzzy_tokens(normalized: str) -> list[str]:
    """Tokens for fuzzy phrase matching: elisions dropped (d'applications → applications), plural s/x stripped."""
    tokens = _ELISION.sub("", normalized).split()
    return [t[:-1] if len(t) > 3 and t[-1] in "sx" and t[-2] != t[-1] else t for t in tokens]


def _make_doc(text: str, kind: str) -> _Doc:
    sections = dc.split_sections(text) if kind == "cv" else {}
    return _Doc(text=text or "", kind=kind, sections=sections)


# --------------------------------------------------------------------------- keyword credit


def _skill_credit(doc: _Doc, name: str) -> tuple[float, str | None]:
    satisfying = {name} | lexicon.descendants(name)
    in_body = any(doc.body_skills.get(s) for s in satisfying)
    in_section = any(doc.section_skills.get(s) for s in satisfying)
    if in_body or in_section:
        return _presence_credit(doc.has_skill_section, in_body, in_section), None
    present = set(doc.all_skills)
    parents = sorted(lexicon.ancestors(name) & present)
    if parents:
        return 0.5, f"« {name} » : seul « {parents[0]} » apparaît (crédit partiel)"
    siblings = sorted(lexicon.related(name) & present)
    if siblings:
        return 0.3, f"« {name} » absent, compétence proche « {siblings[0]} » (crédit partiel)"
    return 0.0, None


def _presence_credit(has_skill_section: bool, in_body: bool, in_section: bool) -> float:
    """Evidence in experience/projects AND listed in "Compétences" is best (research §6.3 step B)."""
    if not has_skill_section or (in_body and in_section):
        return 1.0
    return 0.9 if in_body else 0.8


_FUZZY_CREDITS = ((90, 0.9), (85, 0.75), (75, 0.5))


def _phrase_credit(doc: _Doc, phrase: str) -> float:
    p = title_norm(phrase)
    if not p:
        return 0.0
    if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", doc.tnorm):
        return 1.0
    all_tokens = _fuzzy_tokens(p)
    tokens = [t for t in all_tokens if t not in _STOPWORDS] or all_tokens
    if len(tokens) == 1:
        token = tokens[0]
        stem = token[: max(5, len(token) - 2)]
        return 0.8 if len(token) >= 6 and any(t.startswith(stem) for t in doc.tokens) else 0.0
    best = process.extractOne(" ".join(all_tokens), doc.windows(len(tokens) + 2), scorer=fuzz.token_set_ratio)
    score = best[1] if best else 0.0
    return next((credit for floor, credit in _FUZZY_CREDITS if score >= floor), 0.0)


def _credit(doc: _Doc, name: str) -> tuple[float, str | None]:
    """Credit 0..1 of one (canonicalized) keyword in the document, with an optional French note."""
    if name in lexicon.SKILLS:
        return _skill_credit(doc, name)
    if name in lexicon.SOFT_SKILLS:
        return (1.0 if name in doc.soft else 0.0), None
    if name in lexicon.LANGUAGE_SKILLS:
        return (1.0 if name in doc.languages else 0.0), None
    return _phrase_credit(doc, name), None


@dataclass(frozen=True)
class _KeywordResult:
    score: float
    matched: list[KeywordHit]
    missing: list[KeywordHit]
    notes: list[str]
    stuffed: list[str]


def _keyword_component(doc: _Doc, keywords: list[KeywordHit]) -> _KeywordResult:
    rows = []  # (weight, credit, keyword with found flag, note, stuffed name)
    for name, kw in _dedupe(keywords):
        credit, note = _credit(doc, name)
        weight = max(0.0, kw.weight) * CATEGORY_FACTOR.get(kw.category, 1.0)
        stuffed = name if doc.all_skills.get(name, 0) > STUFFING_LIMIT[doc.kind] else None
        rows.append((weight, credit, kw.model_copy(update={"found": credit >= MATCH_THRESHOLD}), note, stuffed))
    total = sum(r[0] for r in rows)
    score = NEUTRAL_KEYWORD_SCORE if total <= 0 else 100.0 * sum(r[0] * r[1] for r in rows) / total
    return _KeywordResult(
        score=score,
        matched=[r[2] for r in rows if r[2].found],
        missing=[r[2] for r in rows if not r[2].found],
        notes=[r[3] for r in rows if r[3]],
        stuffed=[r[4] for r in rows if r[4]],
    )


def _dedupe(keywords: list[KeywordHit]) -> list[tuple[str, KeywordHit]]:
    """(canonical name, keyword) pairs, one per canonical name (highest weight kept)."""
    seen: dict[str, tuple[str, KeywordHit]] = {}
    for kw in keywords:
        name = kw.keyword.strip() if kw.category == "title" else lexicon.canonicalize(kw.keyword)
        key = name.lower()
        if key and (key not in seen or seen[key][1].weight < kw.weight):
            seen[key] = (name, kw)
    return list(seen.values())


# --------------------------------------------------------------------------- title


def _title_score(doc: _Doc, job_title: str) -> float:
    target = normalize_title(job_title or "")
    if not target:
        return 100.0
    if re.search(r"(?<![a-z0-9])" + re.escape(target) + r"(?![a-z0-9])", doc.tnorm):
        return 100.0
    best = max((fuzz.token_set_ratio(target, line) for line in doc.head), default=0.0)
    family = family_of(target)
    doc_families = {family_of(line) for line in doc.head} - {None}
    if family and family in doc_families:
        best = max(best, 70.0)
    elif family and doc_families:
        best = max(best, 45.0)
    return float(best)


# --------------------------------------------------------------------------- public API


def score_document(
    text: str, keywords: list[KeywordHit], job_title: str, *, kind: Literal["cv", "letter"] = "cv"
) -> ATSReport:
    """Explainable 0-100 ATS score of ``text`` (CV or letter) for a job described by ``keywords``/``job_title``."""
    kind = "letter" if kind == "letter" else "cv"
    doc = _make_doc(text or "", kind)
    words = dc.count_words(doc.text)
    kw = _keyword_component(doc, keywords)
    title = _title_score(doc, job_title)
    if kind == "cv":
        section, section_warnings = dc.cv_sections_score(doc.sections)
        fmt, format_warnings = dc.cv_format_score(doc.text, words)
        weights = CV_WEIGHTS
    else:
        section, section_warnings = dc.letter_structure_score(doc.text)
        fmt, format_warnings = dc.letter_format_score(doc.text, words)
        weights = LETTER_WEIGHTS
    penalty = min(STUFFING_CAP, STUFFING_PENALTY * len(kw.stuffed))
    total = (weights["keywords"] * kw.score + weights["title"] * title + weights["sections"] * section
             + weights["format"] * fmt) / 100.0 - penalty
    warnings = _keyword_warnings(kw, keywords) + _title_warnings(title, job_title, kind)
    warnings += section_warnings + format_warnings
    return ATSReport(
        score=round(max(0.0, min(100.0, total)), 1), keyword_score=round(kw.score, 1), title_score=round(title, 1),
        section_score=round(section, 1), format_score=round(fmt, 1), matched=kw.matched, missing=kw.missing,
        warnings=warnings, word_count=words,
    )


def keyword_coverage(text: str, keywords: list[KeywordHit]) -> tuple[list[KeywordHit], list[KeywordHit]]:
    """(matched, missing) keywords of ``text`` — same synonym-aware rules as ``score_document``."""
    result = _keyword_component(_make_doc(text or "", "cv"), keywords)
    return result.matched, result.missing


def _keyword_warnings(kw: _KeywordResult, keywords: list[KeywordHit]) -> list[str]:
    warnings: list[str] = []
    if not keywords:
        warnings.append("Aucun mot-clé fourni : score mots-clés neutre")
    important = [k.keyword for k in kw.missing if k.weight >= 0.9 and k.category != "title"]
    if important:
        warnings.append("Mots-clés importants absents : " + ", ".join(important[:8]))
    warnings.extend(kw.notes[:5])
    if kw.stuffed:
        warnings.append("Répétition excessive (bourrage de mots-clés) : " + ", ".join(sorted(set(kw.stuffed))))
    return warnings


def _title_warnings(title_score: float, job_title: str, kind: str) -> list[str]:
    if title_score >= 70 or not normalize_title(job_title or ""):
        return []
    where = "l'en-tête du CV" if kind == "cv" else "l'objet ou l'introduction de la lettre"
    return [f"L'intitulé du poste « {job_title.strip()} » n'apparaît pas dans {where}"]
