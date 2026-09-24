"""Watcher state: posts already processed (repo kv, per course), active hours, run lock, last check.

kv keys: ``classroom:posts:<course id>`` (JSON ``{post id: {"s": stream fingerprint, "h": text hash, "at": iso,
"f": failures}}``, newest ``MAX_REMEMBERED`` kept), ``classroom:last_check`` (JSON ``{at, ok, detail, posts,
login_required}``, shown by ``jobbot doctor``), ``classroom:login_notified_at`` (one « reconnecte-toi »
notification every ``LOGIN_NOTIFY_EVERY_H`` hours at most).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from jobbot import clock
from jobbot.config import parse_active_hours
from jobbot.db import Repository

log = logging.getLogger(__name__)

POSTS_KEY = "classroom:posts:{course}"
LAST_CHECK_KEY = "classroom:last_check"
LOGIN_NOTIFIED_KEY = "classroom:login_notified_at"
MAX_REMEMBERED = 500
MAX_POST_FAILURES = 3
LOGIN_NOTIFY_EVERY_H = 6
LOCK_FILE = "classroom.lock"
LOCK_STALE_S = 2 * 3600


class ClassroomBusy(RuntimeError):
    """Another Classroom check is running (scheduled task + manual run)."""


# --------------------------------------------------------------------------- seen posts


def load_seen(repo: Repository, course_id: str) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(repo.kv_get(POSTS_KEY.format(course=course_id)) or "{}")
    except ValueError:
        log.warning("État Classroom illisible : toutes les publications seront relues.")
        return {}
    return data if isinstance(data, dict) else {}


def save_seen(repo: Repository, course_id: str, seen: dict[str, dict[str, Any]]) -> None:
    newest = sorted(seen.items(), key=lambda kv: str(kv[1].get("at") or ""), reverse=True)[:MAX_REMEMBERED]
    repo.kv_set(POSTS_KEY.format(course=course_id), json.dumps(dict(newest), ensure_ascii=False))


def wants(seen: dict[str, dict[str, Any]], post_id: str, fingerprint: str) -> bool:
    """Read the post when never processed, or when its stream card changed (text edited, attachment added…)."""
    entry = seen.get(post_id)
    return entry is None or entry.get("s") != fingerprint


def remember(seen: dict[str, dict[str, Any]], post_id: str, **entry: Any) -> dict[str, dict[str, Any]]:
    """New mapping with ``post_id`` recorded (never mutates ``seen``)."""
    return {**seen, post_id: {**entry, "at": clock.iso(clock.now())}}


def already_processed(seen: dict[str, dict[str, Any]], post_id: str, text_hash: str) -> bool:
    entry = seen.get(post_id)
    return entry is not None and entry.get("h") == text_hash


def failures(seen: dict[str, dict[str, Any]], post_id: str) -> int:
    return int((seen.get(post_id) or {}).get("f") or 0)


# --------------------------------------------------------------------------- hours & lock


def in_active_hours(spec: str, when: datetime | None = None) -> bool:
    """True inside the daily window (Paris time); a window like 22:00-06:00 spans midnight."""
    start, end = parse_active_hours(spec)
    now = clock.to_paris(when or clock.now()).time()
    return start <= now <= end if start <= end else (now >= start or now <= end)


@contextmanager
def classroom_lock(ctx: Any) -> Iterator[None]:
    """Exclusive lock file in the data folder (taken over after ``LOCK_STALE_S``)."""
    path = ctx.path(f"{ctx.settings.paths.data_dir}/{LOCK_FILE}")
    if path.exists() and time.time() - path.stat().st_mtime < LOCK_STALE_S:
        raise ClassroomBusy("Vérification Classroom déjà en cours : celle-ci est sautée.")
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Verrou Classroom %s non supprimé : %s", path, exc)


# --------------------------------------------------------------------------- last check & login alert


def record_check(repo: Repository, *, ok: bool, detail: str, posts: int = 0, login_required: bool = False) -> None:
    payload = {"at": clock.iso(clock.now()), "ok": ok, "detail": detail, "posts": posts,
               "login_required": login_required}
    repo.kv_set(LAST_CHECK_KEY, json.dumps(payload, ensure_ascii=False))


def last_check(repo: Repository) -> dict[str, Any] | None:
    try:
        data = json.loads(repo.kv_get(LAST_CHECK_KEY) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def login_alert_due(repo: Repository, now: datetime | None = None) -> bool:
    """True at most once every ``LOGIN_NOTIFY_EVERY_H`` hours (and records it)."""
    current = now or clock.now()
    last = repo.kv_get(LOGIN_NOTIFIED_KEY)
    try:
        if last and current - clock.parse_dt(last) < timedelta(hours=LOGIN_NOTIFY_EVERY_H):  # type: ignore[operator]
            return False
    except ValueError:
        log.debug("Date de dernière alerte Classroom illisible")
    repo.kv_set(LOGIN_NOTIFIED_KEY, clock.iso(current) or "")
    return True
