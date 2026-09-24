"""Shared helpers of the command line: state, console, logging, French labels, report rendering."""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jobbot.context import AppContext, find_root
from jobbot.models import ApplicationStatus, ApplyMethod, JobStatus

log = logging.getLogger(__name__)

LOG_FILE = "jobbot.log"
LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 5
MAX_DETAIL_LINES = 40
PIPED_WIDTH = 120

JOB_STATUS_FR: dict[JobStatus, str] = {
    JobStatus.NEW: "Nouvelle", JobStatus.FILTERED_OUT: "Écartée (filtre)", JobStatus.SHORTLISTED: "Retenue",
    JobStatus.DOCS_READY: "Dossier prêt", JobStatus.PENDING_REVIEW: "À valider", JobStatus.APPROVED: "Validée",
    JobStatus.APPLIED: "Candidature envoyée", JobStatus.NEEDS_MANUAL: "À finir à la main",
    JobStatus.FAILED: "Échec", JobStatus.SKIPPED: "Ignorée", JobStatus.EXPIRED: "Expirée",
}
APP_STATUS_FR: dict[ApplicationStatus, str] = {
    ApplicationStatus.APPLIED: "Envoyée", ApplicationStatus.FOLLOWED_UP: "Relancée",
    ApplicationStatus.RESPONSE: "Réponse reçue", ApplicationStatus.INTERVIEW: "Entretien",
    ApplicationStatus.OFFER: "Proposition", ApplicationStatus.REJECTED: "Refus",
    ApplicationStatus.NO_RESPONSE: "Sans réponse", ApplicationStatus.WITHDRAWN: "Retirée",
    ApplicationStatus.BOUNCED: "Adresse invalide",
}
COMPANY_STATUS_FR = {
    "new": "Nouvelle", "enriched": "Contact trouvé", "no_email": "Sans e-mail", "pending_review": "À valider",
    "approved": "Validée", "contacted": "Contactée", "rejected": "Écartée", "blacklisted": "Liste noire",
}
EVENT_FR = {
    "applied": "Candidature envoyée", "followup_sent": "Relance envoyée", "manual_followup": "Relance à faire",
    "reply": "Réponse", "interview": "Entretien", "rejected": "Refus", "bounce": "Adresse invalide",
    "note": "Note", "status_change": "Changement de statut", "retry_after_bounce": "Nouvel essai (autre adresse)",
}
CONTACT_KIND_FR = {
    "recruitment": "Recrutement", "hr": "RH (nommé)", "tech": "Responsable technique", "leader": "Dirigeant",
    "generic": "Adresse générale", "named": "Personne nommée",
}
METHOD_FR: dict[ApplyMethod, str] = {
    ApplyMethod.LINKEDIN_EASY_APPLY: "LinkedIn (simplifiée)", ApplyMethod.HELLOWORK: "HelloWork",
    ApplyMethod.LBA: "La Bonne Alternance", ApplyMethod.EMAIL: "E-mail",
    ApplyMethod.EXTERNAL: "Site de l'employeur", ApplyMethod.MANUAL: "Manuelle",
}
FIELD_FR = {
    "new": "Nouvelles", "updated": "Mises à jour", "scored": "Notées", "shortlisted": "Retenues",
    "filtered_out": "Écartées", "prepared": "Dossiers préparés", "failed": "Échecs", "skipped": "Ignorées",
    "applied": "Envoyées", "needs_manual": "À finir à la main", "pending_review": "À valider",
    "found": "Trouvées", "processed": "Traitées", "with_email": "Avec e-mail", "without_email": "Sans e-mail",
    "sent": "Envoyées", "scanned": "Messages lus", "matched": "Rattachés à une candidature",
    "manual": "À faire à la main", "closed": "Closes",
}
LIST_FIELDS = ("details", "updates", "errors")


def label(mapping: dict[Any, str], value: Any) -> str:
    return mapping.get(value, str(getattr(value, "value", value)))


# --------------------------------------------------------------------------- console & logging


def make_console(*, stderr: bool = False) -> Console:
    stream = sys.stderr if stderr else sys.stdout
    piped = not (hasattr(stream, "isatty") and stream.isatty())
    return Console(stderr=stderr, width=PIPED_WIDTH if piped else None, highlight=False)


