"""Steps 6-11: private files (only when their sha256 differs), AI check, jobbot init + doctor, cron, dashboard
service, summary. Secret values are never printed: only file names, sizes and states."""

from __future__ import annotations

import hashlib
from typing import Any

from . import templates as tpl
from .config import TELEGRAM_KEYS, DeployError, filter_env
from .session import Deployment

SESSIONS_DIR = "data/sessions"
SESSION_SITES = ("linkedin", "hellowork", "wttj", "classroom")
PROFILE = "config/profile.yaml"
SETTINGS_LOCAL = "config/settings.local.yaml"
ENV_PC = ".env.pc"  # last filtered .env sent by the PC (kept on the server: idempotency, never contains root keys)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- 6. private files


def private_files(dep: Deployment) -> list[tuple[str, bytes]]:
    """(path relative to ~/jobbot, content) of the profile and the exported sessions present on the PC."""
    files: list[tuple[str, bytes]] = []
    profile = dep.root / PROFILE
    if profile.is_file():
        files.append((PROFILE, profile.read_bytes()))
    else:
        dep.notes.append("Profil absent du PC (config/profile.yaml) : le serveur garde le modèle fictif.")
    sessions = sorted(p for p in (dep.root / SESSIONS_DIR).glob("*.json") if p.stem in SESSION_SITES)
    files.extend((f"{SESSIONS_DIR}/{p.name}", p.read_bytes()) for p in sessions)
    if not sessions:
        dep.notes.append("Aucune session exportée : sur le PC, « jobbot login linkedin » (hellowork, classroom…), "
                         "« jobbot session export all », puis « python scripts/deploy_server.py --only private ».")
    return files


def parse_remote_state(out: str) -> tuple[dict[str, str], bool, str]:
    """(sha256 by path, .env present?, settings.local.yaml text) from ``remote_hashes_script``."""
    head, _, settings = out.partition("---SETTINGS_LOCAL---")
    hashes = {}
    for line in head.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            hashes[parts[1].removeprefix("./")] = parts[0]
    return hashes, "ENV_PRESENT" in head, settings.lstrip("\n")


def server_settings(dep: Deployment, remote_text: str) -> tuple[str, bool]:
    """(settings.local.yaml content, changed?) — forced headless values, defaults, server edits kept."""
    import yaml

    try:
        current = yaml.safe_load(remote_text) or {}
    except yaml.YAMLError:
        current = {}
    current = current if isinstance(current, dict) else {}
    forced: dict[str, Any] = {**tpl.FORCED_SETTINGS, **dep.config.local.settings_local}
    defaults: dict[str, Any] = {**tpl.DEFAULT_SETTINGS, **({"notify_telegram": True} if dep.config.local.telegram
                                                           else {})}
    text = tpl.settings_local_yaml(current, forced, defaults)
    return text, yaml.safe_load(text) != current or not remote_text.strip()


def _send(dep: Deployment, rel: str, data: bytes, remote_hash: str | None, label: str) -> bool:
    if remote_hash == sha256(data):
        dep.say(f"{label} : à jour")
        return False
    dep.require(dep.runner.upload(dep.config.project_alias, f"{tpl.APP_DIR}/{rel}", data), f"Envoi de {label}")
    dep.say(f"{label} : envoyé ({len(data)} octets, droits 600)")
    return True


def step_private(dep: Deployment) -> None:
    dep.title(6, "Fichiers privés : profil, .env filtré, sessions, réglages du serveur")
    files = private_files(dep)
    probe = dep.runner.ssh_probe(dep.config.project_alias, tpl.remote_hashes_script(
        [rel for rel, _ in files] + [ENV_PC]))
    hashes, env_present, settings_text = parse_remote_state(probe.out)
    for rel, data in files:
        _send(dep, rel, data, hashes.get(rel), rel)
    _send_env(dep, hashes.get(ENV_PC), env_present)
    text, changed = server_settings(dep, settings_text)
    if changed:
        _send(dep, SETTINGS_LOCAL, text.encode("utf-8"), None, SETTINGS_LOCAL)
    else:
        dep.say(f"{SETTINGS_LOCAL} : à jour")
    if dep.config.local.telegram:
        _telegram(dep)


def _send_env(dep: Deployment, remote_hash: str | None, env_present: bool) -> None:
    drop = TELEGRAM_KEYS if dep.config.local.telegram else ()
    data = filter_env(dep.env_text, drop=drop).encode("utf-8")
    if remote_hash == sha256(data) and env_present:
        dep.say(".env : à jour")
        return
    _send(dep, ENV_PC, data, None, ".env filtré (sans clés SERVER_/PROJECT_/WEBHOOK_ ni mots de passe root)")
    dep.user_ssh(tpl.merge_env_script(), "Fusion du .env sur le serveur")
    dep.say(".env du serveur mis à jour (clés propres au serveur conservées), droits 600")


def _telegram(dep: Deployment) -> None:
    local = dep.config.local
    out = dep.root_ssh(tpl.telegram_script(dep.config.project_user, str(local.telegram_token_file),
                                           str(local.telegram_chat_id_file)), "Clés Telegram").out
    if "TELEGRAM_OK" in out:
        dep.say("Telegram : jeton du bot et conversation copiés sur le serveur (valeurs jamais affichées)")
    elif not dep.dry_run:
        dep.notes.append("Telegram : fichiers du bot illisibles sur le serveur (voir telegram dans "
                         "config/deploy.local.yaml) : notifications Telegram inactives.")


# --------------------------------------------------------------------------- 7. llm


