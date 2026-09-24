"""Remote shell scripts and generated files of the deployment (pure functions: easy to test and to read).

Paths on the server are relative to the project user's home: ``~/jobbot`` (work tree), ``~/jobbot.git`` (bare
repository whose post-receive hook checks the code out), ``~/.config/systemd/user/jobbot-dashboard.service``.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

APP_DIR = "jobbot"
BARE_REPO = "jobbot.git"
BRANCH = "master"
DASHBOARD_UNIT = "jobbot-dashboard.service"
DASHBOARD_PORT = 8501
UNIT_PATH = f".config/systemd/user/{DASHBOARD_UNIT}"
APT_PACKAGES = ("git", "curl", "xvfb", "ca-certificates", "cron")
LOCAL_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
USER_BUS = 'export XDG_RUNTIME_DIR="/run/user/$(id -u)"'
MIN_FREE_MB = 3000

# settings forced on the server (headless machine) / defaults kept when the server already has a value
FORCED_SETTINGS: dict[str, Any] = {"headless": True, "browser_channel": None}
DEFAULT_SETTINGS: dict[str, Any] = {"notify_email": True}


# --------------------------------------------------------------------------- 1. check


def check_project_script() -> str:
    return f"""
{LOCAL_PATH}
echo "user=$(id -un)"
echo "home=$HOME"
echo "os=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "arch=$(uname -m)"
echo "python=$(python3 --version 2>&1)"
echo "uv=$(command -v uv)"
echo "claude=$(command -v claude)"
echo "free_mb=$(df -Pm "$HOME" | awk 'NR==2 {{print $4}}')"
echo "port=$(ss -ltnH 'sport = :{DASHBOARD_PORT}' 2>/dev/null | head -1)"
{USER_BUS}
echo "dashboard=$(systemctl --user is-active {DASHBOARD_UNIT} 2>/dev/null)"
echo "timezone=$(timedatectl show -p Timezone --value 2>/dev/null)"
"""


def check_root_script() -> str:
    return 'echo "user=$(id -un)"\necho "xvfb=$(command -v xvfb-run)"\necho "docker=$(command -v docker)"\n'


def parse_keyvalues(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().isidentifier():
            values[key.strip()] = value.strip()
    return values


# --------------------------------------------------------------------------- 2-3. system & toolchain


def system_script() -> str:
    return f"""set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq {' '.join(APT_PACKAGES)}
systemctl enable --now cron >/dev/null 2>&1 || true
if [ "$(timedatectl show -p Timezone --value 2>/dev/null)" != "Europe/Paris" ]; then
  timedatectl set-timezone Europe/Paris && echo "Fuseau horaire réglé sur Europe/Paris (heures de cron)"
fi
echo "SYSTEM_OK"
"""


def toolchain_script() -> str:
    return f"""set -eu
{LOCAL_PATH}
if ! command -v uv >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi
uv python install 3.12
if ! command -v claude >/dev/null 2>&1; then
  curl -fsSL https://claude.ai/install.sh | bash || echo "installation native de Claude Code en échec"
fi
if command -v claude >/dev/null 2>&1; then echo "CLAUDE=$(command -v claude)"; else echo "CLAUDE_MISSING"; fi
"""


def claude_npm_script() -> str:
    """Root fallback when the native installer failed (Node.js / npm are on the server)."""
    return "set -eu\nnpm install -g @anthropic-ai/claude-code\necho \"CLAUDE=$(command -v claude)\"\n"


# --------------------------------------------------------------------------- 4. code


def post_receive_hook() -> str:
    return f"""#!/bin/sh
# jobbot : chaque « git push serveur » met à jour ~/{APP_DIR} (branche {BRANCH}). Installé par deploy_server.py.
set -e
while read -r oldrev newrev ref; do
  if [ "$ref" = "refs/heads/{BRANCH}" ]; then
    mkdir -p "$HOME/{APP_DIR}"
    git --git-dir="$HOME/{BARE_REPO}" --work-tree="$HOME/{APP_DIR}" checkout -f {BRANCH}
    echo "jobbot : code déployé dans $HOME/{APP_DIR} ($(printf %s "$newrev" | cut -c1-8))"
  fi
