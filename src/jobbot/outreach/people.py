"""Named people to write to at a company (HR / recruiters, tech managers, leaders), to maximise the reach of
spontaneous applications.

Sources, cheapest first: the company registry (``recherche-entreprises`` dirigeants, natural persons only, never
the "Commissaire aux comptes"), the company's own team/about pages, Hunter ``domain-search`` (when a key is set;
it also gives the domain's address pattern) and public search-result *titles* of professional profiles
("Prénom Nom - Poste - Entreprise | LinkedIn") through a self-hosted SearXNG (``settings.search_engine_url``),
else Brave Search (key) or DuckDuckGo HTML (≥ 25 s between queries, stopped for the run on a bot-flag).
Profile pages themselves are never fetched (LinkedIn ToS, CNIL
KASPR decision): only the title shown by the search engine is read.

People are ranked by ``settings.outreach.preferred_roles`` (HR/talent/recrutement > CTO/tech lead/directeur
technique > dirigeant, the latter only for small companies).
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from jobbot.config import OutreachSettings, Secrets
from jobbot.http import make_client, request_with_retry
from jobbot.models import Company, ContactEmail
from jobbot.outreach import domains as _domains
from jobbot.outreach._brave import BraveEngine
from jobbot.outreach._extract import page_links
from jobbot.outreach._mailcheck import hunter_contact, hunter_domain_data
from jobbot.outreach._patterns import is_valid_pattern
from jobbot.outreach._people_parse import (
    TEAM_PAGE,
    Person,
    display_name,
    people_from_html,
    people_from_titles,
    person_from_title,
    role_category,
    split_name,
)
from jobbot.outreach._searxng import SearxEngine, searxng_search
from jobbot.textutil import normalize

__all__ = [
    "PeopleSearch", "Person", "display_name", "find_people", "find_people_detailed", "is_small_company",
    "merge_people", "people_from_html", "people_from_pages", "people_from_titles", "person_from_title",
    "rank_people", "registry_people", "role_category", "role_rank",
]

log = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_ROLES = tuple(OutreachSettings().preferred_roles)
CATEGORY_ORDER = {"hr": 0, "tech": 1, "leader": 2, "other": 3}
SOURCE_ORDER = {"website": 0, "hunter": 1, "registry": 2, "search": 3}
# preferred role (normalised) → the wordings that express it in titles
ROLE_ALIASES = {
    "rh": ("rh", "drh", "rrh", "hrbp", "hr", "ressources humaines", "human resources", "people"),
    "recrutement": ("recrut", "recruit", "talent acquisition"),
    "talent": ("talent",),
    "cto": ("cto", "chief technology"),
    "directeur technique": ("directeur technique", "directrice technique", "responsable technique",
                            "head of engineering", "vp engineering", "engineering manager", "dsi"),
    "lead": ("lead",),
    "gerant": ("gerant", "gerante", "co-gerant"),
    "president": ("president", "presidente", "ceo", "pdg", "directeur general", "directrice generale", "fondat",
                  "founder", "dirigeant"),
}
SMALL_TRANCHES = frozenset({"NN", "00", "01", "02", "03", "11", "12"})  # < 50 employees (or unknown)
LARGE_CATEGORIES = frozenset({"ETI", "GE"})
MAX_TEAM_PAGES = 3
LINKEDIN_QUERY = 'site:linkedin.com/in "{name}"'
BRAVE_ROLE_TERMS = ' (RH OR recrutement OR talent OR CTO OR "directeur technique" OR fondateur OR gérant)'
_HUNTER_DEPARTMENTS = {"hr": "hr", "executive": "leader", "management": "leader"}


class PeopleSearch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    people: list[Person] = Field(default_factory=list)  # ranked, best first
    contacts: list[ContactEmail] = Field(default_factory=list)  # every address Hunter returned
    pattern: str | None = None  # the domain's address pattern according to Hunter
    invalid: list[str] = Field(default_factory=list)  # addresses Hunter knows to be invalid (never guess them)
    sources: list[str] = Field(default_factory=list)  # sources that were queried


# --------------------------------------------------------------------------- ranking


def is_small_company(company: Company) -> bool:
    """< 50 employees (or unknown headcount) and not an ETI / large company: the leader is a relevant contact."""
    if company.raw.get("categorie_entreprise") in LARGE_CATEGORIES:
        return False
    return str(company.raw.get("tranche_effectif_salarie") or "NN") in SMALL_TRANCHES


@functools.lru_cache(maxsize=256)
def _alias_re(alias: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(alias)}" + (r"\b" if len(alias) <= 3 else ""))


def role_rank(role: str, preferred: Sequence[str]) -> int:
    """Index of the first preferred role the title expresses (len(preferred) when none)."""
    text = normalize(role)
    for index, wanted in enumerate(preferred):
        key = normalize(wanted)
        if any(_alias_re(alias).search(text) for alias in ROLE_ALIASES.get(key, (key,)) if alias):
            return index
    return len(preferred)


def rank_people(
    people: Iterable[Person], preferred_roles: Sequence[str] | None = None, *, small_company: bool = True
) -> list[Person]:
    """Best contact first: preferred roles order, then category (hr > tech > leader > other), known address first.
    Leaders of larger companies go last (their inbox is not where applications land)."""
    preferred = list(preferred_roles or DEFAULT_ROLES)

    def key(person: Person) -> tuple:
        rank = role_rank(person.role, preferred)
        if person.category == "other":
            rank = max(rank, len(preferred))
        if person.category == "leader" and not small_company:
            rank = len(preferred) + 1
        return rank, CATEGORY_ORDER[person.category], person.email is None, -person.confidence, person.name

    return sorted(people, key=key)


def _person_key(person: Person) -> str:
    return " ".join(sorted(normalize(person.name).replace("-", " ").split()))


def _merge_two(a: Person, b: Person) -> Person:
    best, other = sorted((a, b), key=lambda p: (CATEGORY_ORDER[p.category], SOURCE_ORDER[p.source]))
    named = next((p for p in (a, b) if p.source == "registry" and p.first and p.last), best)
    mail = next((p for p in (best, other) if p.email and p.verified), None) or \
        next((p for p in (best, other) if p.email), None)
    return best.model_copy(update={
        "first": named.first or best.first, "last": named.last or best.last,
        "email": mail.email if mail else None, "verified": bool(mail and mail.verified),
        "confidence": max(a.confidence, b.confidence), "source_url": best.source_url or other.source_url,
    })


def merge_people(people: Iterable[Person]) -> list[Person]:
    """One entry per person (same name in any order/case), keeping the most useful role and address."""
    merged: dict[str, Person] = {}
    for person in people:
        key = _person_key(person)
        merged[key] = person if key not in merged else _merge_two(merged[key], person)
    return list(merged.values())


# --------------------------------------------------------------------------- registry


def _fetch_registry_leaders(siren: str, client: httpx.Client) -> list[dict[str, str]]:
    from jobbot.outreach._registry import person_leaders
    from jobbot.outreach.companies import _THROTTLE, API_BASE, API_UA

    resp = request_with_retry(client, "GET", f"{API_BASE}/search", params={"q": siren, "per_page": 1},
                              headers={"User-Agent": API_UA}, throttle=_THROTTLE, retries=2, backoff_s=1.0)
    if resp.status_code != 200:
        log.info("Dirigeants de %s indisponibles (HTTP %s)", siren, resp.status_code)
        return []
    results = resp.json().get("results") or []
    item = next((r for r in results if isinstance(r, dict) and str(r.get("siren")) == siren), None)
    return person_leaders(item) if item else []


def _leader_details(company: Company, client: httpx.Client) -> list[dict]:
    detail = company.raw.get("leaders_detail")
    if isinstance(detail, list):  # fetched at discovery ([] = no natural-person leader)
        return [d for d in detail if isinstance(d, dict)]
    if company.siren and re.fullmatch(r"\d{9}", company.siren):
        fetched = _fetch_registry_leaders(company.siren, client)
        if fetched:
            return fetched
    return [dict(zip(("first", "last"), split_name(name), strict=True), role="") for name in company.leaders]


def registry_people(company: Company, client: httpx.Client | None = None) -> list[Person]:
    """Natural-person leaders from the company registry (statutory auditors excluded)."""
    own = client is None
    http = client or make_client()
    try:
        details = _leader_details(company, http)
    finally:
        if own:
            http.close()
    out = []
    for d in details:
        role = str(d.get("role") or "")
        first, last = str(d.get("first") or "").strip(), str(d.get("last") or "").strip()
        if "commissaire" in role.lower() or not first or not last:
            continue
        category = role_category(role)
        out.append(Person(
            name=f"{first} {last}", first=first, last=last, role=role or "Dirigeant", source="registry",
            category=category if category in ("hr", "tech", "leader") else "leader", confidence=0.9,
        ))
    return out


# --------------------------------------------------------------------------- website


def _same_host(url: str, base: str) -> bool:
    a, b = (urlparse(u).hostname or "" for u in (url, base))
    return bool(a) and a.removeprefix("www.") == b.removeprefix("www.")


def _team_pages(client: httpx.Client, domain: str) -> tuple[list[tuple[str, str]], set[str]]:
    """Home page + up to 3 team/about pages linked from it, and the set of team page URLs."""
    home = _domains.fetch_home(client, domain)
    if home is None:
        return [], set()
    links = [u.split("#", 1)[0] for u, text in page_links(home[1], home[0])
             if _same_host(u, home[0]) and TEAM_PAGE.search(f"{urlparse(u).path} {text}")]
    pages, team = [home], set()
    for url in list(dict.fromkeys(links))[:MAX_TEAM_PAGES]:
        page = _domains.fetch_html(client, url)
        if page is not None:
            pages.append(page)
            team.add(page[0])
    return pages, team


def people_from_pages(
    pages: Iterable[tuple[str, str]], site_domain: str, team_urls: Iterable[str] = ()
) -> list[Person]:
    """People found on company pages; ``team_urls`` are parsed as team pages whatever their path."""
    team = set(team_urls)
    out: list[Person] = []
    for url, html in pages:
        out.extend(people_from_html(html, url, site_domain, team_page=True if url in team else None))
    return out


# --------------------------------------------------------------------------- Hunter


def _hunter_person(item: dict) -> Person | None:
    first, last = str(item.get("first_name") or "").strip(), str(item.get("last_name") or "").strip()
    if not first or not last:
        return None
    position = str(item.get("position") or "")
    department = str(item.get("department") or "").lower()
    category = role_category(position) if position else None
    if category in (None, "other"):
        category = _HUNTER_DEPARTMENTS.get(department, category or "other")
    status = str((item.get("verification") or {}).get("status") or "")
    email = str(item.get("value") or "").strip().lower() or None
    return Person(
        name=f"{first} {last}", first=first, last=last, role=position or department, category=category,
        source="hunter", email=None if status == "invalid" else email, verified=status == "valid", confidence=0.8,
    )


def _hunter(domain: str, key: str, client: httpx.Client) -> PeopleSearch:
    data = hunter_domain_data(domain, key, client=client)
    items = [i for i in data.get("emails") or [] if isinstance(i, dict)]
    invalid = [str(i.get("value") or "").strip().lower() for i in items
               if str((i.get("verification") or {}).get("status") or "") == "invalid"]
    pattern = data.get("pattern")
    return PeopleSearch(
        people=[p for p in (_hunter_person(i) for i in items) if p is not None],
        contacts=[c for c in (hunter_contact(i) for i in items) if c is not None],
        pattern=pattern if is_valid_pattern(pattern) else None, invalid=[e for e in invalid if e],
    )


# --------------------------------------------------------------------------- search engines


def _brave_titles(client: httpx.Client, query: str, brave: BraveEngine) -> list[str]:
    resp = request_with_retry(
        client, "GET", _domains.BRAVE_URL, retries=0,  # a retry would be one more (uncounted) paid query
        params={"q": query, "country": "FR", "search_lang": "fr", "count": 20},
        headers={"X-Subscription-Token": brave.api_key, "Accept": "application/json"},
    )
    if resp.status_code != 200:
        log.warning("Brave Search (personnes) : HTTP %s", resp.status_code)
        return []
    results = (resp.json().get("web") or {}).get("results") or []
    return [str(r.get("title") or "") for r in results if isinstance(r, dict)]


def _ddg_titles(client: httpx.Client, query: str) -> list[str]:
    """Result titles from DuckDuckGo HTML; shares the domain finder's breaker (≥ 25 s apart, stop on bot-flag)."""
    from bs4 import BeautifulSoup

    breaker = _domains._DDG
    if not breaker.allowed():
        return []
    breaker.wait_turn()
    resp = client.get(_domains.DDG_URL, params={"q": query, "kl": "fr-fr"})
    if resp.status_code != 200 or "anomaly" in resp.text[:20000]:
        log.warning("DuckDuckGo bloque les requêtes (HTTP %s) : recherche de personnes suspendue", resp.status_code)
        breaker.trip()
        return []
    return [a.get_text(" ", strip=True) for a in BeautifulSoup(resp.text, "lxml").select("a.result__a")]


