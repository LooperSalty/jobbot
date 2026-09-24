"""« jobbot init » (folders, .env, database) and « jobbot doctor » (environment checks, never shows a secret)."""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import shutil
import smtplib
import ssl
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jobbot.config import ConfigError, Settings, load_profile, load_settings

Level = Literal["ok", "warn", "error"]
LEVEL_LABEL = {"ok": "[green]OK[/]", "warn": "[yellow]ATTENTION[/]", "error": "[red]ERREUR[/]"}
REQUIRED_MODULES = (
    ("httpx", "httpx"), ("pydantic", "pydantic"), ("typer", "typer"), ("rich", "rich"), ("jinja2", "jinja2"),
    ("playwright", "playwright"), ("bs4", "beautifulsoup4"), ("lxml", "lxml"), ("dns", "dnspython"),
    ("rapidfuzz", "rapidfuzz"), ("docx", "python-docx"), ("pypdf", "pypdf"), ("yaml", "pyyaml"),
    ("ruamel.yaml", "ruamel.yaml"), ("openpyxl", "openpyxl"), ("anthropic", "anthropic"),
)
OPTIONAL_MODULES = (
    ("streamlit", "streamlit", "tableau de bord : pip install -e .[dashboard]"),
    ("pandas", "pandas", "tableau de bord : pip install -e .[dashboard]"),
)
ENV_KEYS = (
    ("SMTP_USER", "smtp_user", "warn", "envoi d'e-mails et lecture des réponses"),
    ("SMTP_PASSWORD", "smtp_password", "warn", "mot de passe d'application Gmail"),
    ("FRANCE_TRAVAIL_CLIENT_ID", "france_travail_client_id", "warn", "offres France Travail"),
    ("FRANCE_TRAVAIL_CLIENT_SECRET", "france_travail_client_secret", "warn", "offres France Travail"),
    ("LBA_API_TOKEN", "lba_api_token", "ok", "optionnel : API officielle La Bonne Alternance"),
    ("HUNTER_API_KEY", "hunter_api_key", "ok", "optionnel : vérification d'adresses"),
    ("BRAVE_API_KEY", "brave_api_key", "ok", "optionnel : recherche de sites"),
)
CHROME_PATHS = (
    ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
    ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
    ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
)
SMTP_TIMEOUT_S = 20
GMAIL_APP_PASSWORD_LEN = 16
PROFILE_EXAMPLE = "config/profile.example.yaml"  # fictitious template, copied by « jobbot init »
_COMMENT_VALUE = re.compile(r"^([A-Z][A-Z0-9_]*)=\s*(#.*)$")


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str


# --------------------------------------------------------------------------- init


def sanitized_env(text: str) -> str:
    """``KEY=   # comment`` → comment line + ``KEY=`` (dotenv would otherwise read the comment as the value)."""
    lines: list[str] = []
    for line in text.splitlines():
        match = _COMMENT_VALUE.match(line.strip())
        lines.extend([match[2], f"{match[1]}="] if match else [line])
    return "\n".join(lines) + "\n"


def is_set(value: Any) -> bool:
    """A secret counts as present when it is non-empty and not a leftover comment."""
    return bool(value) and not str(value).strip().startswith("#")


def init_profile(root: Path, profile_rel: str) -> str | None:
    """Copy the fictitious ``PROFILE_EXAMPLE`` to the missing profile; None when the profile already exists."""
    profile = root / profile_rel
    if profile.exists():
        return None
    example = root / PROFILE_EXAMPLE
    if not example.exists():
        return f"Profil absent ({profile_rel}) : lance « jobbot import-cv mon_cv.pdf »."
    profile.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, profile)
    return (f"{profile_rel} créé à partir de {PROFILE_EXAMPLE} (profil FICTIF) : remplace-le par le tien, "
            "à la main ou avec « jobbot import-cv mon_cv.pdf » (puis recopie le résultat relu).")


