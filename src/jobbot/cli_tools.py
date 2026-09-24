"""CLI tools: status tables, ATS view, assisted applications, export (xlsx/csv), Windows scheduled tasks,
dashboard launcher."""

from __future__ import annotations

import codecs
import csv
import importlib.util
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jobbot import clock
from jobbot.cli_support import APP_STATUS_FR, COMPANY_STATUS_FR, JOB_STATUS_FR, METHOD_FR, label
from jobbot.context import AppContext
from jobbot.models import ApplicationStatus, JobOffer, JobStatus

log = logging.getLogger(__name__)

TASKS = {"jobbot-matin": ("09:30", "run"), "jobbot-soir": ("18:00", "evening")}
CLASSROOM_TASK = "jobbot-classroom"  # « jobbot classroom watch --once » every classroom.poll_minutes minutes
SCHTASKS_TIMEOUT_S = 30
DASHBOARD_APP = Path(__file__).resolve().parent / "dashboard" / "app.py"
QUOTA_CHANNELS = ("linkedin", "hellowork", "lba", "email", "spontaneous", "followup")
CSV_DELIMITER = ";"  # French Excel


# --------------------------------------------------------------------------- tables


def jobs_table(jobs: list[JobOffer], title: str) -> Table:
    table = Table(title=title, title_justify="left")
    for column in ("Id", "Score", "Offre", "Entreprise", "Lieu", "Canal", "Statut"):
        table.add_column(column)
    for job in jobs:
        score = f"{job.match_score:.0f}" if job.match_score is not None else "—"
        table.add_row(job.id, score, escape(job.title[:60]), escape(job.company[:30]), escape(job.location[:25]),
                      label(METHOD_FR, job.apply_method), label(JOB_STATUS_FR, job.status))
    return table


def _counts_table(title: str, counts: dict[str, int], labels: dict[Any, str], cast: Callable[[str], Any]) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column("Statut")
    table.add_column("Nombre", justify="right")
    for key, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        try:
            name = label(labels, cast(key))
        except ValueError:
            name = key
        table.add_row(name, str(count))
    return table


def status_tables(ctx: AppContext) -> list[Table]:
    from jobbot.outreach.selection import CHANNEL as SPONTANEOUS
    from jobbot.outreach.selection import UNVERIFIED_ACTION
    from jobbot.pacing import Pacer
    from jobbot.preferences import effective_mode

    stats = ctx.repo.stats()
    tables = [
        _counts_table("Offres", stats["jobs_by_status"], JOB_STATUS_FR, JobStatus),
        _counts_table("Candidatures", stats["applications_by_status"], APP_STATUS_FR, ApplicationStatus),
        _counts_table("Entreprises (spontanées)", stats["companies_by_status"], COMPANY_STATUS_FR, str),
    ]
    pacer = Pacer(ctx.repo, ctx.settings.limits)
    quotas = Table(title=f"Aujourd'hui — mode effectif : {effective_mode(ctx.settings)}", title_justify="left")
    for column in ("Canal", "Utilisé", "Quota"):
        quotas.add_column(column)
    for channel in QUOTA_CHANNELS:
        quotas.add_row(channel, str(pacer.used_today(channel)), str(pacer.quota(channel)))
    guessed = ctx.repo.count_actions_today(SPONTANEOUS, UNVERIFIED_ACTION)
    quotas.add_row("adresses devinées", str(guessed), str(ctx.settings.outreach.max_unverified_per_day))
    quotas.add_row("relances dues", str(len(ctx.repo.due_followups())), "")
    quotas.add_row("e-mails envoyés (total)", str(stats["emails_sent"]), "")
    tables.append(quotas)
    return tables


