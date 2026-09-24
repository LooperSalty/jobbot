"""CLI commands (registered in ``jobbot.cli``): installation, configuration, sessions, status, export,
dashboard, scheduled tasks."""

from __future__ import annotations

import logging
import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from jobbot.cli_support import State, fail, state_of

log = logging.getLogger(__name__)

NEXT_STEPS = (
    "\n[bold]Étapes suivantes[/]\n"
    "  1. jobbot setup            (réglages : zone, contrats, quotas, mot de passe Gmail…)\n"
    "  2. jobbot doctor           (vérifie l'installation)\n"
    "  3. jobbot login linkedin   (puis hellowork et classroom : connexion manuelle, une seule fois)\n"
    "  4. jobbot --dry-run run    (essai à blanc)\n"
    "  5. jobbot dashboard        (valider les candidatures en mode review)"
)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Site(str, Enum):
    linkedin = "linkedin"
    hellowork = "hellowork"
    wttj = "wttj"
    classroom = "classroom"


class ExportFormat(str, Enum):
    xlsx = "xlsx"
    csv = "csv"


# --------------------------------------------------------------------------- installation


def init(ctx: typer.Context) -> None:
    """Prépare le projet : dossiers, fichier .env, profil (modèle fictif à remplacer), base de données (n'écrase
    rien)."""
    from jobbot.cli_admin import run_init

    state = state_of(ctx)
    for line in run_init(state.root, state.console):
        state.console.print(f"• {escape(line)}")
    state.console.print(NEXT_STEPS)


def doctor(
    ctx: typer.Context,
    smtp: Annotated[bool, typer.Option("--smtp", help="Teste aussi la connexion Gmail (aucun e-mail envoyé).")] = False,
) -> None:
    """Vérifie l'installation : dépendances, Claude, navigateur, clés .env (présence seulement), sources."""
    from jobbot.cli_admin import doctor_table, run_doctor

    state = state_of(ctx)
    checks = run_doctor(state, smtp=smtp)
    state.console.print(doctor_table(checks))
    errors = [c for c in checks if c.level == "error"]
    if errors:
        state.console.print(f"[red]{len(errors)} problème(s) bloquant(s).[/]")
        raise typer.Exit(1)
    state.console.print("[green]Tout est prêt.[/]" if all(c.level == "ok" for c in checks)
                        else "[yellow]Utilisable, avec des avertissements.[/]")


def setup(
    ctx: typer.Context,
    section: Annotated[str | None, typer.Option("--section", "-s", help="Refaire une seule section (ex. Comptes).")]
    = None,
) -> None:
    """Assistant de configuration (zone, contrats, automatisation, relances, comptes)."""
    from jobbot.cli_wizard import run_wizard

    state = state_of(ctx)
    if not run_wizard(state.root, state.console, section):
        raise typer.Exit(1)


def config_show(ctx: typer.Context,
                section: Annotated[str | None, typer.Argument(help="Section à afficher.")] = None) -> None:
    """Affiche les réglages (secrets : ✓ présent / ✗ absent, jamais la valeur)."""
    from jobbot.cli_wizard import config_table

    state = state_of(ctx)
    state.console.print(config_table(state.root, section))


def config_get(ctx: typer.Context,
               key: Annotated[str, typer.Argument(help="Clé pointée, ex. limits.linkedin_per_day")]) -> None:
    """Affiche une valeur (préfixe « profile. » pour le profil)."""
    from jobbot.cli_wizard import config_get as get
    from jobbot.config import ConfigError

    state = state_of(ctx)
    try:
        state.console.print(escape(get(state.root, key)))
    except ConfigError as exc:
        fail(state.console, str(exc))


def config_set(ctx: typer.Context, key: Annotated[str, typer.Argument(help="Clé pointée")],
               value: Annotated[str, typer.Argument(help="Nouvelle valeur")]) -> None:
    """Modifie une valeur (validée ; les commentaires du fichier sont conservés)."""
    from jobbot.cli_wizard import config_set as set_
    from jobbot.config import ConfigError

    state = state_of(ctx)
    if state.dry_run:
        state.console.print(escape(f"[simulation] {key} non modifié."))
        return
    try:
        state.console.print(escape(set_(state.root, key, value)))
    except ConfigError as exc:
        fail(state.console, str(exc))


def import_cv(
    ctx: typer.Context,
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Ton CV actuel (PDF).")],
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Fichier YAML produit.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Écraser le fichier s'il existe.")] = False,
) -> None:
    """Extrait un profil (YAML) d'un CV PDF existant, à relire avant de l'utiliser."""
    from jobbot.documents.importer import import_cv as extract
    from jobbot.documents.importer import write_profile_yaml

    state = state_of(ctx)
    target = out or state.root / "config" / "profile.imported.yaml"
    if target.exists() and not force:
        fail(state.console, f"{target} existe déjà (--force pour l'écraser).")
    path = write_profile_yaml(extract(pdf, _llm_only(state)), target)
    state.console.print(f"Profil extrait dans [bold]{escape(str(path))}[/] : relis-le, complète-le puis copie-le "
                        "dans config/profile.yaml (rien n'est inventé : vérifie les dates).")


def _llm_only(state: State) -> Any:
    from jobbot.config import load_secrets, load_settings
    from jobbot.llm import DisabledLLM, LLMError, build_llm

    try:
        return build_llm(load_settings(state.root).llm, load_secrets(state.root))
    except LLMError as exc:
        log.warning("LLM indisponible (%s) : extraction par règles.", exc)
        return DisabledLLM()