done
"""


def bare_repo_script() -> str:
    return (f'set -eu\ngit init --bare -q "$HOME/{BARE_REPO}"\nmkdir -p "$HOME/{APP_DIR}"\n'
            f'git --git-dir="$HOME/{BARE_REPO}" symbolic-ref HEAD refs/heads/{BRANCH}\n')


def worktree_changes_script() -> str:
    return (f'git --git-dir="$HOME/{BARE_REPO}" rev-parse --verify -q {BRANCH} >/dev/null 2>&1 || exit 0\n'
            f'git --git-dir="$HOME/{BARE_REPO}" --work-tree="$HOME/{APP_DIR}" status --porcelain '
            "--untracked-files=no 2>/dev/null\n")


def checkout_script() -> str:
    return (f'set -eu\ngit --git-dir="$HOME/{BARE_REPO}" --work-tree="$HOME/{APP_DIR}" checkout -f {BRANCH}\n'
            f'echo "REV=$(git --git-dir="$HOME/{BARE_REPO}" rev-parse --short {BRANCH})"\n')


# --------------------------------------------------------------------------- 5. dependencies


def deps_script() -> str:
    return f"""set -eu
{LOCAL_PATH}
cd "$HOME/{APP_DIR}"
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dashboard]"
.venv/bin/python -m playwright install chromium
echo "DEPS_OK"
"""


def chromium_deps_script() -> str:
    """Project user (read-only): the apt command Playwright would run for Chromium's system libraries."""
    return f'cd "$HOME/{APP_DIR}" && .venv/bin/python -m playwright install-deps --dry-run chromium 2>&1; exit 0\n'


_APT_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9.+-]*(:[a-z0-9]+)?$")


def parse_apt_packages(text: str) -> list[str]:
    """Package names after « apt-get install » in Playwright's dry-run output (validated: root installs them,
    so nothing produced by the user account is ever executed as root)."""
    packages: list[str] = []
    for chunk in text.split("apt-get install")[1:]:
        for token in chunk.replace('"', " ").replace("'", " ").split():
            if token in ("&&", ";", "|") or token.startswith(("&", ";")):
                break
            if not token.startswith("-") and _APT_PACKAGE.match(token) and token not in packages:
                packages.append(token)
    return packages


def apt_install_script(packages: list[str]) -> str:
    valid = [p for p in packages if _APT_PACKAGE.match(p)]
    return ("set -eu\nexport DEBIAN_FRONTEND=noninteractive\n"
            f"apt-get install -y -qq --no-install-recommends {' '.join(valid)}\necho SYSTEM_DEPS_OK\n")


# --------------------------------------------------------------------------- 6. private files


def remote_hashes_script(paths: list[str]) -> str:
    quoted = " ".join(shlex.quote(p) for p in paths)
    return (f'cd "$HOME/{APP_DIR}" 2>/dev/null || exit 0\n'
            f'for f in {quoted}; do [ -f "$f" ] && sha256sum "$f"; done\n'
            '[ -f .env ] && echo "ENV_PRESENT"\n'
            'echo "---SETTINGS_LOCAL---"\ncat config/settings.local.yaml 2>/dev/null\nexit 0\n')


def merge_env_script() -> str:
    """Server side: .env = the PC's filtered .env.pc (non-empty values win) + keys only present on the server
    (e.g. TELEGRAM_*, a token added there). No secret value leaves the server."""
    return f"""set -eu
umask 077
cd "$HOME/{APP_DIR}"
touch .env
awk 'FNR==NR {{ if (match($0, /^[A-Za-z_][A-Za-z0-9_]*=/)) {{ k=substr($0,1,RLENGTH-1); v=substr($0,RLENGTH+1);
  if (v != "") {{ pc[k]=1; print }} }} next }}
  match($0, /^[A-Za-z_][A-Za-z0-9_]*=/) {{ k=substr($0,1,RLENGTH-1); if (!(k in pc)) print }}' .env.pc .env > .env.new
chmod 600 .env.new .env.pc
mv -f .env.new .env
echo "ENV_MERGED"
"""


