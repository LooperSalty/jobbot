"""Views of the night work for « jobbot status », « jobbot doctor » and the dashboard (read only)."""

from __future__ import annotations

import math
from typing import Any

from jobbot import clock
from jobbot.context import AppContext
from jobbot.night.report import last_run_label, load_last
from jobbot.outreach import _searxng as searx
from jobbot.outreach.priority import ready_queue
from jobbot.outreach.spontaneous import fresh_documents

QUEUE_ROWS = 50
NIGHT_CHECK = "Nuit (collecte d'entreprises)"


def _days(queue: int, cap: int) -> str:
    if cap <= 0 or queue <= 0:
        return ""
    days = math.ceil(queue / cap)
    return f" ≈ {days} jour{'s' if days != 1 else ''} d'envoi"


def status_lines(ctx: AppContext) -> list[str]:
    """Sending ramp, ready queue, what the next night will do, last night's report."""
    from jobbot.night.enrich import enrichment_queue
    from jobbot.ramp import describe, ramp_state

    state = ramp_state(ctx.repo, ctx.settings.limits)
    queue = ready_queue(ctx)
    wanted = state.cap + ctx.settings.night.prepare_margin
    missing_docs = sum(1 for r in queue[:wanted]
                       if r.company.status == "enriched" and fresh_documents(ctx.repo, r.company.id) is None)
    to_enrich = len(enrichment_queue(ctx, set_aside=False)[0])  # read only
    lines = [
        f"Candidatures spontanées : plafond du jour {describe(state, ctx.settings.limits)}",
        f"File d'attente : {len(queue)} entreprise(s) prête(s){_days(len(queue), state.cap)}",
        f"Nuit prochaine : {to_enrich} entreprise(s) à enrichir, {missing_docs} dossier(s) à préparer",
    ]
    last = last_run_label(ctx.repo)
    lines.append(f"Dernière nuit : {last}" if last else "Dernière nuit : jamais lancée (jobbot night)")
    return lines


def queue_rows(ctx: AppContext, limit: int = QUEUE_ROWS) -> list[dict[str, Any]]:
    """The ready queue, best first (dashboard « Nuit »)."""
    rows = []
    for rank, item in enumerate(ready_queue(ctx)[:limit], start=1):
        company = item.company
        rows.append({
            "rang": rank, "entreprise": company.name, "ville": company.city, "priorite": round(item.score),
            "raisons": " · ".join(item.reasons), "statut": company.status,
            "dossier_pret": fresh_documents(ctx.repo, company.id) is not None,
        })
    return rows


def last_report(ctx: AppContext) -> dict | None:
    return load_last(ctx.repo)


def searxng_health(ctx: AppContext) -> tuple[str, str]:
    """(level ok | warn, French detail) of the SearXNG instance as seen by jobbot (never queried here)."""
    if not ctx.settings.search_engine_url:
        return "ok", "non configuré (recherche web : Brave si clé, sinon DuckDuckGo en dernier recours)"
    engines = ", ".join(ctx.settings.search_engine_engines) or "tous"
    used = f"{searx.queries_today(ctx.repo)}/{ctx.settings.night.max_search_queries} requête(s) aujourd'hui"
    until = searx.backoff_until(ctx.repo)
    last = searx.last_backoff(ctx.repo)
    if until is not None:
        reason = (last or {}).get("reason") or "blocage"
        fallback = "Brave, " if ctx.secrets.brave_api_key else ""
        return "warn", (f"moteurs bloqués ({reason}) : pause jusqu'au {clock.to_paris(until):%d/%m %H:%M}, repli sur "
                        f"{fallback}Clearbit, slug + DNS, Wikidata — {used}")
    detail = f"moteurs : {engines} — {used}"
    if last:
        detail += f" — dernier blocage le {str(last.get('at') or '')[:10]} ({last.get('reason')})"
    return "ok", detail


def doctor_checks(ctx: AppContext) -> list[Any]:
    """« jobbot doctor » lines: last night run and SearXNG health."""
    from jobbot.cli_admin import Check

    last = last_run_label(ctx.repo)
    if last:
        night = Check(NIGHT_CHECK, "ok", f"dernière exécution : {last}")
    else:
        never = "warn" if ctx.settings.night.enabled else "ok"
        night = Check(NIGHT_CHECK, never,
                      "jamais lancée : jobbot night (planifiée à 00:30 par jobbot schedule install)")
    level, detail = searxng_health(ctx)
    return [night, Check("SearXNG", level, detail)]  # type: ignore[arg-type]
