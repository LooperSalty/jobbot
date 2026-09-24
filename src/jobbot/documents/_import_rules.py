"""Rule-based CV text → profile dict (fallback of ``importer.import_cv`` when no LLM is available).

Best effort on a one-column CV: identity from the first lines, sections from standard French headings,
"Catégorie : a, b" skill lines, experiences as "title" + "meta line with dates" + bullets.
"""

from __future__ import annotations

import re
from typing import Any

from jobbot.documents._format import merge_unbalanced
from jobbot.matching._doccheck import PHONE_RX, detect_heading
from jobbot.textutil import EMAIL_RE, strip_accents

BULLET_CHARS = "\u2022\x7f-\u2013\u25aa*\uf0b7\u00b7\u25e6"  # bullets: • DEL - – ▪ * Symbol ·
_MONTHS = ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre",
           "novembre", "decembre")
_MONTH_RX = re.compile(r"\b(" + "|".join(_MONTHS) + r")\b")
_YEAR_RX = re.compile(r"\b(19|20)\d{2}\b")
_ONGOING_RX = re.compile(r"depuis|aujourd'hui|present|en cours|actuel")
_CITY_RX = re.compile(r"([A-ZÀ-Ý][\wÀ-ÿ' -]+?)\s*\((\d{2,5})\)")
_SPLIT_ITEMS = re.compile(r"\s*[,;•·]\s*")
_META_SPLIT = re.compile(r"\s+[•·|]\s+")
_KIND_RX = (("seasonal", re.compile(r"saisonni|ete|etes", re.I)), ("internship", re.compile(r"\bstage|alternan", re.I)),
            ("freelance", re.compile(r"freelance|auto-entrepreneur|independant", re.I)))


def _fold(text: str) -> str:
    return strip_accents(text).lower()


def _is_bullet(line: str) -> bool:
    return bool(line) and line[0] in BULLET_CHARS


def _strip_bullet(line: str) -> str:
    return line.lstrip(BULLET_CHARS + " ").strip()


def _identity(lines: list[str], header: list[str], text: str) -> dict[str, Any]:
    tokens = lines[0].split() if lines else ["Prénom", "NOM"]
    upper = [t for t in tokens if t.isupper() and len(t) > 1]
    last = " ".join(upper) or " ".join(tokens[1:]) or "NOM"
    first = " ".join(t for t in tokens if t not in upper) if upper else tokens[0]
    email = EMAIL_RE.search(text)
    phone = PHONE_RX.search(text)
    head = " ".join(header[:6])
    city = _CITY_RX.search(head)
    linkedin = re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+", text)
    github = re.search(r"(?:https?://)?github\.com/[\w-]+", text)
    permit = re.search(r"Permis\s+[A-Z]\b[^•·|]*", head)
    return {
        "first_name": first or "Prénom", "last_name": last,
        "email": email.group(0) if email else "", "phone": phone.group(0) if phone else "",
        "city": city.group(1).strip() if city else "",
        "postal_code": city.group(2) if city and len(city.group(2)) == 5 else "",
        "linkedin": linkedin.group(0) if linkedin else None, "github": github.group(0) if github else None,
        "driving_license": permit.group(0).strip() if permit else None,
    }


def _sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in lines:
        heading = detect_heading(line)
        if heading:
            current = heading
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _join_wrapped(lines: list[str]) -> list[str]:
    """Re-join lines wrapped by the PDF layout (continuation = starts lowercase, digit or punctuation)."""
    out: list[str] = []
    for line in lines:
        if out and line and not _is_bullet(line) and (line[0].islower() or line[0] in "(),.;:/0123456789"):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return out


def parse_period(text: str) -> tuple[str, str]:
    """'Février – Juin 2026' → ('2026-02', '2026-06'); 'Depuis 2025' → ('2025', ''); '2025' → ('2025', '2025')."""
    folded = _fold(text)
    parts = re.split(r"\s*[-–—]\s*|\s+a\s+|\s+au\s+", folded, maxsplit=1)
    dates = [(_MONTH_RX.search(p), _YEAR_RX.search(p)) for p in parts]
    years = [y.group(0) for _, y in dates if y]
    if not years:
        return "", ""
    out = []
    for month, year in dates:
        y = year.group(0) if year else years[-1]
        out.append(f"{y}-{_MONTHS.index(month.group(1)) + 1:02d}" if month else y)
    if _ONGOING_RX.search(folded):
        return out[0], ""
    return out[0], out[-1]