def configure_stdio() -> None:
    """Never crash on a character the console encoding cannot show (scheduled task logs, cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):  # already detached / not reconfigurable
                reconfigure(errors="replace")


def setup_logging(root: Path, verbose: bool) -> None:
    """Console (rich, stderr) + rotating file ``data/logs/jobbot.log``. Idempotent."""
    from rich.logging import RichHandler

    logger = logging.getLogger("jobbot")
    teardown_logging()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler = RichHandler(console=make_console(stderr=True), show_path=False, show_time=False,
                                  markup=False, rich_tracebacks=False)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handlers: list[logging.Handler] = [console_handler]
    try:
        logs = root / _logs_dir(root)
        logs.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            logs / LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s : %(message)s"))
        file_handler.setLevel(logging.DEBUG)
        handlers.append(file_handler)
    except OSError as exc:
        log.debug("Journal fichier indisponible : %s", exc)
    for handler in handlers:
        handler._jobbot_cli = True  # type: ignore[attr-defined]
        logger.addHandler(handler)


def _logs_dir(root: Path) -> str:
    from jobbot.config import load_settings

    try:
        return load_settings(root).paths.logs_dir
    except Exception:  # invalid settings: the command reports it, logging still works
        return "data/logs"


def teardown_logging() -> None:
    logger = logging.getLogger("jobbot")
    for handler in list(logger.handlers):
        if getattr(handler, "_jobbot_cli", False):
            logger.removeHandler(handler)
            handler.close()


# --------------------------------------------------------------------------- state & context


def resolve_root(option: Path | None) -> Path:
    if option is not None:
        return option.resolve()
    env = os.environ.get("JOBBOT_ROOT", "").strip()
    return Path(env).resolve() if env else find_root()


def build_app_context(root: Path, dry_run: bool) -> AppContext:
    """AppContext; a missing LLM (claude CLI absent…) degrades to the deterministic fallbacks."""
    from jobbot.config import load_secrets, load_settings
    from jobbot.llm import DisabledLLM, LLMError, build_llm

    try:
        llm = build_llm(load_settings(root).llm, load_secrets(root))
    except LLMError as exc:
        log.warning("LLM indisponible (%s) : textes générés par les modèles de secours.", exc)
        llm = DisabledLLM()
    return AppContext.build(root, dry_run=dry_run, llm=llm)


@dataclass
class State:
    root: Path
    dry_run: bool = False
    verbose: bool = False
    console: Console = field(default_factory=make_console)
    _app: AppContext | None = None

    @property
    def app(self) -> AppContext:
        """Lazily built AppContext (config errors → French message + exit code 1)."""
        if self._app is None:
            from pydantic import ValidationError

            from jobbot.config import ConfigError

            try:
                self._app = build_app_context(self.root, self.dry_run)
            except (ConfigError, ValidationError) as exc:
                fail(self.console, f"Configuration invalide : {exc}")
        return self._app  # type: ignore[return-value]


def state_of(ctx: typer.Context) -> State:
    obj = ctx.obj
    if not isinstance(obj, State):
        obj = State(root=resolve_root(None))
        ctx.obj = obj
    return obj


def fail(console: Console, message: str, code: int = 1) -> None:
    console.print(f"[bold red]Erreur :[/] {escape(message)}")
    raise typer.Exit(code)


def dry_banner(state: State) -> None:
    if state.dry_run:
        state.console.print("[yellow]Mode simulation (--dry-run) : aucune candidature ni aucun e-mail envoyé.[/]")


# --------------------------------------------------------------------------- report rendering


def _scalar_rows(report: BaseModel) -> list[tuple[str, str]]:
    return [(FIELD_FR.get(name, name), str(value)) for name, value in report
            if not isinstance(value, (list, dict, BaseModel))]


def _detail_lines(report: BaseModel) -> list[str]:
    lines: list[str] = []
    per_source = getattr(report, "per_source", None)
    if isinstance(per_source, dict) and per_source:
        lines.append("Par source : " + ", ".join(f"{k} {v}" for k, v in sorted(per_source.items())))
    for name in LIST_FIELDS:
        value = getattr(report, name, None)
        if isinstance(value, dict):
            lines.extend(f"{k} : {v}" for k, v in value.items())
        elif isinstance(value, list):
            lines.extend(str(v) for v in value)
    return lines


def print_report(console: Console, title: str, report: Any) -> None:
    """Counters as a table + detail lines (capped). Strings are step errors."""
    if report is None:
        return
    if isinstance(report, str):
        console.print(f"[bold]{escape(title)}[/] : [red]{escape(report)}[/]")
        return
    table = Table(title=title, show_header=False, title_justify="left")
    table.add_column("Indicateur")
    table.add_column("Valeur", justify="right")
    for name, value in _scalar_rows(report):
        table.add_row(name, value)
    console.print(table)
    lines = _detail_lines(report)
    for line in lines[:MAX_DETAIL_LINES]:
        console.print(f"  • {escape(line)}")
    if len(lines) > MAX_DETAIL_LINES:
        console.print(f"  … et {len(lines) - MAX_DETAIL_LINES} autre(s) ligne(s)")
