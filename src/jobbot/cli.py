"""Command line « jobbot » (Typer). Global options: --dry-run, --root, --verbose.

The commands are thin wrappers defined in ``cli_commands_setup`` / ``cli_commands_run``; the work lives in the
modules (sources, matching, documents, apply, mail, outreach, followup, pipeline).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from typer.core import TyperGroup

from jobbot import cli_commands_run as run_cmds
from jobbot import cli_commands_setup as setup_cmds
from jobbot.cli_support import State, configure_stdio, fail, resolve_root, setup_logging, state_of, teardown_logging

log = logging.getLogger(__name__)

HELP = ("[bold]jobbot[/] — recherche d'alternance / d'emploi automatisée : offres, tri, CV et lettre ciblés "
        "(score ATS), candidatures, relances et candidatures spontanées.")
NO_WIZARD = frozenset({"setup", "init", "doctor", "config", "import-cv", "schedule"})

configure_stdio()  # help pages are printed before any callback runs (cp1252 pipes on Windows)


class JobbotGroup(TyperGroup):
    """Keeps the sub-command arguments visible to the callback (Click clears ``ctx.args`` before it)."""

    def resolve_command(self, ctx: Any, args: list[str]) -> Any:  # type: ignore[override]
        ctx.meta["jobbot_sub_args"] = list(args)
        return super().resolve_command(ctx, args)


app = typer.Typer(name="jobbot", help=HELP, no_args_is_help=True, add_completion=False, cls=JobbotGroup,
                  rich_markup_mode="rich", pretty_exceptions_enable=False)
config_app = typer.Typer(help="Lire ou modifier les réglages (settings.yaml, profile.yaml, .env).",
                         no_args_is_help=True)
schedule_app = typer.Typer(help="Tâches planifiées Windows : « run » à 9 h 30, boîte mail + relances à 18 h.",
                           no_args_is_help=True)


# --------------------------------------------------------------------------- global options & first run


def _stdin_is_tty() -> bool:
    """True only for an interactive terminal (on Windows ``isatty()`` is also True for NUL: scheduled tasks)."""
    try:
        if not (sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
            return False
    except (OSError, ValueError):
        return False
    return sys.platform != "win32" or _is_windows_console(sys.stdin)


def _is_windows_console(stream: Any) -> bool:
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))  # type: ignore[attr-defined]
    except Exception:  # unknown stream type: trust isatty()
        return True


def _asks_help(ctx: typer.Context) -> bool:
    args = [*ctx.meta.get("jobbot_sub_args", []), *ctx.args]
    return any(a in ("--help", "-h") for a in args)


def _first_run(ctx: typer.Context, state: State) -> None:
    """Run the setup wizard once, before any command other than setup/init/doctor/config/import-cv/--help."""
    if ctx.resilient_parsing or ctx.invoked_subcommand in NO_WIZARD or _asks_help(ctx):
        return
    from jobbot.preferences import is_setup_completed

    try:
        if is_setup_completed(state.root):
            return
    except Exception as exc:  # broken settings: the command itself will report it
        log.debug("État de la configuration illisible : %s", exc)
        return
    if not _stdin_is_tty():
        log.warning("Configuration initiale jamais faite (« jobbot setup ») : réglages actuels utilisés.")
        return
    from jobbot.cli_wizard import run_wizard

    state.console.print("[bold]Première utilisation : configurons jobbot ensemble.[/]")
    run_wizard(state.root, state.console)


@app.callback()
def main(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Simulation : rien n'est envoyé (e-mails écrits dans "
                                                            "output/outbox, formulaires non soumis).")] = False,
    root: Annotated[Path | None, typer.Option("--root", help="Dossier du projet (détecté par défaut).")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Journal détaillé.")] = False,
) -> None:
    configure_stdio()
    state = State(root=resolve_root(root), dry_run=dry_run, verbose=verbose)
    ctx.obj = state
    setup_logging(state.root, verbose)
    ctx.call_on_close(teardown_logging)
    _first_run(ctx, state)


# --------------------------------------------------------------------------- reports (entretiens, rapport hebdo)


def prep(
    ctx: typer.Context,
    application_id: Annotated[int, typer.Argument(help="Id de la candidature (tableau de bord, page Entretiens).")],
) -> None:
    """Fiche de préparation d'entretien : entreprise, exemples STAR, questions probables, logistique."""
    from jobbot.reports.interview import build_interview_prep

    state = state_of(ctx)
    application = state.app.repo.get_application(application_id)
    if application is None:
        fail(state.console, f"Candidature {application_id} introuvable.")
        return
    try:
        path = build_interview_prep(state.app, application)
    except Exception as exc:  # French message instead of a traceback
        log.exception("Fiche d'entretien impossible")
        fail(state.console, f"Fiche d'entretien impossible : {exc}")
        return
    state.console.print(f"Fiche d'entretien : [bold]{escape(str(path))}[/]")


def report(
    ctx: typer.Context,
    previous: Annotated[bool, typer.Option("--precedente", "-p", help="Semaine précédente complète.")] = False,
    day: Annotated[str | None, typer.Option("--date", help="Dernier jour inclus (AAAA-MM-JJ) ; défaut : "
                                                           "aujourd'hui (semaine en cours).")] = None,
    email: Annotated[bool, typer.Option("--email", help="M'envoyer aussi le rapport par e-mail.")] = False,
) -> None:
    """Rapport de la semaine : offres, candidatures, réponses, relances, justificatif France Travail."""
    from jobbot.reports.weekly import build_weekly_report, parse_day, previous_week_end

    state = state_of(ctx)
    try:
        week_end = parse_day(day) if day else None
        if week_end is None and previous:
            week_end = previous_week_end()
        result = build_weekly_report(state.app, week_end, send_email=email or None)
    except ValueError as exc:
        fail(state.console, str(exc))
        return
    state.console.print(f"[bold]{escape(result.summary())}[/]")
    for line in result.details:
        state.console.print(f"  • {escape(line)}")


# --------------------------------------------------------------------------- registration (help order)

for _name, _func in (
    ("init", setup_cmds.init), ("doctor", setup_cmds.doctor), ("setup", setup_cmds.setup),
    ("import-cv", setup_cmds.import_cv), ("login", setup_cmds.login),
    ("search", run_cmds.search), ("score", run_cmds.score), ("prepare", run_cmds.prepare), ("ats", run_cmds.ats),
    ("apply", run_cmds.apply), ("assist", run_cmds.assist), ("approve", run_cmds.approve),
    ("reject", run_cmds.reject), ("discover", run_cmds.discover), ("enrich", run_cmds.enrich),
    ("spontaneous", run_cmds.spontaneous), ("inbox", run_cmds.inbox), ("followup", run_cmds.followup),
    ("run", run_cmds.run), ("prep", prep), ("report", report), ("status", setup_cmds.status),
    ("export", setup_cmds.export), ("dashboard", setup_cmds.dashboard),
):
    app.command(_name)(_func)

config_app.command("show")(setup_cmds.config_show)
config_app.command("get")(setup_cmds.config_get)
config_app.command("set")(setup_cmds.config_set)
schedule_app.command("install")(setup_cmds.schedule_install)
schedule_app.command("remove")(setup_cmds.schedule_remove)
schedule_app.command("show")(setup_cmds.schedule_show)
app.add_typer(config_app, name="config")
app.add_typer(schedule_app, name="schedule")


if __name__ == "__main__":  # python -m jobbot.cli
    app()
