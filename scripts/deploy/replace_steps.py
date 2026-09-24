"""Replacement of the previous application: discover → show the plan → backup (always first) → stop → crontab
lines (filtered on the server) → port check. ``--purge-openclaw`` deletes, only when a backup already exists and
after a fresh one; ``--restore-openclaw`` brings the application back from a backup. With ``--dry-run`` only
read-only discovery runs: the plan is printed, nothing changes."""

from __future__ import annotations

import json
from datetime import datetime

from . import replace_scripts as rs
from .config import DeployError
from .replace_plan import (
    KEEP_PATTERNS,
    STOP_PATTERNS,
    Discovery,
    Plan,
    build_plan,
    clean_patterns,
    describe,
    manifest_json,
    matches,
    parse_discovery,
    redact,
)
from .session import Deployment

RESTORE_HINT = "python scripts/deploy_server.py --restore-openclaw"


def _keep(dep: Deployment) -> tuple[str, ...]:
    return clean_patterns((*KEEP_PATTERNS, *dep.config.local.keep))


def discover(dep: Deployment) -> tuple[Discovery, Plan]:
    cfg = dep.config
    root_script = rs.discovery_root_script(cfg.project_user, STOP_PATTERNS, _keep(dep), cfg.old_app_port)
    root_out = dep.runner.ssh_probe(cfg.root_alias, root_script).out
    user_out = dep.runner.ssh_probe(cfg.project_alias, rs.discovery_user_script(STOP_PATTERNS)).out
    disc = parse_discovery(root_out, user_out)  # the project account's output is trusted less (see parse)
    return disc, build_plan(disc, stop=STOP_PATTERNS, keep=_keep(dep), extra=cfg.local.backup_extra)


def _show(dep: Deployment, disc: Discovery, plan: Plan) -> bool:
    """Print the plan; False when the server could not be read (dry-run offline)."""
    if dep.runner.offline:
        dep.say("Serveur injoignable : découverte impossible, rien à montrer (simulation).")
        return False
    for line in describe(plan, disc, dep.config.old_app_port):
        dep.say(line)
    return True


# --------------------------------------------------------------------------- backup / stop


def backup(dep: Deployment, disc: Discovery, plan: Plan) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest = manifest_json(plan, disc, user=dep.config.project_user, created=stamp, port=dep.config.old_app_port,
                             stop=STOP_PATTERNS, keep=_keep(dep))
    script = rs.backup_script(dep.config.project_user, stamp, manifest, [(u.scope, u.path) for u in plan.stop_units],
                              list(plan.backup_paths), [c.id for c in plan.stop_containers])
    shown = (f"sauvegarde (root, droits 600) de {len(plan.backup_paths)} chemin(s), {len(plan.stop_units)} "
             f"fichier(s) de service, crontabs, docker inspect et état pm2 → ~/backups/openclaw-{stamp}.tar.gz")
    out = dep.root_ssh(script, "Sauvegarde de l'ancienne application", show=shown).out
    archive = next((line.split("=", 1)[1] for line in out.splitlines() if line.startswith("BACKUP=")), "")
    if not archive and not dep.dry_run:
        raise DeployError("Sauvegarde non créée : rien n'a été arrêté ni supprimé.")
    size = next((line.split("=", 1)[1] for line in out.splitlines() if line.startswith("SIZE=")), "?")
    dep.say(f"Sauvegarde : {archive or '~/backups/openclaw-<date>.tar.gz'} ({size})")
    return archive


def stop(dep: Deployment, plan: Plan) -> None:
    system = [u.name for u in plan.stop_units if u.scope == "system"]
    user = [u.name for u in plan.stop_units if u.scope == "user"]
    root_pm2 = [a.name for a in plan.stop_pm2 if a.owner == "root"]
    user_pm2 = [a.name for a in plan.stop_pm2 if a.owner == "user"]
    containers = [c.id for c in plan.stop_containers]
    if system or containers or root_pm2:
        out = dep.root_ssh(rs.stop_script(system, containers, root_pm2, user_scope=False), "Arrêt (root)").out
        _report(dep, out)
    if user or user_pm2:
        out = dep.user_ssh(rs.stop_script(user, [], user_pm2, user_scope=True), "Arrêt (compte projet)").out
        _report(dep, out)


def _report(dep: Deployment, out: str) -> None:
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[0] in ("UNIT", "DOCKER", "PM2") and len(parts) >= 3:
            dep.say(f"  arrêté : {parts[1]} ({parts[2] or 'arrêté'})")


def remove_cron_lines(dep: Deployment, plan: Plan) -> None:
    """Filtered on the server (root + project user's crontabs): no crontab content goes through the PC."""
    if not any(plan.cron_removed.values()):
        return
    script = rs.cron_clean_script(dep.config.project_user, STOP_PATTERNS, _keep(dep))
    out = dep.root_ssh(script, "Crontabs", show="retrait des lignes de l'ancienne application des crontabs").out
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[0] == "CRONREMOVED" and len(parts) == 3:
            dep.say(f"  crontab ({parts[1]}) : {parts[2]} ligne(s) de l'ancienne application retirée(s)")


