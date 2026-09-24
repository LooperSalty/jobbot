"""Small formatting helpers shared by the text, HTML and DOCX renderings of the documents."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime

from jobbot.config import Identity
from jobbot.textutil import strip_accents

MONTHS_FR = (
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
)
ONGOING_LABEL = "aujourd'hui"
EN_DASH = "–"
SEPARATOR = " · "

_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{1,2})(?:-\d{1,2})?$")
_MONTH_YEAR = re.compile(r"^(\d{1,2})/(\d{4})$")
_YEAR = re.compile(r"^\d{4}$")
_EMPHASIS = re.compile(r"\*\*(.+?)\*\*")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class ContactItem:
    text: str
    href: str | None = None


def format_date(value: str) -> str:
    """'2026-02' → 'Février 2026', '02/2026' → 'Février 2026', '2025' → '2025', other text kept."""
    v = (value or "").strip()
    m = _YEAR_MONTH.match(v)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{MONTHS_FR[int(m.group(2)) - 1]} {m.group(1)}"
    m = _MONTH_YEAR.match(v)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{MONTHS_FR[int(m.group(1)) - 1]} {m.group(2)}"
    return v


def format_period(start: str, end: str) -> str:
    """'Février 2026 – Juin 2026', '2025 – aujourd'hui' (empty end = ongoing), '2025' (same start/end)."""
    s, e = format_date(start), format_date(end)
    if not s:
        return e
    if not e:
        return f"{s} {EN_DASH} {ONGOING_LABEL}"
    return s if s == e else f"{s} {EN_DASH} {e}"


def month_label(value: str, *, lower: bool = True) -> str:
    """'2026-10' → 'octobre 2026' (for sentences)."""
    label = format_date(value)
    return label[:1].lower() + label[1:] if lower and label else label


def parse_year_month(value: str) -> tuple[int, int] | None:
    m = _YEAR_MONTH.match((value or "").strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return int(m.group(1)), int(m.group(2))


def has_started(start: str, now: datetime) -> bool:
    ym = parse_year_month(start)
    return ym is not None and (now.year, now.month) >= ym


def department(postal_code: str) -> str:
    code = (postal_code or "").strip()
    if len(code) != 5 or not code.isdigit():
        return ""
    return code[:3] if code.startswith("97") else code[:2]


def city_label(identity: Identity) -> str:
    dept = department(identity.postal_code)
    return f"{identity.city} ({dept})" if identity.city and dept else identity.city


def display_url(url: str | None) -> str:
    text = re.sub(r"^https?://", "", (url or "").strip(), flags=re.I)
    return re.sub(r"^www\.", "", text, flags=re.I).rstrip("/")


def _href(url: str) -> str:
    return url if re.match(r"^https?://", url, re.I) else f"https://{url}"


def contact_items(identity: Identity) -> list[ContactItem]:
    """Contact block of the CV header, in the body (never in a PDF header — research §5)."""
    items: list[ContactItem] = []
    if identity.city:
        items.append(ContactItem(city_label(identity)))
    if identity.phone:
        items.append(ContactItem(identity.phone, "tel:" + re.sub(r"[^\d+]", "", identity.phone)))
    items.append(ContactItem(identity.email, f"mailto:{identity.email}"))
    for url in (identity.linkedin, identity.github, identity.portfolio):
        if url:
            items.append(ContactItem(display_url(url), _href(url)))
    if identity.driving_license:
        items.append(ContactItem(identity.driving_license))
    return items


def contact_line(identity: Identity) -> str:
    return SEPARATOR.join(item.text for item in contact_items(identity))


def strip_emphasis(text: str) -> str:
    """Remove the **emphasis** markers used by the tailoring step."""
    return clean_spaces(_EMPHASIS.sub(r"\1", text or "").replace("**", ""))


def emphasis_html(text: str) -> str:
    """HTML-escape ``text`` and turn **x** into <strong>x</strong> (unpaired markers dropped)."""
    escaped = html.escape(clean_spaces(text or ""), quote=False)
    return _EMPHASIS.sub(r"<strong>\1</strong>", escaped).replace("**", "")


def clean_spaces(text: str) -> str:
    return _SPACES.sub(" ", text or "").strip()


def merge_unbalanced(items: list[str]) -> list[str]:
    """Re-join list items split inside parentheses by a YAML flow list ("jeux (Roblox", "Unity)")."""
    merged: list[str] = []
    buffer = ""
    for item in (clean_spaces(i) for i in items):
        if not item:
            continue
        buffer = f"{buffer}, {item}" if buffer else item
        if buffer.count("(") <= buffer.count(")"):
            merged.append(buffer)
            buffer = ""
    if buffer:
        merged.append(buffer)
    return merged


def ascii_token(text: str) -> str:
    """File-name-safe ASCII token: 'Hélène Marie' → 'Helene-Marie'."""
    token = re.sub(r"[^A-Za-z0-9]+", "-", strip_accents(text or "")).strip("-")
    return token or "x"
