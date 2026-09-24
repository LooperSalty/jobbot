"""« jobbot session export|import » — logins done on the PC, reused on a headless server (registered in ``cli``)."""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from jobbot.cli_support import State, fail, state_of

log = logging.getLogger(__name__)

session_app = typer.Typer(help="Sessions des sites (LinkedIn, HelloWork, WTTJ, Classroom) : export depuis le PC "
                               "(avec écran), import sur un serveur sans écran.", no_args_is_help=True)


class SessionSite(str, Enum):
    linkedin = "linkedin"
    hellowork = "hellowork"
    wttj = "wttj"
    classroom = "classroom"
    all = "all"


@session_app.command("export")
def export(
    ctx: typer.Context,
    site: Annotated[SessionSite, typer.Argument(help="linkedin, hellowork, wttj, classroom ou all")] = SessionSite.all,
) -> None:
    """Écrit les cookies de connexion dans data/sessions/<site>.json (à copier sur le serveur, jamais à partager)."""
    from jobbot.sessions import expand_sites, export_sessions

    state = state_of(ctx)
    if state.dry_run:
        state.console.print(escape(f"[simulation] export des sessions {site.value} non effectué."))
        return
    lines = export_sessions(state.app, expand_sites(site.value))
    for line in lines:
        state.console.print(f"• {escape(line)}")
    if not any("exporté" in line for line in lines):
        fail(state.console, "Aucune session exportée : connecte-toi d'abord (« jobbot login <site> »).")
    state.console.print("Copie sur le serveur : [bold]python scripts/deploy_server.py --only private[/] "
                        "(ou « jobbot session import <dossier> » sur le serveur).")


@session_app.command("import")
def import_(
    ctx: typer.Context,
    path: Annotated[Path | None, typer.Argument(help="Fichier <site>.json ou dossier qui en contient ; sans "
                                                     "chemin : état des sessions déjà présentes.")] = None,
) -> None:
    """Copie des sessions exportées dans data/sessions/ ; appliquées à la prochaine ouverture du navigateur."""
    from jobbot.sessions import SessionError, copy_session_files

    state = state_of(ctx)
    app = state.app
    if path is None:
        _print_status(state)
        return
    if state.dry_run:
        state.console.print(escape(f"[simulation] {path} non copié."))
        return
    try:
        lines = copy_session_files(app.root, app.settings, path)
    except SessionError as exc:
        fail(state.console, str(exc))
        return
    for line in lines:
        state.console.print(f"• {escape(line)}")


def _print_status(state: State) -> None:
    from jobbot import clock
    from jobbot.sessions import session_status

    app = state.app
    infos = session_status(app.root, app.settings, app.repo)
    console = state.console
    if not infos:
        console.print("Aucune session dans data/sessions/ (sur le PC : « jobbot session export all »).")
        return
    for info in infos:
        when = clock.to_paris(info.exported_at).strftime("%d/%m/%Y %H:%M")
        todo = "à importer à la prochaine ouverture" if info.pending else "déjà importée"
        console.print(escape(f"• {info.site} : {info.cookies} cookie(s), fichier du {when} ({todo})"))
