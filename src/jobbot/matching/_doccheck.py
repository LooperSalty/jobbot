"""Structure and format checks of a CV / cover letter (deterministic, French warnings).

Scores are 0-100 sub-scores used by ``matching.ats``. Sources: docs/research/ats-and-documents.md §5, §6.4, §6.6.
"""

from __future__ import annotations

import re

from jobbot.matching._text import norm, prep
from jobbot.textutil import EMAIL_RE

CV_HEADINGS: dict[str, tuple[str, ...]] = {
    "profile": ("profil", "profil professionnel", "resume", "a propos", "a propos de moi", "objectif professionnel",
                "objectif", "presentation", "summary", "profile", "about me"),
    "experience": ("experience professionnelle", "experiences professionnelles", "parcours professionnel",
                   "experiences", "experience", "work experience", "professional experience",
                   "employment history"),
    "projects": ("projets", "projets personnels", "projets academiques", "projets techniques", "realisations",
                 "projects", "personal projects"),
    "education": ("formation", "formations", "etudes", "diplomes", "parcours academique", "cursus", "education",
                  "diplomes et formations", "formation et diplomes"),
    "skills": ("competences", "competences techniques", "competences informatiques", "competences cles",
               "savoir faire", "stack technique", "technologies", "skills", "technical skills", "hard skills"),
    "languages": ("langues", "langues etrangeres", "languages"),
    "certifications": ("certifications", "certificats", "certification"),
    "interests": ("centres d'interet", "centres d'interets", "loisirs", "interets", "interests", "hobbies",
                  "engagement associatif", "benevolat", "activites extra professionnelles"),
}
HEADING_LABELS = {
    "experience": "Expérience professionnelle", "education": "Formation", "skills": "Compétences",
    "profile": "Profil", "languages": "Langues",
}
_SECTION_POINTS = (("experience", 30), ("education", 25), ("skills", 25), ("profile", 8), ("languages", 7))
_BONUS_SECTIONS = ("projects", "interests", "certifications")  # any of them: 5 points

PHONE_RX = re.compile(r"(?:\+33\s?|0033\s?|(?<!\d)0)[1-9](?:[\s.-]?\d{2}){4}(?!\d)|\+352[\s.]?\d(?:[\s.]?\d){5,10}")
_MONTHS = (r"(?:janv(?:ier)?|fevr?(?:ier)?|mars|avr(?:il)?|mai|juin|juil(?:let)?|aout|sept?(?:embre)?|oct(?:obre)?|"
           r"nov(?:embre)?|dec(?:embre)?|january|february|march|april|may|june|july|august|september|october|"
           r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|nov|dec)")
_MONTH_YEAR = re.compile(rf"\b{_MONTHS}\.?\s+(?:19|20)\d{{2}}\b|\b(?:0?[1-9]|1[0-2])[/.-](?:19|20)\d{{2}}\b")
_YEAR_RANGE = re.compile(
    r"\b(?:19|20)\d{2}\s*(?:-|a|au|to)\s*(?:(?:19|20)\d{2}|present|aujourd'hui|en cours|actuel|now)\b"
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
# Private-use icons (Font Awesome), U+FFFD, ligatures, dingbats/symbols, emoji.
_BAD_RANGES = ((0xE000, 0xF8FF), (0xFFFD, 0xFFFD), (0xFB00, 0xFB06), (0x2600, 0x27BF), (0x1F300, 0x1FAFF))
_BAD_GLYPHS = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _BAD_RANGES) + "]")
_LINK = re.compile(r"linkedin\.com|github\.com|gitlab\.com|https?://|www\.", re.I)

GREETING_RX = re.compile(r"^(?:madame|monsieur|madame, monsieur|mesdames|bonjour|dear|hello)\b")
CLOSING_RX = re.compile(
    r"je vous prie d'agreer|veuillez agreer|salutations distinguees|sinceres salutations|respectueuses salutations|"
    r"cordialement|bien a vous|sincerely|best regards|kind regards"
)
PLACEHOLDER_RX = re.compile(r"\{|\}|\[\[|\]\]|\bxxx\b|lorem ipsum|\[(?:nom|entreprise|poste|date|ville)\]", re.I)
CLICHES = (
    "dynamique et motive", "motive et dynamique", "je suis quelqu'un de", "team player", "passionne depuis toujours",
    "rigoureux et organise", "sens du relationnel", "je me permets de vous adresser", "votre entreprise leader",
    "n'hesitez pas a me contacter",
)


def split_sections(text: str) -> dict[str, str]:
    """Canonical section → its text. Lines before the first heading go to ``"header"``."""
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in prep(text).splitlines():
        head, _, rest = line.partition(":")
        inline = detect_heading(head) if rest.strip() else None  # "Compétences : React, Docker"
        heading = inline or detect_heading(line)
        if heading:
            current = heading
            sections.setdefault(current, [])
            if inline:
                sections[current].append(rest)
            continue
        sections.setdefault(current, []).append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def detect_heading(line: str) -> str | None:
    """Canonical section name when ``line`` is a CV heading (FR/EN, any case), else None."""
    n = norm(line).strip(" :-•*|#_=.")
    if not n or len(n) > 60 or len(n.split()) > 6 or ":" in n:
        return None
    for canonical, variants in CV_HEADINGS.items():
        for v in variants:
            if n == v:
                return canonical
            if n.startswith(v + " ") and not re.search(r"\d|[.,;]", n) and len(n.split()) - len(v.split()) <= 3:
                return canonical
    return None


