"""Self-hosted SearXNG metasearch (``settings.search_engine_url``), used before Brave / DuckDuckGo.

``GET <base>/search?q=…&format=json&engines=bing`` (the instance must have the JSON format enabled).

Hardened for an IP whose search engines get CAPTCHAs (:class:`SearxEngine`, built by :func:`engine_for`):

- ``engines=`` = ``settings.search_engine_engines`` (default ``bing``: the only engine still answering);
- one query at a time per process, at least ``settings.night.search_min_interval_s`` apart (jittered);
- at most ``settings.night.max_search_queries`` queries per day (kv ``searxng:queries:<date>``);
- when the JSON reports the configured engines as blocked (« CAPTCHA », « too many requests » / « trop de
  requêtes », « access denied » / « accès refusé », suspended), SearXNG is left alone for
  ``settings.night.search_backoff_hours`` (kv ``searxng:backoff_until``): callers fall back on the keyless
  strategies (Clearbit, slug + DNS + SIREN check, Wikidata) and Brave when ``BRAVE_API_KEY`` exists.

Errors never propagate: an empty list lets the next strategy take over. A bare base URL (tests, old callers) keeps
the historical behaviour: 1-2 s between queries, no budget, no backoff.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from jobbot import clock

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 1.0
JITTER_S = 1.0
JITTER_RATIO = 0.5  # jitter = max(JITTER_S, interval × ratio): 6 s → 6-9 s
MAX_RESULTS = 20
TIMEOUT_S = 15.0
BUDGET_KEY = "searxng:queries:"  # + Paris date
BACKOFF_KEY = "searxng:backoff_until"
LAST_BACKOFF_KEY = "searxng:last_backoff"  # JSON {at, until, reason} (doctor)
_BLOCKED = re.compile(r"captcha|too many requests|trop de requ|access denied|acc[eè]s refus|suspend", re.I)
_COUNT_LOCK = threading.Lock()


def _sleep(seconds: float) -> None:  # indirection so tests can disable waiting
    time.sleep(seconds)


class _Polite:
    """One query at a time, ``interval`` + jitter apart (thread-safe: parallel workers queue here)."""

    def __init__(self) -> None:
        self.last = 0.0
        self._lock = threading.Lock()

    def wait_turn(self, interval: float = MIN_INTERVAL_S) -> None:
        with self._lock:
            jitter = max(JITTER_S, interval * JITTER_RATIO)
            delay = self.last + interval + random.uniform(0, jitter) - time.monotonic()
            if self.last and delay > 0:
                _sleep(delay)
            self.last = time.monotonic()


_POLITE = _Polite()


@dataclass(frozen=True)
class SearxEngine:
    """How to query the instance: engines, spacing, daily budget and backoff (kv of ``repo``)."""

    base_url: str
    engines: tuple[str, ...] = ()
    min_interval_s: float = MIN_INTERVAL_S
    max_queries_per_day: int | None = None
    backoff_hours: float = 0.0
    repo: Any = field(default=None, compare=False, repr=False)  # jobbot.db.Repository


def engine_for(ctx: Any) -> SearxEngine | None:
    """The hardened engine of ``ctx`` (None when ``settings.search_engine_url`` is not set)."""
    settings = ctx.settings
    if not settings.search_engine_url:
        return None
    night = settings.night
    return SearxEngine(
        base_url=settings.search_engine_url, engines=tuple(settings.search_engine_engines),
        min_interval_s=night.search_min_interval_s, max_queries_per_day=night.max_search_queries,
        backoff_hours=night.search_backoff_hours, repo=ctx.repo,
    )


def search_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/search"


# --------------------------------------------------------------------------- budget & backoff (kv)


def budget_key(now: datetime | None = None) -> str:
    return f"{BUDGET_KEY}{clock.to_paris(now or clock.now()).date().isoformat()}"


def queries_today(repo: Any, now: datetime | None = None) -> int:
    try:
        return int(repo.kv_get(budget_key(now), "0") or 0)
    except (TypeError, ValueError):
        return 0


def backoff_until(repo: Any, now: datetime | None = None) -> datetime | None:
    """End of the current backoff, None when SearXNG may be used."""
    if repo is None:
        return None
    try:
        until = clock.parse_dt(repo.kv_get(BACKOFF_KEY))
    except ValueError:
        return None
    return until if until is not None and until > (now or clock.now()) else None


def last_backoff(repo: Any) -> dict | None:
    """{at, until, reason} of the latest backoff (doctor), None when SearXNG was never blocked."""
    try:
        data = json.loads(repo.kv_get(LAST_BACKOFF_KEY) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def start_backoff(repo: Any, hours: float, reason: str, now: datetime | None = None) -> datetime:
    at = now or clock.now()
    until = at + timedelta(hours=hours)
    repo.kv_set(BACKOFF_KEY, clock.iso(until) or "")
    repo.kv_set(LAST_BACKOFF_KEY, json.dumps({"at": clock.iso(at), "until": clock.iso(until), "reason": reason},
                                             ensure_ascii=False))
    log.warning("SearXNG : moteurs bloqués (%s) — pause jusqu'au %s, repli sur Clearbit / slug / Wikidata / Brave",
                reason, clock.to_paris(until).strftime("%d/%m %H:%M"))
    return until


def _budget_left(engine: SearxEngine) -> bool:
    if engine.repo is None or engine.max_queries_per_day is None:
        return True
    return queries_today(engine.repo) < engine.max_queries_per_day


def _take_query(engine: SearxEngine) -> bool:
    """Count one query in today's budget; False (nothing counted) when the budget is spent."""
    if engine.repo is None or engine.max_queries_per_day is None:
        return True
    with _COUNT_LOCK:
        used = queries_today(engine.repo)
        if used >= engine.max_queries_per_day:
            log.info("SearXNG : budget du jour atteint (%d requêtes)", engine.max_queries_per_day)
            return False
        engine.repo.kv_set(budget_key(), str(used + 1))
    return True


