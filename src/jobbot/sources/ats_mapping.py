"""Public ATS postings (Lever, Greenhouse, Recruitee, SmartRecruiters, Workable, Teamtailor RSS, JSON-LD) →
``JobOffer`` (source ``company_ats``). Pure functions, no network; shapes live-checked 2026-09-27.

Apply is always EXTERNAL with the posting's own apply page. SmartRecruiters and Workable list postings without
their text: those offers carry ``raw["needs_details"]`` and ``CompanyATSSource.fetch_details`` completes them.
"""

from __future__ import annotations

import html as htmllib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from jobbot import clock
from jobbot.models import ApplyMethod, JobOffer
from jobbot.sources.ats_detect import AtsBoard, board_url
from jobbot.sources.jsonld import PostingFields
from jobbot.sources.offer_utils import (
    clean_title,
    country_code,
    detect_contract,
    detect_remote,
    float_or_none,
    format_salary,
    parse_date,
)
from jobbot.textutil import html_to_text, stable_id

SOURCE = "company_ats"
DETAILS_MIN_CHARS = 400  # same threshold as registry.DETAILS_MIN_CHARS
_TT_NS = {"tt": "https://teamtailor.com/locations"}
_TT_REMOTE = {"fully": True, "none": False}
_SENIOR_LEVELS = frozenset({"director", "executive"})  # SmartRecruiters experienceLevel ids
_DTD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.I)


@dataclass(frozen=True)
class Posting:
    """Provider-neutral fields of one posting, before the ``JobOffer`` is built."""

    posting_id: str
    title: str
    url: str
    company: str = ""
    description: str = ""
    apply_url: str | None = None
    city: str = ""
    postal_code: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    published_at: datetime | None = None
    contract_hint: str = ""
    remote: bool | None = None
    salary: str | None = None
    extra: tuple[tuple[str, Any], ...] = ()


def to_offer(board: AtsBoard, posting: Posting, *, company_hint: str = "", found_at: datetime | None = None,
             needs_details: bool = False) -> JobOffer:
    external_id = f"{board.key}:{posting.posting_id}"
    raw = {
        "ats": board.provider, "ats_slug": board.slug, "ats_region": board.region, "posting_id": posting.posting_id,
        "country_code": posting.country, "board_url": board_url(board), "contract_label": posting.contract_hint,
        **dict(posting.extra), **({"needs_details": True} if needs_details else {}),
    }
    remote = posting.remote if posting.remote is not None else detect_remote(posting.title, posting.description)
    return JobOffer(
        id=stable_id(SOURCE, external_id), source=SOURCE, external_id=external_id, title=posting.title,
        company=posting.company or company_hint or board.slug.replace("-", " ").title(),
        location=location_label(posting.city, posting.postal_code, posting.country),
        postal_code=posting.postal_code, latitude=posting.latitude, longitude=posting.longitude,
        description=posting.description, url=posting.url, apply_url=posting.apply_url,
        apply_method=ApplyMethod.EXTERNAL,
        contract_type=detect_contract(posting.title, posting.description, posting.contract_hint),
        remote=remote, salary=posting.salary, published_at=posting.published_at,
        found_at=found_at or clock.now(), raw=raw,
    )


def location_label(city: str, postal_code: str | None, country: str | None) -> str:
    """« Metz (57) » in France (same style as the other sources), « Luxembourg, LU » abroad."""
    city = clean_title(city)
    postal = (postal_code or "").strip()
    if country not in (None, "FR"):
        return ", ".join(p for p in (city, country) if p)
    return f"{city} ({postal[:2]})" if city and re.fullmatch(r"\d{5}", postal) else city


def _split_place(text: str) -> tuple[str, str | None]:
    """« Paris, France » → (« Paris », « FR »); the last comma part is the country when recognised."""
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    if not parts:
        return "", None
    country = country_code(parts[-1]) if len(parts) > 1 else country_code(parts[0])
    return ("" if len(parts) == 1 and country else parts[0]), country


# --------------------------------------------------------------------------- providers