def ats_lines(ctx: AppContext, job_id: str, cv_path: Path | None = None) -> list[str]:
    """French explanation of the ATS score (latest documents, or an arbitrary CV PDF against the offer)."""
    job = ctx.repo.get_job(job_id)
    if job is None:
        return [f"Offre {job_id} introuvable."]
    if cv_path is not None:
        from jobbot.documents.render import pdf_text
        from jobbot.matching.ats import score_document
        from jobbot.matching.keywords import extract_keywords

        report = score_document(pdf_text(cv_path), extract_keywords(job, ctx.llm), job.title, kind="cv")
        return [f"{job.title} — {job.company}", *_ats_report_lines(f"CV {cv_path.name}", report)]
    docs = ctx.repo.latest_documents(job_id=job_id)
    if docs is None:
        return [f"Aucun document pour cette offre : lance « jobbot prepare --job {job_id} »."]
    lines = [f"{job.title} — {job.company}", f"CV : {docs.cv_pdf}"]
    for name, report in (("CV", docs.cv_ats), ("Lettre", docs.letter_ats)):
        if report is not None:
            lines.extend(_ats_report_lines(name, report))
    lines.extend(f"Vérité : {w}" for w in docs.truth_warnings)
    return lines


def _ats_report_lines(name: str, report: Any) -> list[str]:
    lines = [
        f"{name} : {report.score:.0f}/100 (mots-clés {report.keyword_score:.0f}, intitulé {report.title_score:.0f}, "
        f"sections {report.section_score:.0f}, format {report.format_score:.0f}) — {report.word_count} mots",
        "  Présents : " + (", ".join(k.keyword for k in report.matched) or "—"),
        "  Manquants : " + (", ".join(f"{k.keyword} ({k.weight:g})" for k in report.missing) or "—"),
    ]
    lines.extend(f"  Attention : {w}" for w in report.warnings)
    return lines


# --------------------------------------------------------------------------- assist (external applications)


def assist_loop(ctx: AppContext, console: Console, ask: Callable[[str], str], *, limit: int = 10) -> dict[str, int]:
    """Open each NEEDS_MANUAL offer pre-filled, then ask whether it was sent (o / n / passer / q)."""
    from jobbot.apply.answers import FormAnswerer
    from jobbot.apply.assisted import assisted_apply
    from jobbot.apply.browser import open_browser

    jobs = ctx.repo.list_jobs(JobStatus.NEEDS_MANUAL, limit=limit)
    counts = {"envoyées": 0, "abandonnées": 0, "passées": 0}
    if not jobs:
        console.print("Aucune candidature à finir à la main.")
        return counts
    with open_browser(ctx, headless=False) as browser:
        for job in jobs:
            docs = ctx.repo.latest_documents(job_id=job.id)
            if docs is None:
                console.print(f"[yellow]{escape(job.title)} : documents absents, ignorée.[/]")
                continue
            console.rule(escape(f"{job.company} — {job.title}"))
            result = assisted_apply(ctx, browser, job, docs, FormAnswerer(ctx.profile, ctx.llm, ctx.repo, job))
            console.print(escape(result.detail))
            if result.status == JobStatus.EXPIRED:
                ctx.repo.update_job_status(job.id, JobStatus.EXPIRED)
                continue
            if not _assist_answer(ctx, console, job, ask("Candidature envoyée ? [o/n/passer/q]"), counts):
                break
    return counts


def _assist_answer(ctx: AppContext, console: Console, job: JobOffer, answer: str, counts: dict[str, int]) -> bool:
    from jobbot.review import record_manual_application, reject_job

    word = (answer or "").strip().lower()
    if word in ("q", "quitter"):
        return False
    if word in ("o", "oui"):
        if ctx.dry_run:
            console.print(escape("[simulation] candidature non enregistrée."), style="yellow")
        else:
            app = record_manual_application(ctx, job, detail="Envoyée à la main (jobbot assist).")
            console.print(f"[green]Enregistrée[/] (relance prévue le {_fmt(app.next_followup_at)}).")
        counts["envoyées"] += 1
    elif word in ("n", "non"):
        if not ctx.dry_run:
            reject_job(ctx, job.id, "Abandonnée pendant « jobbot assist ».")
        counts["abandonnées"] += 1
    else:
        counts["passées"] += 1
    return True


def _fmt(dt: Any) -> str:
    return clock.to_paris(dt).strftime("%d/%m/%Y") if dt else "—"


# --------------------------------------------------------------------------- export


