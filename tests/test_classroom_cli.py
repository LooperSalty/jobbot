"""CLI of the Classroom feature: classroom watch / test, login classroom, schedule (jobbot-classroom), doctor."""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from jobbot import cli, cli_admin, cli_classroom, cli_support, cli_tools
from jobbot.classroom import state
from jobbot.classroom.preview import PreviewRow
from jobbot.classroom.watch import ClassroomReport

from .fixtures.classroom.fakes import COURSE_URL, with_classroom

runner = CliRunner()


@pytest.fixture
def run_cli(ctx, project_root, monkeypatch):
    holder = {"ctx": with_classroom(ctx)}
    monkeypatch.setattr(cli_support, "build_app_context", lambda root, dry_run: holder["ctx"])
    monkeypatch.chdir(project_root)

    def invoke(*args: str, app_ctx=None):
        if app_ctx is not None:
            holder["ctx"] = app_ctx
        return runner.invoke(cli.app, ["--root", str(project_root), *args])

    yield invoke
    cli_support.teardown_logging()


def _ok(res):
    assert res.exit_code == 0, res.output
    return res.output


def test_watch_once_and_loop(run_cli, monkeypatch):
    import jobbot.classroom.watch as watch

    calls = []
    monkeypatch.setattr(watch, "run_classroom", lambda c: calls.append(c) or ClassroomReport(
        posts=3, offers=2, applied=1, details=["Acme — Dev : candidature envoyée — ok"]))
    out = _ok(run_cli("--dry-run", "classroom", "watch", "--once"))
    assert "Mode simulation" in out and "Publications lues" in out and "Acme — Dev" in out and len(calls) == 1
    sleeps = []
    monkeypatch.setattr(cli_classroom, "_sleep", sleeps.append)
    out = _ok(run_cli("classroom", "watch", "--interval", "2", "--max-cycles", "2"))
    assert len(calls) == 3 and sleeps == [120] and "Prochaine vérification dans 2 min" in out

    def interrupted(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_classroom, "_sleep", interrupted)
    assert "arrêtée" in _ok(run_cli("classroom", "watch"))


def test_classroom_test_prints_table(run_cli, monkeypatch):
    import jobbot.classroom.preview as preview

    rows = [PreviewRow(post="12 sept. · Mme Claire Exemple", post_id="p-1", title="Développeur full-stack",
                       company="Acme Logiciels", level="Bac+3 à Bac+5", channel="e-mail", score=88,
                       verdict="candidature immédiate (e-mail)"),
            PreviewRow(post="10 sept. · M. Paul Exemple", post_id="p-2", title="Rappel", verdict="pas une offre")]
    monkeypatch.setattr(preview, "preview_classroom", lambda c, limit=10: (rows, ["2 publication(s) dans le flux"]))
    out = _ok(run_cli("classroom", "test", "-n", "5"))
    assert "aucune candidature envoyée" in out and "Acme Logiciels" in out and "pas une offre" in out
    assert COURSE_URL not in out

    def broken(c, limit=10):
        raise RuntimeError("navigateur absent")

    monkeypatch.setattr(preview, "preview_classroom", broken)
    res = run_cli("classroom", "test")
    assert res.exit_code == 1 and "navigateur absent" in res.output


def test_login_classroom_browser_and_api(run_cli, ctx, monkeypatch):
    import jobbot.apply.browser as browser
    import jobbot.classroom.api as api

    seen = []
    monkeypatch.setattr(browser, "login", lambda c, site, timeout_s=600: seen.append(site) or True)
    assert "Connecté à classroom" in _ok(run_cli("login", "classroom"))
    assert seen == ["classroom"]
    api_ctx = with_classroom(ctx, backend="api")
    monkeypatch.setattr(api, "authorize", lambda c: (True, "Autorisation Google Classroom enregistrée."))
    assert "Autorisation" in _ok(run_cli("login", "classroom", app_ctx=api_ctx))
    monkeypatch.setattr(api, "authorize", lambda c: (False, "Client OAuth absent"))
    res = run_cli("login", "classroom", app_ctx=api_ctx)
    assert res.exit_code == 1 and "Client OAuth absent" in res.output and seen == ["classroom"]

    def no_display(c, site, timeout_s=600):
        raise RuntimeError("Missing X server or $DISPLAY")

    monkeypatch.setattr(browser, "login", no_display)
    res = run_cli("login", "classroom", app_ctx=with_classroom(ctx))
    assert res.exit_code == 1 and "Il faut un écran" in res.output


