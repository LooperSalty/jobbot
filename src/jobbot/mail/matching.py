"""Find the application an incoming e-mail belongs to.

Order (research §1): Message-ID thread (In-Reply-To/References) → Gmail X-GM-THRID → sender address/domain.
Platform notifications are matched by company name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from jobbot import clock
from jobbot.db import Repository
from jobbot.mail.messages import InboundMessage
from jobbot.mail.platforms import platform_of
from jobbot.models import TERMINAL_APPLICATION_STATUSES, Application, ApplyMethod
from jobbot.textutil import normalize, normalize_company

log = logging.getLogger(__name__)

THREAD_KV_PREFIX = "gmthread:"
SENDER_MATCH_DAYS = 180
MIN_COMPANY_KEY = 3
WEBMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "outlook.fr", "hotmail.com", "hotmail.fr", "live.com", "live.fr",
    "msn.com", "yahoo.com", "yahoo.fr", "icloud.com", "me.com", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr",
    "laposte.net", "neuf.fr", "bbox.fr", "gmx.fr", "gmx.com", "protonmail.com", "proton.me", "aol.com",
})


def domain_of(address: str | None) -> str:
    return address.rsplit("@", 1)[-1].strip().lower() if address and "@" in address else ""


def _prefer_open(apps: list[Application]) -> Application | None:
    """Most recent non-terminal application, else the most recent one."""
    ordered = sorted(apps, key=lambda a: a.applied_at, reverse=True)
    for app in ordered:
        if app.status not in TERMINAL_APPLICATION_STATUSES:
            return app
    return ordered[0] if ordered else None


# --------------------------------------------------------------------------- thread matching


def remember_thread(repo: Repository, gm_thread_id: str | None, app_id: int | None) -> None:
    if gm_thread_id and app_id is not None:
        repo.kv_set(THREAD_KV_PREFIX + gm_thread_id, str(app_id))


def match_by_thread(repo: Repository, msg: InboundMessage) -> Application | None:
    candidates = [msg.in_reply_to, *reversed(msg.references)]
    for mid in (c for c in candidates if c):
        app = repo.find_application_by_message_id(mid)
        if app is not None:
            return app
    if msg.gm_thread_id:
        stored = repo.kv_get(THREAD_KV_PREFIX + msg.gm_thread_id)
        if stored and stored.isdigit():
            return repo.get_application(int(stored))
    return None


def learn_own_message(repo: Repository, own: InboundMessage) -> None:
    """Our own sent copy in All Mail: map its Gmail thread to the application that owns its Message-ID."""
    if not (own.gm_thread_id and own.message_id):
        return
    app = repo.find_application_by_message_id(own.message_id)
    if app is not None:
        remember_thread(repo, own.gm_thread_id, app.id)


# --------------------------------------------------------------------------- sender / company index


@dataclass(frozen=True)
class _Entry:
    app: Application
    domains: frozenset[str]
    company_key: str


def _app_domains(repo: Repository, app: Application) -> frozenset[str]:
    domains = {domain_of(app.email_to)}
    if app.company_id:
        company = repo.get_company(app.company_id)
        if company is not None:
            domains.add((company.domain or "").lower())
    if app.job_id:
        job = repo.get_job(app.job_id)
        if job is not None:
            domains.update({(job.company_domain or "").lower(), domain_of(job.contact_email)})
    return frozenset(d for d in domains if d and d not in WEBMAIL_DOMAINS)


class ApplicationIndex:
    """Recent applications with their domains and normalised company names (built once per inbox run)."""

    def __init__(self, repo: Repository, entries: tuple[_Entry, ...]):
        self.repo = repo
        self.entries = entries

    @classmethod
    def build(cls, repo: Repository, *, days: int = SENDER_MATCH_DAYS) -> ApplicationIndex:
        since = clock.now() - timedelta(days=days)
        entries: list[_Entry] = []
        for app in repo.list_applications():
            if app.applied_at < since:
                continue
            try:
                domains = _app_domains(repo, app)
            except Exception:  # a broken row must not hide the others
                log.warning("Domaines introuvables pour la candidature %s", app.id, exc_info=True)
                domains = frozenset()
            entries.append(_Entry(app, domains, normalize_company(app.company_name)))
        return cls(repo, tuple(entries))

    def match_sender(self, address: str) -> Application | None:
        """Exact address first (webmail included), then same company domain (or a subdomain of it)."""
        address = (address or "").strip().lower()
        if not address or platform_of(address):
            return None
        exact = [e.app for e in self.entries if (e.app.email_to or "").lower() == address]
        if exact:
            return _prefer_open(exact)
        domain = domain_of(address)
        if not domain or domain in WEBMAIL_DOMAINS:
            return None
        same = [e.app for e in self.entries if any(domain == d or domain.endswith("." + d) for d in e.domains)]
        return _prefer_open(same)

    def match_company(
        self, company: str | None, text: str, methods: tuple[ApplyMethod, ...] = ()
    ) -> Application | None:
        """By extracted company name, else by a known company name appearing in the text."""
        wanted = normalize_company(company or "")
        hits = [e for e in self.entries if _same_company(wanted, e.company_key)] if wanted else []
        if not hits:
            haystack = f" {normalize(text)} "
            hits = [e for e in self.entries if _mentioned(e.company_key, haystack)]
        preferred = [e for e in hits if e.app.method in methods] if methods else []
        return _prefer_open([e.app for e in (preferred or hits)])


def _same_company(a: str, b: str) -> bool:
    if len(a) < MIN_COMPANY_KEY or len(b) < MIN_COMPANY_KEY:
        return False
    return a == b or f" {a} " in f" {b} " or f" {b} " in f" {a} "


def _mentioned(company_key: str, haystack: str) -> bool:
    if len(company_key) < MIN_COMPANY_KEY:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(company_key)}(?![a-z0-9])", haystack) is not None
