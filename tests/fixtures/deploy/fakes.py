"""Fake command executor for scripts/deploy: records every ssh / git command, answers from scripted rules.

Only placeholders (mon-serveur, jobuser, 100.64.0.1): no real infrastructure detail in tests.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from scripts.deploy.runner import Result

ROOT_ALIAS = "mon-serveur-root"
USER_ALIAS = "mon-serveur"
USER = "jobuser"
HOME = f"/home/{USER}"
VPN_IP = "100.64.0.1"

ENV_TEXT = f"""# Commentaire décrivant l'infrastructure (ne doit jamais partir)
SMTP_USER=candidat@example.org
SMTP_PASSWORD=abcd efgh ijkl mnop
FRANCE_TRAVAIL_CLIENT_ID="ft-id"
HUNTER_API_KEY=   # optionnel
CLAUDE_CODE_OAUTH_TOKEN=jeton-abonnement
SERVER_SSH_USER={USER}
SERVER_SSH_PASSWORD=mdp-serveur
SERVER_ROOT_PASSWORD=mdp-root
SERVER_ROOT_HOST_ALIAS={ROOT_ALIAS}
SERVER_IP_LAN=192.0.2.10
SERVER_IP_TAILSCALE={VPN_IP}
PROJECT_USER={USER}
PROJECT_USER_PASSWORD=mdp-projet
PROJECT_SSH_HOST_ALIAS={USER_ALIAS}
WEBHOOK_SECRET=secret-webhook
WEBHOOK_BASE_URL=http://{VPN_IP}:3000
DB_ROOT_TOKEN=racine
"""
PRIVATE_VALUES = ("mdp-serveur", "mdp-root", "mdp-projet", "secret-webhook", "racine", "192.0.2.10")

_B64 = re.compile(r"printf %s (\S+) \| base64 -d")


def script_of(argv: list[str]) -> str:
    """Decoded remote script of an ssh command ('' for other commands)."""
    if not argv or argv[0] != "ssh":
        return ""
    match = _B64.search(argv[-1])
    return base64.b64decode(match[1]).decode("utf-8") if match else argv[-1]


@dataclass
class Call:
    argv: list[str]
    data: bytes | None
    env: dict | None
    script: str

    @property
    def alias(self) -> str:
        return self.argv[5] if self.argv and self.argv[0] == "ssh" else ""

    @property
    def text(self) -> str:
        return self.script or " ".join(self.argv)


@dataclass
class FakeServer:
    """``rules``: (predicate on Call, answer); first match wins, default = success with empty output.
    An answer is a ``Result`` or a callable returning one (successive answers)."""

    rules: list[tuple[Callable[[Call], bool], Result | Callable[[], Result]]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def __call__(self, argv: list[str], data: bytes | None, env: dict | None) -> Result:
        call = Call(list(argv), data, env, script_of(argv))
        self.calls.append(call)
        for predicate, answer in self.rules:
            if predicate(call):
                return answer if isinstance(answer, Result) else answer()
        return Result(0, "", "")

    def on(self, needle: str, out: str = "", code: int = 0, alias: str | None = None) -> FakeServer:
        self.rules.append((lambda c: needle in c.text and (alias is None or c.alias == alias), Result(code, out, "")))
        return self

    def sequence(self, needle: str, outs: list[str]) -> FakeServer:
        """Successive answers for the calls containing ``needle`` (the last one repeats)."""
        queue = list(outs)

        def answer() -> Result:
            return Result(0, queue.pop(0) if len(queue) > 1 else queue[0], "")

        self.rules.append((lambda c: needle in c.text, answer))
        return self

    def index(self, needle: str) -> int:
        return next(i for i, c in enumerate(self.calls) if needle in c.text)

    def has(self, needle: str) -> bool:
        return any(needle in c.text for c in self.calls)

    def uploads(self) -> dict[str, bytes]:
        found = {}
        for c in self.calls:
            match = re.search(r"cat > (\S+)\.tmp", c.script)
            if match and c.data is not None:
                found[match[1].strip("'")] = c.data
        return found


def answering_checks(server: FakeServer | None = None) -> FakeServer:
    """``server`` + answers to the two read-only probes of the « check » step."""
    server = server or FakeServer()
    server.on('echo "user=$(id -un)"\necho "xvfb', "user=root\nxvfb=\ndocker=/usr/bin/docker\n", alias=ROOT_ALIAS)
    server.on('echo "home=$HOME"', f"user={USER}\nhome={HOME}\nos=Debian GNU/Linux 13\narch=x86_64\n"
                                   "python=Python 3.13\nuv=\nclaude=\nfree_mb=40000\nport=\ndashboard=inactive\n"
                                   "timezone=Europe/Paris\n", alias=USER_ALIAS)
    return server


def healthy_server() -> FakeServer:
    """A reachable server where everything succeeds."""
    server = answering_checks()
    server.on("remote get-url", "", code=2)
    server.on("rev-parse --short HEAD", "abc1234\n")
    server.on("install-deps --dry-run chromium", 'sudo -- sh -c "apt-get update&& apt-get install -y '
                                                 '--no-install-recommends libnss3 libasound2t64 fonts-liberation"\n')
    server.on("checkout -f master", "REV=abc1234\n")
    server.on(".venv/bin/python - <<'PY'", "PROVIDER=claude_cli\nTOKEN=yes\nLLM_OK\n")
    server.on(".venv/bin/jobbot init", "Diagnostic jobbot\nEXIT_0=0\nEXIT_1=0\n")
    server.on(".venv/bin/jobbot schedule install", "Crontab : 3 tâche(s)\nEXIT_0=0\n")
    server.on("systemctl --user restart jobbot-dashboard", "STATE=active\n")
    server.on("TELEGRAM_BOT_TOKEN=%s", "TELEGRAM_OK\n")
    return server