def telegram_script(user: str, token_file: str, chat_file: str) -> str:
    """Root: copy the bot token / chat id from root-only files into the user's .env (values never printed).
    The user's .env is read and written AS the user (runuser): a symlink planted in the user's home can never
    make root read or overwrite another file."""
    q = shlex.quote
    return f"""set -eu
umask 077
U={q(user)}
H=$(getent passwd "$U" | cut -d: -f6)
ENV_FILE="$H/{APP_DIR}/.env"
if [ ! -r {q(token_file)} ] || [ ! -r {q(chat_file)} ] || [ -z "$H" ]; then echo "TELEGRAM_MISSING"; exit 0; fi
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
runuser -u "$U" -- cat "$ENV_FILE" 2>/dev/null | grep -v -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' > "$TMP" || true
printf 'TELEGRAM_BOT_TOKEN=%s\\nTELEGRAM_CHAT_ID=%s\\n' "$(tr -d '[:space:]' < {q(token_file)})" \\
  "$(tr -d '[:space:]' < {q(chat_file)})" >> "$TMP"
runuser -u "$U" -- sh -c 'umask 077; cat > "$1.tmp" && mv -f "$1.tmp" "$1"' sh "$ENV_FILE" < "$TMP"
echo "TELEGRAM_OK"
"""


def settings_local_yaml(current: dict[str, Any], forced: dict[str, Any], defaults: dict[str, Any]) -> str:
    """Server settings.local.yaml: defaults < what the server already has < forced values (headless…)."""
    import yaml

    merged = {**defaults, **current, **forced}
    header = ("# Réglages propres au serveur (prioritaires sur config/settings.yaml, jamais versionnés).\n"
              "# Géré par scripts/deploy_server.py : headless et browser_channel sont imposés, le reste est "
              "conservé.\n")
    return header + yaml.safe_dump(merged, allow_unicode=True, sort_keys=True, default_flow_style=False)


# --------------------------------------------------------------------------- 7-10. llm, init, schedule, dashboard


LLM_CHECK = '''
from pydantic import BaseModel
from jobbot.config import load_secrets, load_settings
from jobbot.context import find_root
from jobbot.llm import LLMError, build_llm

class Ping(BaseModel):
    ok: bool

root = find_root()
settings, secrets = load_settings(root), load_secrets(root)
print("PROVIDER=" + settings.llm.provider)
print("TOKEN=" + ("yes" if secrets.claude_code_oauth_token else "no"))
try:
    answer = build_llm(settings.llm, secrets).complete_json('Réponds exactement {"ok": true}.', Ping, fast=True)
    print("LLM_OK" if answer.ok else "LLM_KO: réponse inattendue")
except LLMError as exc:
    print("LLM_KO: " + " ".join(str(exc).split())[:300])
'''


def llm_script() -> str:
    return f'{LOCAL_PATH}\ncd "$HOME/{APP_DIR}"\n.venv/bin/python - <<\'PY\'\n{LLM_CHECK.strip()}\nPY\n'


def jobbot_script(*commands: str) -> str:
    lines = [LOCAL_PATH, f'cd "$HOME/{APP_DIR}"']
    lines += [f".venv/bin/jobbot {cmd}; echo \"EXIT_{i}=$?\"" for i, cmd in enumerate(commands)]
    return "\n".join(lines) + "\n"


def dashboard_unit(host: str, port: int = DASHBOARD_PORT) -> str:
    return f"""[Unit]
Description=jobbot — tableau de bord (Streamlit)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/{APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=%h/{APP_DIR}/.venv/bin/jobbot dashboard --host {host} --port {port}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""


def dashboard_start_script() -> str:
    return f"""set -eu
{USER_BUS}
systemctl --user daemon-reload
systemctl --user enable {DASHBOARD_UNIT} >/dev/null 2>&1
systemctl --user restart {DASHBOARD_UNIT}
sleep 3
echo "STATE=$(systemctl --user is-active {DASHBOARD_UNIT} || true)"
"""


def linger_script(user: str) -> str:
    return f"set -eu\nloginctl enable-linger {shlex.quote(user)}\necho LINGER_OK\n"
