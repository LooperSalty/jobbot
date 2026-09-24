"""Contact e-mail discovery for spontaneous applications.

Per company: verified website domain → polite crawl (≤ 8 pages, ≤ 1 req/s per host, robots.txt honoured)
→ addresses (Cloudflare-protected, obfuscated, JSON-LD) → role ranking → named people (registry, team pages,
Hunter, public search titles; :mod:`jobbot.outreach._nominative`) → the domain's address pattern is learned →
best-effort nominative guesses for HR / tech managers / leaders of small companies (Hunter-verified when
possible, else kept unverified only when ``settings.outreach.allow_unverified_guesses``) → a short summary of
the website becomes ``Company.description`` (personalisation).

Legal stance (CNIL / GDPR): generic role addresses first; the source page of every address is recorded so
the e-mail can say where it was found; guesses are sent only under the campaign's caps (see ``selection``).
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel, ConfigDict, Field

from jobbot import clock
from jobbot.config import OutreachSettings
from jobbot.context import AppContext
from jobbot.http import make_client, request_with_retry
from jobbot.models import Company, ContactEmail
from jobbot.outreach import _nominative, _site
from jobbot.outreach import domains as _domains
from jobbot.outreach._extract import (
    WEBMAIL_DOMAINS,
    ats_links,
    careers_link,
    classify_email,
    decode_cfemail,
    default_confidence,
    deobfuscate,
    has_contact_form,
    page_links,
    prioritized_links,
    scrape_emails,
)
from jobbot.outreach._mailcheck import guess_patterns, has_mx, hunter_domain_search
from jobbot.outreach.selection import usable

__all__ = [
    "EnrichReport", "SiteCrawl", "crawl_site", "crawl_site_emails", "decode_cfemail", "deobfuscate",
    "enrich_companies", "enrich_company", "guess_patterns", "has_mx", "hunter_domain_search", "is_sendable",
    "rank_contacts", "scrape_emails",
]

log = logging.getLogger(__name__)

MAX_CRAWL_PAGES = 8
FALLBACK_PATHS = (
    "/contact", "/nous-contacter", "/contactez-nous", "/mentions-legales", "/recrutement", "/carrieres",
    "/nous-rejoindre", "/jobs", "/equipe", "/a-propos",
)
KIND_ORDER = {"recruitment": 0, "hr": 1, "tech": 2, "leader": 3, "generic": 4, "named": 5}
MAX_ENRICH_ATTEMPTS = 3
_KEEP_STATUS = frozenset({"contacted", "pending_review", "approved", "blacklisted"})


class SiteCrawl(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: str
    contacts: list[ContactEmail] = Field(default_factory=list)
    sources: dict[str, str] = Field(default_factory=dict)  # email -> page where it was found
    careers_url: str | None = None
    contact_form_url: str | None = None
    pages: int = 0
    meta_description: str | None = None  # home page <meta name="description">
    site_text: str = ""  # visible text of the home + about pages (summary input)
    page_html: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)  # url -> html (people)


class EnrichReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    processed: int
    with_email: int
    without_email: int


# --------------------------------------------------------------------------- ranking


def is_sendable(contact: ContactEmail, *, allow_unverified: bool = False) -> bool:
    """Guessed (pattern) addresses are sendable once verified, or when unverified guesses are allowed."""
    return contact.source != "pattern" or contact.verified or allow_unverified


def _rank_key(c: ContactEmail) -> tuple:
    return (0 if is_sendable(c) else 1, KIND_ORDER.get(c.kind, 5), -c.confidence, c.email)


def _merge_contact(a: ContactEmail, b: ContactEmail) -> ContactEmail:
    keep = min(a, b, key=_rank_key)
    source = next((x.source for x in (keep, a, b) if x.source != "pattern"), "pattern")
    return keep.model_copy(update={
        "verified": a.verified or b.verified,
        "person_name": keep.person_name or a.person_name or b.person_name,
        "confidence": max(a.confidence, b.confidence),
        "source": source,
    })


def rank_contacts(contacts: list[ContactEmail]) -> list[ContactEmail]:
    """Drop unusable addresses (noreply, webmaster, rgpd/dpo, compta, support, host/agency, placeholders),
    dedupe, and order: recruitment > named HR > leader > contact/info > other people."""
    best: dict[str, ContactEmail] = {}
    for contact in contacts:
        email = contact.email.strip().lower()
        if classify_email(email) is None:
            continue
        current = contact if contact.email == email else contact.model_copy(update={"email": email})
        previous = best.get(email)
        best[email] = current if previous is None else _merge_contact(previous, current)
    return sorted(best.values(), key=_rank_key)


# --------------------------------------------------------------------------- crawling


def _robots(client: httpx.Client, home_url: str) -> RobotFileParser | None:
    try:
        resp = request_with_retry(client, "GET", urljoin(home_url, "/robots.txt"), retries=0,
                                  throttle=_domains._SITE_THROTTLE)
    except (httpx.HTTPError, ValueError):
        return None
    if resp.status_code != 200:
        return None
    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    return parser


def _crawl_queue(links: list[tuple[str, str]], home_url: str) -> list[str]:
    queue = prioritized_links(links, home_url)
    seen = {urlparse(u).path.rstrip("/").lower() for u in queue}
    seen.add(urlparse(home_url).path.rstrip("/").lower())
    for path in FALLBACK_PATHS:
        if path.rstrip("/") not in seen:
            queue.append(urljoin(home_url, path))
            seen.add(path.rstrip("/"))
    return queue


def _analyse(domain: str, links: list[tuple[str, str]], pages: list[tuple[str, str]]) -> SiteCrawl:
    home_url = pages[0][0]
    site_domains = {domain, _domains.normalize_domain(home_url) or domain}
    sources: dict[str, str] = {}
    ats: list[str] = []
    form_url: str | None = None
    for url, html in pages:
        for site in site_domains:
            for email in scrape_emails(html, site):
                sources.setdefault(email, url)
        ats.extend(ats_links(html))
        if form_url is None and has_contact_form(html):
            form_url = url
    contacts = [
        ContactEmail(email=email, confidence=default_confidence(kind), source="scraped", kind=kind)
        for email in sources
        if (kind := classify_email(email)) is not None
    ]
    return SiteCrawl(
        domain=domain, contacts=rank_contacts(contacts), sources=sources, pages=len(pages),
        careers_url=ats[0] if ats else careers_link(links, home_url), contact_form_url=form_url,
        meta_description=_site.meta_description(pages[0][1]), site_text=_site.site_text(pages),
        page_html=dict(pages),
    )


def crawl_site(domain: str, *, client: httpx.Client | None = None, max_pages: int = MAX_CRAWL_PAGES) -> SiteCrawl:
    """Crawl home + careers/contact/legal/team pages of ``domain`` and extract what matters."""
    own = client is None
    http = client or make_client()
    try:
        home = _domains.fetch_home(http, domain)
        if home is None:
            log.info("Site %s injoignable", domain)
            return SiteCrawl(domain=domain)
        robots = _robots(http, home[0])
        links = page_links(home[1], home[0])
        pages, attempts = [home], 1
        for url in _crawl_queue(links, home[0]):
            if attempts >= max_pages:
                break
            if robots is not None and not robots.can_fetch("*", url):
                continue
            attempts += 1
            page = _domains.fetch_html(http, url)
            if page is not None:
                pages.append(page)
        return _analyse(domain, links, pages)
    finally:
        if own:
            http.close()


def crawl_site_emails(domain: str, *, client: httpx.Client | None = None, max_pages: int = 8) -> list[ContactEmail]:
    """Ranked addresses published on the company website."""
    return crawl_site(domain, client=client, max_pages=max_pages).contacts


# --------------------------------------------------------------------------- enrichment helpers


def _deliverable(contacts: list[ContactEmail]) -> list[ContactEmail]:
    cache: dict[str, bool] = {}
    out = []
    for c in contacts:
        domain = c.email.split("@", 1)[1]
        if domain not in WEBMAIL_DOMAINS and domain not in cache:
            cache[domain] = has_mx(domain)
        if domain in WEBMAIL_DOMAINS or cache[domain]:
            out.append(c)
    return out


def _scaled(contacts: list[ContactEmail], factor: float) -> list[ContactEmail]:
    if factor >= 1.0:
        return contacts
    return [c.model_copy(update={"confidence": round(c.confidence * factor, 2)}) for c in contacts]


def _known_domain(company: Company) -> _domains.DomainMatch | None:
    raw = company.raw
    if company.domain and raw.get("domain_verified_by"):
        return _domains.DomainMatch(
            domain=company.domain, confidence=float(raw.get("domain_confidence", 1.0)),
            method=str(raw.get("domain_method", "website")), verified_by=str(raw["domain_verified_by"]),
        )
    return None


def _finish(company: Company, contacts: list[ContactEmail], raw_update: dict, cfg: OutreachSettings) -> Company:
    status = company.status
    if status not in _KEEP_STATUS:
        status = "enriched" if any(usable(c, cfg) for c in contacts) else "no_email"
    raw = {**company.raw, **raw_update, "enriched_at": clock.iso(clock.now())}
    return company.model_copy(update={"contacts": contacts, "status": status, "raw": raw})


def _with_description(ctx: AppContext, company: Company, crawl: SiteCrawl) -> Company:
    """Summarise the website into ``description`` for companies that can be written to (LLM fast, meta fallback)."""
    if company.description or not any(usable(c, ctx.settings.outreach) for c in company.contacts):
        return company
    summary = _site.summarize_site(ctx.llm, company.name, crawl.site_text, crawl.meta_description)
    return company.model_copy(update={"description": summary}) if summary else company


# --------------------------------------------------------------------------- enrichment


def enrich_company(ctx: AppContext, company: Company) -> Company:
    """Find the website, careers page, named people and best contact addresses of ``company`` (new copy)."""
    cfg = ctx.settings.outreach
    if ctx.repo.is_blacklisted(company.name, company.siren or "", company.domain or ""):
        return company.model_copy(update={"status": "blacklisted"})
    with make_client() as client:
        match = _known_domain(company) or _domains.find_domain_verified(
            company, client=client, secrets=ctx.secrets, contact=_domains.profile_contact(ctx.profile),
            search_url=ctx.settings.search_engine_url)
        if match is None:
            return _finish(company, rank_contacts(list(company.contacts)), {"enrich_note": "site web introuvable"}, cfg)
        crawl = crawl_site(match.domain, client=client)
        found = _nominative.nominative_contacts(
            ctx, company, match.domain, _scaled(crawl.contacts, match.confidence), list(crawl.page_html.items()),
            client,
        )
        contacts = _deliverable(found.contacts)
    updated = company.model_copy(update={
        "domain": match.domain,
        "website": company.website or f"https://{match.domain}",
        "careers_url": crawl.careers_url or company.careers_url,
    })
    sources = {**(company.raw.get("email_sources") or {}), **crawl.sources, **found.sources}
    enriched = _finish(updated, rank_contacts([*company.contacts, *contacts]), {
        "domain_confidence": match.confidence, "domain_method": match.method,
        "domain_verified_by": match.verified_by, "contact_form_url": crawl.contact_form_url,
        "email_sources": sources, "crawl_pages": crawl.pages, **found.raw_update(),
    }, cfg)
    return _with_description(ctx, enriched, crawl)


def _failed_attempt(company: Company, exc: Exception) -> Company:
    attempts = int(company.raw.get("enrich_attempts", 0)) + 1
    status = "no_email" if attempts >= MAX_ENRICH_ATTEMPTS else company.status
    raw = {**company.raw, "enrich_attempts": attempts, "enrich_error": str(exc)[:300]}
    return company.model_copy(update={"status": status, "raw": raw})


def enrich_companies(ctx: AppContext, *, limit: int = 50) -> EnrichReport:
    """Enrich up to ``limit`` companies in status ``new`` (best-scored first). One failure never stops the batch."""
    pending = sorted(ctx.repo.list_companies(status="new"), key=lambda c: -int(c.raw.get("outreach_score", 0)))
    batch = pending[: max(0, limit)]
    with_email = 0
    for company in batch:
        try:
            updated = enrich_company(ctx, company)
        except Exception as exc:
            log.exception("Enrichissement de %s impossible", company.name)
            updated = _failed_attempt(company, exc)
        try:
            ctx.repo.upsert_company(updated)
        except Exception:
            log.exception("Enregistrement de %s impossible", company.name)
            continue
        with_email += int(updated.status == "enriched")
    log.info("Enrichissement : %d entreprises traitées, %d avec e-mail", len(batch), with_email)
    return EnrichReport(processed=len(batch), with_email=with_email, without_email=len(batch) - with_email)
