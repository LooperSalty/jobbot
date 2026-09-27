"""schema.org ``JobPosting`` blocks (JSON-LD) found in a web page — company careers pages, offer pages.

Pure parsing, no network. Handles ``@graph``, lists, ``ItemList`` (``itemListElement[].item``) and the usual
variants of ``jobLocation`` / ``hiringOrganization`` / ``employmentType``.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from jobbot.sources.offer_utils import clean_title, country_code, float_or_none, parse_date
from jobbot.textutil import html_to_text

log = logging.getLogger(__name__)

MAX_POSTINGS = 200


@dataclass(frozen=True)
class PostingFields:
    """What jobbot keeps from one JSON-LD JobPosting."""

    title: str
    description: str = ""
    company: str = ""
    city: str = ""
    postal_code: str | None = None
    country: str | None = None  # ISO code
    latitude: float | None = None
    longitude: float | None = None
    published_at: datetime | None = None
    valid_through: str | None = None
    employment_type: str = ""
    remote: bool | None = None
    url: str | None = None
    identifier: str | None = None


def job_postings(page_html: str) -> list[dict[str, Any]]:
    """Every JobPosting object of the page (raw JSON-LD dicts), in document order."""
    found: list[dict[str, Any]] = []
    soup = BeautifulSoup(page_html or "", "lxml")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError, RecursionError):  # invalid or absurdly nested JSON: block ignored
            continue
        _collect(data, found)
        if len(found) >= MAX_POSTINGS:
            break
    return found[:MAX_POSTINGS]


def _collect(node: object, found: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node:
            _collect(item, found, depth + 1)
        return
    if not isinstance(node, dict):
        return
    kinds = node.get("@type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if "JobPosting" in kinds:
        found.append(node)
        return
    for key in ("@graph", "itemListElement", "item", "mainEntity"):
        if key in node:
            _collect(node[key], found, depth + 1)


def posting_fields(posting: dict[str, Any]) -> PostingFields | None:
    """Normalised fields of one JobPosting (None without a title)."""
    title = clean_title(posting.get("title") or posting.get("name"))
    if not title:
        return None
    place = _first_place(posting.get("jobLocation"))
    address = _address(place.get("address"))
    geo = place.get("geo") if isinstance(place.get("geo"), dict) else {}
    city = clean_title(address.get("addressLocality"))
    # TELECOMMUTE with a physical place = remote allowed (often hybrid): only a place-less one is full remote
    telecommute = "TELECOMMUTE" in str(posting.get("jobLocationType") or "").upper()
    return PostingFields(
        title=title, description=_description(posting.get("description")), company=_company(posting),
        city=city, postal_code=str(address.get("postalCode") or "") or None,
        country=_country(address.get("addressCountry")), latitude=float_or_none(geo.get("latitude")),
        longitude=float_or_none(geo.get("longitude")), published_at=parse_date(posting.get("datePosted")),
        valid_through=str(posting.get("validThrough") or "") or None, employment_type=_employment(posting),
        remote=True if telecommute and not city else None, url=str(posting.get("url") or "") or None,
        identifier=_identifier(posting.get("identifier")),
    )


def _first_place(value: object) -> dict[str, Any]:
    places = value if isinstance(value, list) else [value]
    return next((p for p in places if isinstance(p, dict)), {})


def _address(value: object) -> dict[str, Any]:
    """PostalAddress dict; a plain-text address (« 1 rue X, 57000 Metz ») gives its city and postal code."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    postal = re.search(r"\b(\d{5})\b\s*([^,]*)", value)
    if postal:
        return {"postalCode": postal.group(1), "addressLocality": postal.group(2).strip() or value.split(",")[0]}
    return {"addressLocality": value.split(",")[0].strip()}


def _description(value: object) -> str:
    text = htmllib.unescape(str(value or ""))
    return html_to_text(text) if "<" in text else text.strip()


def _company(posting: dict[str, Any]) -> str:
    org = posting.get("hiringOrganization")
    if isinstance(org, list):
        org = next((o for o in org if isinstance(o, dict | str)), None)
    if isinstance(org, dict):
        return clean_title(org.get("name"))
    return clean_title(org) if isinstance(org, str) else ""


def _country(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("identifier")
    return country_code(value)


def _employment(posting: dict[str, Any]) -> str:
    value = posting.get("employmentType")
    values = value if isinstance(value, list) else [value]
    return " ".join(str(v) for v in values if v)


def _identifier(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("value") or value.get("@id")
    text = str(value or "").strip()
    return text or None