def cv_sections_score(sections: dict[str, str]) -> tuple[float, list[str]]:
    present = {name for name, body in sections.items() if name != "header"}
    score, warnings = 0.0, []
    for name, points in _SECTION_POINTS:
        if name in present:
            score += points
        elif points >= 25:
            warnings.append(f"Section « {HEADING_LABELS[name]} » introuvable (titre standard attendu)")
    if any(name in present for name in _BONUS_SECTIONS):
        score += 5
    return score, warnings


def count_words(text: str) -> int:
    return len(re.findall(r"[^\W_][\w'’+#.-]*", text or ""))


def _words_points(words: int) -> tuple[float, str | None]:
    if 350 <= words <= 700:
        return 30.0, None
    if 250 <= words < 350 or 700 < words <= 800:
        return 20.0, None
    if words > 800:
        return (8.0 if words <= 950 else 0.0), f"CV trop long pour une page ({words} mots, viser 350-700)"
    return (8.0 if words >= 150 else 0.0), f"CV très court ({words} mots)"


def _dates_points(text_norm: str) -> tuple[float, str | None]:
    dated = len(_MONTH_YEAR.findall(text_norm)) + len(_YEAR_RANGE.findall(text_norm))
    if dated >= 2:
        return 20.0, None
    if len(_YEAR.findall(text_norm)) >= 2:
        return 12.0, "Dates au format année seule : préférer « Septembre 2023 – Juin 2026 »"
    return 0.0, "Aucune date détectée dans le CV (expériences et formations doivent être datées)"


def cv_format_score(text: str, words: int) -> tuple[float, list[str]]:
    """Word count (1 page), e-mail, phone, dates, clean glyphs, links → 0-100 + French warnings."""
    text_norm = norm(text)
    score, warnings = 0.0, []
    for points, warning in (_words_points(words), _dates_points(text_norm)):
        score += points
        if warning:
            warnings.append(warning)
    if EMAIL_RE.search(text or ""):
        score += 20
    else:
        warnings.append("Adresse e-mail absente du corps du CV")
    if PHONE_RX.search(text or ""):
        score += 15
    else:
        warnings.append("Numéro de téléphone absent ou illisible")
    if _BAD_GLYPHS.search(text or ""):
        warnings.append("Caractères non lisibles par un ATS (icônes, emoji, ligatures)")
    else:
        score += 10
    if _LINK.search(text or ""):
        score += 5
    return score, warnings


def letter_paragraphs(text: str) -> list[str]:
    blocks = [" ".join(b.split()) for b in re.split(r"\n\s*\n", prep(text or ""))]
    return [b for b in blocks if count_words(b) >= 20]


def letter_structure_score(text: str) -> tuple[float, list[str]]:
    """Greeting, closing formula, 3-5 body paragraphs, no clichés → 0-100 + French warnings."""
    lines = [norm(line) for line in prep(text or "").splitlines() if line.strip()]
    text_norm = norm(text or "")
    score, warnings = 0.0, []
    if any(GREETING_RX.match(line) for line in lines[:15]):
        score += 30
    else:
        warnings.append("Formule d'appel absente (« Madame, Monsieur, »)")
    if CLOSING_RX.search(text_norm):
        score += 30
    else:
        warnings.append("Formule de politesse finale absente")
    paragraphs = len(letter_paragraphs(text))
    if 3 <= paragraphs <= 5:
        score += 40
    else:
        score += 20 if paragraphs in (2, 6) else 0
        warnings.append(f"{paragraphs} paragraphe(s) de corps : viser 3 ou 4 (vous / moi / nous)")
    cliches = [c for c in CLICHES if c in text_norm]
    if cliches:
        score = max(0.0, score - 5 * len(cliches))
        warnings.append("Clichés à éviter : " + ", ".join(f"« {c} »" for c in cliches))
    return score, warnings


def letter_format_score(text: str, words: int) -> tuple[float, list[str]]:
    """Length (250-400 words), no placeholder, clean glyphs → 0-100 + French warnings."""
    warnings: list[str] = []
    if 250 <= words <= 400:
        length = 60.0
    elif words < 250:
        length = max(0.0, 60.0 * (words - 150) / 100)
        warnings.append(f"Lettre courte ({words} mots, viser 250-400)")
    else:
        length = max(0.0, 60.0 * (550 - words) / 150)
        warnings.append(f"Lettre longue ({words} mots, viser 250-400)")
    score = length
    if PLACEHOLDER_RX.search(text or ""):
        warnings.append("Champ à compléter oublié dans la lettre ({...}, [[...]], XXX)")
    else:
        score += 25
    if _BAD_GLYPHS.search(text or ""):
        warnings.append("Caractères non lisibles par un ATS (icônes, emoji, ligatures)")
    else:
        score += 15
    return score, warnings
