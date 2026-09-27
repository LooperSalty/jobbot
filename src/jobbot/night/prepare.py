"""Prepare ahead: tailored CV + letter for the next day's first targets (the ready queue's top ``daily cap +
settings.night.prepare_margin``) that have no documents less than a week old — the morning campaign reuses them
(:func:`jobbot.outreach.spontaneous.fresh_documents`) instead of waiting for the model. Validated (``approved``)
companies are never touched: they are sent the documents the user reviewed. Dry-run: nothing generated.
"""

from __future__ import annotations

import logging

from jobbot.context import AppContext
from jobbot.night.budget import Deadline
from jobbot.night.models import PrepareResult
from jobbot.outreach.priority import ready_queue
from jobbot.outreach.spontaneous import fresh_documents
from jobbot.textutil import truncate

log = logging.getLogger(__name__)


def _prepare(ctx: AppContext, company) -> None:
    from jobbot.documents.service import prepare_documents

    prepare_documents(ctx, company=company)


def prepare_ahead(ctx: AppContext, deadline: Deadline, *, daily_cap: int) -> PrepareResult:
    """Documents for the top ``daily_cap + prepare_margin`` targets still without fresh documents."""
    wanted = max(0, daily_cap) + ctx.settings.night.prepare_margin
    counts = {"prepared": 0, "already_ready": 0, "planned": 0, "failed": 0}
    details: list[str] = []
    stopped = False
    for ranked in ready_queue(ctx)[:wanted]:
        company = ranked.company
        if company.status != "enriched":
            continue  # a validated company keeps the documents the user reviewed: never replaced at night
        if fresh_documents(ctx.repo, company.id) is not None:
            counts["already_ready"] += 1
            continue
        if deadline.expired():
            stopped = True
            break
        if ctx.dry_run:
            counts["planned"] += 1
            continue
        try:
            _prepare(ctx, company)
            counts["prepared"] += 1
        except Exception as exc:  # one company never stops the others
            log.exception("Dossier de nuit impossible pour %s", company.name)
            counts["failed"] += 1
            details.append(truncate(f"{company.name} : dossier impossible ({exc})", 200))
    log.info("Nuit : %d dossier(s) préparé(s), %d déjà prêt(s)", counts["prepared"], counts["already_ready"])
    return PrepareResult(**counts, stopped=stopped, details=details)