def _search_titles(client: httpx.Client, company: Company, brave: BraveEngine | None,
                   search_url: SearxEngine | str | None = None) -> list[str]:
    """Result titles: SearXNG first (self-hosted, ``settings.search_engine_url``), else Brave (key, within its
    monthly budget) / DuckDuckGo."""
    names = _domains.company_names(company)
    if not names:
        return []
    query = LINKEDIN_QUERY.format(name=names[0].replace('"', " ").strip())
    titles = [title for _, title in searxng_search(client, search_url, query) if title]
    if titles:
        return titles
    if brave is not None and brave.take():
        return _brave_titles(client, query + BRAVE_ROLE_TERMS, brave)
    return _ddg_titles(client, query)


# --------------------------------------------------------------------------- public API


def _safe(label: str, func: Callable[[], T], default: T) -> T:
    try:
        return func()
    except Exception as exc:  # one failing source never stops the others
        log.warning("Recherche de personnes via %s impossible : %s", label, exc)
        return default


def find_people_detailed(
    company: Company,
    *,
    client: httpx.Client | None = None,
    secrets: Secrets | None = None,
    domain: str | None = None,
    pages: list[tuple[str, str]] | None = None,
    preferred_roles: Sequence[str] | None = None,
    use_hunter: bool = True,
    use_search: bool = True,
    search_url: SearxEngine | str | None = None,
    brave: BraveEngine | None = None,
) -> PeopleSearch:
    """Every source at once; ``pages`` (already crawled company pages) avoids fetching the site again;
    ``search_url``: SearXNG base URL tried before Brave (``_brave.brave_for(ctx)``) / DuckDuckGo."""
    own = client is None
    http = client or make_client()
    site = domain or _domains.normalize_domain(company.domain or company.website)
    people: list[Person] = []
    hunter = PeopleSearch()
    used = ["registry"]
    try:
        people += _safe("registre", lambda: registry_people(company, http), [])
        if site:
            used.append("website")
            site_pages, team = (pages, set()) if pages is not None else _safe(
                "site", lambda: _team_pages(http, site), ([], set()))
            people += people_from_pages(site_pages, site, team)
        key = secrets.hunter_api_key if secrets is not None else None
        if use_hunter and key and site:
            used.append("hunter")
            hunter = _safe("Hunter", lambda: _hunter(site, key, http), hunter)
            people += hunter.people
        if use_search:
            used.append("search")
            titles = _safe("moteur de recherche", lambda: _search_titles(http, company, brave, search_url), [])
            people += people_from_titles(titles, _domains.company_names(company))
    finally:
        if own:
            http.close()
    ranked = rank_people(merge_people(people), preferred_roles, small_company=is_small_company(company))
    return hunter.model_copy(update={"people": ranked, "sources": used})


def find_people(company: Company, *, client: httpx.Client | None = None, secrets: Secrets | None = None,
                **options) -> list[Person]:
    """People to write to at ``company`` (name, role, source, address when known), best contact first.
    ``options`` are those of :func:`find_people_detailed` (domain, pages, preferred_roles, use_hunter, use_search)."""
    return find_people_detailed(company, client=client, secrets=secrets, **options).people
