"""Interview logistics read from an invitation e-mail (date, time, visio link, address, phone) — rules only.

Nothing is guessed: a field is filled only when the e-mail literally contains it, so the prep sheet never shows
a wrong date or a made-up link. The LLM is deliberately not used here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from jobbot.textutil import strip_accents

_DAYS = r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche"
_MONTHS = r"janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre"
_I = re.IGNORECASE

DATE_LONG = re.compile(rf"\b(?:(?:{_DAYS})\s+)?(?:1er|[0-3]?\d)\s+(?:{_MONTHS})(?:\s+20\d\d)?\b", _I)
DATE_NUMERIC = re.compile(rf"\b(?:(?:{_DAYS})\s+)?[0-3]?\d/[01]?\d(?:/(?:20)?\d\d)?\b", _I)
DATE_RELATIVE = re.compile(
    rf"\b(?:apr[èe]s-demain|demain|(?:ce\s+|cette\s+semaine\s+)?(?:{_DAYS})(?:\s+prochain)?)\b", _I
)
TIME = re.compile(r"(?<![\d:/])([01]?\d|2[0-3])\s?(?:h|heures?|:)\s?([0-5]\d)?(?![\w:])", _I)
LINK = re.compile(
    r"https?://(?:[\w-]+\.)*(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com|teams\.live\.com|whereby\.com"
    r"|webex\.com|jit\.si|calendly\.com|livestorm\.co)(?:/[^\s<>\"')\]]*)?",
    _I,
)
PHONE = re.compile(r"(?<![\d+])(?:\+33\s?[1-9]|0[1-9])(?:[\s.-]?\d{2}){4}(?!\d)")
_STREET = (
    r"\b\d{1,4}(?:\s?(?:bis|ter))?,?\s+(?:rue|avenue|av\.|boulevard|bd|place|all[ée]e|impasse|chemin|route|quai"
    r"|cours|esplanade|parvis)\s+[^\n,.;]{2,60}"
)
_POSTAL = r"(?:,?\s*\d{5}\s+[A-Za-zÀ-ÿ'’-]+(?:[ -][A-Za-zÀ-ÿ'’-]+){0,3})?"
ADDRESS = re.compile(_STREET + _POSTAL, _I)

_VISIO_WORDS = re.compile(r"\bvisio|visioconference|\bteams\b|google meet|\bzoom\b|\ben ligne\b|\bvideo\b")
_PHONE_WORDS = re.compile(r"telephon|par tel\b|\bappel\b|vous appeler|vous rappeler")
_ONSITE_WORDS = re.compile(r"dans nos locaux|sur place|nos bureaux|au siege|a l'adresse|en presentiel|presentiel"
                           r"|nous rendre visite|a notre agence")


class Logistics(BaseModel):
    """What the invitation says about the meeting. Every field is optional (None = not in the e-mail)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: str | None = None
    time: str | None = None
    mode: str | None = None  # visio | téléphone | sur place
    place: str | None = None
    link: str | None = None
    phone: str | None = None
    contact: str | None = None

    def found(self) -> bool:
        return any((self.date, self.time, self.mode, self.place, self.link, self.phone))

    def short(self) -> str:
        """'mardi 29 septembre · 10h · visio' (empty when nothing was found)."""
        return " · ".join(p for p in (self.date, self.time, self.mode) if p)


def _fold(text: str) -> str:
    return strip_accents(text or "").lower().replace("’", "'")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" ,.;:")


def _date(text: str) -> str | None:
    for pattern in (DATE_LONG, DATE_NUMERIC, DATE_RELATIVE):
        match = pattern.search(text)
        if match:
            return _clean(match.group(0))
    return None


def _time(text: str) -> str | None:
    match = TIME.search(text)
    if not match:
        return None
    hours, minutes = match.groups()
    return f"{int(hours)}h{minutes or ''}"


def _digits(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return "0" + digits[2:] if digits.startswith("33") else digits


def _phone(text: str, exclude: Iterable[str]) -> str | None:
    """First phone number of the e-mail that is not the candidate's own (quoted signatures, forwards)."""
    mine = {_digits(p) for p in exclude if p}
    for match in PHONE.finditer(text):
        if _digits(match.group(0)) not in mine:
            return _clean(match.group(0))
    return None


def _mode(folded: str, link: str | None, place: str | None) -> str | None:
    if link or _VISIO_WORDS.search(folded):
        return "visio"
    if _PHONE_WORDS.search(folded):
        return "téléphone"
    if place or _ONSITE_WORDS.search(folded):
        return "sur place"
    return None


def extract_logistics(text: str, *, contact: str = "", exclude_phones: Iterable[str] = ()) -> Logistics:
    """Date, time, mode, address, video link and phone found in ``text`` (the new part of the e-mail)."""
    body = text or ""
    link_match = LINK.search(body)
    link = link_match.group(0).rstrip(".,;:") if link_match else None
    address = ADDRESS.search(body)
    place = _clean(address.group(0)) if address else None
    return Logistics(
        date=_date(body), time=_time(body), mode=_mode(_fold(body), link, place), place=place, link=link,
        phone=_phone(body, exclude_phones), contact=_clean(contact) or None,
    )


def logistics_lines(found: Logistics) -> list[str]:
    """Markdown bullet lines for the « Logistique » section (French labels)."""
    labels = (("Date", found.date), ("Heure", found.time), ("Format", found.mode), ("Adresse", found.place),
              ("Lien de visio", found.link), ("Téléphone du recruteur", found.phone), ("Contact", found.contact))
    return [f"- **{label}** : {value}" for label, value in labels if value]
