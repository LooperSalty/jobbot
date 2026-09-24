"""Deployment configuration: the PC's .env (key names documented in .env.example) and the optional, gitignored
``config/deploy.local.yaml`` (server-specific details that must never be committed).

- Required .env keys: SERVER_ROOT_HOST_ALIAS (ssh alias of root), PROJECT_SSH_HOST_ALIAS + PROJECT_USER (the
  unprivileged account that owns the install). Optional: SERVER_IP_TAILSCALE (dashboard on the VPN, only with
  ``--dashboard-vpn``), WEBHOOK_BASE_URL (only its port is read: port of the application being replaced).
- ``filter_env``: the .env copied to the server never contains a SERVER_*, PROJECT_*, WEBHOOK_* key, nor any
  *ROOT* / *PASSWORD* key except SMTP_PASSWORD; comments are dropped (they may describe the infrastructure).
- ``config/deploy.local.yaml`` (all optional)::

      settings_local: {search_engine_url: "http://127.0.0.1:8888"}   # forced into the server settings.local.yaml
      telegram: {token_file: /chemin/jeton-du-bot, chat_id_file: /chemin/id-conversation}  # root-only files read ON the server
      openclaw: {backup_extra: [scripts, .env], keep: [mon-service]}  # replacement step (paths under the home)
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REQUIRED_KEYS = ("SERVER_ROOT_HOST_ALIAS", "PROJECT_SSH_HOST_ALIAS", "PROJECT_USER")
DROP_PREFIXES = ("SERVER_", "PROJECT_", "WEBHOOK_")
KEEP_PASSWORDS = frozenset({"SMTP_PASSWORD"})
TELEGRAM_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")  # filled on the server from root-only files
DEFAULT_OLD_APP_PORT = 3000
DEPLOY_LOCAL = "config/deploy.local.yaml"
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")  # never starts with "-" (ssh option injection)
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._/@+-]+$")


class DeployError(RuntimeError):
    """Configuration or deployment problem (French, user-facing)."""


# --------------------------------------------------------------------------- .env


def raw_env(text: str) -> list[tuple[str, str]]:
    """``(KEY, raw value)`` in order — the value exactly as written (quotes kept); comments, blank and malformed
    lines ignored; a value that is only a comment counts as empty."""
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if _KEY_RE.match(key):
            value = value.strip()
            pairs.append((key, "" if value.startswith("#") else value))
    return pairs


def parse_env(text: str) -> list[tuple[str, str]]:
    """``(KEY, value)`` with surrounding quotes removed (values used by the deployment itself)."""
    unquote = lambda v: v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v  # noqa: E731
    return [(key, unquote(value)) for key, value in raw_env(text)]


def is_private_key(key: str) -> bool:
    """True for keys that must never reach the server: SERVER_* / PROJECT_* / WEBHOOK_*, anything containing ROOT,
    anything containing PASSWORD except SMTP_PASSWORD."""
    upper = key.upper()
    if upper.startswith(DROP_PREFIXES) or "ROOT" in upper:
        return True
    return "PASSWORD" in upper and upper not in KEEP_PASSWORDS


def filter_env(text: str, *, drop: tuple[str, ...] = ()) -> str:
    """The .env copied to the server: allowed ``KEY=value`` lines only (raw values, last occurrence wins),
    no comment."""
    kept: dict[str, str] = {}
    for key, value in raw_env(text):
        if not is_private_key(key) and key not in drop:
            kept[key] = value
    return "".join(f"{key}={value}\n" for key, value in kept.items())


# --------------------------------------------------------------------------- deploy.local.yaml


@dataclass(frozen=True)
class LocalDeploy:
    settings_local: dict[str, Any] = field(default_factory=dict)
    telegram_token_file: str | None = None
    telegram_chat_id_file: str | None = None
    backup_extra: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()

    @property
    def telegram(self) -> bool:
        return bool(self.telegram_token_file and self.telegram_chat_id_file)


def _clean_path(value: Any, what: str) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not _PATH_RE.match(text) or ".." in text.split("/"):
        raise DeployError(f"{DEPLOY_LOCAL} : chemin invalide pour {what} : « {text} »")
    return text


def load_local_deploy(root: Path) -> LocalDeploy:
    path = root / DEPLOY_LOCAL
    if not path.exists():
        return LocalDeploy()
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise DeployError(f"{DEPLOY_LOCAL} illisible : {exc}") from exc
    if not isinstance(data, dict):
        raise DeployError(f"{DEPLOY_LOCAL} doit contenir des paires clé: valeur")
    telegram, old_app = data.get("telegram") or {}, data.get("openclaw") or {}
    settings_local = data.get("settings_local") or {}
    if not all(isinstance(x, dict) for x in (telegram, old_app, settings_local)):
        raise DeployError(f"{DEPLOY_LOCAL} : settings_local, telegram et openclaw sont des sections clé: valeur")
    extra = [_clean_path(p, "openclaw.backup_extra") for p in old_app.get("backup_extra") or []]
    return LocalDeploy(
        settings_local=dict(settings_local),
        telegram_token_file=_clean_path(telegram.get("token_file"), "telegram.token_file"),
        telegram_chat_id_file=_clean_path(telegram.get("chat_id_file"), "telegram.chat_id_file"),
        backup_extra=tuple(p for p in extra if p),
        keep=tuple(str(k).strip().lower() for k in old_app.get("keep") or [] if str(k).strip()),
    )


# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class DeployConfig:
    root_alias: str
    project_alias: str
    project_user: str
    vpn_ip: str | None = None
    old_app_port: int = DEFAULT_OLD_APP_PORT
    local: LocalDeploy = field(default_factory=LocalDeploy)


def _port_of(url: str | None) -> int:
    if not url:
        return DEFAULT_OLD_APP_PORT
    try:
        return urlparse(url).port or DEFAULT_OLD_APP_PORT
    except ValueError:
        return DEFAULT_OLD_APP_PORT


def config_from_env(values: dict[str, str], local: LocalDeploy | None = None) -> DeployConfig:
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise DeployError(f"Clé(s) absente(s) du .env du PC : {', '.join(missing)}. Ajoute-les (voir .env.example "
                          "et le README, « Installation sur un serveur Linux »).")
    aliases = {k: values[k].strip() for k in ("SERVER_ROOT_HOST_ALIAS", "PROJECT_SSH_HOST_ALIAS")}
    bad = [k for k, v in aliases.items() if not _ALIAS_RE.match(v)]
    if bad:
        raise DeployError(f"Alias SSH invalide dans .env : {', '.join(bad)} (nom d'hôte de ton ~/.ssh/config)")
    user = values["PROJECT_USER"].strip()
    if not _USER_RE.match(user) or user == "root":
        raise DeployError("PROJECT_USER invalide : un compte Linux ordinaire (pas root) est attendu.")
    vpn = (values.get("SERVER_IP_TAILSCALE") or "").strip() or None
    if vpn:
        try:
            vpn = str(ipaddress.ip_address(vpn))
        except ValueError as exc:
            raise DeployError("SERVER_IP_TAILSCALE n'est pas une adresse IP.") from exc
    return DeployConfig(root_alias=aliases["SERVER_ROOT_HOST_ALIAS"], project_alias=aliases["PROJECT_SSH_HOST_ALIAS"],
                        project_user=user, vpn_ip=vpn, old_app_port=_port_of(values.get("WEBHOOK_BASE_URL")),
                        local=local or LocalDeploy())


def load_config(root: Path) -> tuple[DeployConfig, str]:
    """(configuration, raw text of the PC .env). DeployError (French) when unusable."""
    env = root / ".env"
    if not env.exists():
        raise DeployError(f".env introuvable dans {root} : copie .env.example en .env et remplis les clés serveur.")
    text = env.read_text(encoding="utf-8")
    return config_from_env(dict(parse_env(text)), load_local_deploy(root)), text
