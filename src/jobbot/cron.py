"""Linux scheduling (« jobbot schedule install|remove|show » on a server): jobbot lines of the user's crontab.

Every managed line ends with the marker ``# jobbot:<name>``; every other line of the crontab is kept as is.

| name        | when                                                        | command                                  |
|-------------|-------------------------------------------------------------|------------------------------------------|
| ``run``       | daily at --matin (09:30)                                    | ``jobbot run``                           |
| ``soir``      | daily at --soir (18:00)                                     | ``jobbot run --step inbox --step followup`` |
| ``classroom`` | every ``classroom.poll_minutes`` within ``classroom.active_hours`` | ``jobbot classroom watch --once``   |
| ``night``     | daily at --nuit (00:30), when ``night.enabled``             | ``jobbot night``                         |

Line: ``cd <root> && PATH=… flock -n /tmp/jobbot-<name>.lock [xvfb-run -a] <root>/.venv/bin/jobbot <cmd> >>
<root>/data/logs/cron.log 2>&1 # jobbot:<name>`` — ``xvfb-run -a`` only when available and ``settings.headless``
is false (virtual screen for a visible browser). The night job never opens a visible browser and runs with a low
CPU / disk priority (``nice -n 10 ionice -c3`` when available). Cron uses the server's local time: it should be
Europe/Paris. The watcher checks ``classroom.active_hours`` itself, so the hour range of its line may be approximate.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jobbot import clock

log = logging.getLogger(__name__)

MARKER = "# jobbot:"
JOB_NAMES: tuple[str, ...] = ("run", "soir", "classroom", "night")
COMMANDS = {"run": "run", "soir": "run --step inbox --step followup", "classroom": "classroom watch --once",
            "night": "night"}
DEFAULT_TIMES = {"run": "09:30", "soir": "18:00", "night": "00:30"}
LOW_PRIORITY = "nice -n 10 ionice -c3 "  # night job: idle CPU and disk only
LOCK_DIR = "/tmp"
CRON_LOG = "cron.log"
CRON_TIMEOUT_S = 30
_MARKER_RE = re.compile(r"\s#\s?jobbot:([a-z]+)\s*$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


@dataclass(frozen=True)
class CronJob:
    name: str
    schedule: str
    command: str

    def line(self) -> str:
        return f"{self.schedule} {self.command} {MARKER}{self.name}"


def scheduler_kind() -> str | None:
    """'windows' (Task Scheduler), 'cron' (Linux) or None (unsupported)."""
    if sys.platform == "win32":
        return "windows"
    return "cron" if sys.platform.startswith("linux") else None


def run_crontab(cmd: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=CRON_TIMEOUT_S, check=False)


# --------------------------------------------------------------------------- building the lines


def daily_schedule(hhmm: str) -> str:
    match = _TIME_RE.match(hhmm.strip())
    if not match:
        raise ValueError(f"Heure invalide : {hhmm} (format HH:MM)")
    return f"{int(match[2])} {int(match[1])} * * *"


def classroom_schedule(poll_minutes: int, active_hours: str) -> str:
    """Cron fields for « every poll_minutes minutes within active_hours » (hours inclusive; approximate)."""
    from jobbot.config import parse_active_hours

    start, end = parse_active_hours(active_hours)
    wraps = start > end
    if wraps:
        hours = f"{start.hour}-23,0-{end.hour}"
    elif start.hour == end.hour:
        hours = str(start.hour)
    else:
        hours = f"{start.hour}-{end.hour}"
    if poll_minutes < 60:
        return f"{'*' if poll_minutes == 1 else f'*/{poll_minutes}'} {hours} * * *"
    step = max(1, poll_minutes // 60)
    if step == 1:
        return f"{start.minute} {hours} * * *"
    return f"{start.minute} {'*' if wraps else hours}/{step} * * *"


def jobbot_executable(root: Path) -> str:
    """<root>/.venv/bin/jobbot when present, else the current interpreter with ``-m jobbot.cli``."""
    exe = root / ".venv" / "bin" / "jobbot"
    return shlex.quote(exe.as_posix()) if exe.exists() else f"{shlex.quote(sys.executable)} -m jobbot.cli"


def cron_path(env_path: str | None = None, home: Path | None = None) -> str:
    """PATH for cron (minimal by default): ~/.local/bin first (uv, claude), then the current PATH."""
    entries = [((home or Path.home()) / ".local" / "bin").as_posix(),
               *(env_path if env_path is not None else os.environ.get("PATH", "")).split(":"),
               "/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(e for e in entries if e.startswith("/") and "%" not in e))


def job_command(root: Path, name: str, *, xvfb: bool, exe: str, path_env: str, logs: Path,
                low_priority: str = "") -> str:
    wrapper = f"{low_priority}{'xvfb-run -a ' if xvfb else ''}"
    log_file = shlex.quote((logs / CRON_LOG).as_posix())
    command = (f"cd {shlex.quote(root.as_posix())} && PATH={shlex.quote(path_env)} "
               f"flock -n {LOCK_DIR}/jobbot-{name}.lock {wrapper}{exe} {COMMANDS[name]} >> {log_file} 2>&1")
    return command.replace("%", r"\%")  # « % » means newline in a crontab line


def low_priority_prefix(which: Callable[[str], str | None] = shutil.which) -> str:
    """``nice -n 10 ionice -c3 `` when both tools exist (Debian: coreutils + util-linux), else ``nice`` alone."""
    if which("nice") and which("ionice"):
        return LOW_PRIORITY
    return "nice -n 10 " if which("nice") else ""


def build_jobs(root: Path, settings: Any, times: dict[str, str] | None = None, *, xvfb: bool = False,
               exe: str | None = None, path_env: str | None = None, low_priority: str = "") -> list[CronJob]:
    at = {**DEFAULT_TIMES, **{k: v for k, v in (times or {}).items() if v}}
    logs = root / settings.paths.logs_dir
    options = dict(xvfb=xvfb, exe=exe or jobbot_executable(root), path_env=path_env or cron_path(), logs=logs)
    jobs = [CronJob(name, daily_schedule(at[name]), job_command(root, name, **options)) for name in ("run", "soir")]
    cfg = settings.classroom
    if cfg.enabled:
        jobs.append(CronJob("classroom", classroom_schedule(cfg.poll_minutes, cfg.active_hours),
                            job_command(root, "classroom", **options)))
    if settings.night.enabled:
        night = {**options, "xvfb": False, "low_priority": low_priority}
        jobs.append(CronJob("night", daily_schedule(at["night"]), job_command(root, "night", **night)))
    return jobs


# --------------------------------------------------------------------------- crontab text


def managed_name(line: str) -> str | None:
    match = _MARKER_RE.search(line)
    return match[1] if match and not line.lstrip().startswith("#") else None


def without_jobbot(text: str) -> list[str]:
    """Every line of the crontab that is not a jobbot line (untouched)."""
    return [line for line in text.splitlines() if managed_name(line) is None]


def merged_crontab(text: str, jobs: list[CronJob]) -> str:
    kept = without_jobbot(text)
    while kept and not kept[-1].strip():
        kept.pop()
    lines = [*kept, *(job.line() for job in jobs)]
    return "\n".join(lines) + "\n" if lines else ""


def read_crontab(runner: Runner) -> str:
    proc = runner(["crontab", "-l"], None)
    if proc.returncode == 0:
        return proc.stdout or ""
    if "no crontab" in (proc.stderr or "").lower():
        return ""
    raise RuntimeError(f"crontab -l a échoué (code {proc.returncode}) : {(proc.stderr or '').strip()[:200]}")


def write_crontab(runner: Runner, text: str) -> None:
    proc = runner(["crontab", "-"], text)
    if proc.returncode != 0:
        raise RuntimeError(f"crontab - a échoué (code {proc.returncode}) : {(proc.stderr or '').strip()[:200]}")


def timezone_note(now: datetime | None = None) -> str | None:
    """French warning when the machine's local time is not Paris time (cron runs at local time)."""
    moment = now or datetime.now().astimezone()
    if moment.utcoffset() == clock.to_paris(moment).utcoffset():
        return None
    return ("Attention : le fuseau horaire de cette machine n'est pas celui de Paris ; cron suit l'heure locale "
            "(sudo timedatectl set-timezone Europe/Paris).")