def export_rows(ctx: AppContext) -> dict[str, list[dict[str, Any]]]:
    jobs = []
    for job in ctx.repo.list_jobs():
        docs = ctx.repo.latest_documents(job_id=job.id)
        jobs.append({
            "id": job.id, "source": job.source, "offre": job.title, "entreprise": job.company, "lieu": job.location,
            "contrat": job.contract_type.value, "canal": label(METHOD_FR, job.apply_method),
            "statut": label(JOB_STATUS_FR, job.status), "score": job.match_score,
            "ats_cv": docs.cv_ats.score if docs and docs.cv_ats else None, "lien": job.url,
            "lien_candidature": job.apply_url or "", "trouvée_le": _fmt(job.found_at),
        })
    apps = [{
        "id": a.id, "entreprise": a.company_name, "poste": a.job_title, "canal": label(METHOD_FR, a.method),
        "statut": label(APP_STATUS_FR, a.status), "envoyée_le": _fmt(a.applied_at), "email": a.email_to or "",
        "relances": a.followup_count, "prochaine_relance": _fmt(a.next_followup_at),
        "dernière_réponse": _fmt(a.last_response_at),
    } for a in ctx.repo.list_applications()]
    companies = [{
        "nom": c.name, "siren": c.siren or "", "ville": c.city, "naf": c.naf_label or c.naf_code or "",
        "statut": label(COMPANY_STATUS_FR, c.status), "site": c.website or c.domain or "",
        "contact": c.contacts[0].email if c.contacts else "",
    } for c in ctx.repo.list_companies()]
    return {"offres": jobs, "candidatures": apps, "entreprises": companies}


def export_data(ctx: AppContext, fmt: str, out: Path | None = None) -> list[Path]:
    """Write every table to one .xlsx (one sheet each) or to one .csv per table (``;``, UTF-8 BOM)."""
    rows = export_rows(ctx)
    stamp = clock.to_paris(clock.now()).strftime("%Y%m%d-%H%M")
    if fmt == "xlsx":
        path = out or ctx.output_dir / f"export-jobbot-{stamp}.xlsx"
        return [_write_xlsx(rows, path)]
    folder = out or ctx.output_dir / f"export-jobbot-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    return [_write_csv(table, folder / f"{name}.csv") for name, table in rows.items()]


def _write_xlsx(rows: dict[str, list[dict[str, Any]]], path: Path) -> Path:
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for name, table in rows.items():
        sheet = book.create_sheet(name)
        headers = list(table[0]) if table else ["(vide)"]
        sheet.append(headers)
        for row in table:
            sheet.append([row.get(h) for h in headers])
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


def _write_csv(table: list[dict[str, Any]], path: Path) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        headers = list(table[0]) if table else ["(vide)"]
        writer = csv.DictWriter(handle, fieldnames=headers, delimiter=CSV_DELIMITER)
        writer.writeheader()
        writer.writerows(table)
    return path


# --------------------------------------------------------------------------- Windows scheduled tasks


def _encoding(cmd: list[str]) -> str:
    """schtasks prints in the OEM code page; install_task.ps1 in UTF-8."""
    if cmd[0] != "schtasks":
        return "utf-8"
    try:
        codecs.lookup("oem")
        return "oem"
    except LookupError:  # not Windows
        return "utf-8"


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, encoding=_encoding(cmd), errors="replace",
                          timeout=SCHTASKS_TIMEOUT_S, check=False)


def _classroom_schedule(root: Path) -> tuple[bool, int]:
    """(watcher enabled, minutes between two checks) from settings.yaml (defaults when unreadable)."""
    from jobbot.config import ClassroomSettings, load_settings

    try:
        cfg = load_settings(root).classroom
    except Exception as exc:  # invalid settings: « jobbot doctor » reports it
        log.warning("Réglages illisibles (%s) : tâche Classroom avec les valeurs par défaut.", exc)
        cfg = ClassroomSettings()
    return cfg.enabled, cfg.poll_minutes