def blocked_reason(data: dict, engines: tuple[str, ...]) -> str | None:
    """French-ready reason when every wanted engine is reported blocked (CAPTCHA, rate limit, suspended)."""
    blocked: dict[str, str] = {}
    for item in data.get("unresponsive_engines") or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2 and _BLOCKED.search(str(item[1])):
            blocked[str(item[0]).strip().lower()] = str(item[1])
    wanted = {e.strip().lower() for e in engines if e.strip()} or set(blocked)
    if not wanted or not wanted <= set(blocked):
        return None
    return ", ".join(f"{name} : {blocked[name]}" for name in sorted(wanted))


# --------------------------------------------------------------------------- query


def _as_engine(engine: SearxEngine | str | None) -> SearxEngine | None:
    if isinstance(engine, SearxEngine):
        return engine
    return SearxEngine(base_url=engine) if engine else None


def _params(engine: SearxEngine, query: str) -> dict[str, str | int]:
    params: dict[str, str | int] = {"q": query, "format": "json", "language": "fr-FR", "safesearch": 0}
    if engine.engines:
        params["engines"] = ",".join(engine.engines)
    return params


def _fetch(client: httpx.Client, engine: SearxEngine, query: str) -> dict | None:
    try:
        resp = client.get(search_url(engine.base_url), params=_params(engine, query), timeout=TIMEOUT_S)
        if resp.status_code == 429 and engine.repo is not None and engine.backoff_hours > 0:
            start_backoff(engine.repo, engine.backoff_hours, "HTTP 429 (trop de requêtes)")
            return None
        if resp.status_code != 200:
            log.warning("SearXNG : HTTP %s (format JSON activé sur l'instance ?)", resp.status_code)
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("SearXNG injoignable : %s", exc)
        return None
    return data if isinstance(data, dict) else None


def searxng_search(client: httpx.Client, engine: SearxEngine | str | None, query: str) -> list[tuple[str, str]]:
    """(url, title) of the results, best first; [] when unconfigured, paused, over budget or failing."""
    cfg = _as_engine(engine)
    if cfg is None or not query.strip():
        return []
    if backoff_until(cfg.repo) is not None or not _budget_left(cfg):
        return []
    _POLITE.wait_turn(cfg.min_interval_s)
    # checked again after the wait: another worker may have hit a CAPTCHA meanwhile
    if backoff_until(cfg.repo) is not None or not _take_query(cfg):
        return []
    data = _fetch(client, cfg, query)
    if data is None:
        return []
    results = data.get("results") or []
    if not results and cfg.repo is not None and cfg.backoff_hours > 0:
        reason = blocked_reason(data, cfg.engines)
        if reason:
            start_backoff(cfg.repo, cfg.backoff_hours, reason)
    pairs = [(str(r.get("url") or ""), str(r.get("title") or "")) for r in results if isinstance(r, dict)]
    return [(url, title) for url, title in pairs if url or title][:MAX_RESULTS]