def login(
    ctx: typer.Context, site: Annotated[Site, typer.Argument(help="linkedin, hellowork, wttj ou classroom")],
    timeout: Annotated[int, typer.Option("--timeout", help="Attente maximale (secondes).")] = 600,
) -> None:
    """Ouvre le site dans le navigateur de jobbot : connecte-toi à la main (jamais automatisé)."""
    from jobbot.apply.browser import login as browser_login

    state = state_of(ctx)
    if site is Site.classroom and state.app.settings.classroom.backend == "api":
        from jobbot.classroom.api import authorize

        ok, message = authorize(state.app)
        if not ok:
            fail(state.console, message)
        state.console.print(f"[green]{escape(message)}[/]")
        return
    try:
        connected = browser_login(state.app, site.value, timeout_s=timeout)
    except Exception as exc:  # no display (server), browser missing, profile in use…
        log.exception("Fenêtre de connexion impossible")
        fail(state.console, f"Impossible d'ouvrir la fenêtre de connexion ({exc}). Il faut un écran : connecte-toi "
                            "depuis un poste avec affichage.")
        return
    if connected:
        state.console.print(f"[green]Connecté à {site.value}[/] : la session est gardée dans le profil navigateur.")
        return
    fail(state.console, f"Connexion à {site.value} non détectée.")


# --------------------------------------------------------------------------- follow-up of the search


def status(ctx: typer.Context) -> None:
    """Résumé texte : offres, candidatures, entreprises, quotas du jour, files d'attente."""
    from jobbot.cli_tools import jobs_table, status_tables
    from jobbot.models import JobStatus

    state = state_of(ctx)
    for table in status_tables(state.app):
        state.console.print(table)
    for job_status, title in ((JobStatus.PENDING_REVIEW, "À valider (jobbot approve / reject)"),
                              (JobStatus.NEEDS_MANUAL, "À finir à la main (jobbot assist)")):
        jobs = state.app.repo.list_jobs(job_status, limit=10)
        if jobs:
            state.console.print(jobs_table(jobs, title))


def export(
    ctx: typer.Context,
    fmt: Annotated[ExportFormat, typer.Option("--format", "-f", help="xlsx ou csv.")] = ExportFormat.xlsx,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Fichier (xlsx) ou dossier (csv).")] = None,
) -> None:
    """Exporte offres, candidatures et entreprises (Excel ou CSV)."""
    from jobbot.cli_tools import export_data

    state = state_of(ctx)
    for path in export_data(state.app, fmt.value, out):
        state.console.print(f"Exporté : {escape(str(path))}")


def dashboard(
    ctx: typer.Context,
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="Port du tableau de bord.")] = 8501,
    host: Annotated[str, typer.Option("--host", help="Adresse d'écoute : 127.0.0.1 (défaut, tunnel SSH) ou l'IP "
                                                     "du VPN ; jamais 0.0.0.0.")] = "127.0.0.1",
) -> None:
    """Ouvre le tableau de bord (Streamlit) dans le navigateur (sur un serveur : service, sans navigateur)."""
    from jobbot.cli_tools import launch_dashboard

    state = state_of(ctx)
    try:
        code = launch_dashboard(state.root, port, host=host)
    except (RuntimeError, ValueError) as exc:
        fail(state.console, str(exc))
    if code:
        raise typer.Exit(code)


# --------------------------------------------------------------------------- scheduled tasks


def schedule_install(
    ctx: typer.Context,
    morning: Annotated[str, typer.Option("--matin", help="Heure du lancement complet (HH:MM).")] = "09:30",
    evening: Annotated[str, typer.Option("--soir", help="Heure boîte mail + relances (HH:MM).")] = "18:00",
) -> None:
    """Crée les tâches planifiées (Windows : jobbot-matin, jobbot-soir, jobbot-classroom ; Linux : crontab)."""
    state = state_of(ctx)
    bad = [t for t in (morning, evening) if not _TIME_RE.match(t)]
    if bad:
        fail(state.console, f"Heure invalide : {', '.join(bad)} (format HH:MM)")
    _schedule(state, "install", {"jobbot-matin": morning, "jobbot-soir": evening})


def schedule_remove(ctx: typer.Context) -> None:
    """Supprime les tâches planifiées de jobbot."""
    _schedule(state_of(ctx), "remove")


def schedule_show(ctx: typer.Context) -> None:
    """Affiche l'état des tâches planifiées (prochaine exécution…)."""
    _schedule(state_of(ctx), "show")


def _schedule(state: State, action: str, times: dict[str, str] | None = None) -> None:
    """Windows: Task Scheduler (install_task.ps1 + schtasks); Linux: jobbot lines of the user's crontab."""
    from jobbot.cli_tools import run_schedule
    from jobbot.cron import run_cron, scheduler_kind

    kind = scheduler_kind()
    if kind == "cron":
        cron_times = {"run": (times or {}).get("jobbot-matin", ""), "soir": (times or {}).get("jobbot-soir", "")}
        lines = run_cron(state.root, action, dry_run=state.dry_run, times=cron_times)
    elif kind == "windows" or state.dry_run:
        lines = run_schedule(state.root, action, dry_run=state.dry_run, times=times)
    else:
        fail(state.console, "Tâches planifiées gérées sous Windows (Planificateur) et Linux (cron) seulement.")
        return
    for line in lines:
        state.console.print(escape(line))
    if kind == "cron" and any(line.startswith(("Crontab : échec", "crontab introuvable")) for line in lines):
        raise typer.Exit(1)
