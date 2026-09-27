"""Night step « priorité » : store each open company's priority (``raw["priority"]``, ``raw["priority_reasons"]``,
:mod:`jobbot.outreach.priority`) for the dashboard, and count the ready queue."""

from __future__ import annotations

import logging
from collections.abc import Callable

from jobbot.context import AppContext
from jobbot.models import Company
from jobbot.night.budget import Deadline
from jobbot.outreach.priority import PRIORITY_KEY, REASONS_KEY, company_priority, ready_queue, with_priority

log = logging.getLogger(__name__)

SCORED_STATUSES = ("enriched", "approved", "pending_review")


def _setting(status: str, score: float, reasons: list[str]) -> Callable[[Company], Company | None]:
    """Priority written onto the stored version, only if the day did not change its status meanwhile."""
    def change(current: Company) -> Company | None:
        return with_priority(current, score, reasons) if current.status == status else None
    return change


def prioritize(ctx: AppContext, deadline: Deadline) -> tuple[int, int]:
    """(companies whose stored priority changed, size of the ready queue)."""
    changed = 0
    for company in ctx.repo.list_companies(SCORED_STATUSES):
        if deadline.expired():
            break
        score, reasons = company_priority(company, ctx.profile, ctx.repo, cfg=ctx.settings.outreach)
        if company.raw.get(PRIORITY_KEY) == score and company.raw.get(REASONS_KEY) == reasons:
            continue
        ctx.repo.update_company(company.id, _setting(company.status, score, reasons))
        changed += 1
    queue = len(ready_queue(ctx))
    log.info("Nuit : priorité recalculée pour %d entreprise(s), file d'attente : %d", changed, queue)
    return changed, queue
