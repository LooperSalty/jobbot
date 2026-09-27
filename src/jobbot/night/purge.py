"""GDPR retention: the named contacts of companies never contacted are deleted once the company has been in the
database for ``settings.night.retention_days`` (default 365) — people, leaders, nominative addresses (published,
Hunter or guessed) and where they were found. Role addresses (recrutement@, contact@…) are not personal data and
stay. The company is marked (``raw["nominative_purged_at"]``): enrichment and the registry never bring named contacts
back (:func:`jobbot.outreach.emails.enrich_or_note_failure`, :func:`jobbot.outreach.companies.merge_company`).
Dry-run: counted, nothing deleted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from jobbot import clock
from jobbot.context import AppContext
from jobbot.models import Company
from jobbot.outreach.companies import PURGED_KEY
from jobbot.outreach.emails import PERSONAL_RAW, drop_nominative
from jobbot.outreach.selection import company_applications, is_nominative

log = logging.getLogger(__name__)


def _collected_at(company: Company) -> datetime:
    """When the company entered the database (its data is refreshed later, the retention runs from there)."""
    return clock.parse_dt(company.found_at) or clock.now()


def has_personal_data(company: Company) -> bool:
    return (any(is_nominative(c) for c in company.contacts) or bool(company.leaders)
            or any(company.raw.get(k) for k in PERSONAL_RAW))


def without_personal_data(company: Company, ctx: AppContext) -> Company:
    cleaned = drop_nominative(company, ctx.settings.outreach)
    emails = {c.email for c in cleaned.contacts}
    raw = {**cleaned.raw, PURGED_KEY: clock.iso(clock.now())}
    if str(raw.get("review_contact") or "") not in emails:
        raw.pop("review_contact", None)
    return cleaned.model_copy(update={"raw": raw})


def _purging(ctx: AppContext, status: str) -> Callable[[Company], Company | None]:
    """Applied to the stored version, only if the day did not change its status meanwhile (contacted…)."""
    def change(current: Company) -> Company | None:
        return without_personal_data(current, ctx) if current.status == status else None
    return change


def purge_personal_data(ctx: AppContext, *, now: datetime | None = None) -> int:
    """Number of companies whose named contacts were (or, in dry-run, would be) deleted."""
    cutoff = (now or clock.now()) - timedelta(days=ctx.settings.night.retention_days)
    apps = ctx.repo.list_applications()
    purged = 0
    for company in ctx.repo.list_companies():
        if company.status == "contacted" or _collected_at(company) > cutoff or not has_personal_data(company):
            continue
        if company_applications(apps, company):
            continue
        purged += 1
        if not ctx.dry_run:
            ctx.repo.update_company(company.id, _purging(ctx, company.status))
    if purged:
        log.info("Nuit : contacts nominatifs supprimés pour %d entreprise(s) jamais contactée(s) (> %d jours)",
                 purged, ctx.settings.night.retention_days)
    return purged
