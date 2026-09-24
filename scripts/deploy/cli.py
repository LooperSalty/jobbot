"""Command line of ``scripts/deploy_server.py``: step selection, error handling (French), exit codes.

Exit codes: 0 OK, 2 configuration / step error, 3 SSH unreachable (instructions printed).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from . import replace_steps, steps_app, steps_setup
from .config import DeployError, load_config
from .runner import Executor, Runner, SshUnreachable, ssh_help, subprocess_executor
from .session import Deployment

STEPS: tuple[str, ...] = ("check", "replace", "system", "toolchain", "code", "deps", "private", "llm", "init",
                          "schedule", "dashboard", "summary")
UPDATE_STEPS: tuple[str, ...] = ("check", "code", "deps", "private", "schedule", "dashboard", "summary")
STEP_FUNCTIONS: dict[str, Callable[[Deployment], None]] = {
    "check": steps_setup.step_check, "replace": replace_steps.step_replace, "system": steps_setup.step_system,
    "toolchain": steps_setup.step_toolchain, "code": steps_setup.step_code, "deps": steps_setup.step_deps,
    "private": steps_app.step_private, "llm": steps_app.step_llm, "init": steps_app.step_init,
    "schedule": steps_app.step_schedule, "dashboard": steps_app.step_dashboard, "summary": steps_app.step_summary,
    "purge": replace_steps.step_purge,
}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="deploy_server.py", description="Installe ou met à jour jobbot sur le serveur Debian (clés SSH du .env).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Montre le plan et les commandes ; seules des lectures sont faites sur le serveur.")
    parser.add_argument("--only", choices=STEPS, help="Une seule étape (ex. private après « jobbot session export »).")
    parser.add_argument("--update", action="store_true", help="Mise à jour : code, dépendances, fichiers privés, cron.")
    parser.add_argument("--replace-openclaw", action="store_true",
                        help="Sauvegarde puis arrête l'ancienne application du serveur (réversible).")
    parser.add_argument("--purge-openclaw", action="store_true",
                        help="Supprime l'ancienne application (exige une sauvegarde existante).")
    parser.add_argument("--restore-openclaw", nargs="?", const="", default=None, metavar="ARCHIVE",
                        help="Relance l'ancienne application depuis sa sauvegarde (la plus récente par défaut).")
    parser.add_argument("--dashboard-vpn", action="store_true",
                        help="Tableau de bord sur l'IP VPN (SERVER_IP_TAILSCALE) au lieu de 127.0.0.1 + tunnel SSH.")
    return parser.parse_args(argv)


def select_steps(args: argparse.Namespace) -> list[str]:
    """Steps to run, in order (DeployError in French for an incompatible combination)."""
    actions = [name for name, on in (("--only", args.only), ("--update", args.update),
                                     ("--purge-openclaw", args.purge_openclaw),
                                     ("--restore-openclaw", args.restore_openclaw is not None)) if on]
    if len(actions) > 1:
        raise DeployError(f"Options incompatibles : {' et '.join(actions)}.")
    if args.purge_openclaw:
        return ["check", "purge"]
    if args.restore_openclaw is not None:
        return ["check", "restore"]
    if args.only:
        if args.only == "replace" and not args.replace_openclaw:
            raise DeployError("L'étape replace demande aussi --replace-openclaw (choix explicite).")
        return [args.only]
    if args.update:
        if args.replace_openclaw:
            raise DeployError("--replace-openclaw ne fait jamais partie d'une mise à jour (--update).")
        return list(UPDATE_STEPS)
    return [step for step in STEPS if step != "replace" or args.replace_openclaw]


def _run(dep: Deployment, steps: list[str], args: argparse.Namespace) -> None:
    for name in steps:
        if name == "restore":
            replace_steps.step_restore(dep, args.restore_openclaw or None)
        else:
            STEP_FUNCTIONS[name](dep)


def main(argv: list[str] | None = None, *, executor: Executor | None = None,
         echo: Callable[[str], None] = print, root: Path | None = None) -> int:
    args = parse_args(argv)
    try:
        steps = select_steps(args)
        config, env_text = load_config(root or PROJECT_ROOT)
        runner = Runner(executor or subprocess_executor, dry_run=args.dry_run, echo=echo)
        dep = Deployment(config=config, env_text=env_text, root=root or PROJECT_ROOT, runner=runner, say=echo,
                         dashboard_vpn=args.dashboard_vpn)
        mode = "SIMULATION (rien n'est modifié)" if args.dry_run else "exécution"
        echo(f"Déploiement de jobbot — {mode} — étapes : {', '.join(steps)}")
        _run(dep, steps, args)
    except SshUnreachable as exc:
        echo(ssh_help(exc.alias))
        return 3
    except DeployError as exc:
        echo(f"Erreur : {exc}")
        return 2
    echo("\nTerminé." if not args.dry_run else "\nSimulation terminée : aucune modification.")
    return 0
