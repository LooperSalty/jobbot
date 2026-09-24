"""HelloWork (hellowork.com) job source.

Search is kept tiny on purpose (robots.txt ``Disallow: /*?`` + CGU art. 8.2): page 1 only, ≥ 5 s between
requests. Offer pages ``/fr-fr/emplois/{id}.html`` are robots-allowed and carry a complete JSON-LD
``JobPosting`` plus a ``dataLayer`` object giving the apply mode. See docs/research/hellowork.md.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

from jobbot import clock
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import ApplyMethod, ContractType, JobOffer
from jobbot.sources.base import JobSource, SearchQuery, SourceUnavailable
from jobbot.sources.linkedin import (  # shared board helpers
    mark_expired,
    node_text,
    parse_date,
)
from jobbot.textutil import html_to_text, normalize, stable_id

log = logging.getLogger(__name__)

BASE = "https://www.hellowork.com"
SEARCH_PATH = "/fr-fr/emploi/recherche.html"
OFFER_PATH = "/fr-fr/emplois/{id}.html"
MIN_INTERVAL_S = 5.0
RADII_KM = (0, 5, 10, 20, 50, 100)  # UI values (0..50) + 100 accepted by the server
FUNCTIONS = ("Informatique_dev",)  # best precision filter ("développeur" also matches business developers)
IN_SITE_TYPES = frozenset({"ATS", "Smartapply", "Smartapply_v2"})
EXTERNAL_TYPES = frozenset({"Url", "UrlWithForm"})
CONTRACT_PARAMS = {ContractType.ALTERNANCE: "Alternance", ContractType.CDI: "CDI", ContractType.CDD: "CDD",
                   ContractType.STAGE: "Stage", ContractType.FREELANCE: "Freelance",
                   ContractType.INTERIM: "Travail_temp"}
LOCATION_URIS = {  # l_autocomplete URIs (live-verified 2026-09-23)
    "metz": "http://www.rj.com/commun/localite/commune/57463",
    "nancy": "http://www.rj.com/commun/localite/commune/54395",
    "thionville": "http://www.rj.com/commun/localite/commune/57672",
    "moselle": "http://www.rj.com/commun/localite/departement/57",
    "grand est": "http://www.rj.com/commun/localite/region/44",
    "luxembourg": "http://www.rj.com/commun/localite/pays/LU",
}
_CONTRACT_RULES = (
    (re.compile(r"\b(alternance|apprentissage|professionnalisation|apprenti)"), ContractType.ALTERNANCE),
    (re.compile(r"\bcdi\b"), ContractType.CDI),
    (re.compile(r"\bcdd\b"), ContractType.CDD),
    (re.compile(r"\bstage"), ContractType.STAGE),
    (re.compile(r"\b(interim|travail temp)"), ContractType.INTERIM),
    (re.compile(r"\b(freelance|independant|franchise)"), ContractType.FREELANCE),
)
_OFFER_ID_RE = re.compile(r"^\d{5,10}$")
_DATALAYER_RE = re.compile(r"window\.dataLayer\.push\((\{.*?\})\);", re.S)
_TOTAL_RE = re.compile(r"(\d[\d\s]*)offres?", re.I)
_REL_DATE_RE = re.compile(r"il y a\s+(\d+)\s+(minute|heure|jour|semaine|mois)", re.I)
_REL_UNITS = {"minute": timedelta(minutes=1), "heure": timedelta(hours=1), "jour": timedelta(days=1),
              "semaine": timedelta(days=7), "mois": timedelta(days=30)}
_SALARY_UNITS = {"MONTH": "mois", "YEAR": "an", "HOUR": "heure", "DAY": "jour", "WEEK": "semaine"}


# --------------------------------------------------------------------------- helpers
def offer_url(offer_id: str) -> str:
    return BASE + OFFER_PATH.format(id=offer_id)


def is_offer_id(value: str) -> bool:
    return bool(_OFFER_ID_RE.match(str(value or "").strip()))


def contract_from_label(label: str | None) -> ContractType:
    """Map a HelloWork contract label ("Alternance", "CDI", "Intérim"...) to ContractType."""
    text = normalize(label or "").replace("_", " ")
    return next((contract for pattern, contract in _CONTRACT_RULES if pattern.search(text)), ContractType.OTHER)


def apply_method_for(response_type: str | None, direct_apply: bool | None = None,
                     label: str | None = None) -> ApplyMethod:
    """ATS / Smartapply* → in-site HelloWork application; Url / UrlWithForm → external site."""
    rt = (response_type or "").strip()
    if rt in IN_SITE_TYPES or rt.lower().startswith("smartapply"):
        return ApplyMethod.HELLOWORK
    if rt in EXTERNAL_TYPES:
        return ApplyMethod.EXTERNAL
    if label and "site d" in normalize(label):  # "Postuler sur le site du partenaire / du recruteur"
        return ApplyMethod.EXTERNAL
    return ApplyMethod.HELLOWORK if direct_apply is True else ApplyMethod.EXTERNAL


def remote_from_label(label: str | None) -> bool | None:
    """True = full remote, False = no remote, None = unknown or partial."""
    t = normalize(label or "")
    if "teletravail" not in t and "remote" not in t:
        return None
    if "pas de" in t or "pas teletravail" in t:
        return False
    return True if ("complet" in t or "total" in t or "100" in t) else None


def radius_param(radius_km: int) -> int:
    return next((r for r in RADII_KM if r >= radius_km), RADII_KM[-1])


def date_param(max_age_days: int) -> str:
    return next((code for limit, code in ((1, "h"), (3, "d"), (7, "w"), (31, "m")) if max_age_days <= limit), "all")


def build_search_params(query: SearchQuery) -> list[tuple[str, str]]:
    params = [("k", query.keywords), ("st", "date"), ("p", "1"), ("d", date_param(query.max_age_days))]
    loc = query.location
    if loc is not None and loc.city:
        params.append(("l", loc.city))
        uri = LOCATION_URIS.get(normalize(loc.city))
        if uri:
            params.append(("l_autocomplete", uri))
        params.append(("ray", str(radius_param(loc.radius_km))))
    params += [("c", CONTRACT_PARAMS[c]) for c in query.contract_types if c in CONTRACT_PARAMS]
    if query.remote:
        params.append(("t", "Complet"))
    return params + [("f", f) for f in FUNCTIONS]


def _relative_date(text: str, now: datetime) -> datetime | None:
    t = normalize(text)
    if "aujourd" in t or "a l'instant" in t:
        return now
    if "hier" in t:
        return now - timedelta(days=1)
    m = _REL_DATE_RE.search(t)
    return now - int(m.group(1)) * _REL_UNITS[m.group(2).lower()] if m else None


# --------------------------------------------------------------------------- search page
def parse_search_page(html: str) -> tuple[int | None, list[dict[str, Any]]]:
    """(total announced in <h1>, card dicts). total is None when the page has no result header."""
    soup = BeautifulSoup(html or "", "lxml")
    h1 = soup.select_one("h1")
    m = _TOTAL_RE.search(node_text(h1)) if h1 else None
    total = int(re.sub(r"\D", "", m.group(1))) if m else None
    cards = [c for c in (_parse_card(li) for li in soup.select("li[data-id-storage-item-id]")) if c]
    return total, cards


def _card_analytics(li: Tag) -> dict[str, Any]:
    serp = li.select_one("[data-cy=serpCard]")
    raw = serp.get("data-analytics-values-param") if serp is not None else None
    if not raw:
        return {}
    try:
        return (json.loads(raw).get("product_data") or [{}])[0] or {}
    except (json.JSONDecodeError, AttributeError, IndexError):
        log.debug("Analytics HelloWork illisibles sur une carte")
        return {}


def _parse_card(li: Tag) -> dict[str, Any] | None:
    offer_id = (li.get("data-id-storage-item-id") or "").strip()
    link = li.select_one("a[data-cy=offerTitle]")
    if not is_offer_id(offer_id) or link is None:
        return None
    parts = [node_text(p) for p in link.select("p")]  # never "h3 p": lxml moves <p> out of <h3>
    location = node_text(li.select_one("[data-cy=localisationCard]"))
    contract = node_text(li.select_one("[data-cy=contractCard]"))
    salary = node_text(li.select_one("div.tag-secondary-s.typo-s-bold"))
    tags = [node_text(t) for t in li.select("div.tag-secondary-s")]
    analytics = _card_analytics(li)
    return {
        "id": offer_id, "title": parts[0] if parts else (link.get("title") or "").strip(),
        "company": parts[1] if len(parts) > 1 else "", "location": location, "contract": contract,
        "salary": salary, "tags": [t for t in tags if t and t not in {location, contract, salary}],
        "date_text": node_text(li.select_one("div.typo-s.text-grey-500")),
        "response_type": analytics.get("product_response_type"), "match_block": analytics.get("product_list"),
        "follow_up": li.select_one("[data-cy=followUpTag]") is not None,
    }


def card_to_job(card: dict[str, Any], now: datetime) -> JobOffer:
    remote = next((r for r in (remote_from_label(t) for t in card["tags"]) if r is not None), None)
    contract = contract_from_label(card["contract"])
    return JobOffer(
        id=stable_id("hellowork", card["id"]), source="hellowork", external_id=card["id"],
        title=card["title"], company=card["company"], location=card["location"], url=offer_url(card["id"]),
        apply_method=apply_method_for(card["response_type"]),
        contract_type=contract if contract is not ContractType.OTHER else contract_from_label(card["title"]),
        remote=remote, salary=card["salary"] or None, published_at=_relative_date(card["date_text"], now),
        found_at=now,
        raw={"response_type": card["response_type"], "match_block": card["match_block"],
             "contract_label": card["contract"], "tags": card["tags"], "follow_up": card["follow_up"],
             "needs_details": True},
    )


# --------------------------------------------------------------------------- offer page
def _find_jobposting(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _find_datalayer(html: str) -> dict[str, Any]:
    for m in _DATALAYER_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and (data.get("idOffre") or data.get("categoryPage") == "Detail"):
            return data
    return {}


def _datalayer_response_type(datalayer: dict[str, Any]) -> str | None:
    products = ((datalayer.get("ecommerce") or {}).get("detail") or {}).get("products") or [{}]
    first = products[0] if isinstance(products, list) and products else {}
    return first.get("response_type") if isinstance(first, dict) else None


def _as_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in items if v is not None and str(v).strip()]


def _format_salary(posting: dict[str, Any]) -> str | None:
    base = posting.get("baseSalary") or {}
    value = base.get("value") if isinstance(base, dict) else None
    if not isinstance(value, dict):
        return None
    amounts = [v for v in (value.get("minValue"), value.get("maxValue")) if isinstance(v, int | float)]
    if not amounts:
        return None
    unit = _SALARY_UNITS.get(str(value.get("unitText") or "").upper(), "")
    text = " - ".join(f"{round(v):,}".replace(",", " ") for v in amounts)
    return f"{text} €" + (f" / {unit}" if unit else "")


def _description(posting: dict[str, Any]) -> str:
    text = html_to_text(str(posting.get("description") or ""))
    qualifications = html_to_text(str(posting.get("qualifications") or ""))
    if qualifications and normalize(qualifications)[:80] not in normalize(text):
        text = f"{text}\n\n{qualifications}".strip()
    skills = _as_list(posting.get("skills"))
    return f"{text}\n\nCompétences : {', '.join(skills)}".strip() if skills else text


def _posting_fields(posting: dict[str, Any]) -> dict[str, Any]:
    org = posting.get("hiringOrganization") if isinstance(posting.get("hiringOrganization"), dict) else {}
    place = posting.get("jobLocation") or {}
    place = place[0] if isinstance(place, list) and place else place
    address = (place.get("address") or {}) if isinstance(place, dict) else {}
    identifier = posting.get("identifier") if isinstance(posting.get("identifier"), dict) else {}
    return {
        "title": str(posting.get("title") or "").strip(), "company": str(org.get("name") or "").strip(),
        "location": str(address.get("addressLocality") or "").strip(),
        "postal_code": str(address.get("postalCode") or "").strip() or None,
        "description": _description(posting), "salary": _format_salary(posting),
        "published_at": parse_date(posting.get("datePosted")),
        "remote": True if posting.get("jobLocationType") == "TELECOMMUTE" else None,
        "raw": {
            "direct_apply": posting.get("directApply"), "valid_through": posting.get("validThrough"),
            "occupational_category": posting.get("occupationalCategory"),
            "industry": _as_list(posting.get("industry")), "skills": _as_list(posting.get("skills")),
            "employment_type": _as_list(posting.get("employmentType")),
            "reference": identifier.get("value"), "company_url": org.get("sameAs"),
        },
    }


def _html_fields(soup: BeautifulSoup) -> dict[str, Any]:
    """Fallback when the JSON-LD block is missing: read the visible header / panel."""
    header, panel = soup.select_one("h1#main-content"), soup.select_one("#offer-panel")
    facts = [node_text(li) for li in soup.select("h1#main-content + ul li")]
    return {
        "title": node_text(soup.select_one("[data-cy=jobTitle]")),
        "company": node_text(header.select_one("a")) if header is not None else "",
        "location": facts[0] if facts else "", "postal_code": None, "salary": None, "published_at": None,
        "description": html_to_text(str(panel)) if panel is not None else "", "remote": None,
        "raw": {"contract_label_html": facts[1] if len(facts) > 1 else None},
    }


def parse_offer_page(offer_id: str, html: str, now: datetime | None = None) -> JobOffer | None:
    """Build a full JobOffer from an offer page; None when the page is not a recognisable offer."""
    soup = BeautifulSoup(html or "", "lxml")
    posting = _find_jobposting(soup)
    fields = _posting_fields(posting) if posting else _html_fields(soup)
    if not fields["title"]:
        return None
    datalayer = _find_datalayer(html)
    response_type = _datalayer_response_type(datalayer)
    label = node_text(soup.select_one("a[data-cy=applyButtonHeader]")) or None
    contract_label = datalayer.get("contrat") or fields["raw"].get("contract_label_html")
    contract = contract_from_label(contract_label)
    raw = {**fields.pop("raw"), "response_type": response_type, "apply_label": label,
           "contract_label": contract_label, "company_type": datalayer.get("typeEntreprise"),
           "domain": datalayer.get("Domaine")}
    return JobOffer(
        id=stable_id("hellowork", offer_id), source="hellowork", external_id=offer_id, url=offer_url(offer_id),
        apply_method=apply_method_for(response_type, raw.get("direct_apply"), label),
        contract_type=contract if contract is not ContractType.OTHER else contract_from_label(fields["title"]),
        found_at=now or clock.now(),
        raw={**{k: v for k, v in raw.items() if v not in (None, [], "")}, "needs_details": False},
        **fields,
    )


def merge_details(job: JobOffer, detail: JobOffer) -> JobOffer:
    """Copy of ``job`` enriched with ``detail`` (keeps id, status, score, found_at)."""
    fields = ("title", "company", "location", "postal_code", "description", "url", "salary", "published_at", "remote")
    update: dict[str, Any] = {f: getattr(detail, f) for f in fields if getattr(detail, f) not in (None, "")}
    update["apply_method"] = detail.apply_method
    if detail.contract_type is not ContractType.OTHER:
        update["contract_type"] = detail.contract_type
    update["raw"] = {**job.raw, **detail.raw, "needs_details": False}
    return job.model_copy(update=update)


# --------------------------------------------------------------------------- source
class HelloWorkSource(JobSource):
    """HelloWork board: tiny search (page 1) + offer pages. Never logs in, never applies."""

    name = "hellowork"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or make_client()
        self._throttle = HostThrottle(MIN_INTERVAL_S)
        self._blocked: str | None = None

    def available(self) -> tuple[bool, str]:
        return (False, self._blocked) if self._blocked else (True, "")

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        resp = self._get(BASE + SEARCH_PATH, params=build_search_params(query))
        if resp.status_code >= 400:
            raise SourceUnavailable(f"HelloWork : recherche impossible (HTTP {resp.status_code})")
        total, cards = parse_search_page(resp.text)
        if total is None and not cards:
            raise SourceUnavailable("HelloWork : page de résultats méconnaissable (site modifié ?)")
        log.info("HelloWork « %s » : %s offres annoncées, %d lues (page 1)", query.keywords, total, len(cards))
        now = clock.now()
        for card in cards[: max(query.max_results, 0)]:
            yield card_to_job(card, now)

    def job_from_id(self, offer_id: str) -> JobOffer | None:
        """Full offer from its page; None when it expired (HTTP 410) or does not exist (404)."""
        offer_id = str(offer_id or "").strip()
        if not is_offer_id(offer_id):
            raise ValueError(f"Identifiant d'offre HelloWork invalide : {offer_id!r}")
        resp = self._get(offer_url(offer_id))
        if resp.status_code in (404, 410):
            log.info("Offre HelloWork %s expirée ou supprimée (HTTP %s)", offer_id, resp.status_code)
            return None
        if resp.status_code >= 400:
            raise SourceUnavailable(f"HelloWork : offre {offer_id} illisible (HTTP {resp.status_code})")
        job = parse_offer_page(offer_id, resp.text)
        if job is None:
            raise SourceUnavailable(f"HelloWork : page de l'offre {offer_id} méconnaissable (site modifié ?)")
        return job

    def fetch_details(self, job: JobOffer) -> JobOffer:
        if job.source != self.name or not is_offer_id(job.external_id):
            return job
        detail = self.job_from_id(job.external_id)
        return mark_expired(job) if detail is None else merge_details(job, detail)

    def _get(self, url: str, params: list[tuple[str, str]] | None = None) -> httpx.Response:
        if self._blocked:
            raise SourceUnavailable(self._blocked)
        try:
            resp = request_with_retry(self._client, "GET", url, params=params, retries=2, throttle=self._throttle)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"HelloWork injoignable : {exc}") from exc
        if resp.status_code in (403, 429):
            self._blocked = (f"HelloWork bloque nos requêtes (HTTP {resp.status_code}) : "
                             "source suspendue pour cette exécution, réessayer plus tard")
            log.warning(self._blocked)
            raise SourceUnavailable(self._blocked)
        return resp