def test_schedule_installs_the_classroom_task(run_cli, project_root, monkeypatch):
    commands: list[list[str]] = []

    def fake(cmd):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if "/Delete" in cmd else 0, stdout="SUCCÈS", stderr="")

    monkeypatch.setattr(cli_tools, "run_command", fake)
    out = _ok(run_cli("schedule", "install"))
    task = commands[-1]
    assert task[:3] == ["schtasks", "/Create", "/F"] and task[task.index("/TN") + 1] == "jobbot-classroom"
    assert task[task.index("/SC") + 1] == "MINUTE" and task[task.index("/MO") + 1] == "15"
    command = task[task.index("/TR") + 1]
    assert "classroom watch --once" in command and f'--root "{project_root}"' in command
    assert "jobbot-classroom : OK" in out
    out = _ok(run_cli("schedule", "remove"))
    assert commands[-2] == ["schtasks", "/Delete", "/F", "/TN", "jobbot-classroom"] and "absente" in out
    _ok(run_cli("schedule", "show"))
    assert commands[-1] == ["schtasks", "/Query", "/FO", "LIST", "/TN", "jobbot-classroom"]
    (project_root / "config" / "settings.yaml").write_text("classroom:\n  enabled: false\n", encoding="utf-8")
    before = len(commands)
    _ok(run_cli("schedule", "install"))
    assert all("jobbot-classroom" not in c for c in commands[before:])
    (project_root / "config" / "settings.yaml").write_text("classroom:\n  poll_minutes: 5\n", encoding="utf-8")
    assert cli_tools.classroom_commands(project_root, "install")[0][8] == "5"
    (project_root / "config" / "settings.yaml").write_text("classroom: {poll_minutes: 0}\n", encoding="utf-8")
    assert cli_tools._classroom_schedule(project_root) == (True, 15)  # invalid settings → defaults
    assert cli_tools.classroom_python(project_root).name.startswith("python")


def test_doctor_classroom_checks(ctx):
    configured = with_classroom(ctx)
    checks = {c.name: c for c in cli_admin.check_classroom(configured)}
    assert checks["Classroom : lien du cours"].detail == "✓ 1 cours configuré(s)"
    assert "jamais vérifiée" in checks["Classroom : session"].detail
    assert all(COURSE_URL not in c.detail for c in checks.values())
    state.record_check(configured.repo, ok=False, detail="Google demande de te reconnecter", login_required=True)
    session = {c.name: c for c in cli_admin.check_classroom(configured)}["Classroom : session"]
    assert session.level == "warn" and "reconnecter" in session.detail
    missing = {c.name: c for c in cli_admin.check_classroom(with_classroom(ctx, urls=None))}
    assert missing["Classroom : lien du cours"].detail.startswith("✗")
    assert missing["Classroom : lien du cours"].level == "warn"
    assert cli_admin.check_classroom(with_classroom(ctx, enabled=False))[0].detail.startswith("surveillance désactivée")
    invalid = with_classroom(ctx, urls=f"{COURSE_URL}, https://exemple.org")
    assert "1 lien(s) invalide(s)" in cli_admin.check_classroom(invalid)[0].detail


def test_config_set_validates_the_course_url(run_cli, project_root):
    res = run_cli("config", "set", "CLASSROOM_COURSE_URLS", "https://exemple.org/x")
    assert res.exit_code == 1 and "Lien de cours invalide" in res.output
    out = _ok(run_cli("config", "set", "CLASSROOM_COURSE_URLS", COURSE_URL))
    assert COURSE_URL not in out and "masquée" in out
    assert _ok(run_cli("config", "get", "CLASSROOM_COURSE_URLS")).strip() == "✓"
    assert "classroom.poll_minutes" in _ok(run_cli("config", "show", "classroom"))