LLM_FIXES = (
    "  1) Abonnement Claude (recommandé) : sur le PC, lance « claude setup-token », copie le jeton dans le .env du "
    "PC (CLAUDE_CODE_OAUTH_TOKEN=…), puis « python scripts/deploy_server.py --only private ».\n"
    "  2) Clé API (payante) : ANTHROPIC_API_KEY dans le .env du PC et « llm: {provider: anthropic} » dans "
    "settings_local de config/deploy.local.yaml, puis --only private."
)


def step_llm(dep: Deployment) -> None:
    dep.title(7, "IA : test de Claude sur le serveur")
    result = dep.user_ssh(tpl.llm_script(), "Test de l'IA", check=False)
    if dep.dry_run:
        return
    values = tpl.parse_keyvalues(result.out)
    if "LLM_OK" in result.out:
        dep.say(f"IA opérationnelle ({values.get('PROVIDER', '?')}).")
        return
    reason = next((line for line in result.out.splitlines() if line.startswith("LLM_KO")), "LLM_KO: test impossible")
    missing = values.get("PROVIDER") == "claude_cli" and values.get("TOKEN") == "no"
    dep.say(f"IA indisponible ({reason.removeprefix('LLM_KO: ')})"
            + (" — jeton CLAUDE_CODE_OAUTH_TOKEN absent du .env." if missing else ""))
    dep.say("Deux solutions :\n" + LLM_FIXES)
    dep.notes.append("IA indisponible sur le serveur : jobbot utilise les textes de secours (voir l'étape 7).")


# --------------------------------------------------------------------------- 8-9. init/doctor, schedule


def _print_jobbot(dep: Deployment, out: str) -> dict[str, str]:
    codes = {}
    for line in out.splitlines():
        if line.startswith("EXIT_"):
            key, _, value = line.partition("=")
            codes[key] = value
        else:
            dep.say(line)
    return codes


def step_init(dep: Deployment) -> None:
    dep.title(8, "jobbot init + jobbot doctor sur le serveur")
    result = dep.user_ssh(tpl.jobbot_script("init", "doctor"), "jobbot init / doctor", check=False)
    codes = _print_jobbot(dep, result.out)
    if codes.get("EXIT_0", "0") != "0" and not dep.dry_run:
        raise DeployError("jobbot init a échoué (voir ci-dessus).")
    if codes.get("EXIT_1", "0") != "0":
        dep.notes.append("jobbot doctor signale des problèmes bloquants : lis le tableau de l'étape 8.")


def step_schedule(dep: Deployment) -> None:
    dep.title(9, "Tâches planifiées (cron) : run 09:30, réponses + relances 18:00, Classroom, nuit 00:30 "
                 "(collecte, priorité basse)")
    result = dep.user_ssh(tpl.jobbot_script("schedule install"), "jobbot schedule install", check=False)
    codes = _print_jobbot(dep, result.out)
    if codes.get("EXIT_0", "0") != "0" and not dep.dry_run:
        raise DeployError("jobbot schedule install a échoué (voir ci-dessus).")


# --------------------------------------------------------------------------- 10-11. dashboard, summary


def dashboard_host(dep: Deployment) -> str:
    if not dep.dashboard_vpn:
        return "127.0.0.1"
    if not dep.config.vpn_ip:
        raise DeployError("--dashboard-vpn demandé mais SERVER_IP_TAILSCALE est absent du .env.")
    return dep.config.vpn_ip


def step_dashboard(dep: Deployment) -> None:
    dep.title(10, "Tableau de bord : service systemd utilisateur jobbot-dashboard")
    host = dashboard_host(dep)
    dep.require(dep.runner.upload(dep.config.project_alias, tpl.UNIT_PATH, tpl.dashboard_unit(host).encode("utf-8"),
                                  mode="644"), "Service du tableau de bord")
    dep.root_ssh(tpl.linger_script(dep.config.project_user), "Services au démarrage (loginctl enable-linger)")
    out = dep.user_ssh(tpl.dashboard_start_script(), "Démarrage du tableau de bord").out
    state = tpl.parse_keyvalues(out).get("STATE", "")
    if state and state != "active":
        dep.notes.append(f"Le service jobbot-dashboard est « {state} » : journalctl --user -u jobbot-dashboard.")
    if host == "127.0.0.1":
        tunnel = dep.root_ssh(tpl.ssh_tunnel_script(dep.config.project_user), "Tunnel SSH du tableau de bord").out
        if "TUNNEL_INVALID" in tunnel:
            dep.notes.append("Autorisation du tunnel SSH refusée par sshd -t (config restaurée) : voir /etc/ssh.")
        dep.say(f"Tableau de bord sur le serveur en 127.0.0.1:{tpl.DASHBOARD_PORT} (jamais exposé au réseau).")
        dep.say("Depuis le PC : powershell -ExecutionPolicy Bypass -File scripts\\dashboard_serveur.ps1 "
                f"(tunnel SSH), puis http://localhost:{tpl.DASHBOARD_PORT}")
    else:
        dep.say(f"Tableau de bord : http://{host}:{tpl.DASHBOARD_PORT} (VPN uniquement)")


def step_summary(dep: Deployment) -> None:
    dep.title(11, "Résumé")
    dep.say("jobbot est installé sur le serveur : ~/jobbot (code), cron (run 09:30, réponses 18:00, Classroom, "
            "nuit 00:30), "
            "tableau de bord en service.")
    for note in dep.notes:
        dep.say(f"• {note}")
    dep.say("Étapes suivantes (sur le PC) :\n"
            "  1. jobbot login linkedin / hellowork / classroom (connexion à la main, une fois)\n"
            "  2. jobbot session export all\n"
            "  3. python scripts/deploy_server.py --only private   (envoie les sessions)\n"
            "  4. Mises à jour du code : python scripts/deploy_server.py --update")