def classroom_python(root: Path) -> Path:
    """pythonw.exe of the project venv (no console window every few minutes), else the current Python."""
    venv = root / ".venv" / "Scripts"
    candidates = (venv / "pythonw.exe", Path(sys.executable).with_name("pythonw.exe"), venv / "python.exe")
    return next((c for c in candidates if c.exists()), Path(sys.executable))


def classroom_task_command(root: Path) -> str:
    return f'"{classroom_python(root)}" -m jobbot.cli --root "{root}" classroom watch --once'


def classroom_commands(root: Path, action: str) -> list[list[str]]:
    """schtasks commands of the « jobbot-classroom » task (every classroom.poll_minutes minutes)."""
    if action == "show":
        return [["schtasks", "/Query", "/FO", "LIST", "/TN", CLASSROOM_TASK]]
    if action == "remove":
        return [["schtasks", "/Delete", "/F", "/TN", CLASSROOM_TASK]]
    enabled, minutes = _classroom_schedule(root)
    if not enabled:
        return []
    return [["schtasks", "/Create", "/F", "/TN", CLASSROOM_TASK, "/SC", "MINUTE", "/MO", str(minutes),
             "/TR", classroom_task_command(root)]]


def schedule_commands(root: Path, action: str, times: dict[str, str] | None = None) -> list[list[str]]:
    """install/remove → scripts/install_task.ps1 (Register-ScheduledTask, catch-up when the PC was off) + the
    Classroom watcher task (schtasks /SC MINUTE); show → schtasks /Query."""
    if action == "show":
        return [*(["schtasks", "/Query", "/FO", "LIST", "/TN", name] for name in TASKS),
                *classroom_commands(root, "show")]
    script = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
              str(root / "scripts" / "install_task.ps1")]
    if action == "remove":
        return [*classroom_commands(root, "remove"), [*script, "-Supprimer"]]
    at = {name: (times or {}).get(name, default) for name, (default, _) in TASKS.items()}
    return [[*script, "-Matin", at["jobbot-matin"], "-Soir", at["jobbot-soir"]], *classroom_commands(root, action)]


def _result_line(action: str, name: str, cmd: list[str], proc: subprocess.CompletedProcess[str]) -> str:
    if action == "show" and proc.returncode != 0:  # schtasks: « le fichier spécifié est introuvable »
        return f"{name} : non installée (lance « jobbot schedule install »)"
    if "/Delete" in cmd and proc.returncode != 0:
        return f"{name} : absente"
    text = (proc.stdout or proc.stderr or "").strip()
    state = "OK" if proc.returncode == 0 else f"échec (code {proc.returncode})"
    return f"{name} : {state}" + (f"\n{text}" if text else "")


def run_schedule(root: Path, action: str, *, dry_run: bool, times: dict[str, str] | None = None) -> list[str]:
    """Execute (or, in dry-run, only show) the commands; returns French result lines."""
    lines: list[str] = []
    for cmd in schedule_commands(root, action, times):
        name = cmd[cmd.index("/TN") + 1] if "/TN" in cmd else "Tâches jobbot"
        if dry_run:
            lines.append(f"[simulation] {' '.join(cmd)}")
            continue
        try:
            proc = run_command(cmd)
        except (OSError, subprocess.SubprocessError) as exc:
            lines.append(f"{name} : schtasks indisponible ({exc})")
            continue
        lines.append(_result_line(action, name, cmd, proc))
    return lines


# --------------------------------------------------------------------------- dashboard


def launch_dashboard(root: Path, port: int) -> int:
    """Run ``streamlit run dashboard/app.py`` (blocking until Ctrl+C). Returns the exit code."""
    if importlib.util.find_spec("streamlit") is None:
        raise RuntimeError("Streamlit n'est pas installé : .venv\\Scripts\\pip install -e .[dashboard]")
    env = {**os.environ, "JOBBOT_ROOT": str(root)}
    cmd = [sys.executable, "-m", "streamlit", "run", str(DASHBOARD_APP), "--server.port", str(port),
           "--browser.gatherUsageStats", "false"]
    try:
        return subprocess.run(cmd, env=env, check=False).returncode
    except KeyboardInterrupt:
        return 0
