"""Website domain discovery + ownership verification for a company (address geocoding: ``_geocode``).

Candidates, in order: known website → Clearbit autocomplete → slug guessing (+ DNS) → Wikidata by SIREN →
SearXNG (``settings.search_engine_url``, self-hosted) → Brave Search (if key) → DuckDuckGo HTML (last resort,
≥ 25 s between queries, circuit breaker on bot-flag).
A candidate is accepted only when its homepage or legal notice shows the SIREN (or TVA FRxx+SIREN);
otherwise a strong name match (+ postcode) is kept with a lower confidence.
"""

from __future__ import annotations

import html as htmlmod
import logging
import random
import re
import socket
import threading
import time
import unicodedata
from collections.abc import Callable
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from pydantic import BaseModel, ConfigDict
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from jobbot.config import Profile
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import Company
from jobbot.outreach._brave import BraveEngine
from jobbot.outreach._geocode import GEOCODE_URL, geocode_address  # noqa: F401
from jobbot.outreach._searxng import SearxEngine, searxng_search

log = logging.getLogger(__name__)

CLEARBIT_URL = "https://autocomplete.clearbit.com/v1/companies/suggest"
WIKIDATA_URL = "https://query.wikidata.org/sparql"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
DDG_URL = "https://html.duckduckgo.com/html/"
# Wikidata rejects generic user agents (HTTP 403): the contact (URL or e-mail) comes from the profile at runtime.
_BOT_PURPOSE = "jobbot/0.1 (recherche d'emploi personnelle"
BOT_UA = f"{_BOT_PURPOSE})"

SLUG_TLDS = ("fr", "lu", "com", "eu", "io", "tech", "dev", "net")
MAX_VERIFICATIONS = 8
MIN_NAME_CONFIDENCE = 0.75
MAX_PAGE_BYTES = 2_000_000
DDG_MIN_INTERVAL_S = 25.0
DDG_BLOCK_S = 3600.0
DDG_MAX_PER_RUN = 10
LEGAL_PATHS = ("/mentions-legales", "/mentions-legales/", "/fr/mentions-legales")
PARKING_WORDS = ("domaine à vendre", "domain is for sale", "this domain may be for sale", "sedo.com",
                 "afternic", "dan.com", "parkingcrew", "domaine en vente")
DIRECTORY_HOSTS = frozenset({
    "societe.com", "pappers.fr", "annuaire-entreprises.data.gouv.fr", "infogreffe.fr", "verif.com", "manageo.fr",
    "societeinfo.com", "datalegal.fr", "dnb.com", "kompass.com", "pagesjaunes.fr", "cylex-locale.fr", "cylex.fr",
    "agenceliste.com", "agences.io", "sortlist.fr", "europages.fr", "bilansgratuits.fr", "lefigaro.fr",
    "corporama.com", "score3.fr", "lagazettefrance.fr", "bodacc.fr", "journal-officiel.gouv.fr", "linkedin.com",
    "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com", "indeed.com", "welcometothejungle.com",
    "hellowork.com", "glassdoor.fr", "glassdoor.com", "jobteaser.com", "francetravail.fr", "apec.fr",
    "trustpilot.com", "google.com", "wikipedia.org", "duckduckgo.com", "data.gouv.fr", "gouv.fr",
})
_LEGAL_FORMS = re.compile(r"\b(sas|sasu|sarl|eurl|sa|sci|scop|selarl|groupe|group|france|holding)\b")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_SIREN_STRIP = re.compile(r"[\s\u00a0\u202f.\-]")  # spaces, (narrow) no-break spaces, dots, hyphens
_TAG_RE = re.compile(r"<[^>]+>")
_LEGAL_HREF = re.compile(r'href="([^"]*(?:mention|legal|cgu|cgv)[^"]*)"', re.I)

_SITE_THROTTLE = HostThrottle(1.0)


def _sleep(seconds: float) -> None:  # indirection so tests can disable waiting
    time.sleep(seconds)