def run_init(root: Path, console: Console) -> list[str]:
    """Create folders, .env (from .env.example), the profile (from the example) and the database.
    Never overwrites anything."""
    done: list[str] = []
    try:
        settings = load_settings(root)
    except ConfigError as exc:
        console.print(f"[yellow]settings.yaml illisible ({escape(str(exc))}) : valeurs par défaut.[/]")
        settings = Settings()
    for rel in (settings.paths.data_dir, settings.paths.logs_dir, settings.paths.output_dir, "config"):
        folder = root / rel
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            done.append(f"Dossier créé : {rel}")
    env, example = root / ".env", root / ".env.example"
    if not env.exists() and example.exists():
        env.write_text(sanitized_env(example.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
        done.append(".env créé à partir de .env.example (à compléter : jobbot setup)")
    profile_note = init_profile(root, settings.paths.profile)
    if profile_note:
        done.append(profile_note)
    from jobbot.db import Repository

    db_path = Path(settings.paths.db)
    Repository(db_path if db_path.is_absolute() else root / db_path).close()
    done.append(f"Base de données prête : {settings.paths.db}")
    return done


# --------------------------------------------------------------------------- doctor checks


def check_python() -> Check:
    version = ".".join(map(str, sys.version_info[:3]))
    ok = sys.version_info >= (3, 12)
    return Check("Python", "ok" if ok else "error", version if ok else f"{version} (3.12 minimum)")


def check_modules() -> list[Check]:
    missing = [pkg for mod, pkg in REQUIRED_MODULES if importlib.util.find_spec(mod.split(".")[0]) is None]
    checks = [Check("Dépendances", "error" if missing else "ok",
                    f"manquantes : {', '.join(missing)} (pip install -e .)" if missing else "toutes présentes")]
    for mod, pkg, why in OPTIONAL_MODULES:
        present = importlib.util.find_spec(mod) is not None
        checks.append(Check(f"Module {pkg}", "ok" if present else "warn", "présent" if present else why))
    return checks


def check_config(root: Path) -> tuple[list[Check], Settings | None]:
    checks: list[Check] = []
    try:
        settings = load_settings(root)
        checks.append(Check("config/settings.yaml", "ok", "valide"))
    except Exception as exc:  # pydantic ValidationError or ConfigError
        return [Check("config/settings.yaml", "error", str(exc)[:300])], None
    try:
        profile = load_profile(root / settings.paths.profile)
        checks.append(Check("Profil", "ok", f"{profile.identity.full_name} — {len(profile.target.job_titles)} "
                                            "intitulé(s) cible(s)"))
    except ConfigError as exc:
        checks.append(Check("Profil", "error", str(exc)[:300]))
    from jobbot.preferences import effective_mode, is_setup_completed

    done = is_setup_completed(root)
    checks.append(Check("Assistant de configuration", "ok" if done else "warn",
                        f"terminé — mode effectif : {effective_mode(settings)}" if done else "à lancer : jobbot setup"))
    return checks, settings


def check_llm(settings: Settings, secrets: Any) -> Check:
    provider = settings.llm.provider
    if provider == "claude_cli":
        path = shutil.which(settings.llm.cli_path)
        return Check("LLM (Claude Code)", "ok" if path else "warn",
                     f"trouvé : {path}" if path else "commande « claude » introuvable : modèles de secours utilisés")
    if provider == "anthropic":
        ok = bool(getattr(secrets, "anthropic_api_key", None))
        return Check("LLM (API Anthropic)", "ok" if ok else "error", "clé présente" if ok else "ANTHROPIC_API_KEY absente")
    return Check("LLM", "warn", "désactivé (llm.provider = none) : textes générés par modèles")


def chromium_path() -> str | None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        path = pw.chromium.executable_path
    return path if path and Path(path).exists() else None


def chrome_installed() -> bool:
    return any(Path(os.environ[var], rel).exists() for var, rel in CHROME_PATHS if os.environ.get(var))


def check_browser(settings: Settings) -> list[Check]:
    try:
        path = chromium_path()
        bundled = Check("Chromium (Playwright)", "ok" if path else "error",
                        "installé" if path else "absent : .venv\\Scripts\\python -m playwright install chromium")
    except Exception as exc:
        bundled = Check("Chromium (Playwright)", "error", f"Playwright inutilisable : {str(exc)[:200]}")
    if settings.browser_channel != "chrome":
        return [bundled]
    chrome = chrome_installed()
    return [bundled, Check("Google Chrome", "ok" if chrome else "warn",
                           "installé" if chrome else "absent : repli sur Chromium (browser_channel: null)")]


def check_env(secrets: Any) -> list[Check]:
    checks = []
    for name, attr, level_if_missing, why in ENV_KEYS:
        value = getattr(secrets, attr, None)
        present = is_set(value)
        detail = f"présent ({why})" if present else f"absent ({why})"
        if value and not present:
            detail = f"la valeur est un commentaire : mets « {name}= » seul sur sa ligne ({why})"
        checks.append(Check(f".env {name}", "ok" if present else level_if_missing,  # type: ignore[arg-type]
                            detail))
    return checks


def check_sources(ctx: Any) -> list[Check]:
    from jobbot.sources.registry import SECRET_SOURCES, SOURCE_SPECS, canonical_name

    checks = []
    for raw in ctx.settings.search.sources:
        name = canonical_name(raw)
        try:
            module, cls_name = SOURCE_SPECS[name]
            cls = getattr(importlib.import_module(module), cls_name)
            ok, reason = (cls(ctx.secrets) if name in SECRET_SOURCES else cls()).available()
        except KeyError:
            ok, reason = False, "source inconnue"
        except Exception as exc:  # optional dependency missing, broken source
            ok, reason = False, str(exc)[:200]
        checks.append(Check(f"Source {name}", "ok" if ok else "warn", "disponible" if ok else reason))
    return checks


def check_database(ctx: Any) -> Check:
    try:
        stats = ctx.repo.stats()
    except Exception as exc:
        return Check("Base de données", "error", str(exc)[:200])
    jobs = sum(stats["jobs_by_status"].values())
    apps = sum(stats["applications_by_status"].values())
    return Check("Base de données", "ok", f"{jobs} offre(s), {apps} candidature(s)")


def check_classroom(ctx: Any) -> list[Check]:
    """Course link configured (✓/✗, never shown) and session state (last check recorded by the watcher)."""
    from jobbot.classroom.models import parse_course_urls
    from jobbot.classroom.state import last_check

    cfg = ctx.settings.classroom
    if not cfg.enabled:
        return [Check("Classroom (offres de l'école)", "ok", "surveillance désactivée (classroom.enabled)")]
    courses, invalid = parse_course_urls(getattr(ctx.secrets, "classroom_course_urls", None))
    link = f"✓ {len(courses)} cours configuré(s)" + (f", {invalid} lien(s) invalide(s)" if invalid else "")
    checks = [Check("Classroom : lien du cours", "ok" if courses and not invalid else "warn",
                    link if courses else "✗ CLASSROOM_COURSE_URLS absent (jobbot setup --section classroom)")]
    last = last_check(ctx.repo)
    if last is None:
        state = f"inconnue (jamais vérifiée) — backend {cfg.backend}, connexion : jobbot login classroom"
        return [*checks, Check("Classroom : session", "warn" if courses else "ok", state)]
    when = str(last.get("at") or "")[:16].replace("T", " ")
    level: Level = "ok" if last.get("ok") else "warn"  # a lost session never blocks the other features
    return [*checks, Check("Classroom : session", level, f"dernière vérification {when} UTC : {last.get('detail')}")]


def check_gmail_app_password(secrets: Any) -> Check | None:
    """Offline format check: a Gmail app password is 16 letters (spaces ignored). Never shows the value."""
    password = (getattr(secrets, "smtp_password", None) or "").replace(" ", "").strip()
    gmail = "gmail" in f"{getattr(secrets, 'smtp_host', '')} {getattr(secrets, 'smtp_user', '')}".lower()
    if not password or not gmail:
        return None
    if len(password) == GMAIL_APP_PASSWORD_LEN and password.isalpha():
        return Check("Mot de passe d'application Gmail", "ok", "format valide (16 lettres)")
    return Check("Mot de passe d'application Gmail", "warn",
                 f"{len(password)} caractère(s) au lieu de 16 lettres : vérifie-le (jobbot doctor --smtp) ou "
                 "recrée-le sur https://myaccount.google.com/apppasswords")


def check_smtp(secrets: Any, smtp_factory: Callable[..., Any] | None = None) -> Check:
    """Login test only (EHLO/STARTTLS/LOGIN/QUIT): no e-mail is sent."""
    user = (secrets.smtp_user or "").strip()
    password = (secrets.smtp_password or "").replace(" ", "").strip()
    if not user or not password:
        return Check("Connexion SMTP", "warn", "SMTP_USER / SMTP_PASSWORD absents")
    host, port = secrets.smtp_host, int(secrets.smtp_port)
    try:
        if smtp_factory is not None:
            smtp = smtp_factory(host, port, timeout=SMTP_TIMEOUT_S)
        elif port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S)
        if port != 465:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(user, password)
        smtp.quit()
    except smtplib.SMTPAuthenticationError:
        return Check("Connexion SMTP", "error", "identifiants refusés : vérifie le mot de passe d'application Gmail")
    except (OSError, smtplib.SMTPException) as exc:
        return Check("Connexion SMTP", "error", f"connexion impossible : {str(exc)[:200]}")
    return Check("Connexion SMTP", "ok", f"connexion réussie à {host}:{port} (aucun e-mail envoyé)")


def run_doctor(state: Any, *, smtp: bool = False, smtp_factory: Callable[..., Any] | None = None) -> list[Check]:
    checks = [check_python(), *check_modules()]
    config_checks, settings = check_config(state.root)
    checks.extend(config_checks)
    if settings is not None and not any(c.level == "error" for c in config_checks):
        ctx = state.app
        checks.append(check_llm(settings, ctx.secrets))
        checks.extend(check_browser(settings))
        checks.extend(check_env(ctx.secrets))
        gmail = check_gmail_app_password(ctx.secrets)
        if gmail is not None:
            checks.append(gmail)
        checks.extend(check_sources(ctx))
        checks.append(check_database(ctx))
        checks.extend(check_classroom(ctx))
        if smtp:
            checks.append(check_smtp(ctx.secrets, smtp_factory))
    return checks


def doctor_table(checks: list[Check]) -> Table:
    table = Table(title="Diagnostic jobbot", title_justify="left")
    for column in ("Vérification", "État", "Détail"):
        table.add_column(column)
    for check in checks:
        table.add_row(escape(check.name), LEVEL_LABEL[check.level], escape(check.detail))
    return table
