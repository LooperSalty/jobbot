"""« jobbot classroom watch|test » — offers relayed by the school in Google Classroom (registered in ``cli``)."""

from __future__ import annotations

import logging
import time
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from jobbot.cli_support import dry_banner, fail, print_report, state_of

log = logging.getLogger(__name__)

TITLE = "Offres de l'école (Classroom)"
_sleep = time.sleep

classroom_app = typer.Typer(help="Offres de l'école publiées dans Google Classroom : surveillance et candidature "
                                 "immédiate.", no_args_is_help=True)


@classroom_app.command("watch")
def watch(
    ctx: typer.Context,
    once: Annotated[bool, typer.Option("--once", help="Une seule vérification (tâche planifiée).")] = False,
    interval: Annotated[int | None, typer.Option("--interval", "-i", min=1,
                                                 help="Minutes entre deux vérifications (défaut : "
                                                      "classroom.poll_minutes).")] = None,
    max_cycles: Annotated[int, typer.Option("--max-cycles", hidden=True, min=0)] = 0,
) -> None:
    """Surveille le flux Classroom et postule tout de suite aux offres adaptées (Ctrl+C pour arrêter)."""
    from jobbot.classroom.watch import run_classroom

    state = state_of(ctx)
    dry_banner(state)
    minutes = interval or state.app.settings.classroom.poll_minutes
    cycles = 0
    try:
        while True:
            report = run_classroom(state.app)
            print_report(state.console, TITLE, report)
            cycles += 1
            if once or (max_cycles and cycles >= max_cycles):
                return
            state.console.print(f"Prochaine vérification dans {minutes} min (Ctrl+C pour arrêter).")
            _sleep(minutes * 60)
    except KeyboardInterrupt:
        state.console.print("Surveillance Classroom arrêtée.")


@classroom_app.command("test")
def test(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Publications les plus récentes à analyser.")]
    = 10,
) -> None:
    """Lit les dernières publications, extrait et note les offres, dit ce qui serait fait (ne postule jamais)."""
    from jobbot.classroom.preview import preview_classroom

    state = state_of(ctx)
    try:
        rows, notes = preview_classroom(state.app, limit=limit)
    except Exception as exc:  # French message instead of a traceback
        log.exception("Test Classroom impossible")
        fail(state.console, f"Test Classroom impossible : {exc}")
        return
    for note in notes:
        state.console.print(escape(note))
    if not rows:
        return
    table = Table(title="Classroom — ce que jobbot ferait (aucune candidature envoyée)", title_justify="left")
    for column in ("Publication", "Offre", "Entreprise", "Niveau", "Canal", "Score", "Verdict"):
        table.add_column(column)
    for row in rows:
        score = f"{row.score:.0f}" if row.score is not None else "—"
        table.add_row(escape(row.post), escape(row.title[:60]), escape(row.company[:30]), escape(row.level[:25]),
                      escape(row.channel), score, escape(row.verdict))
    state.console.print(table)
