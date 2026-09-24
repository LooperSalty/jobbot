"""Browser sessions moved between machines: logins done on the PC (with a screen), reused on a headless server.

- ``export_sessions``: Playwright ``storage_state`` (cookies + origins) of the persistent profile, reduced to the
  site's own domains, written to ``data/sessions/<site>.json`` (chmod 600 where supported).
- ``copy_session_files``: ``jobbot session import <path>`` copies such files into ``data/sessions/``.
- ``import_sessions``: called each time a persistent context is opened (``apply.browser.open_browser``, hence also
  ``classroom.browser.open_classroom_browser``): a ``data/sessions/<site>.json`` newer than repo kv
  ``session_imported:<site>`` has its cookies added (``context.add_cookies``, that site's domains only), then the
  kv records the import time. Never raises: a broken file is reported and skipped.

LinkedIn / HelloWork / WTTJ share the main profile (``paths.browser_profile``); Classroom has its own
(``classroom.browser_profile``). Session files are secrets: never commit or share them (``data/`` is ignored).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobbot import clock

log = logging.getLogger(__name__)

SITES: tuple[str, ...] = ("linkedin", "hellowork", "wttj", "classroom")
MAIN_PROFILE_SITES: tuple[str, ...] = ("linkedin", "hellowork", "wttj")
SITE_DOMAINS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",), "hellowork": ("hellowork.com",), "wttj": ("welcometothejungle.com",),
    "classroom": ("google.com",),  # Google session cookies live on .google.com / accounts / classroom
}
KV_PREFIX = "session_imported:"
SESSIONS_SUBDIR = "sessions"
MAX_AGE_DAYS = 30  # older export: doctor suggests a fresh « jobbot session export »
COOKIE_KEYS = frozenset({"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite",
                         "partitionKey"})
SAME_SITE = frozenset({"Strict", "Lax", "None"})

Opener = Callable[[Any, str], AbstractContextManager[Any]]  # (ctx, "main" | "classroom") → BrowserContext


class SessionError(RuntimeError):
    """Unusable session file or export (French message)."""


@dataclass(frozen=True)
class SessionInfo:
    site: str
    path: Path
    exported_at: datetime
    cookies: int
    pending: bool  # newer than the last import: applied at the next browser opening

    @property
    def age_days(self) -> int:
        return max(0, (clock.now() - self.exported_at).days)


# --------------------------------------------------------------------------- pure helpers


def expand_sites(name: str) -> tuple[str, ...]:
    """'all' → every site; a known site → (site,); ValueError (French) otherwise."""
    wanted = (name or "all").strip().lower()
    if wanted == "all":
        return SITES
    if wanted not in SITES:
        raise ValueError(f"Site inconnu : {name} (possibles : {', '.join(SITES)}, all)")
    return (wanted,)


def sessions_dir(root: Path, settings: Any) -> Path:
    data = Path(settings.paths.data_dir)
    return (data if data.is_absolute() else root / data) / SESSIONS_SUBDIR


def session_file(root: Path, settings: Any, site: str) -> Path:
    return sessions_dir(root, settings) / f"{site}.json"


def domain_matches(domain: str, bases: Iterable[str]) -> bool:
    host = (domain or "").strip().lstrip(".").lower()
    return any(host == base or host.endswith("." + base) for base in bases)


def _clean_cookie(cookie: Any) -> dict[str, Any] | None:
    if not isinstance(cookie, dict):
        return None
    if not all(isinstance(cookie.get(k), str) for k in ("name", "value", "domain")) or not cookie["name"]:
        return None
    clean = {k: v for k, v in cookie.items() if k in COOKIE_KEYS}
    clean.setdefault("path", "/")
    if clean.get("sameSite") not in SAME_SITE:
        clean.pop("sameSite", None)
    expires = clean.get("expires")
    if isinstance(expires, (int, float)) and 0 < expires < clock.now().timestamp():
        return None  # already expired
    return clean


def site_cookies(cookies: Iterable[Any], site: str) -> list[dict[str, Any]]:
    """Valid, unexpired cookies of ``site``'s domains only."""
    bases = SITE_DOMAINS[site]
    kept = (_clean_cookie(c) for c in cookies)
    return [c for c in kept if c is not None and domain_matches(c["domain"], bases)]


def site_state(state: dict[str, Any], site: str) -> dict[str, Any]:
    """storage_state reduced to ``site`` (cookies and localStorage origins of its domains)."""
    from urllib.parse import urlparse

    origins = [o for o in state.get("origins") or [] if isinstance(o, dict)
               and domain_matches(urlparse(str(o.get("origin") or "")).hostname or "", SITE_DOMAINS[site])]
    return {"cookies": site_cookies(state.get("cookies") or [], site), "origins": origins}