def lever_posting(item: dict) -> Posting:
    cats = item.get("categories") or {}
    lists = [f"{entry.get('text') or ''}\n{html_to_text(entry.get('content') or '')}"
             for entry in item.get("lists") or [] if isinstance(entry, dict)]
    text = "\n\n".join(p for p in (item.get("descriptionPlain"), *lists, item.get("additionalPlain")) if p)
    city, country = _split_place(cats.get("location") or "")
    workplace = str(item.get("workplaceType") or "")
    return Posting(
        posting_id=str(item["id"]), title=clean_title(item["text"]), url=item.get("hostedUrl") or "",
        apply_url=item.get("applyUrl") or None, description=text.strip(), city=city,
        country=country_code(item.get("country")) or country, published_at=parse_date(item.get("createdAt")),
        contract_hint=str(cats.get("commitment") or ""), remote=True if workplace == "remote" else None,
        extra=(("department", cats.get("department")), ("team", cats.get("team")), ("workplace", workplace)),
    )


def greenhouse_posting(item: dict) -> Posting:
    city, country = _split_place((item.get("location") or {}).get("name") or "")
    labels = [str(m.get("value")) for m in item.get("metadata") or []
              if isinstance(m, dict) and isinstance(m.get("value"), str)
              and re.search(r"contract|contrat|employment|time type|type", str(m.get("name") or ""), re.I)]
    url = item.get("absolute_url") or ""
    text = html_to_text(htmllib.unescape(item.get("content") or ""))  # Greenhouse escapes its HTML
    return Posting(
        posting_id=str(item["id"]), title=clean_title(item["title"]), url=url, apply_url=url or None,
        company=clean_title(item.get("company_name")), description=text, city=city, country=country,
        published_at=parse_date(item.get("first_published") or item.get("updated_at")),
        contract_hint=" ".join(labels), extra=(("language", item.get("language")),),
    )


def recruitee_posting(item: dict) -> Posting:
    salary = item.get("salary") or {}
    text = "\n\n".join(html_to_text(item.get(k) or "") for k in ("description", "requirements") if item.get(k))
    return Posting(
        posting_id=str(item.get("id") or item["slug"]), title=clean_title(item["title"]),
        url=item.get("careers_url") or "", apply_url=item.get("careers_apply_url") or None,
        company=clean_title(item.get("company_name")), description=text, city=clean_title(item.get("city")),
        postal_code=str(item.get("postal_code") or "") or None, country=country_code(item.get("country_code")),
        published_at=parse_date(item.get("published_at") or item.get("created_at")),
        contract_hint=str(item.get("employment_type_code") or ""), remote=_recruitee_remote(item),
        salary=format_salary(float_or_none(salary.get("min")), float_or_none(salary.get("max")),
                             salary.get("currency") or "EUR", salary.get("period")),
        extra=(("department", item.get("department")), ("experience", item.get("experience_code")),
               ("hybrid", item.get("hybrid"))),
    )


def _recruitee_remote(item: dict) -> bool | None:
    """Full remote only: ``remote`` without ``hybrid`` / ``on_site`` (Recruitee sets several flags at once)."""
    return True if item.get("remote") is True and not item.get("hybrid") and not item.get("on_site") else None


def smartrecruiters_posting(item: dict, slug: str) -> Posting | None:
    """List item (no text yet). None for director / executive postings."""
    level = (item.get("experienceLevel") or {}).get("id") or ""
    if level in _SENIOR_LEVELS:
        return None
    loc = item.get("location") or {}
    fields = [str(f.get("valueLabel") or "") for f in item.get("customField") or []
              if isinstance(f, dict) and re.search(r"contra|employ", str(f.get("fieldLabel") or ""), re.I)]
    posting_id = str(item["id"])
    return Posting(
        posting_id=posting_id, title=clean_title(item["name"]),
        url=f"https://jobs.smartrecruiters.com/{slug}/{posting_id}",
        company=clean_title((item.get("company") or {}).get("name")), city=clean_title(loc.get("city")),
        postal_code=str(loc.get("postalCode") or "") or None, country=country_code(loc.get("country")),
        latitude=float_or_none(loc.get("latitude")), longitude=float_or_none(loc.get("longitude")),
        published_at=parse_date(item.get("releasedDate")),
        contract_hint=" ".join([*fields, str((item.get("typeOfEmployment") or {}).get("label") or "")]).strip(),
        remote=True if loc.get("remote") is True and not loc.get("hybrid") else None,
        extra=(("experience", level), ("detail_url", item.get("ref"))),
    )


