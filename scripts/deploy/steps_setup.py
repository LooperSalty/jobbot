"""Steps 1-5: check the server, system packages (root), user toolchain (uv, Python 3.12, Claude Code), code
(bare repository + post-receive hook + push), Python dependencies and Chromium."""

from __future__ import annotations

from . import templates as tpl
from .config import DeployError
from .session import Deployment

REMOTE_NAME = "serveur"


# --------------------------------------------------------------------------- 1. check


def step_check(dep: Deployment) -> None:
    dep.title(1, "Vérification du serveur")
    root = tpl.parse_keyvalues(dep.runner.ssh_probe(dep.config.root_alias, tpl.check_root_script()).out)
    user = tpl.parse_keyvalues(dep.runner.ssh_probe(dep.config.project_alias, tpl.check_project_script()).out)
    if dep.runner.offline:
        dep.say("Lecture du serveur impossible (simulation) : plan affiché sans vérification.")
        return
    if not user or not root:
        raise DeployError("Réponse vide du serveur à la vérification : relance, ou teste « ssh <alias> » à la main.")
    if root.get("user") != "root":
        raise DeployError(f"L'alias root se connecte en tant que « {root.get('user')} » et non root.")
    if user.get("user") != dep.config.project_user:
        raise DeployError(f"L'alias du projet se connecte en tant que « {user.get('user')} », attendu PROJECT_USER.")
    dep.facts.update(user)
    _report_server(dep, user, root)
    _check_disk(dep, user.get("free_mb", ""))
    _check_port(dep, user)


def _report_server(dep: Deployment, user: dict[str, str], root: dict[str, str]) -> None:
    state = lambda present: "présent" if present else "à installer"  # noqa: E731
    dep.say(f"Système : {user.get('os') or '?'} ({user.get('arch') or '?'}) — {user.get('python') or 'pas de Python'}")
    dep.say(f"uv : {state(user.get('uv'))} · Claude Code : {state(user.get('claude'))} · xvfb-run : "
            f"{state(root.get('xvfb'))}")
    if user.get("timezone") and user["timezone"] != "Europe/Paris":
        dep.say(f"Fuseau horaire {user['timezone']} : il sera réglé sur Europe/Paris (étape système).")


def _check_port(dep: Deployment, user: dict[str, str]) -> None:
    if user.get("port") and user.get("dashboard") != "active":
        dep.notes.append(f"Le port {tpl.DASHBOARD_PORT} est déjà utilisé sur le serveur par un autre programme : "
                         "le tableau de bord ne pourra pas démarrer tant qu'il n'est pas libéré.")
        dep.say(f"Attention : port {tpl.DASHBOARD_PORT} déjà occupé.")
    else:
        dep.say(f"Port {tpl.DASHBOARD_PORT} du tableau de bord : disponible.")


def _check_disk(dep: Deployment, free_mb: str) -> None:
    try:
        free = int(free_mb)
    except ValueError:
        return
    dep.say(f"Espace libre : {free} Mo")
    if free < tpl.MIN_FREE_MB:
        raise DeployError(f"Espace disque insuffisant ({free} Mo libres, {tpl.MIN_FREE_MB} Mo au moins : Chromium, "
                          "Python, dépendances).")


# --------------------------------------------------------------------------- 2-3. system, toolchain


def step_system(dep: Deployment) -> None:
    dep.title(2, f"Paquets système (root) : {', '.join(tpl.APT_PACKAGES)}")
    dep.root_ssh(tpl.system_script(), "Installation des paquets système")
    dep.say("Paquets système à jour (les dépendances de Chromium suivent après l'environnement Python).")


def step_toolchain(dep: Deployment) -> None:
    dep.title(3, "Outils du compte projet : uv, Python 3.12, Claude Code")
    result = dep.user_ssh(tpl.toolchain_script(), "Installation de uv / Python 3.12")
    if "CLAUDE_MISSING" in result.out:
        dep.say("Installateur natif de Claude Code en échec : installation par npm (root).")
        fallback = dep.root_ssh(tpl.claude_npm_script(), "Installation de Claude Code (npm)", check=False)
        if not fallback.ok and not dep.dry_run:
            dep.notes.append("Claude Code n'a pas pu être installé sur le serveur : l'IA utilisera les modèles de "
                             "secours (voir l'étape IA).")
    dep.say("uv, Python 3.12 et Claude Code prêts.")