# --------------------------------------------------------------------------- actions


def _settings(root: Path) -> Any:
    from jobbot.config import Settings, load_settings

    try:
        return load_settings(root)
    except Exception as exc:  # invalid settings: « jobbot doctor » reports it
        log.warning("Réglages illisibles (%s) : tâches cron avec les valeurs par défaut.", exc)
        return Settings()


def _describe(job: CronJob) -> str:
    return f"{job.name} : {job.schedule} → jobbot {COMMANDS[job.name]}"


def _install(root: Path, runner: Runner, current: str, *, dry_run: bool,
             times: dict[str, str] | None) -> list[str]:
    settings = _settings(root)
    xvfb = not settings.headless and shutil.which("xvfb-run") is not None
    jobs = build_jobs(root, settings, times, xvfb=xvfb, low_priority=low_priority_prefix())
    lines = [_describe(job) for job in jobs]
    if not settings.headless and not xvfb:
        lines.append("xvfb-run absent : navigateur sans écran conseillé (headless: true dans "
                     "config/settings.local.yaml) ou sudo apt-get install xvfb")
    note = timezone_note()
    if note:
        lines.append(note)
    new = merged_crontab(current, jobs)
    if dry_run:
        return ["[simulation] crontab après installation :", *new.splitlines(), *lines]
    (root / settings.paths.logs_dir).mkdir(parents=True, exist_ok=True)
    if new != current:
        write_crontab(runner, new)
    kept = len([line for line in without_jobbot(current) if line.strip() and not line.lstrip().startswith("#")])
    return [f"Crontab : {len(jobs)} tâche(s) jobbot installée(s) ({kept} autre(s) ligne(s) conservée(s))", *lines]


