"""Linux server support: crontab scheduling (fake crontab), doctor checks, dashboard listen address."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobbot import cli, cli_admin, cli_support, cli_tools, cron, server_checks, sessions
from jobbot.config import Settings

OTHER_LINES = ("# m h dom mon dow command\n"
               "*/5 * * * * /usr/bin/autre-appli --check >> /var/log/autre.log 2>&1\n"
               "@reboot /opt/outil/start.sh\n")


class FakeCrontab:
    """``crontab -l`` / ``crontab -`` on an in-memory text; records every call."""

    def __init__(self, text: str | None = OTHER_LINES):
        self.text = text
        self.calls: list[list[str]] = []

    def __call__(self, cmd, input_text=None):
        self.calls.append(cmd)
        if cmd == ["crontab", "-l"]:
            if self.text is None:
                return subprocess.CompletedProcess(cmd, 1, "", "no crontab for jobuser")
            return subprocess.CompletedProcess(cmd, 0, self.text, "")
        self.text = input_text
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @property
    def writes(self) -> int:
        return sum(1 for c in self.calls if c == ["crontab", "-"])


# --------------------------------------------------------------------------- cron lines


def test_job_lines_follow_the_contract(tmp_path):
    root = Path("/home/jobuser/jobbot")
    jobs = cron.build_jobs(root, Settings(), exe="/home/jobuser/jobbot/.venv/bin/jobbot", path_env="/usr/bin:/bin")
    assert [j.name for j in jobs] == ["run", "soir", "classroom"]
    run = jobs[0].line()
    assert run == ("30 9 * * * cd /home/jobuser/jobbot && PATH=/usr/bin:/bin flock -n /tmp/jobbot-run.lock "
                   "/home/jobuser/jobbot/.venv/bin/jobbot run >> /home/jobuser/jobbot/data/logs/cron.log 2>&1 "
                   "# jobbot:run")
    assert "run --step inbox --step followup" in jobs[1].command and jobs[1].schedule == "0 18 * * *"
    assert jobs[2].schedule == "*/15 7-21 * * *" and "classroom watch --once" in jobs[2].command
    with_screen = cron.build_jobs(root, Settings(), {"run": "08:05"}, xvfb=True, exe="jobbot", path_env="/bin")
    assert with_screen[0].schedule == "5 8 * * *" and "jobbot-run.lock xvfb-run -a jobbot run" in with_screen[0].command
    off = Settings(classroom={"enabled": False})
    assert [j.name for j in cron.build_jobs(root, off, exe="jobbot", path_env="/bin")] == ["run", "soir"]
    assert cron.job_command(Path("/srv/j%b"), "run", xvfb=False, exe="jobbot", path_env="/bin",
                            logs=Path("/srv/logs")).count(r"\%") == 1  # % is a newline for cron
    with pytest.raises(ValueError):
        cron.daily_schedule("25:00")


@pytest.mark.parametrize(("poll", "hours", "expected"), [
    (15, "07:30-21:00", "*/15 7-21 * * *"), (1, "08:00-08:45", "* 8 * * *"), (60, "07:30-21:00", "30 7-21 * * *"),
    (120, "07:30-21:00", "30 7-21/2 * * *"), (10, "22:00-06:00", "*/10 22-23,0-6 * * *"),
])
def test_classroom_schedule(poll, hours, expected):
    assert cron.classroom_schedule(poll, hours) == expected


def test_cron_path_puts_local_bin_first():
    path = cron.cron_path("/usr/bin:/opt/x", home=Path("/home/jobuser"))
    assert path.split(":")[:3] == ["/home/jobuser/.local/bin", "/usr/bin", "/opt/x"] and path.endswith("/bin")


def test_install_keeps_other_lines_and_is_idempotent(project_root):
    crontab = FakeCrontab()
    lines = cron.run_cron(project_root, "install", dry_run=False, runner=crontab)
    assert lines[0].startswith("Crontab : 3 tâche(s) jobbot installée(s) (2 autre(s) ligne(s) conservée(s))")
    text = crontab.text
    assert text.startswith(OTHER_LINES) and text.count("# jobbot:") == 3 and text.endswith("\n")
    assert (project_root / "data" / "logs").is_dir()
    cron.run_cron(project_root, "install", dry_run=False, runner=crontab)
    assert crontab.writes == 1 and crontab.text == text  # same content: not rewritten
    changed = cron.run_cron(project_root, "install", dry_run=False, runner=crontab, times={"run": "08:00"})
    assert crontab.writes == 2 and "0 8 * * *" in crontab.text and crontab.text.count("# jobbot:") == 3
    assert any(line.startswith("run : 0 8 * * *") for line in changed)
    shown = cron.run_cron(project_root, "show", dry_run=False, runner=crontab)
    assert shown[0].startswith("run : installée — 0 8 * * *") and len(shown) == 3
    removed = cron.run_cron(project_root, "remove", dry_run=False, runner=crontab)
    assert "3 tâche(s) jobbot supprimée(s)" in removed[0] and crontab.text == OTHER_LINES
    assert cron.run_cron(project_root, "remove", dry_run=False, runner=crontab) == [
        "Crontab : aucune tâche jobbot à supprimer"]
    assert cron.run_cron(project_root, "show", dry_run=False, runner=crontab)[0].startswith("run : non installée")


def test_install_on_empty_crontab_and_dry_run(project_root):
    crontab = FakeCrontab(text=None)
    preview = cron.run_cron(project_root, "install", dry_run=True, runner=crontab)
    assert preview[0] == "[simulation] crontab après installation :" and crontab.writes == 0
    assert sum("# jobbot:" in line for line in preview) == 3
    cron.run_cron(project_root, "install", dry_run=False, runner=crontab)
    assert crontab.text.splitlines()[0].endswith("# jobbot:run")

    def broken(cmd, input_text=None):
        return subprocess.CompletedProcess(cmd, 1, "", "permission denied")

    assert cron.run_cron(project_root, "show", dry_run=False, runner=broken)[0].startswith("Crontab : échec")


def test_schedule_cli_uses_cron_on_linux(run_cli_linux, project_root):
    invoke, crontab = run_cli_linux
    out = invoke("schedule", "install", "--matin", "08:15")
    assert out.exit_code == 0 and "Crontab : 3 tâche(s)" in out.output and "15 8 * * *" in crontab.text
    assert "run : installée" in invoke("schedule", "show").output
    assert "supprimée" in invoke("schedule", "remove").output and "# jobbot:" not in crontab.text


@pytest.fixture
def run_cli_linux(ctx, project_root, monkeypatch):
    crontab = FakeCrontab()
    runner = CliRunner()
    monkeypatch.setattr(cli_support, "build_app_context", lambda root, dry_run: ctx)
    monkeypatch.setattr(cron, "scheduler_kind", lambda: "cron")
    monkeypatch.setattr(cron, "run_crontab", crontab)
    monkeypatch.setattr(cron.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "crontab" else None)

    def invoke(*args):
        result = runner.invoke(cli.app, ["--root", str(project_root), *args])
        cli_support.teardown_logging()
        return result

    return invoke, crontab


# --------------------------------------------------------------------------- doctor on Linux


def test_linux_doctor_checks(ctx, project_root):
    crontab = FakeCrontab()
    which = {"xvfb-run": None, "crontab": "/usr/bin/crontab"}.get
    checks = {c.name: c for c in server_checks.linux_checks(project_root, ctx.settings, ctx.repo, runner=crontab,
                                                            which=which)}
    assert checks["Écran virtuel (xvfb-run)"].level == "warn"  # headless: false without xvfb
    assert checks["Tâches cron"].level == "warn" and "run, soir, classroom" in checks["Tâches cron"].detail
    assert checks["Sessions importées"].level == "warn"
    assert checks["config/settings.local.yaml"].level == "warn"
    cron.run_cron(project_root, "install", dry_run=False, runner=crontab)
    (project_root / "config" / "settings.local.yaml").write_text("headless: true\n", encoding="utf-8")
    path = sessions.session_file(project_root, ctx.settings, "linkedin")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": [{"name": "li_at", "value": "VALEUR-COOKIE-SECRETE", "domain": ".linkedin.com"}]}),
                    encoding="utf-8")
    headless = ctx.settings.model_copy(update={"headless": True})
    checks = {c.name: c for c in server_checks.linux_checks(project_root, headless, ctx.repo, runner=crontab,
                                                            which=which)}
    assert checks["Écran virtuel (xvfb-run)"].level == "ok" and checks["Tâches cron"].level == "ok"
    assert checks["Session linkedin"].level == "ok" and "à importer" in checks["Session linkedin"].detail
    assert checks["config/settings.local.yaml"].level == "ok"
    assert "VALEUR-COOKIE-SECRETE" not in json.dumps([c.detail for c in checks.values()])  # never shown
    none = {"crontab": None}.get
    assert server_checks.check_crontab(ctx.settings, which=none).detail.startswith("crontab introuvable")


def test_run_doctor_adds_linux_checks_only_on_linux(ctx, project_root, monkeypatch):
    monkeypatch.setattr(cli_admin, "chromium_path", lambda: "chromium")
    monkeypatch.setattr(cli_admin, "chrome_installed", lambda: True)
    monkeypatch.setattr(cli_admin, "check_sources", lambda c: [])
    state = SimpleNamespace(root=project_root, app=ctx)
    assert not any(c.name == "Tâches cron" for c in cli_admin.run_doctor(state))
    monkeypatch.setattr(server_checks, "is_linux", lambda: True)
    monkeypatch.setattr(server_checks, "linux_checks", lambda root, settings, repo: [
        cli_admin.Check("Tâches cron", "ok", "3 tâche(s)")])
    assert any(c.name == "Tâches cron" for c in cli_admin.run_doctor(state))


# --------------------------------------------------------------------------- dashboard address


def test_dashboard_host_validation_and_command(run_cli_linux, monkeypatch):
    assert cli_tools.dashboard_host("127.0.0.1") == "127.0.0.1" and cli_tools.dashboard_host("localhost")
    assert cli_tools.dashboard_host("100.64.0.1") == "100.64.0.1"
    for bad in ("0.0.0.0", "::", "mon-serveur"):
        with pytest.raises(ValueError):
            cli_tools.dashboard_host(bad)
    seen: dict = {}
    monkeypatch.setattr(cli_tools.subprocess, "run",
                        lambda cmd, env=None, check=False: seen.update(cmd=cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(cli_tools.sys, "platform", "linux")
    invoke, _ = run_cli_linux
    assert invoke("dashboard", "--host", "100.64.0.1", "--port", "8600").exit_code == 0
    cmd = seen["cmd"]
    assert cmd[cmd.index("--server.address") + 1] == "100.64.0.1" and cmd[cmd.index("--server.port") + 1] == "8600"
    assert cmd[cmd.index("--server.headless") + 1] == "true"
    refused = invoke("dashboard", "--host", "0.0.0.0")
    assert refused.exit_code == 1 and "tout le réseau" in refused.output
    monkeypatch.setattr(cli_tools.sys, "platform", "win32")
    invoke("dashboard")
    assert "--server.headless" not in seen["cmd"] and "127.0.0.1" in seen["cmd"]