def _kind(title: str) -> str:
    folded = _fold(title)
    return next((kind for kind, rx in _KIND_RX if rx.search(folded)), "job")


def _meta(line: str) -> dict[str, str]:
    parts = _META_SPLIT.split(line)
    dated = [p for p in parts if _YEAR_RX.search(p)]
    start, end = parse_period(dated[-1]) if dated else ("", "")
    place = [p for p in parts if p not in dated]
    company, _, location = (place[0] if place else "").partition(", ")
    if not location and _CITY_RX.fullmatch(company.strip()):
        company, location = "", company
    return {"company": company.strip(), "location": location.strip(), "start": start, "end": end}


def _new_experience(title: str, meta_line: str) -> dict[str, Any]:
    meta = _meta(meta_line) if meta_line else {}
    company = meta.get("company") or (title.split(" — ")[1] if " — " in title else "")
    return {"title": title, "company": company, "location": meta.get("location", ""),
            "start": meta.get("start", ""), "end": meta.get("end", ""), "kind": _kind(title), "bullets": []}


def _is_meta(line: str) -> bool:
    return bool(line) and not _is_bullet(line) and bool(_YEAR_RX.search(line))


def _experiences(lines: list[str]) -> list[dict[str, Any]]:
    """Title line + meta line (company, place, dates) + bullets; other lines continue the previous bullet."""
    exps: list[dict[str, Any]] = []
    skip = False
    for i, line in enumerate(lines):
        if skip:
            skip = False
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if _is_bullet(line):
            if exps:
                exps[-1]["bullets"].append(_strip_bullet(line))
        elif _is_meta(nxt) and not _YEAR_RX.search(line):
            exps.append(_new_experience(line, nxt))
            skip = True
        elif exps and exps[-1]["bullets"]:
            exps[-1]["bullets"][-1] = f"{exps[-1]['bullets'][-1]} {line}"
        else:
            exps.append(_new_experience(line, ""))
    return exps


def _education(lines: list[str]) -> list[dict[str, Any]]:
    out = []
    for line in _join_wrapped(lines):
        parts = [p.strip() for p in _META_SPLIT.split(line) if p.strip()]
        if not parts:
            continue
        school = next((p for p in parts[1:] if re.search(r"iut|universit|ecole|school|lycee|institut", _fold(p))), "")
        start, end = parse_period(" ".join(p for p in parts[1:] if _YEAR_RX.search(p)))
        out.append({"degree": parts[0], "school": school.split(", 20")[0].strip(), "start": start, "end": end})
    return out


def _skills(lines: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    groups, languages = [], []
    for line in _join_wrapped(lines):
        for chunk in re.split(r"\s+•\s+", line):
            label, sep, items = chunk.partition(" : ")
            if not sep:
                label, items = "Compétences", chunk
            if re.fullmatch(r"(?i)anglais|allemand|espagnol|italien|fran[cç]ais", label.strip()):
                languages.append({"name": label.strip(), "level": items.strip()})
                continue
            values = merge_unbalanced([v for v in _SPLIT_ITEMS.split(items) if v])
            if values:
                groups.append({"category": label.strip(), "items": values})
    return groups, languages


def _languages(lines: list[str]) -> list[dict[str, str]]:
    out = []
    for chunk in _SPLIT_ITEMS.split(" ".join(lines)):
        name, _, level = chunk.partition(":")
        if name.strip():
            out.append({"name": name.strip(), "level": level.strip()})
    return out


def parse_cv_text(text: str) -> dict[str, Any]:
    """Profile-like dict (``ImportedCV`` shape) from the extracted text of a one-column CV."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    sections = _sections(lines)
    header = _join_wrapped(sections.get("header", []))
    skills, skill_languages = _skills(sections.get("skills", []))
    headline = next((h for h in header[1:3] if not EMAIL_RE.search(h) and not PHONE_RX.search(h)), "")
    return {
        "identity": _identity(lines, sections.get("header", []), text),
        "headline": headline,
        "summary": " ".join(sections.get("profile", [])),
        "experiences": _experiences(sections.get("experience", [])),
        "education": _education(sections.get("education", [])),
        "projects": [],
        "skills": skills,
        "languages": _languages(sections.get("languages", [])) or skill_languages,
        "interests": merge_unbalanced(_SPLIT_ITEMS.split(" ".join(sections.get("interests", [])))),
        "qualities": [],
    }