def _remove(runner: Runner, current: str, *, dry_run: bool) -> list[str]:
    found = [managed_name(line) for line in current.splitlines() if managed_name(line)]
    if not found:
        return ["Crontab : aucune tâche jobbot à supprimer"]
    if dry_run:
        return [f"[simulation] suppression de : {', '.join(found)}"]
    write_crontab(runner, merged_crontab(current, []))
    return [f"Crontab : {len(found)} tâche(s) jobbot supprimée(s) ({', '.join(found)}), autres lignes conservées"]


def _show(current: str, names: tuple[str, ...] = JOB_NAMES) -> list[str]:
    installed = {managed_name(line): line for line in current.splitlines() if managed_name(line)}
    lines = []
    for name in names:
        line = installed.get(name)
        if line is None:
            lines.append(f"{name} : non installée (lance « jobbot schedule install »)")
        else:
            lines.append(f"{name} : installée — {' '.join(line.split()[:5])} → jobbot {COMMANDS[name]}")
    return lines


def run_cron(root: Path, action: str, *, dry_run: bool, times: dict[str, str] | None = None,
             runner: Runner | None = None) -> list[str]:
    """install / remove / show the jobbot lines of the user's crontab; French result lines."""
    if runner is None and shutil.which("crontab") is None:
        return ["crontab introuvable : installe cron (sudo apt-get install -y cron)"]
    run = runner or run_crontab
    try:
        current = read_crontab(run)
        if action == "show":
            return _show(current)
        if action == "remove":
            return _remove(run, current, dry_run=dry_run)
        return _install(root, run, current, dry_run=dry_run, times=times)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        return [f"Crontab : échec ({exc})"]
