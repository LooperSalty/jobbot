"""User notifications: log line + Windows toast (best effort) + optional Jarvis TTS. Never raises."""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobbot.context import AppContext

log = logging.getLogger(__name__)

TOAST_TIMEOUT_S = 10
JARVIS_TIMEOUT_S = 15
MAX_TITLE_CHARS = 80
MAX_MESSAGE_CHARS = 300
# AppUserModelID of Windows PowerShell: toasts shown under its name without registering an app.
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# Title/message travel through environment variables and are XML-escaped inside PowerShell:
# no user text is ever interpolated into the script itself.
_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [System.Security.SecurityElement]::Escape($env:JOBBOT_TOAST_TITLE)
$m = [System.Security.SecurityElement]::Escape($env:JOBBOT_TOAST_MESSAGE)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$body = "<text>$t</text><text>$m</text>"
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'>$body</binding></visual></toast>")
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:JOBBOT_TOAST_APPID).Show($toast)
"""


def _clean(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _side_effects_enabled() -> bool:
    """No toast / voice while tests run or when JOBBOT_NO_TOAST is set."""
    if os.environ.get("JOBBOT_NO_TOAST", "").strip() not in ("", "0"):
        return False
    return "PYTEST_CURRENT_TEST" not in os.environ


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _toast(title: str, message: str) -> None:
    if sys.platform != "win32":
        return
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    if not exe:
        log.debug("PowerShell introuvable : pas de notification Windows")
        return
    env = {**os.environ, "JOBBOT_TOAST_TITLE": title, "JOBBOT_TOAST_MESSAGE": message,
           "JOBBOT_TOAST_APPID": TOAST_APP_ID}
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand",
             _encoded(_TOAST_SCRIPT)],
            env=env, capture_output=True, timeout=TOAST_TIMEOUT_S, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("Notification Windows impossible : %s", exc)
        return
    if proc.returncode != 0:
        log.debug("Notification Windows refusée (code %s)", proc.returncode)


def _jarvis(ctx: AppContext, message: str) -> None:
    settings = ctx.settings
    if not (settings.notify_jarvis and settings.jarvis_script):
        return
    script = Path(settings.jarvis_script)
    if not script.is_file():
        log.debug("Script Jarvis introuvable : %s", script)
        return
    try:
        subprocess.run([sys.executable, str(script), "--type", "tell", message], timeout=JARVIS_TIMEOUT_S,
                       capture_output=True, check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("Jarvis injoignable : %s", exc)


def notify(ctx: AppContext, title: str, message: str) -> None:
    """Tell the user something important happened (reply, interview...). Best effort, never raises."""
    try:
        clean_title = _clean(title, MAX_TITLE_CHARS) or "jobbot"
        clean_message = _clean(message, MAX_MESSAGE_CHARS)
        log.info("Notification : %s — %s", clean_title, clean_message)
        if not _side_effects_enabled():
            return
        _toast(clean_title, clean_message)
        _jarvis(ctx, clean_message)
    except Exception:  # a notification must never break the pipeline
        log.debug("Notification échouée", exc_info=True)
