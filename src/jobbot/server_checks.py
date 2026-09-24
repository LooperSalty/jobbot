"""« jobbot doctor » on Linux (headless server): virtual screen, crontab lines, imported sessions, local settings.

Never shows a secret: session files are summarised (site, age, cookie count), never printed.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jobbot.cli_admin import Check
from jobbot.config import LOCAL_SETTINGS_FILE


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def check_xvfb(settings: Any, which: Callable[[str], str | None] = shutil.which) -> Check:
    if which("xvfb-run"):
        return Check("Écran virtuel (xvfb-run)", "ok", "présent")
    if settings.headless:
        return Check("Écran virtuel (xvfb-run)", "ok", "absent (inutile : headless: true)")
    return Check("Écran virtuel (xvfb-run)", "warn",
                 "absent alors que headless: false : sudo apt-get install -y xvfb, ou headless: true dans "
                 f"{LOCAL_SETTINGS_FILE}")


def check_crontab(settings: Any, runner: Any = None, which: Callable[[str], str | None] = shutil.which) -> Check:
    from jobbot import cron

    if runner is None and which("crontab") is None:
        return Check("Tâches cron", "warn", "crontab introuvable : sudo apt-get install -y cron")
    try:
        current = cron.read_crontab(runner or cron.run_crontab)
    except (OSError, RuntimeError) as exc:
        return Check("Tâches cron", "warn", f"crontab illisible : {str(exc)[:160]}")
    installed = {cron.managed_name(line) for line in current.splitlines()} - {None}
    wanted = [n for n in cron.JOB_NAMES if n != "classroom" or settings.classroom.enabled]
    missing = [n for n in wanted if n not in installed]
    if not missing:
        return Check("Tâches cron", "ok", f"{len(wanted)} tâche(s) jobbot : {', '.join(wanted)}")
    return Check("Tâches cron", "warn", f"manquante(s) : {', '.join(missing)} — jobbot schedule install")


def check_sessions(root: Path, settings: Any, repo: Any) -> list[Check]:
    from jobbot.sessions import MAX_AGE_DAYS, session_status

    infos = session_status(root, settings, repo)
    if not infos:
        return [Check("Sessions importées", "warn", "aucune dans data/sessions/ : sur le PC, « jobbot login … » puis "
                                                    "« jobbot session export all » et déploiement --only private")]
    checks = []
    for info in infos:
        name = f"Session {info.site}"
        if info.cookies == 0:
            checks.append(Check(name, "warn", "vide ou expirée : refais « jobbot login » + « session export » sur "
                                              "le PC"))
        elif info.age_days > MAX_AGE_DAYS:
            checks.append(Check(name, "warn", f"exportée il y a {info.age_days} jours : si la connexion échoue, "
                                              "exporte-la de nouveau depuis le PC"))
        else:
            state = "à importer à la prochaine ouverture du navigateur" if info.pending else "importée"
            checks.append(Check(name, "ok", f"{info.cookies} cookie(s), exportée il y a {info.age_days} jour(s), "
                                            f"{state}"))
    return checks


def check_local_settings(root: Path) -> Check:
    if (root / LOCAL_SETTINGS_FILE).is_file():
        return Check(LOCAL_SETTINGS_FILE, "ok", "présent (réglages propres à cette machine, prioritaires)")
    return Check(LOCAL_SETTINGS_FILE, "warn", "absent : sur un serveur, mets-y headless: true et "
                                              "browser_channel: null (le déploiement le crée)")


def check_timezone() -> Check:
    from jobbot.cron import timezone_note

    note = timezone_note()
    return Check("Fuseau horaire", "warn", note) if note else Check("Fuseau horaire", "ok", "heure de Paris")


def linux_checks(root: Path, settings: Any, repo: Any, *, runner: Any = None,
                 which: Callable[[str], str | None] = shutil.which) -> list[Check]:
    return [check_xvfb(settings, which), check_crontab(settings, runner, which), *check_sessions(root, settings, repo),
            check_local_settings(root), check_timezone()]
