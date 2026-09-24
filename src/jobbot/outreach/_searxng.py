"""Self-hosted SearXNG metasearch (``settings.search_engine_url``), used before Brave / DuckDuckGo.

``GET <base>/search?q=…&format=json`` (the instance must have the JSON format enabled). A local instance
needs no 25 s spacing like DuckDuckGo: a short politeness delay (``MIN_INTERVAL_S``) is enough.
Errors never propagate: an empty list lets the next search engine take over.
"""

from __future__ import annotations

import logging
import random
import threading
import time

import httpx

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 1.0
JITTER_S = 1.0
MAX_RESULTS = 20
TIMEOUT_S = 15.0


def _sleep(seconds: float) -> None:  # indirection so tests can disable waiting
    time.sleep(seconds)


class _Polite:
    """1-2 s between two queries to the same instance (thread-safe)."""

    def __init__(self) -> None:
        self.last = 0.0
        self._lock = threading.Lock()

    def wait_turn(self) -> None:
        with self._lock:
            delay = self.last + MIN_INTERVAL_S + random.uniform(0, JITTER_S) - time.monotonic()
            if self.last and delay > 0:
                _sleep(delay)
            self.last = time.monotonic()


_POLITE = _Polite()


def search_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/search"


def searxng_search(client: httpx.Client, base_url: str | None, query: str) -> list[tuple[str, str]]:
    """(url, title) of the results, best first; [] when unconfigured, unreachable or not JSON."""
    if not base_url or not query.strip():
        return []
    _POLITE.wait_turn()
    try:
        resp = client.get(search_url(base_url), params={"q": query, "format": "json", "language": "fr-FR",
                                                        "safesearch": 0}, timeout=TIMEOUT_S)
        if resp.status_code != 200:
            log.warning("SearXNG : HTTP %s (format JSON activé sur l'instance ?)", resp.status_code)
            return []
        results = resp.json().get("results") or []
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        log.warning("SearXNG injoignable : %s", exc)
        return []
    pairs = [(str(r.get("url") or ""), str(r.get("title") or "")) for r in results if isinstance(r, dict)]
    return [(url, title) for url, title in pairs if url or title][:MAX_RESULTS]
