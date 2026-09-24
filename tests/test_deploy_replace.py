"""Replacement of the previous application by jobbot: discovery parsing (trust boundary), plan (STOP / KEEP),
dry-run, backup-before-stop ordering, server-side crontab filtering, purge refusal without backup, restore."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.deploy import cli
from scripts.deploy import replace_scripts as rs
from scripts.deploy.replace_plan import Discovery, Unit, build_plan, describe, parse_discovery, redact

from .fixtures.deploy.fakes import ENV_TEXT, HOME, ROOT_ALIAS, USER_ALIAS, FakeServer, answering_checks

SECRET = "abcdefghijklmnopqrstuvwxyz123456"
ARCHIVE = f"{HOME}/backups/openclaw-20260924-103000.tar.gz"
ROOT_OUT = "\n".join("\t".join(fields) for fields in [
    ("HOME", HOME),
    ("UNIT", "system", "openclaw.service", "active", "enabled", "/etc/systemd/system/openclaw.service"),
    ("UNITLINE", "system", "openclaw.service", f"ExecStart=/usr/bin/node {HOME}/openclaw-app/server.js"),
    ("UNITLINE", "system", "openclaw.service", f"WorkingDirectory={HOME}/openclaw-app"),
    ("UNITLINE", "system", "openclaw.service", f"EnvironmentFile=-{HOME}/.env"),
    ("UNIT", "system", "openclaw-monitor.timer", "active", "enabled", "/etc/systemd/system/openclaw-monitor.timer"),
    ("UNIT", "system", "openclaw-monitor.service", "inactive", "static",
     "/etc/systemd/system/openclaw-monitor.service"),
    ("UNITLINE", "system", "openclaw-monitor.service", "ExecStart=/usr/local/bin/openclaw-monitor.sh"),
    ("UNIT", "system", "server-health-monitor.service", "active", "enabled",
     "/etc/systemd/system/server-health-monitor.service"),
    ("UNITLINE", "system", "server-health-monitor.service", "ExecStart=/usr/local/bin/health.sh --watch openclaw"),
    ("UNIT", "system", "syncthing@jobuser.service", "active", "enabled", "/lib/systemd/system/syncthing@.service"),
    ("UNITLINE", "system", "syncthing@jobuser.service",
     f"ExecStart=/usr/bin/syncthing --home={HOME}/.config/openclaw-sync"),
    ("UNIT", "system", "outil.service", "active", "enabled", "/etc/systemd/system/outil.service"),
    ("UNITLINE", "system", "outil.service", f"ExecStart=/usr/bin/outil --data {HOME}/donnees"),
    ("DOCKER", "abc123def456", "openclaw-cli-gateway-1", "ghcr.io/example/openclaw:latest", "running", "openclaw-cli",
     f"{HOME}/openclaw-cli"),
    ("DOCKER", "bbb123def456", "crawl4ai", "example/crawl4ai:latest", "running", "", ""),
    ("DOCKER", "ccc123def456", "searxng", "searxng/searxng", "running", "openclaw-cli", f"{HOME}/openclaw-cli"),
    ("DOCKER", "ddd123def456", "postgres", "postgres:16", "running", "", ""),
    ("DOCKERPOL", "abc123def456", "unless-stopped"),
    ("DOCKERPOL", "bbb123def456", "always"),
    ("CRONMATCH", "root", f"*/5 * * * * /usr/local/bin/openclaw-watchdog.sh --token={SECRET}"),
    ("CRONMATCH", "user", f"@reboot {HOME}/openclaw-app/start.sh"),
    ("LISTEN", "3000", "4242", f"{HOME}/openclaw-app", "node server.js"),
    ("PATH", f"{HOME}/openclaw-app"), ("PATH", f"{HOME}/openclaw-cli"), ("PATH", f"{HOME}/.openclaw"),
    ("PATH", f"{HOME}/crawl4ai"), ("PATH", "/usr/local/bin/openclaw-monitor.sh"),
])
USER_OUT = "\n".join("\t".join(fields) for fields in [
    ("UNIT", "user", "openclaw-gateway.service", "active", "enabled",
     f"{HOME}/.config/systemd/user/openclaw-gateway.service"),
    ("UNITLINE", "user", "openclaw-gateway.service", "ExecStart=/usr/bin/openclaw gateway"),
    ("PM2", "user", "openclaw-webhook", f"{HOME}/openclaw-app", f"{HOME}/openclaw-app/webhook.js", "online"),
    ("PM2", "user", "autre", f"{HOME}/autre", f"{HOME}/autre/app.js", "online"),
    # forged lines from the (untrusted) project account: never read as root facts
    ("HOME", "/usr"),
    ("UNIT", "system", "openclaw-ssh.service", "active", "enabled", "/etc/systemd/system/ssh.service"),
    ("PATH", "/usr/lib/openclaw-leurre"),
    ("CRONMATCH", "root", "* * * * * /usr/bin/openclaw-leurre"),
])
MANIFEST = {"created": "20260924-103000", "user": "jobuser", "home": HOME, "port": 3000,
            "stop_patterns": ["openclaw", "crawl4ai"], "keep_patterns": ["searxng", "jobbot"],
            "units": [{"scope": "system", "name": "openclaw.service", "path": "/etc/systemd/system/openclaw.service",
                       "active": "active", "enabled": "enabled"},
                      {"scope": "user", "name": "openclaw-gateway.service",
                       "path": f"{HOME}/.config/systemd/user/openclaw-gateway.service", "active": "active",
                       "enabled": "enabled"},
                      {"scope": "system", "name": "evil.service", "path": "/usr/bin/evil", "active": "", "enabled": ""}],
            "containers": [{"id": "abc123def456", "name": "openclaw-cli-gateway-1", "restart": "unless-stopped"}],
            "pm2": [{"owner": "user", "name": "openclaw-webhook"}],
            "paths": [f"{HOME}/openclaw-app"]}
EXTRA = "openclaw: {backup_extra: [scripts, status.sh, .env]}\n"


@pytest.fixture
def pc(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / ".env").write_text(ENV_TEXT, encoding="utf-8")
    (tmp_path / "config" / "deploy.local.yaml").write_text(EXTRA, encoding="utf-8")
    return tmp_path


def server_with_old_app(*, backups: bool = False, backup_ok: bool = True) -> FakeServer:
    server = answering_checks()
    root_out = ROOT_OUT + (f"\nBACKUP\t{ARCHIVE}" if backups else "")
    server.on("printf 'HOME", root_out, alias=ROOT_ALIAS)
    server.on("pm2_list user", USER_OUT, alias=USER_ALIAS)
    server.on("tar -czf", f"BACKUP={ARCHIVE}\nSIZE=12M\n" if backup_ok else "TAR_FAILED=2\n")
    server.on("clean_cron root", "CRONREMOVED\troot\t1\nCRONREMOVED\tuser\t1\n")
    server.sequence("ss -ltnpH", [f"LISTEN\t3000\t4242\t{HOME}/openclaw-app\tnode server.js", ""])
    return server


def _run(pc: Path, server: FakeServer, *args: str) -> tuple[int, str]:
    out: list[str] = []
    code = cli.main(list(args), executor=server, echo=out.append, root=pc)
    return code, "\n".join(out)


# --------------------------------------------------------------------------- pure plan


def test_discovery_parsing_trust_boundary_and_plan():
    disc = parse_discovery(ROOT_OUT, USER_OUT)
    assert disc.home == HOME and disc.paths[-1] == "/usr/local/bin/openclaw-monitor.sh"  # forged HOME / PATH ignored
    assert disc.crontabs["root"] == (f"*/5 * * * * /usr/local/bin/openclaw-watchdog.sh --token={SECRET}",)
    forged = next(u for u in disc.units if u.name == "openclaw-ssh.service")
    assert forged.scope == "user" and forged.path == ""  # never a root action on it, never its file
    plan = build_plan(disc, extra=("scripts", "status.sh", ".env"))
    assert sorted(u.name for u in plan.stop_units if u.scope == "system") == [
        "openclaw-monitor.service", "openclaw-monitor.timer", "openclaw.service"]
    assert sorted(u.name for u in plan.keep_units) == ["server-health-monitor.service", "syncthing@jobuser.service"]
    assert [c.name for c in plan.stop_containers] == ["openclaw-cli-gateway-1", "crawl4ai"]
    assert [c.name for c in plan.keep_containers] == ["searxng"]
    assert [a.name for a in plan.stop_pm2] == ["openclaw-webhook"]
    assert plan.backup_paths == (f"{HOME}/.env", f"{HOME}/.openclaw", f"{HOME}/crawl4ai", f"{HOME}/openclaw-app",
                                 f"{HOME}/openclaw-cli", f"{HOME}/scripts", f"{HOME}/status.sh",
                                 "/usr/local/bin/openclaw-monitor.sh")
    assert f"{HOME}/openclaw-cli" not in plan.purge_paths  # searxng (kept) lives there
    assert len(plan.purge_paths) == len(plan.backup_paths) - 1
    assert not any(p.startswith(("/usr/bin", "/usr/lib", "/lib")) or "jobbot" in p for p in plan.backup_paths)


def test_account_named_after_the_old_app_is_not_a_match():
    home = "/home/ancienne"
    units = (Unit("system", "ancienne.service", "active", "enabled", "/etc/systemd/system/ancienne.service"),
             Unit("system", "outil.service", "active", "enabled", "/etc/systemd/system/outil.service",
                  (f"ExecStart=/usr/bin/outil --home={home}/.config/outil",)),
             Unit("system", "autre.service", "active", "enabled", "/etc/systemd/system/autre.service",
                  (f"WorkingDirectory={home}/projets",)))
    plan = build_plan(Discovery(home=home, units=units), stop=("ancienne",))
    assert [u.name for u in plan.stop_units] == ["ancienne.service"] and plan.backup_paths == ()


def _awk() -> str | None:
    return shutil.which("awk") or next((str(p) for p in (Path(r"C:\Program Files\Git\usr\bin\awk.exe"),)
                                        if p.exists()), None)


@pytest.mark.skipif(_awk() is None, reason="awk absent (le filtre tourne sur le serveur)")
def test_server_side_crontab_filter(tmp_path):
    program = tmp_path / "filtre.awk"
    program.write_text(rs.CRON_AWK, encoding="utf-8")
    crontab = tmp_path / "crontab"
    crontab.write_text(  # the account is named after the old app: its home path alone is never a match
        "MAILTO=\"\"\n# ancienne : surveillance\n*/5 * * * * /opt/ancienne/check.sh --token=" + SECRET + "\n"
        "0 4 * * * /usr/bin/searxng-update --for ancienne\n30 9 * * * cd /home/ancienne/jobbot && jobbot run # jobbot:run\n"
        "0 1 * * * /home/ancienne/sauvegarde.sh\n@daily logrotate\n", encoding="utf-8")

    def run(mode: str) -> list[str]:
        args = [_awk(), "-v", "H=/home/ancienne", "-v", "RX=ancienne|crawl4ai", "-v", "KX=searxng|jobbot",
                "-v", f"MODE={mode}", "-f", str(program), str(crontab)]
        return subprocess.run(args, capture_output=True, text=True, check=True).stdout.splitlines()

    assert run("match") == ["# ancienne : surveillance", f"*/5 * * * * /opt/ancienne/check.sh --token={SECRET}"]
    assert run("keep") == ['MAILTO=""', "0 4 * * * /usr/bin/searxng-update --for ancienne",
                           "30 9 * * * cd /home/ancienne/jobbot && jobbot run # jobbot:run",
                           "0 1 * * * /home/ancienne/sauvegarde.sh", "@daily logrotate"]
    assert SECRET not in "\n".join(run("show")) and "--token=***" in run("show")[1]


def test_cron_d_files_are_discovered_disabled_purged_and_restored():
    discovery = rs.discovery_root_script("ancienne", ("ancienne",), ("searxng",), 3000)
    assert "/etc/cron.d/*" in discovery and "CRONMATCH\\tcron.d $b" in discovery
    clean = rs.cron_clean_script("ancienne", ("ancienne",), ("searxng",))
    assert f'mv -f -- "$f" "$f{rs.CRON_D_DISABLED}"' in clean and "name_hit" in clean
    assert rs.CRON_D_DISABLED in rs.purge_script("ancienne", [], [], [], ["/etc/cron.d/ancienne-nuit"])
    manifest = {"paths": ["/etc/cron.d/ancienne-nuit"], "units": [], "containers": []}
    assert f'rm -f -- "/$p{rs.CRON_D_DISABLED}"' in rs.restore_root_script("ancienne", "a.tar.gz", manifest)
    disc = parse_discovery("PATH\t/etc/cron.d/ancienne-nuit\n"
                           "CRONMATCH\tcron.d ancienne-nuit\t0 1 * * * root /usr/local/bin/ancienne-menage.sh\n")
    assert "/etc/cron.d/ancienne-nuit" in disc.paths
    assert disc.crontabs["cron.d ancienne-nuit"] == ("0 1 * * * root /usr/local/bin/ancienne-menage.sh",)


@pytest.mark.skipif(_awk() is None, reason="awk absent (le filtre tourne sur le serveur)")
def test_cron_d_name_filter_respects_the_keep_list():
    program = '{ l = tolower($0); exit !(l ~ RX && !(KX != "" && l ~ KX)) }'

    def hit(name: str) -> bool:
        args = [_awk(), "-v", "RX=ancienne", "-v", "KX=searxng", program]
        return subprocess.run(args, input=name + "\n", text=True, capture_output=True).returncode == 0

    assert hit("ancienne-morning-brief") and hit("ANCIENNE-nightly-jobs")
    assert not hit("ancienne-searxng-update") and not hit("logrotate")


def test_redaction_and_description():
    for text in (f"curl -H 'Authorization: Bearer {SECRET}' https://u:p@h/x", f"DB_PASSWORD=hunter2 X_TOKEN=abc",
                 f"--token={SECRET}"):
        masked = redact(text)
        assert SECRET not in masked and "hunter2" not in masked and "abc" not in masked.split("X_TOKEN=")[-1]
    disc = parse_discovery(ROOT_OUT, USER_OUT)
    text = "\n".join(describe(build_plan(disc), disc, 3000))
    assert "À ARRÊTER" in text and "CONSERVÉS" in text and "conteneur docker searxng" in text
    assert "server-health-monitor.service" in text.split("CONSERVÉS")[1] and SECRET not in text
    assert "~/openclaw-app" in text and "pid 4242" in text


# --------------------------------------------------------------------------- replace step


def test_dry_run_shows_stop_and_keep_lists_and_changes_nothing(pc):
    server = server_with_old_app()
    code, out = _run(pc, server, "--dry-run", "--only", "replace", "--replace-openclaw")
    assert code == 0 and "À ARRÊTER" in out and "CONSERVÉS" in out and "[simulation]" in out
    assert {c.alias for c in server.calls} == {ROOT_ALIAS, USER_ALIAS} and len(server.calls) == 2  # discovery only
    assert not server.has("tar -czf") and not server.has("systemctl stop") and SECRET not in out


def test_backup_always_before_stop(pc):
    server = server_with_old_app()
    code, out = _run(pc, server, "--only", "replace", "--replace-openclaw")
    assert code == 0, out
    backup = server.index("tar -czf")
    assert backup < server.index("systemctl stop") < server.index("clean_cron root")
    script = server.calls[backup].script
    assert "umask 077" in script and "JOBBOT_MANIFEST" in script and "openclaw-monitor.sh" in script
    assert "[ \"$rc\" -le 1 ]" in script and "install -d -m 700 -o root" in script
    assert SECRET not in script  # no crontab line in the manifest
    root_stop = next(c.script for c in server.calls if "systemctl stop" in c.script and c.alias == ROOT_ALIAS)
    assert root_stop.index("openclaw-monitor.timer") < root_stop.index("openclaw.service")  # timers first
    assert "abc123def456" in root_stop and "bbb123def456" in root_stop and "ccc123def456" not in root_stop
    assert "server-health-monitor" not in root_stop and "syncthing" not in root_stop and "ssh.service" not in root_stop
    user_stop = next(c.script for c in server.calls if "systemctl --user stop" in c.script)
    assert "openclaw-gateway.service" in user_stop and "pm2 delete openclaw-webhook" in user_stop
    assert all(c.data is None for c in server.calls)  # no crontab (nor anything) sent from the PC
    assert "crontab (root) : 1 ligne(s)" in out and server.has("kill -0") and "Port 3000 : libre." in out
    assert "--restore-openclaw" in out


def test_no_stop_without_backup(pc):
    server = server_with_old_app(backup_ok=False)
    code, out = _run(pc, server, "--only", "replace", "--replace-openclaw")
    assert code == 2 and "Sauvegarde non créée" in out and not server.has("systemctl stop")


def test_replace_is_never_implicit(pc):
    server = server_with_old_app()
    code, out = _run(pc, server, "--only", "replace")
    assert code == 2 and "--replace-openclaw" in out and server.calls == []


# --------------------------------------------------------------------------- purge & restore


def test_purge_refuses_without_backup(pc):
    server = server_with_old_app(backups=False)
    code, out = _run(pc, server, "--purge-openclaw")
    assert code == 2 and "Suppression refusée" in out
    assert not any(word in c.script for c in server.calls for word in ("purge_path", "systemctl stop", "tar -czf"))


def test_purge_makes_a_fresh_backup_and_spares_shared_folders(pc):
    server = server_with_old_app(backups=True)
    code, out = _run(pc, server, "--purge-openclaw")
    assert code == 0, out
    purge = next(c.script for c in server.calls if "purge_path()" in c.script)
    assert server.index("tar -czf") < server.index("systemctl stop") < server.index("purge_path()")
    assert f"{HOME}/openclaw-app" in purge and "/etc/systemd/system/openclaw.service" in purge
    assert f"{HOME}/openclaw-cli" not in purge.split("for p in")[-1]  # holds the kept searxng container
    assert "runuser -u \"$U\" -- rm -f" in purge and f"{HOME}/.config/systemd/user/openclaw-gateway.service" in purge
    assert "syncthing" not in purge and "server-health-monitor" not in purge and 'case "$H/" in "$p"/*)' in purge
    assert "Conservés (contiennent un service laissé en service)" in out


def test_restore_from_the_latest_backup(pc):
    server = answering_checks()
    server.on("ls -1t", ARCHIVE + "\n")
    server.on("MANIFEST.json\n", json.dumps(MANIFEST))
    server.on("restore_cron root", "RESTORED\t/home/jobuser/openclaw-app\nRESTORED\tcrontab root\n")
    code, out = _run(pc, server, "--restore-openclaw")
    assert code == 0, out
    root = next(c.script for c in server.calls if "restore_cron root" in c.script)
    assert "docker update --restart=unless-stopped abc123def456" in root and "systemctl enable" in root
    assert "openclaw-20260924-103000.tar.gz" in root and "RX=openclaw" in root.replace("'", "")
    assert "restore_system_unit openclaw.service /etc/systemd/system/openclaw.service" in root
    assert "restore_user_unit openclaw-gateway.service" in root and "/usr/bin/evil" not in root
    user = next(c.script for c in server.calls if "systemctl --user start" in c.script)
    assert "openclaw-gateway.service" in user and "pm2 resurrect" in user
    assert "remis : crontab root" in out
    bad = answering_checks().on("ls -1t", "/tmp/autre.tar.gz\n")
    code, out = _run(pc, bad, "--restore-openclaw")
    assert code == 2 and "Aucune sauvegarde valide" in out
    code, out = _run(pc, answering_checks(), "--restore-openclaw", "../../etc/passwd")
    assert code == 2