def verify_port(dep: Deployment, disc: Discovery) -> None:
    port = dep.config.old_app_port
    if dep.dry_run:
        dep.say(f"(simulation) le port {port} serait vérifié après l'arrêt.")
        return
    listeners = parse_discovery(dep.runner.ssh_probe(dep.config.root_alias, rs.listeners_script(port)).out).listeners
    owned = [lst.pid for lst in listeners if matches(f"{lst.cwd} {lst.command}", STOP_PATTERNS, disc.home)]
    if owned:
        dep.root_ssh(rs.kill_script(owned), f"Arrêt des processus du port {port}", check=False)
        listeners = parse_discovery(dep.runner.ssh_probe(dep.config.root_alias, rs.listeners_script(port)).out
                                    ).listeners
    if not listeners:
        dep.say(f"Port {port} : libre.")
        return
    for lst in listeners:
        dep.say(f"Port {port} toujours utilisé par pid {lst.pid} ({redact(lst.command)[:80]}) : programme non "
                "reconnu, laissé tel quel.")


def step_replace(dep: Deployment) -> None:
    dep.title("R", "Remplacement de l'ancienne application (sauvegarde puis arrêt, réversible)")
    disc, plan = discover(dep)
    if not _show(dep, disc, plan):
        return
    if plan.empty:
        dep.say("Rien à arrêter : l'ancienne application n'est plus en service.")
        return
    archive = backup(dep, disc, plan)  # never stop anything without a backup
    stop(dep, plan)
    remove_cron_lines(dep, plan)
    verify_port(dep, disc)
    dep.say(f"Retour arrière possible à tout moment : {RESTORE_HINT}")
    dep.notes.append(f"Ancienne application arrêtée (rien de supprimé). Sauvegarde : {archive or '~/backups'} ; "
                     f"retour arrière : {RESTORE_HINT}")


# --------------------------------------------------------------------------- purge / restore


def _purgeable_units(disc: Discovery, plan: Plan) -> tuple[list[str], list[str]]:
    """(system unit files under /etc/systemd/system, user unit files in the user's systemd folder)."""
    user_dir = f"{disc.home.rstrip('/')}/.config/systemd/user/"
    system = [u.path for u in plan.stop_units if u.scope == "system" and u.path.startswith("/etc/systemd/system/")]
    user = [u.path for u in plan.stop_units if u.scope == "user" and disc.home and u.path.startswith(user_dir)]
    return system, user


def step_purge(dep: Deployment) -> None:
    dep.title("P", "Suppression définitive de l'ancienne application (après sauvegarde)")
    disc, plan = discover(dep)
    if not _show(dep, disc, plan):
        return
    if not disc.backups:
        raise DeployError("Suppression refusée : aucune sauvegarde ~/backups/openclaw-*.tar.gz sur le serveur. "
                          "Lance d'abord : python scripts/deploy_server.py --replace-openclaw")
    dep.say(f"Sauvegarde existante : {disc.backups[-1]} ; une nouvelle sauvegarde est faite avant toute suppression.")
    system_units, user_units = _purgeable_units(disc, plan)
    dep.say("À SUPPRIMER : " + (", ".join([*system_units, *user_units, *plan.purge_paths]) or "rien"))
    protected = sorted(set(plan.backup_paths) - set(plan.purge_paths))
    if protected:
        dep.say("Conservés (contiennent un service laissé en service) : " + ", ".join(protected))
    backup(dep, disc, plan)
    stop(dep, plan)
    remove_cron_lines(dep, plan)
    script = rs.purge_script(dep.config.project_user, system_units, user_units, [c.id for c in plan.stop_containers],
                             list(plan.purge_paths))
    for line in dep.root_ssh(script, "Suppression").out.splitlines():
        if line.startswith(("REMOVED", "SKIPPED")):
            dep.say("  " + line.replace("REMOVED\t", "supprimé : ").replace("SKIPPED\t", "protégé, conservé : "))
    dep.user_ssh('export XDG_RUNTIME_DIR="/run/user/$(id -u)"\nsystemctl --user daemon-reload || true\n',
                 "Rechargement des services utilisateur", check=False)
    dep.notes.append("Ancienne application supprimée ; ses sauvegardes restent dans ~/backups.")


def _archive_name(dep: Deployment, wanted: str | None) -> str:
    if wanted:
        name = wanted.replace("\\", "/").rsplit("/", 1)[-1]
    else:
        name = dep.runner.ssh_probe(dep.config.root_alias, rs.latest_archive_script(dep.config.project_user)
                                    ).out.strip().rsplit("/", 1)[-1]
    if not rs.ARCHIVE_RE.match(name or ""):
        raise DeployError("Aucune sauvegarde valide (~/backups/openclaw-AAAAMMJJ-HHMMSS.tar.gz) à restaurer.")
    return name


def step_restore(dep: Deployment, wanted: str | None) -> None:
    dep.title("S", "Restauration de l'ancienne application depuis sa sauvegarde")
    name = _archive_name(dep, wanted)
    raw = dep.runner.ssh_probe(dep.config.root_alias, rs.manifest_script(dep.config.project_user, name)).out
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise DeployError(f"Manifeste illisible dans {name}.") from exc
    if not isinstance(manifest, dict):
        raise DeployError(f"Manifeste illisible dans {name}.")
    dep.say(f"Sauvegarde {name} : {len(manifest.get('units', []))} service(s), "
            f"{len(manifest.get('containers', []))} conteneur(s), {len(manifest.get('paths', []))} chemin(s).")
    out = dep.root_ssh(rs.restore_root_script(dep.config.project_user, name, manifest), "Restauration (root)").out
    for line in out.splitlines():
        if line.startswith("RESTORED\t"):
            dep.say("  remis : " + line.split("\t", 1)[1])
    dep.user_ssh(rs.restore_user_script(manifest), "Restauration (compte projet)", check=False)
    dep.notes.append("Ancienne application relancée depuis la sauvegarde (jobbot continue de tourner à côté).")
