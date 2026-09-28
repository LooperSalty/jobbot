"""Parallel night enrichment: website → crawl → people → address pattern → MX (:func:`outreach.emails.enrich_company`)
on ``settings.night.workers`` threads.

Politeness: two companies sharing a domain (or, before it is known, a name) are never enriched at the same time;
every page request keeps the per-host spacing of :mod:`jobbot.outreach.domains` and SearXNG its global throttle.
Website summaries use the fast model, one call at a time, at most ``settings.night.max_llm_summaries``
(:class:`jobbot.llm.CappedLLM`; beyond: the page's meta description).

Candidates: ``new`` companies, ``enriched`` / ``no_email`` ones enriched more than 60 days ago (stale) and
``enriched`` ones without an address usable under the current settings — inside the target only, most promising
first (:func:`jobbot.outreach.priority.potential`). No new company is started once the deadline is reached.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Hashable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from typing import TypeVar

from jobbot import clock
from jobbot.config import OutreachSettings
from jobbot.context import AppContext
from jobbot.db import Repository
from jobbot.llm import CappedLLM
from jobbot.models import Company
from jobbot.night.budget import Deadline
from jobbot.night.models import EnrichResult
from jobbot.outreach.domains import normalize_domain
from jobbot.outreach.emails import enrich_or_note_failure
from jobbot.outreach.priority import potential
from jobbot.outreach.relevance import is_relevant_employer, split_relevant
from jobbot.outreach.selection import is_nominative, usable
from jobbot.textutil import normalize_company

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")
STALE_DAYS = 60
WAITING_STATUSES = ("new", "enriched", "no_email")
# statuses (and their raw keys) the day may set while a night worker holds an older copy of the company
PROTECTED_STATUSES = frozenset({"contacted", "pending_review", "approved", "rejected", "blacklisted", "irrelevant"})
PROTECTED_RAW = ("contacted_at", "contacted_email", "review_contact", "review_cv", "review_channel",
                 "review_prev_status", "irrelevant_reason")


# --------------------------------------------------------------------------- candidates


def needs_enrichment(company: Company, cfg: OutreachSettings, now: datetime) -> bool:
    if company.status == "new":
        return True
    if company.status not in ("enriched", "no_email"):
        return False
    try:
        enriched_at = clock.parse_dt(str(company.raw.get("enriched_at") or ""))
    except ValueError:
        enriched_at = None
    if enriched_at is None or now - enriched_at > timedelta(days=STALE_DAYS):
        return True
    return company.status == "enriched" and not any(usable(c, cfg) for c in company.contacts)


def enrichment_queue(ctx: AppContext, *, now: datetime | None = None,
                     set_aside: bool = True) -> tuple[list[Company], int]:
    """(companies to enrich, most promising first; number outside the target — set aside unless
    ``set_aside=False``, for read-only views such as « jobbot status »)."""
    at = now or clock.now()
    waiting = [c for c in ctx.repo.list_companies(WAITING_STATUSES)
               if needs_enrichment(c, ctx.settings.outreach, at)]
    if set_aside:
        relevant, off = split_relevant(ctx.repo, ctx.profile, waiting)
        outside = len(off)
    else:
        relevant = [c for c in waiting if is_relevant_employer(c, ctx.profile)[0]]
        outside = len(waiting) - len(relevant)
    return sorted(relevant, key=lambda c: -potential(c, ctx.profile, now=at)), outside


def domain_key(company: Company) -> str:
    """Politeness key: the website's domain when known, else the normalised name (same name → same site)."""
    domain = normalize_domain(company.domain or company.website)
    return domain or f"nom:{normalize_company(company.name) or company.id}"


# --------------------------------------------------------------------------- scheduler


def run_parallel(items: Iterable[T], work: Callable[[T], R], *, workers: int, deadline: Deadline,
                 key: Callable[[T], Hashable]) -> tuple[list[R], int]:
    """(results, items not started). ``work`` runs on ``workers`` threads, never twice at once for the same
    ``key``; nothing new starts after the deadline (running items finish). ``work`` must not raise."""
    queue = list(items)
    active: dict[Future, Hashable] = {}
    results: list[R] = []
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="nuit") as pool:
        while queue or active:
            while queue and len(active) < max(1, workers) and not deadline.expired():
                busy = set(active.values())
                index = next((i for i, item in enumerate(queue) if key(item) not in busy), None)
                if index is None:
                    break
                item = queue.pop(index)
                active[pool.submit(work, item)] = key(item)
            if not active:
                break
            done, _ = wait(list(active), return_when=FIRST_COMPLETED)
            for future in done:
                active.pop(future)
                results.append(future.result())
    return results, len(queue)


# --------------------------------------------------------------------------- one company


@dataclasses.dataclass(frozen=True)
class Outcome:
    before: Company
    after: Company

    @property
    def new_contacts(self) -> list:
        known = {c.email for c in self.before.contacts}
        return [c for c in self.after.contacts if c.email not in known]


def save_enriched(repo: Repository, before: Company, after: Company) -> Company:
    """Store ``after`` atomically without undoing what the day changed meanwhile (contacted, validated, set
    aside…)."""
    def change(current: Company) -> Company:
        if current.status != before.status and current.status in PROTECTED_STATUSES:
            kept = {k: current.raw[k] for k in PROTECTED_RAW if k in current.raw}
            return after.model_copy(update={"status": current.status, "raw": {**after.raw, **kept}})
        return after

    stored = repo.update_company(before.id, change)
    if stored is None:  # not in the database (never happens in a night run): plain insert
        repo.upsert_company(after)
        return after
    return stored


def _worker(ctx: AppContext, enrich: Callable[[AppContext, Company], Company]) -> Callable[[Company], Outcome]:
    def work(company: Company) -> Outcome:
        try:
            after = enrich(ctx, company)
            return Outcome(company, save_enriched(ctx.repo, company, after))
        except Exception:  # enrich_or_note_failure never raises; saving might
            log.exception("Enrichissement de nuit de %s non enregistré", company.name)
            return Outcome(company, company)
    return work


def _night_context(ctx: AppContext) -> AppContext:
    return dataclasses.replace(ctx, llm=CappedLLM(ctx.llm, ctx.settings.night.max_llm_summaries))


def run_enrichment(ctx: AppContext, deadline: Deadline, *,
                   enrich: Callable[[AppContext, Company], Company] = enrich_or_note_failure) -> EnrichResult:
    queue, irrelevant = enrichment_queue(ctx)
    log.info("Nuit : %d entreprise(s) à enrichir (%d fil(s))", len(queue), ctx.settings.night.workers)
    outcomes, not_started = run_parallel(queue, _worker(_night_context(ctx), enrich),
                                         workers=ctx.settings.night.workers, deadline=deadline, key=domain_key)
    new = [c for o in outcomes for c in o.new_contacts]
    nominative = sum(1 for c in new if is_nominative(c))
    return EnrichResult(
        processed=len(outcomes), with_email=sum(o.after.status == "enriched" for o in outcomes),
        generic_emails=len(new) - nominative, nominative_emails=nominative, not_started=not_started,
        irrelevant=irrelevant,
    )