def read_session(path: Path, site: str) -> list[dict[str, Any]]:
    """Cookies of a session file (SessionError when unreadable or not a storage_state)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SessionError(f"Session {site} illisible ({path.name}) : {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list):
        raise SessionError(f"{path.name} n'est pas une session exportée par « jobbot session export ».")
    return site_cookies(data["cookies"], site)


def sites_for_profile(settings: Any, profile_dir: str | None) -> tuple[str, ...]:
    """Sites whose session belongs to the persistent profile ``profile_dir`` (None = main profile)."""
    wanted = Path(profile_dir or settings.paths.browser_profile).as_posix().rstrip("/")
    if wanted == Path(settings.paths.browser_profile).as_posix().rstrip("/"):
        return MAIN_PROFILE_SITES
    if wanted == Path(settings.classroom.browser_profile).as_posix().rstrip("/"):
        return ("classroom",)
    return ()


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def _private(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # Windows / exotic file systems: best effort
        log.debug("Droits %o non appliqués à %s", mode, path)


# --------------------------------------------------------------------------- import at browser opening


def _imported_at(repo: Any, site: str) -> datetime | None:
    raw = repo.kv_get(KV_PREFIX + site)
    try:
        return clock.parse_dt(raw) if raw else None
    except (TypeError, ValueError):
        return None


def is_pending(repo: Any, path: Path, site: str) -> bool:
    last = _imported_at(repo, site)
    return path.is_file() and (last is None or _mtime(path) > last)


def _mark_imported(repo: Any, path: Path, site: str) -> None:
    """kv = max(now, file mtime): a file dated in the future (clock skew, coarse file times) is not re-imported
    at every browser opening."""
    repo.kv_set(KV_PREFIX + site, clock.iso(max(clock.now(), _mtime(path))))


def import_sessions(ctx: Any, context: Any, sites: Iterable[str]) -> list[str]:
    """Add the cookies of every pending session file of ``sites`` to ``context``. Returns the imported sites."""
    done: list[str] = []
    for site in sites:
        path = session_file(ctx.root, ctx.settings, site)
        try:
            if not is_pending(ctx.repo, path, site):
                continue
            cookies = read_session(path, site)
            if cookies:
                context.add_cookies(cookies)
            _mark_imported(ctx.repo, path, site)
            log.info("Session %s importée (%d cookie(s)) depuis %s", site, len(cookies), path.name)
            done.append(site)
        except Exception as exc:  # never block the browser for a session file
            log.warning("Session %s non importée : %s", site, exc)
    return done


# --------------------------------------------------------------------------- export / copy


def _default_opener(ctx: Any, group: str) -> AbstractContextManager[Any]:
    if group == "classroom":
        from jobbot.classroom.browser import open_classroom_browser

        return open_classroom_browser(ctx, headless=True, import_session=False)
    from jobbot.apply.browser import open_browser

    return open_browser(ctx, headless=True, import_sessions=False)


def _write_state(ctx: Any, site: str, state: dict[str, Any]) -> str:
    reduced = site_state(state, site)
    if not reduced["cookies"]:
        return f"{site} : aucune session dans le navigateur (lance d'abord « jobbot login {site} »)."
    path = session_file(ctx.root, ctx.settings, site)
    path.parent.mkdir(parents=True, exist_ok=True)
    _private(path.parent, 0o700)
    path.write_text(json.dumps(reduced, ensure_ascii=False, indent=1), encoding="utf-8")
    _private(path)
    _mark_imported(ctx.repo, path, site)  # this machine already has it
    return f"{site} : {len(reduced['cookies'])} cookie(s) exporté(s) dans {path}"


def export_sessions(ctx: Any, sites: Iterable[str], *, opener: Opener | None = None) -> list[str]:
    """Export the sessions of ``sites`` (French result lines). One browser opening per profile."""
    wanted = list(dict.fromkeys(sites))
    groups = {"main": [s for s in wanted if s in MAIN_PROFILE_SITES],
              "classroom": [s for s in wanted if s == "classroom"]}
    lines: list[str] = []
    for group, members in groups.items():
        if not members:
            continue
        try:
            with (opener or _default_opener)(ctx, group) as context:
                state = context.storage_state()
        except Exception as exc:  # profile in use, browser missing…
            log.warning("Export des sessions (%s) impossible : %s", group, exc)
            lines.extend(f"{site} : export impossible ({str(exc)[:160]})" for site in members)
            continue
        lines.extend(_write_state(ctx, site, state) for site in members)
    return lines


def _source_files(source: Path) -> list[Path]:
    if source.is_dir():
        return sorted(p for p in source.glob("*.json") if p.stem in SITES)
    if source.is_file():
        if source.stem not in SITES:
            raise SessionError(f"{source.name} : nom attendu <site>.json ({', '.join(SITES)}).")
        return [source]
    raise SessionError(f"Introuvable : {source}")


def copy_session_files(root: Path, settings: Any, source: Path) -> list[str]:
    """Copy ``source`` (a <site>.json file or a folder of them) into data/sessions/ (validated first)."""
    files = _source_files(source)
    if not files:
        raise SessionError(f"Aucun fichier <site>.json dans {source}.")
    target_dir = sessions_dir(root, settings)
    target_dir.mkdir(parents=True, exist_ok=True)
    _private(target_dir, 0o700)
    lines = []
    for path in files:
        cookies = read_session(path, path.stem)
        target = target_dir / path.name
        if path.resolve() != target.resolve():
            shutil.copyfile(path, target)  # new mtime: imported at the next browser opening
        _private(target)
        lines.append(f"{path.stem} : {len(cookies)} cookie(s) — importée à la prochaine ouverture du navigateur")
    return lines


def session_status(root: Path, settings: Any, repo: Any) -> list[SessionInfo]:
    """Session files present in data/sessions/ (doctor, « jobbot session import » without path)."""
    infos = []
    for site in SITES:
        path = session_file(root, settings, site)
        if not path.is_file():
            continue
        try:
            count = len(read_session(path, site))
        except SessionError:
            count = 0
        infos.append(SessionInfo(site=site, path=path, exported_at=_mtime(path), cookies=count,
                                 pending=is_pending(repo, path, site)))
    return infos
