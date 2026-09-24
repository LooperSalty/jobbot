"""Every command of the deployment goes through ``Runner`` (ssh, local git), so tests can replace the executor.

- ``probe``: read-only command (state of the server, hashes, discovery), executed even with ``--dry-run`` so the
  plan shown is exact; when the server cannot be reached in dry-run, probes return an « unknown » result.
- ``run``: command that changes something; with ``--dry-run`` it is only printed.
- Remote scripts are sent base64-encoded as the argument of ``bash -c`` (stdin stays free for file uploads
  and ``crontab -``; no quoting problem between Windows and Linux shells).
- ssh always runs with ``BatchMode=yes`` (key authentication only, never a password prompt) and
  ``ConnectTimeout=15``; exit code 255 = connection/authentication failure → ``SshUnreachable``.
"""

from __future__ import annotations

import base64
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import DeployError

SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
SSH_UNREACHABLE = 255
TIMEOUT_S = 3600  # apt / uv / playwright downloads can be slow


@dataclass(frozen=True)
class Result:
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


UNKNOWN = Result(-1, "", "serveur injoignable (simulation)")

Executor = Callable[[list[str], bytes | None, dict[str, str] | None], Result]


class SshUnreachable(DeployError):
    def __init__(self, alias: str, detail: str = ""):
        super().__init__(f"Connexion SSH impossible à « {alias} »")
        self.alias = alias
        self.detail = detail


def subprocess_executor(argv: list[str], data: bytes | None, env: dict[str, str] | None) -> Result:
    import os

    try:
        proc = subprocess.run(argv, input=data, capture_output=True, timeout=TIMEOUT_S, check=False,
                              env={**os.environ, **env} if env else None)
    except FileNotFoundError as exc:
        raise DeployError(f"Commande introuvable : {argv[0]} (installe OpenSSH / Git pour Windows)") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"Délai dépassé ({TIMEOUT_S} s) : {argv[0]}") from exc
    decode = lambda raw: (raw or b"").decode("utf-8", "replace")  # noqa: E731
    return Result(proc.returncode, decode(proc.stdout), decode(proc.stderr))


def git_ssh_command(which: Callable[[str], str | None] = shutil.which) -> str:
    """The ssh used by ``git push``: the same executable as every other command (on Windows, OpenSSH and its
    agent, not the ssh bundled with Git for Windows), key authentication only."""
    found = which("ssh")
    exe = f'"{Path(found).as_posix()}"' if found else "ssh"
    return f"{exe} {' '.join(SSH_OPTIONS)}"


def encode_script(script: str) -> str:
    """Remote command running ``script`` with bash (base64: safe through every shell, stdin left free)."""
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f'bash -c "$(printf %s {payload} | base64 -d)"'


class Runner:
    def __init__(self, executor: Executor = subprocess_executor, *, dry_run: bool = False,
                 echo: Callable[[str], None] = print):
        self.executor = executor
        self.dry_run = dry_run
        self.echo = echo
        self.offline = False  # dry-run with an unreachable server: probes answer UNKNOWN

    # ------------------------------------------------------------------ low level

    def probe(self, argv: list[str], data: bytes | None = None, env: dict[str, str] | None = None) -> Result:
        if self.offline:
            return UNKNOWN
        return self.executor(argv, data, env)

    def run(self, argv: list[str], data: bytes | None = None, env: dict[str, str] | None = None, *,
            show: str | None = None) -> Result:
        if self.dry_run:
            self.echo(f"[simulation] {show or shlex.join(argv)}")
            return Result(0)
        return self.executor(argv, data, env)

    # ------------------------------------------------------------------ ssh

    def _ssh_argv(self, alias: str, remote: str) -> list[str]:
        return ["ssh", *SSH_OPTIONS, alias, remote]

    def _checked(self, alias: str, result: Result) -> Result:
        if result.code == SSH_UNREACHABLE:
            if self.dry_run:
                self.offline = True
                self.echo(f"[simulation] « {alias} » injoignable : suite du plan sans lecture du serveur.")
                return UNKNOWN
            raise SshUnreachable(alias, result.err.strip()[:300])
        return result

    def ssh_probe(self, alias: str, script: str) -> Result:
        """Read-only remote script."""
        return self._checked(alias, self.probe(self._ssh_argv(alias, encode_script(script))))

    def ssh_run(self, alias: str, script: str, *, data: bytes | None = None, show: str | None = None) -> Result:
        """Remote script that changes something (only printed in dry-run; ``show`` replaces a script that
        embeds data which must not be printed, e.g. crontab lines)."""
        shown = f"ssh {alias} : {show}" if show else f"ssh {alias} <<script\n{script.strip()}\nscript"
        result = self.run(self._ssh_argv(alias, encode_script(script)), data, show=shown)
        return self._checked(alias, result)

    def upload(self, alias: str, remote_path: str, data: bytes, *, mode: str = "600") -> Result:
        """Write ``data`` to ``remote_path`` (relative to the remote home) atomically, with ``mode``."""
        path = shlex.quote(remote_path)
        script = (f'umask 077 && cd "$HOME" && mkdir -p "$(dirname {path})" && cat > {path}.tmp && '
                  f"chmod {mode} {path}.tmp && mv -f {path}.tmp {path}")
        result = self.run(self._ssh_argv(alias, encode_script(script)), data,
                          show=f"envoi de {remote_path} vers {alias} ({len(data)} octets, droits {mode})")
        return self._checked(alias, result)

    # ------------------------------------------------------------------ local git

    def git(self, args: list[str], *, cwd: Path, probe: bool = False) -> Result:
        argv = ["git", "-C", str(cwd), *args]
        env = {"GIT_SSH_COMMAND": git_ssh_command()}
        return self.probe(argv, env=env) if probe else self.run(argv, env=env, show=f"git {shlex.join(args)}")


def ssh_help(alias: str) -> str:
    """French instructions when key authentication fails (never asks for a password)."""
    return (
        f"Connexion SSH impossible avec l'alias « {alias} » (authentification par clé uniquement).\n"
        "  1. Le serveur est-il allumé et joignable (même réseau ou VPN actif) ? Essaie : ssh " + alias + "\n"
        "  2. L'alias existe-t-il dans ton fichier ~/.ssh/config (HostName, User, IdentityFile) ?\n"
        "  3. Ta clé publique est-elle autorisée sur le serveur ? Depuis un poste qui y a accès :\n"
        "       ssh-copy-id -i ~/.ssh/<ta_cle>.pub <utilisateur>@<serveur>\n"
        "     (sous Windows : type $env:USERPROFILE\\.ssh\\<ta_cle>.pub | ssh <utilisateur>@<serveur> "
        "\"cat >> ~/.ssh/authorized_keys\")\n"
        "  4. Relance ensuite : python scripts/deploy_server.py"
    )