def smartrecruiters_detail(detail: dict) -> dict[str, Any]:
    """``JobOffer`` fields completed from ``/postings/{id}``."""
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    order = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
    parts = [f"{(sections.get(k) or {}).get('title') or ''}\n{html_to_text((sections.get(k) or {}).get('text') or '')}"
             for k in order if (sections.get(k) or {}).get("text")]
    return {"description": "\n\n".join(p.strip() for p in parts), "apply_url": detail.get("applyUrl") or None,
            "url": detail.get("postingUrl") or None, "language": (detail.get("language") or {}).get("code")}


def workable_posting(item: dict, slug: str) -> Posting:
    loc = item.get("location") or {}
    shortcode = str(item["shortcode"])
    page = f"https://apply.workable.com/{slug}/j/{shortcode}/"
    return Posting(
        posting_id=shortcode, title=clean_title(item["title"]), url=page, apply_url=f"{page}apply/",
        city=clean_title(loc.get("city")), country=country_code(loc.get("countryCode") or loc.get("country")),
        published_at=parse_date(item.get("published")), contract_hint=str(item.get("type") or ""),
        remote=True if item.get("workplace") == "remote" else None,
        extra=(("department", item.get("department")), ("workplace", item.get("workplace"))),
    )


def workable_detail(detail: dict) -> dict[str, Any]:
    parts = [html_to_text(detail.get(k) or "") for k in ("description", "requirements", "benefits") if detail.get(k)]
    return {"description": "\n\n".join(parts)}


def teamtailor_postings(rss: bytes | str) -> tuple[str, list[Posting]]:
    """(company name = channel title, postings) of a ``/jobs.rss`` feed (raw bytes: the XML declaration gives the
    encoding). Raises ``ET.ParseError``.

    A feed never needs a DTD: any ``<!DOCTYPE``/``<!ENTITY`` is refused before parsing (no entity expansion)."""
    data = rss.encode("utf-8") if isinstance(rss, str) else rss
    if _DTD.search(data.decode("utf-8", errors="replace")):
        raise ET.ParseError("flux RSS refusé : DTD / entités interdites")
    root = ET.fromstring(data)
    channel = root.find("channel")
    company = clean_title(channel.findtext("title") if channel is not None else "")
    items = channel.findall("item") if channel is not None else []
    return company, [p for p in (_teamtailor_item(item) for item in items) if p is not None]


def _teamtailor_item(item: ET.Element) -> Posting | None:
    title, link = clean_title(item.findtext("title")), (item.findtext("link") or "").strip()
    if not title or not link:
        return None
    place = item.find("tt:locations/tt:location", _TT_NS)
    where = {tag: (place.findtext(f"tt:{tag}", "", _TT_NS) if place is not None else "").strip()
             for tag in ("city", "name", "zip", "country")}
    match = re.search(r"/jobs/(\d+)", link)
    return Posting(
        posting_id=match.group(1) if match else (item.findtext("guid") or link), title=title, url=link,
        apply_url=link, description=html_to_text(item.findtext("description") or ""),
        city=where["city"] or where["name"], postal_code=where["zip"] or None, country=country_code(where["country"]),
        published_at=parse_date(item.findtext("pubDate")),
        remote=_TT_REMOTE.get((item.findtext("remoteStatus") or "").strip()),
        extra=(("department", item.findtext("tt:department", "", _TT_NS)),),
    )


def jsonld_posting(fields: PostingFields, page_url: str) -> Posting:
    """A relative ``url`` is resolved against the careers page; anything but http(s) falls back to the page."""
    own = urljoin(page_url, fields.url) if fields.url else ""
    url = own if own.startswith(("http://", "https://")) else page_url
    posting_id = fields.identifier or (url if url != page_url else stable_id(page_url, fields.title))
    return Posting(
        posting_id=posting_id, title=fields.title, url=url, apply_url=url, company=fields.company,
        description=fields.description, city=fields.city, postal_code=fields.postal_code, country=fields.country,
        latitude=fields.latitude, longitude=fields.longitude, published_at=fields.published_at,
        contract_hint=fields.employment_type, remote=fields.remote,
        extra=(("valid_through", fields.valid_through), ("careers_page", page_url)),
    )


def jsonld_needs_details(posting: Posting, page_url: str) -> bool:
    """A short listing text with its own offer page: ``fetch_details`` reads that page's JSON-LD."""
    return len(posting.description) < DETAILS_MIN_CHARS and posting.url != page_url
