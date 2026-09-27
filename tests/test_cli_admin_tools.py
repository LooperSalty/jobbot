"""jobbot doctor (offline Gmail app-password format check) and « schedule show » when no task is installed."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from jobbot import cli_admin
from jobbot.config import Secrets


def _secrets(**values: str) -> Secrets:
    return Secrets(_env_file=None, **values)


def test_gmail_app_password_valid_format():
    check = cli_admin.check_gmail_app_password(_secrets(smtp_user="moi@gmail.com", smtp_password="abcd efgh ijkl mnop"))
    assert check is not None
    assert check.level == "ok"
    assert "abcd" not in check.detail


@pytest.mark.parametrize("password", ["abcdefghijklmno", "abcd efgh ijkl mnopq", "abcd1234efgh5678"])
def test_gmail_app_password_bad_format_warns(password):
    check = cli_admin.check_gmail_app_password(_secrets(smtp_user="moi@gmail.com", smtp_password=password))
    assert check is not None
    assert check.level == "warn"
    assert "16 lettres" in check.detail
    assert password.replace(" ", "") not in check.detail


def test_gmail_app_password_skipped_when_absent_or_not_gmail():
    assert cli_admin.check_gmail_app_password(_secrets(smtp_user="moi@gmail.com")) is None
    other = _secrets(smtp_user="moi@exemple.fr", smtp_password="secret", smtp_host="smtp.exemple.fr")
    assert cli_admin.check_gmail_app_password(other) is None


def test_doctor_reports_gmail_format(ctx, project_root, monkeypatch):
    monkeypatch.setattr(cli_admin, "chromium_path", lambda: "chromium.exe")
    monkeypatch.setattr(cli_admin, "chrome_installed", lambda: False)
    monkeypatch.setattr(cli_admin, "check_sources", lambda c: [])
    app = dataclasses.replace(ctx, secrets=_secrets(smtp_user="moi@gmail.com", smtp_password="abcdefghijklmno"))
    checks = cli_admin.run_doctor(SimpleNamespace(root=project_root, app=app))
    gmail = [c for c in checks if c.name == "Mot de passe d'application Gmail"]
    assert [c.level for c in gmail] == ["warn"]
    assert all("abcdefghijklmno" not in c.detail for c in checks)


def test_schedule_show_reports_missing_tasks(tmp_path, monkeypatch):
    import subprocess

    from jobbot import cli_tools

    def missing(cmd):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ERREUR : Le fichier spécifié est introuvable.")

    monkeypatch.setattr(cli_tools, "run_command", missing)
    lines = cli_tools.run_schedule(tmp_path, "show", dry_run=False)
    names = [*cli_tools.TASKS, cli_tools.NIGHT_TASK, cli_tools.CLASSROOM_TASK]
    assert lines == [f"{name} : non installée (lance « jobbot schedule install »)" for name in names]