def profile_contact(profile: Profile) -> str | None:
    """Public contact of the candidate for the bot user agent: a profile link first, else the e-mail."""
    identity = profile.identity
    return next((v for v in (identity.github, identity.portfolio, identity.linkedin, identity.email) if v), None)


def bot_user_agent(contact: str | None = None) -> str:
    """``BOT_UA`` with the contact appended when known (required by Wikidata's robot policy)."""
    clean = (contact or "").strip()
    return f"{_BOT_PURPOSE}; {clean})" if clean else BOT_UA


class DomainMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: str
    confidence: float  # 1.0 = SIREN found on the site
    method: str  # website | clearbit | slug | wikidata | brave | duckduckgo
    verified_by: str  # siren | name


# --------------------------------------------------------------------------- helpers


def normalize_domain(value: str | None) -> str | None:
    """'https://www.Acme.fr/contact' → 'acme.fr' (None when not a plausible host)."""
    if not value:
        return None
    text = value.strip().lower()
    host = urlparse(text if "//" in text else f"//{text}").hostname or ""
    host = host.removeprefix("www.").strip(".")
    return host if _DOMAIN_RE.match(host) else None


def is_directory_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS)


def company_names(company: Company) -> list[str]:
    names = [company.name, *[str(n) for n in company.raw.get("names") or []]]
    seen: dict[str, str] = {}
    for n in names:
        if n and n.strip():
            seen.setdefault(n.strip().upper(), n.strip())
    return list(seen.values())


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def slug_candidates(names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names[:3]:
        plain = _LEGAL_FORMS.sub(" ", _ascii(name))
        base = re.sub(r"[^a-z0-9]+", "", plain)
        dashed = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
        if len(base) < 3:
            continue
        for stem in dict.fromkeys([base, dashed]):
            out.extend(f"{stem}.{tld}" for tld in SLUG_TLDS)
    return list(dict.fromkeys(out))


def _resolves(domain: str) -> bool:
    try:
        socket.getaddrinfo(domain, 443)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


def page_has_siren(html: str, siren: str) -> bool:
    """True when the SIREN (alone, in a SIRET or in a TVA number) appears in the page text."""
    if not siren or not re.fullmatch(r"\d{9}", siren):
        return False
    return siren in _SIREN_STRIP.sub("", htmlmod.unescape(_TAG_RE.sub(" ", html)))


def fetch_html(client: httpx.Client, url: str) -> tuple[str, str] | None:
    """(final url, html) for a 200 HTML page, else None. Never raises for network errors."""
    try:
        resp = request_with_retry(client, "GET", url, retries=1, backoff_s=1.0, throttle=_SITE_THROTTLE)
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("GET %s impossible : %s", url, exc)
        return None
    ctype = resp.headers.get("content-type", "text/html")
    if resp.status_code != 200 or "html" not in ctype or len(resp.content) > MAX_PAGE_BYTES:
        return None
    return str(resp.url), resp.text


# --------------------------------------------------------------------------- verification


def fetch_home(client: httpx.Client, domain: str) -> tuple[str, str] | None:
    home = fetch_html(client, f"https://{domain}/")
    if home is None and not domain.startswith("www."):
        home = fetch_html(client, f"https://www.{domain}/")
    return home


def _legal_pages(client: httpx.Client, home_url: str, home_html: str) -> list[str]:
    links = list(dict.fromkeys(_LEGAL_HREF.findall(home_html)))[:2] or list(LEGAL_PATHS)
    home_host = urlparse(home_url).hostname
    pages: list[str] = []
    for link in links:
        url = urljoin(home_url, htmlmod.unescape(link))
        if urlparse(url).hostname != home_host:
            continue
        page = fetch_html(client, url)
        if page is not None:
            pages.append(page[1])
    return pages


def _name_confidence(pages: list[str], names: list[str], postcode: str | None) -> float:
    text = " ".join(_TAG_RE.sub(" ", p) for p in pages)[:200_000]
    lowered = text.lower()
    if any(word in lowered for word in PARKING_WORDS):
        return 0.0
    name_score = max(
        (fuzz.partial_ratio(n, text, processor=default_process) for n in names if len(n) >= 3), default=0.0
    ) / 100
    bonus = 0.2 if postcode and postcode in text else 0.0
    return min(0.9, 0.7 * name_score + bonus)


def verify_domain(
    client: httpx.Client, domain: str, siren: str | None, names: list[str], postcode: str | None
) -> tuple[float, str]:
    """(confidence 0..1, final host). 1.0 means the SIREN was found on the site."""
    home = fetch_home(client, domain)
    if home is None:
        return 0.0, domain
    final_host = normalize_domain(home[0]) or domain
    if is_directory_host(final_host):
        return 0.0, final_host
    if siren and page_has_siren(home[1], siren):
        return 1.0, final_host
    pages = [home[1], *_legal_pages(client, home[0], home[1])]
    if siren and any(page_has_siren(p, siren) for p in pages[1:]):
        return 1.0, final_host
    return _name_confidence(pages, names, postcode), final_host


# --------------------------------------------------------------------------- candidate providers


def _clearbit(client: httpx.Client, names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names[:3]:
        resp = request_with_retry(client, "GET", CLEARBIT_URL, params={"query": name}, retries=1)
        if resp.status_code != 200:
            continue
        items = resp.json()
        for item in items if isinstance(items, list) else []:
            domain = normalize_domain(str(item.get("domain") or ""))
            score = fuzz.token_set_ratio(name, str(item.get("name") or ""), processor=default_process)
            if domain and score >= 80 and not is_directory_host(domain):
                out.append(domain)
    return list(dict.fromkeys(out))


def _slugs(client: httpx.Client, names: list[str]) -> list[str]:
    return [d for d in slug_candidates(names) if _resolves(d)][:4]


def _wikidata(client: httpx.Client, siren: str | None, user_agent: str = BOT_UA) -> list[str]:
    if not siren or not re.fullmatch(r"\d{9}", siren):
        return []
    query = f'SELECT ?site WHERE {{ ?i wdt:P1616 "{siren}" . ?i wdt:P856 ?site }} LIMIT 3'
    resp = request_with_retry(
        client, "GET", WIKIDATA_URL, params={"query": query}, retries=1,
        headers={"Accept": "application/sparql-results+json", "User-Agent": user_agent},
    )
    if resp.status_code != 200:
        return []
    bindings = resp.json().get("results", {}).get("bindings", [])
    hosts = (normalize_domain(b.get("site", {}).get("value")) for b in bindings)
    return [h for h in hosts if h and not is_directory_host(h)]


def _hosts_from_urls(urls: list[str]) -> list[str]:
    hosts = (normalize_domain(u) for u in urls)
    return list(dict.fromkeys(h for h in hosts if h and not is_directory_host(h)))[:3]


def _brave(client: httpx.Client, query: str, brave: BraveEngine) -> list[str]:
    if not brave.take():  # monthly budget spent: the free providers carry on
        return []
    resp = request_with_retry(
        client, "GET", BRAVE_URL, retries=0,
        params={"q": query, "country": "FR", "search_lang": "fr", "count": 10},
        headers={"X-Subscription-Token": brave.api_key, "Accept": "application/json"},
    )
    if resp.status_code != 200:
        log.warning("Brave Search : HTTP %s", resp.status_code)
        return []
    results = (resp.json().get("web") or {}).get("results") or []
    return _hosts_from_urls([str(r.get("url") or "") for r in results])


class _DdgBreaker:
    """Rate limit + circuit breaker for DuckDuckGo HTML (bot-flags an IP after a small burst)."""

    def __init__(self) -> None:
        self.last = 0.0
        self.blocked_until = 0.0
        self.count = 0
        self._lock = threading.Lock()  # parallel enrichment workers (« jobbot night ») queue here

    def allowed(self) -> bool:
        return time.monotonic() >= self.blocked_until and self.count < DDG_MAX_PER_RUN

    def wait_turn(self) -> None:
        with self._lock:
            delay = self.last + DDG_MIN_INTERVAL_S + random.uniform(0, 5) - time.monotonic()
            if self.last and delay > 0:
                _sleep(delay)
            self.last = time.monotonic()
            self.count += 1

    def trip(self) -> None:
        self.blocked_until = time.monotonic() + DDG_BLOCK_S


_DDG = _DdgBreaker()


def _ddg(client: httpx.Client, query: str) -> list[str]:
    if not _DDG.allowed():
        return []
    _DDG.wait_turn()
    resp = client.get(DDG_URL, params={"q": query, "kl": "fr-fr"})
    if resp.status_code == 202 or "anomaly" in resp.text[:20000] or resp.status_code != 200:
        log.warning("DuckDuckGo bloque les requêtes (HTTP %s) : recherche web suspendue", resp.status_code)
        _DDG.trip()
        return []
    targets = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', resp.text):
        target = parse_qs(urlparse(htmlmod.unescape(href)).query).get("uddg", [""])[0]
        targets.append(target or htmlmod.unescape(href))
    return _hosts_from_urls(targets)


def _providers(
    company: Company, names: list[str], brave: BraveEngine | None, contact: str | None,
    search_url: SearxEngine | str | None = None,
) -> list[tuple[str, bool, Callable]]:
    """(method, costly, fetch(client) -> domains). Costly ones are skipped once a name match exists."""
    query = f"{names[0]} {company.city}".strip()
    known = [d for d in (normalize_domain(company.domain), normalize_domain(company.website)) if d]
    user_agent = bot_user_agent(contact)
    providers: list[tuple[str, bool, Callable]] = [
        ("website", False, lambda c: known),
        ("clearbit", False, lambda c: _clearbit(c, names)),
        ("slug", False, lambda c: _slugs(c, names)),
        ("wikidata", False, lambda c: _wikidata(c, company.siren, user_agent)),
    ]
    if search_url:
        providers.append(("searxng", True,
                          lambda c: _hosts_from_urls([u for u, _ in searxng_search(c, search_url, query)])))
    if brave is not None:
        providers.append(("brave", True, lambda c: _brave(c, query, brave)))
    providers.append(("duckduckgo", True, lambda c: _ddg(c, query)))
    return providers


# --------------------------------------------------------------------------- public API


def find_domain_verified(
    company: Company, *, client: httpx.Client | None = None, brave: BraveEngine | None = None,
    contact: str | None = None, search_url: SearxEngine | str | None = None,
) -> DomainMatch | None:
    """Best verified domain with its confidence, or None. ``contact``: see :func:`profile_contact`;
    ``search_url``: SearXNG (``_searxng.engine_for(ctx)``, or a bare base URL), tried before Brave
    (``_brave.brave_for(ctx)``, monthly budget) / DuckDuckGo."""
    names = company_names(company)
    own = client is None
    http = client or make_client()
    tried: set[str] = set()
    best: DomainMatch | None = None
    try:
        for method, costly, fetch in _providers(company, names, brave, contact, search_url):
            if costly and best is not None:
                break
            try:
                candidates = fetch(http)
            except (httpx.HTTPError, ValueError) as exc:
                log.info("Recherche de domaine via %s échouée pour %s : %s", method, company.name, exc)
                continue
            for domain in candidates:
                if domain in tried or len(tried) >= MAX_VERIFICATIONS:
                    continue
                tried.add(domain)
                score, host = verify_domain(http, domain, company.siren, names, company.postal_code)
                if score >= 1.0:
                    return DomainMatch(domain=host, confidence=1.0, method=method, verified_by="siren")
                if score >= MIN_NAME_CONFIDENCE and (best is None or score > best.confidence):
                    best = DomainMatch(domain=host, confidence=round(score, 2), method=method, verified_by="name")
        return best
    finally:
        if own:
            http.close()


def find_domain(
    company: Company, *, client: httpx.Client | None = None, brave: BraveEngine | None = None,
    contact: str | None = None, search_url: SearxEngine | str | None = None,
) -> str | None:
    """Verified website domain of ``company`` (SIREN on the site, else strong name match), or None."""
    match = find_domain_verified(company, client=client, brave=brave, contact=contact, search_url=search_url)
    return match.domain if match else None