# --------------------------------------------------------------------------- 4. code


def _remote_url(dep: Deployment) -> str:
    return f"{dep.config.project_alias}:{tpl.BARE_REPO}"


def _ensure_git_remote(dep: Deployment) -> None:
    current = dep.runner.git(["remote", "get-url", REMOTE_NAME], cwd=dep.root, probe=True)
    wanted = _remote_url(dep)
    if current.ok and current.out.strip() == wanted:
        dep.say(f"Remote git « {REMOTE_NAME} » : déjà configuré.")
        return
    args = ["remote", "set-url" if current.ok else "add", REMOTE_NAME, wanted]
    dep.require(dep.runner.git(args, cwd=dep.root), f"Remote git « {REMOTE_NAME} »")
    dep.say(f"Remote git « {REMOTE_NAME} » → {wanted}")


def step_code(dep: Deployment) -> None:
    dep.title(4, "Code : dépôt ~/jobbot.git + copie de travail ~/jobbot")
    head = dep.runner.git(["rev-parse", "--short", "HEAD"], cwd=dep.root, probe=True)
    if not head.ok and not dep.dry_run:
        raise DeployError("Aucun commit dans le dépôt local : commite d'abord (git commit) puis relance.")
    dirty = dep.runner.git(["status", "--porcelain", "--untracked-files=no"], cwd=dep.root, probe=True)
    if dirty.ok and dirty.out.strip():
        dep.say("Attention : des modifications non commitées ne seront PAS déployées (seul le dernier commit part).")
    dep.user_ssh(tpl.bare_repo_script(), "Création du dépôt sur le serveur")
    dep.require(dep.runner.upload(dep.config.project_alias, f"{tpl.BARE_REPO}/hooks/post-receive",
                                  tpl.post_receive_hook().encode("utf-8"), mode="755"), "Hook post-receive")
    changed = dep.runner.ssh_probe(dep.config.project_alias, tpl.worktree_changes_script())
    if changed.ok and changed.out.strip():
        dep.say("Attention : fichiers suivis modifiés sur le serveur, remplacés par la version du PC :\n  "
                + "\n  ".join(changed.out.strip().splitlines()[:10]))
    _ensure_git_remote(dep)
    dep.require(dep.runner.git(["push", "--force", REMOTE_NAME, f"HEAD:{tpl.BRANCH}"], cwd=dep.root),
                "git push vers le serveur")
    out = dep.user_ssh(tpl.checkout_script(), "Mise à jour de ~/jobbot").out
    rev = tpl.parse_keyvalues(out).get("REV") or (head.out.strip() if head.ok else "?")
    dep.say(f"Code déployé (commit {rev}). Les prochains « git push serveur » mettront ~/jobbot à jour.")


# --------------------------------------------------------------------------- 5. dependencies


def step_deps(dep: Deployment) -> None:
    dep.title(5, "Dépendances : environnement Python, Chromium et ses bibliothèques")
    dep.user_ssh(tpl.deps_script(), "Installation des dépendances Python / Chromium")
    # root never runs the user's .venv: it installs the validated package list Playwright asks for
    packages = tpl.parse_apt_packages(dep.runner.ssh_probe(dep.config.project_alias, tpl.chromium_deps_script()).out)
    if packages:
        dep.root_ssh(tpl.apt_install_script(packages), "Bibliothèques système de Chromium")
        dep.say(f"Bibliothèques système de Chromium : {len(packages)} paquet(s) vérifié(s).")
    elif not dep.dry_run:
        dep.notes.append("Liste des bibliothèques de Chromium introuvable : en root sur le serveur, lance "
                         "« ~<compte>/jobbot/.venv/bin/python -m playwright install-deps chromium » si le navigateur "
                         "ne démarre pas.")
    dep.say("Environnement .venv, tableau de bord et Chromium installés.")
