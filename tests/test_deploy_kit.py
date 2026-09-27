"""scripts/deploy_server.py: configuration, .env filtering, step order, idempotency, dry-run (fake executor)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts.deploy import cli, steps_app, templates
from scripts.deploy.config import (
    DeployError,
    config_from_env,
    filter_env,
    is_private_key,
    load_local_deploy,
    parse_env,
)
from scripts.deploy.runner import Result, Runner, encode_script, git_ssh_command

from .conftest import FIXTURES
from .fixtures.deploy.fakes import (
    ENV_TEXT,
    HOME,
    PRIVATE_VALUES,
    ROOT_ALIAS,
    USER,
    USER_ALIAS,
    VPN_IP,
    FakeServer,
    healthy_server,
    script_of,
)

READ_ONLY_GIT = ("rev-parse", "status", "remote get-url")


@pytest.fixture
def pc(tmp_path: Path) -> Path:
    """A fake project on the PC: .env, profile, one exported session."""
    (tmp_path / "config").mkdir()
    (tmp_path / ".env").write_text(ENV_TEXT, encoding="utf-8")
    shutil.copy(FIXTURES / "profile.yaml", tmp_path / "config" / "profile.yaml")
    sessions = tmp_path / "data" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "linkedin.json").write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    (sessions / "notes.json").write_text("{}", encoding="utf-8")  # not a site: never sent
    return tmp_path


def _run(pc: Path, server: FakeServer, *args: str) -> tuple[int, str]:
    out: list[str] = []
    code = cli.main(list(args), executor=server, echo=out.append, root=pc)
    return code, "\n".join(out)


# --------------------------------------------------------------------------- configuration & .env


def test_parse_and_filter_env():
    pairs = dict(parse_env(ENV_TEXT))
    assert pairs["FRANCE_TRAVAIL_CLIENT_ID"] == "ft-id" and pairs["HUNTER_API_KEY"] == ""
    filtered = filter_env(ENV_TEXT)
    assert "SMTP_PASSWORD=abcd efgh ijkl mnop\n" in filtered and "CLAUDE_CODE_OAUTH_TOKEN=jeton-abonnement" in filtered
    assert 'FRANCE_TRAVAIL_CLIENT_ID="ft-id"\n' in filtered  # raw value: quotes kept for python-dotenv
    assert not any(value in filtered for value in PRIVATE_VALUES)
    assert all(not line.startswith(("SERVER_", "PROJECT_", "WEBHOOK_", "#")) for line in filtered.splitlines())
    for private in ("MY_ROOT_KEY", "ADMIN_PASSWORD", "DBPASSWORD", "MYSQLROOTPW", "PASSWORD2", "ROOTPASSWORD"):
        assert is_private_key(private), private
    assert not is_private_key("SMTP_PASSWORD") and not is_private_key("HUNTER_API_KEY")
    assert "TELEGRAM" not in filter_env("TELEGRAM_BOT_TOKEN=x\n", drop=("TELEGRAM_BOT_TOKEN",))


def test_config_errors_are_french():
    with pytest.raises(DeployError, match="SERVER_ROOT_HOST_ALIAS, PROJECT_SSH_HOST_ALIAS, PROJECT_USER"):
        config_from_env({})
    base = {"SERVER_ROOT_HOST_ALIAS": ROOT_ALIAS, "PROJECT_SSH_HOST_ALIAS": USER_ALIAS, "PROJECT_USER": USER}
    with pytest.raises(DeployError, match="Alias SSH invalide"):
        config_from_env({**base, "PROJECT_SSH_HOST_ALIAS": "-oProxyCommand=mal"})
    with pytest.raises(DeployError, match="PROJECT_USER"):
        config_from_env({**base, "PROJECT_USER": "root"})
    with pytest.raises(DeployError, match="adresse IP"):
        config_from_env({**base, "SERVER_IP_TAILSCALE": "pas-une-ip"})
    config = config_from_env({**base, "WEBHOOK_BASE_URL": "http://example.org:4100/x"})
    assert config.old_app_port == 4100 and config.vpn_ip is None
    assert config_from_env(base).old_app_port == 3000


def test_deploy_local_yaml(tmp_path):
    (tmp_path / "config").mkdir()
    assert load_local_deploy(tmp_path).telegram is False
    local = tmp_path / "config" / "deploy.local.yaml"
    local.write_text("settings_local: {search_engine_url: 'http://127.0.0.1:8888'}\n"
                     "telegram: {token_file: /etc/mon-bot/jeton, chat_id_file: /etc/mon-bot/conversation}\n"
                     "openclaw: {backup_extra: [scripts, .env], keep: [MonService]}\n", encoding="utf-8")
    loaded = load_local_deploy(tmp_path)
    assert loaded.telegram and loaded.backup_extra == ("scripts", ".env") and loaded.keep == ("monservice",)
    local.write_text("telegram: {token_file: '/etc/$(reboot)'}\n", encoding="utf-8")
    with pytest.raises(DeployError, match="chemin invalide"):
        load_local_deploy(tmp_path)


def test_step_selection():
    select = lambda *a: cli.select_steps(cli.parse_args(list(a)))  # noqa: E731
    assert select() == [s for s in cli.STEPS if s != "replace"]
    assert select("--replace-openclaw")[:3] == ["check", "replace", "system"]
    assert "replace" not in select("--update") and select("--update")[1:4] == ["code", "deps", "private"]
    assert select("--only", "private") == ["private"]
    assert select("--purge-openclaw") == ["check", "purge"] and select("--restore-openclaw") == ["check", "restore"]
    for bad in (["--update", "--replace-openclaw"], ["--only", "replace"], ["--update", "--only", "llm"]):
        with pytest.raises(DeployError):
            select(*bad)


def test_encoded_script_round_trip():
    remote = encode_script("echo 'é' \"$HOME\"\n")
    assert remote.startswith('bash -c "$(printf %s ') and script_of(["ssh", "alias", remote]) == "echo 'é' \"$HOME\"\n"


# --------------------------------------------------------------------------- full run


def test_full_install_runs_every_step_in_order(pc):
    server = healthy_server()
    code, out = _run(pc, server)
    assert code == 0, out
    markers = ["xvfb=$(command -v xvfb-run)", "apt-get install -y -qq git curl xvfb ca-certificates cron",
               "uv python install 3.12", "git init --bare", "hooks/post-receive", "push --force serveur HEAD:master",
               "checkout -f master", "uv pip install --python .venv/bin/python -e", "install-deps --dry-run chromium",
               "--no-install-recommends libnss3 libasound2t64 fonts-liberation",
               "sha256sum", "cat > jobbot/.env.pc.tmp", "awk 'FNR==NR", "complete_json",
               ".venv/bin/jobbot init", ".venv/bin/jobbot schedule install", "jobbot-dashboard.service.tmp",
               "loginctl enable-linger", "systemctl --user restart jobbot-dashboard"]
    positions = [server.index(m) for m in markers]
    assert positions == sorted(positions)
    assert not server.has("openclaw-")  # replacement only with --replace-openclaw
    ssh_aliases = {c.alias for c in server.calls if c.argv[0] == "ssh"}
    assert ssh_aliases == {ROOT_ALIAS, USER_ALIAS}
    assert all(c.argv[1:5] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"] for c in server.calls
               if c.argv[0] == "ssh")
    assert "127.0.0.1:8501" in out and "dashboard_serveur.ps1" in out and "Résumé" in out


def test_private_files_are_filtered_and_sent_only_when_changed(pc):
    server = healthy_server()
    code, _ = _run(pc, server, "--only", "private")
    assert code == 0
    sent = server.uploads()
    assert set(sent) == {"jobbot/config/profile.yaml", "jobbot/data/sessions/linkedin.json", "jobbot/.env.pc",
                         "jobbot/config/settings.local.yaml"}
    env = sent["jobbot/.env.pc"].decode("utf-8")
    assert not any(value in env for value in PRIVATE_VALUES) and "SERVER_" not in env and "#" not in env
    settings = sent["jobbot/config/settings.local.yaml"].decode("utf-8")
    assert "headless: true" in settings and "browser_channel: null" in settings and "notify_email: true" in settings

    digest = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731
    hashes = "\n".join(f"{digest(data)}  {path.removeprefix('jobbot/')}" for path, data in sent.items()
                       if not path.endswith("settings.local.yaml"))
    again = healthy_server().on("---SETTINGS_LOCAL---", f"{hashes}\nENV_PRESENT\n---SETTINGS_LOCAL---\n{settings}")
    code, out = _run(pc, again, "--only", "private")
    assert code == 0 and again.uploads() == {} and out.count(": à jour") == 4
    assert not again.has("awk 'FNR==NR")  # .env unchanged: no merge either


def test_server_settings_keep_server_edits_but_force_headless(pc):
    server = healthy_server().on("---SETTINGS_LOCAL---", "---SETTINGS_LOCAL---\nheadless: false\nnotify_email: false\n"
                                                         "classroom:\n  poll_minutes: 5\n")
    (pc / "config" / "deploy.local.yaml").write_text("settings_local: {search_engine_url: 'http://127.0.0.1:8888'}\n",
                                                     encoding="utf-8")
    _run(pc, server, "--only", "private")
    text = server.uploads()["jobbot/config/settings.local.yaml"].decode("utf-8")
    assert "headless: true" in text and "notify_email: false" in text and "poll_minutes: 5" in text
    assert "search_engine_url: http://127.0.0.1:8888" in text


def test_telegram_keys_are_filled_on_the_server_only(pc):
    (pc / ".env").write_text(ENV_TEXT + "TELEGRAM_BOT_TOKEN=jeton-pc\n", encoding="utf-8")
    (pc / "config" / "deploy.local.yaml").write_text(
        "telegram: {token_file: /etc/mon-bot/jeton, chat_id_file: /etc/mon-bot/conversation}\n", encoding="utf-8")
    server = healthy_server()
    code, out = _run(pc, server, "--only", "private")
    assert code == 0 and "valeurs jamais affichées" in out
    telegram = next(c for c in server.calls if "TELEGRAM_BOT_TOKEN=%s" in c.script)
    assert telegram.alias == ROOT_ALIAS and telegram.data is None and "/etc/mon-bot/jeton" in telegram.script
    assert "jeton-pc" not in server.uploads()["jobbot/.env.pc"].decode("utf-8")
    assert "notify_telegram: true" in server.uploads()["jobbot/config/settings.local.yaml"].decode("utf-8")


def test_dry_run_changes_nothing(pc):
    server = healthy_server()
    code, out = _run(pc, server, "--dry-run")
    assert code == 0 and "[simulation]" in out and "aucune modification" in out
    assert server.calls, "reads are allowed to show an exact plan"
    for call in server.calls:
        if call.argv[0] == "git":
            assert any(word in " ".join(call.argv) for word in READ_ONLY_GIT), call.argv
        else:
            assert call.data is None and not any(w in call.script for w in (
                "apt-get", "cat >", "git init", "uv ", "systemctl --user restart", "loginctl", "crontab -\n"))
    assert "ENV_TEXT" not in out and not any(value in out for value in PRIVATE_VALUES)


def test_unreachable_server_prints_key_instructions(pc):
    server = FakeServer().on("", code=255)
    code, out = _run(pc, server)
    assert code == 3 and "ssh-copy-id" in out and ROOT_ALIAS in out
    assert "clé uniquement" in out and "mot de passe" not in out  # never asks for a password
    offline = FakeServer().on("", code=255)
    code, out = _run(pc, offline, "--dry-run")
    assert code == 0 and "injoignable" in out and "[simulation] ssh" in out


def test_missing_keys_stop_before_any_command(tmp_path):
    (tmp_path / ".env").write_text("SMTP_USER=x\n", encoding="utf-8")
    server = FakeServer()
    code, out = _run(tmp_path, server)
    assert code == 2 and "Clé(s) absente(s)" in out and server.calls == []
    code, out = _run(tmp_path / "absent", server)
    assert code == 2 and ".env introuvable" in out


def test_llm_failure_prints_both_fixes(pc):
    server = FakeServer().on(".venv/bin/python - <<'PY'", "PROVIDER=claude_cli\nTOKEN=no\nLLM_KO: claude -p a échoué\n")
    code, out = _run(pc, server, "--only", "llm")
    assert code == 0 and "claude setup-token" in out and "ANTHROPIC_API_KEY" in out and "jeton" in out


def test_dashboard_on_vpn_only_when_asked(pc):
    server = healthy_server()
    _run(pc, server, "--only", "dashboard", "--dashboard-vpn")
    unit = server.uploads()[templates.UNIT_PATH].decode("utf-8")
    assert f"--host {VPN_IP} --port 8501" in unit
    (pc / ".env").write_text(ENV_TEXT.replace(f"SERVER_IP_TAILSCALE={VPN_IP}", "SERVER_IP_TAILSCALE="),
                             encoding="utf-8")
    code, out = _run(pc, healthy_server(), "--only", "dashboard", "--dashboard-vpn")
    assert code == 2 and "SERVER_IP_TAILSCALE" in out


def test_failing_step_stops_with_french_error(pc):
    server = healthy_server().on("apt-get install", code=100)
    code, out = _run(pc, server)
    assert code == 2 and "Installation des paquets système : échec (code 100)" in out
    assert not server.has("uv python install")


def test_check_refuses_wrong_accounts(pc):
    server = FakeServer().on('echo "user=$(id -un)"\necho "xvfb', "user=admin\n", alias=ROOT_ALIAS)
    server.on('echo "home=$HOME"', f"user={USER}\nhome={HOME}\n", alias=USER_ALIAS)
    code, out = _run(pc, server, "--only", "check")
    assert code == 2 and "non root" in out


def test_runner_contract():
    seen = []
    runner = Runner(lambda argv, data, env: seen.append((argv, data, env)) or Result(0, "ok"), dry_run=True,
                    echo=lambda line: None)
    assert runner.run(["git", "push"]).out == "" and seen == []
    assert runner.probe(["git", "status"]).out == "ok" and len(seen) == 1
    assert runner.git(["status"], cwd=Path("."), probe=True).ok
    assert seen[-1][2]["GIT_SSH_COMMAND"].endswith("-o BatchMode=yes -o ConnectTimeout=15")
    assert git_ssh_command(lambda name: r"C:\Windows\System32\OpenSSH\ssh.EXE") == (
        '"C:/Windows/System32/OpenSSH/ssh.EXE" -o BatchMode=yes -o ConnectTimeout=15')  # not Git's own ssh
    assert git_ssh_command(lambda name: None).startswith("ssh -o")
    pkgs = templates.parse_apt_packages('sudo -- sh -c "apt-get update&& apt-get install -y --no-install-recommends '
                                        'libnss3 $(reboot) `id` libfoo;rm -rf / fonts-liberation"')
    assert pkgs == ["libnss3", "fonts-liberation"] and "reboot" not in templates.apt_install_script(pkgs)
    assert steps_app.parse_remote_state(f"{'a' * 64}  ./x.json\nENV_PRESENT\n---SETTINGS_LOCAL---\nk: v\n") == (
        {"x.json": "a" * 64}, True, "k: v\n")


def test_ssh_tunnel_script_is_scoped_idempotent_and_validated():
    from scripts.deploy import templates as tpl

    script = tpl.ssh_tunnel_script("jobuser")
    assert "Match User jobuser" in script and "AllowTcpForwarding local" in script
    assert f"PermitOpen 127.0.0.1:{tpl.DASHBOARD_PORT} localhost:{tpl.DASHBOARD_PORT}" in script
    assert "TUNNEL_ALREADY" in script and "sshd -t" in script and "TUNNEL_INVALID" in script
    assert "AllowTcpForwarding yes" not in script
