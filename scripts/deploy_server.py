"""Déploie jobbot sur le serveur Debian, en une commande depuis le PC (Windows), avec le Python du projet.

    .venv\\Scripts\\python scripts\\deploy_server.py                     # installation complète (idempotente)
    .venv\\Scripts\\python scripts\\deploy_server.py --dry-run           # plan et commandes, rien n'est modifié
    .venv\\Scripts\\python scripts\\deploy_server.py --update            # code + dépendances + fichiers privés + cron
    .venv\\Scripts\\python scripts\\deploy_server.py --only private      # ex. après « jobbot session export all »
    .venv\\Scripts\\python scripts\\deploy_server.py --replace-openclaw  # + sauvegarde et arrêt de l'ancienne appli

Les alias SSH et le compte du projet sont lus dans le .env du PC (voir .env.example) ; l'authentification se fait
par clé uniquement (jamais de mot de passe). Détails : README, « Installation sur un serveur Linux (Debian) ».
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy.cli import main  # noqa: E402  (after the path setup above)

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    sys.exit(main())
