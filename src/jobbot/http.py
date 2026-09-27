"""Shared HTTP client with sane defaults, polite retries and per-host throttling."""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
}
RETRY_STATUSES = {419, 429, 500, 502, 503, 504}  # 419 = API Apprentissage rate limit


class HostThrottle:
    """Minimum interval between two requests to the same host (thread-safe). Each caller reserves its slot under
    the lock and sleeps outside it: parallel workers on other hosts are never held up."""

    def __init__(self, min_interval_s: float = 1.0):
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host)
            slot = now if last is None else max(now, last + self.min_interval_s)
            self._last[host] = slot
        if slot > now:
            time.sleep(slot - now)


def make_client(timeout: float = 20.0, headers: dict[str, str] | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={**DEFAULT_HEADERS, **(headers or {})},
        follow_redirects=True,
    )


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff_s: float = 2.0,
    throttle: HostThrottle | None = None,
    **kwargs,
) -> httpx.Response:
    """Send a request, retrying on network errors and 429/5xx (honours Retry-After)."""
    attempt = 0
    while True:
        if throttle:
            throttle.wait(url)
        try:
            resp = client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt >= retries:
                raise
            wait = backoff_s * (2**attempt)
            log.warning("HTTP %s %s échoue (%s), nouvel essai dans %.1fs", method, url, exc, wait)
            time.sleep(wait)
            attempt += 1
            continue
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else backoff_s * (2**attempt)
            log.warning("HTTP %s sur %s, nouvel essai dans %.1fs", resp.status_code, url, wait)
            time.sleep(min(wait, 60.0))
            attempt += 1
            continue
        return resp
