"""Companies held « à valider » only because of review mode (warm-up days, ``mode: review``) go back to the
sending queue once the effective mode is auto, with the status they had before the hold (``review_prev_status``,
recorded by the e-mail campaign and the La Bonne Alternance relay; older holds: ``enriched`` with an address,
``new`` without). Their documents are reused while fresh. A hold for CV claims the profile does not back (truth
warnings) stays until the user decides in the dashboard; a decision taken there meanwhile always wins.
"""

from __future__ import annotations

import logging

from jobbot.context import AppContext
from jobbot.models import Company

log = logging.getLogger(__name__)

HELD = "pending_review"
PREV_STATUS_KEY = "review_prev_status"
REVIEW_KEYS = frozenset({"review_contact", "review_cv", "review_channel", PREV_STATUS_KEY})


def hold_raw(company: Company, **review: str) -> dict[str, object]:
    """``raw`` of a company put on hold for review: the review fields + the status to come back to."""
    previous = company.raw.get(PREV_STATUS_KEY) if company.status == HELD else company.status
    return {**company.raw, **review, PREV_STATUS_KEY: previous or "enriched"}


def _released(company: Company) -> Company | None:
    if company.status != HELD:
        return None  # decided meanwhile (dashboard): never overridden
    status = company.raw.get(PREV_STATUS_KEY)
    if not status or status == HELD:  # held before the previous status was recorded
        status = "enriched" if company.contacts else "new"
    raw ={key: value for key, value in company.raw.items() if key not in REVIEW_KEYS}
    return company.model_copy(update={"status": status, "raw": raw})


def _release_one(ctx: AppContext, company_id: str) -> bool:
    """Atomic release (read-modify-write): False when the company left the hold meanwhile."""
    done: list[Company] = []

    def release(current: Company) -> Company | None:
        new = _released(current)
        done.extend([new] if new is not None else [])
        return new

    ctx.repo.update_company(company_id, release)
    return bool(done)


def release_review_holds(ctx: AppContext) -> int:
    """Put the companies held by review mode back in the queue (auto mode, never in a simulation). Returns how
    many."""
    from jobbot.preferences import effective_mode

    if ctx.dry_run or effective_mode(ctx.settings) != "auto":
        return 0
    released = 0
    for company in ctx.repo.list_companies(HELD):
        docs = ctx.repo.latest_documents(company_id=company.id)
        if docs is None or not docs.truth_warnings:
            released += int(_release_one(ctx, company.id))
    if released:
        log.info("Candidatures spontanées : %d entreprise(s) en attente de validation remise(s) dans la file "
                 "(mode automatique)", released)
    return released
