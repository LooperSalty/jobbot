"""Night report: one French notification (level « important »: Telegram / e-mail when configured) and the last
report kept in kv (``night:last_report``) for « jobbot status », « jobbot doctor » and the dashboard."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from jobbot import clock
from jobbot.context import AppContext
from jobbot.night.models import NightReport

log = logging.getLogger(__name__)

LAST_REPORT_KEY = "night:last_report"


def save_last(repo, report: NightReport, *, at: datetime | None = None) -> None:
    payload = {"at": clock.iso(at or clock.now()), "summary": report.summary(),
               "report": report.model_dump(mode="json")}
    repo.kv_set(LAST_REPORT_KEY, json.dumps(payload, ensure_ascii=False))


def load_last(repo) -> dict | None:
    """{at, summary, report} of the last night run, None when it never ran."""
    try:
        data = json.loads(repo.kv_get(LAST_REPORT_KEY) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("at") else None


def last_run_label(repo) -> str | None:
    """'27/09/2026 06:12 — Nuit : …' (Paris time), None when the night never ran."""
    last = load_last(repo)
    if last is None:
        return None
    try:
        when = clock.to_paris(clock.parse_dt(str(last["at"]))).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        when = str(last["at"])
    return f"{when} — {last.get('summary') or ''}"


def publish(ctx: AppContext, report: NightReport) -> None:
    """Keep the report (not in dry-run) and notify it (never raises)."""
    try:
        if not ctx.dry_run:
            save_last(ctx.repo, report)
        from jobbot.notify import notify

        title = "jobbot — nuit" + (" (simulation)" if ctx.dry_run else "")
        notify(ctx, title, report.summary(), level="important")
    except Exception:  # a report must never fail the run
        log.exception("Rapport de nuit non publié")
