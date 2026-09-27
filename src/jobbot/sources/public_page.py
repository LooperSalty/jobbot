"""Reading a third-party web page safely: company careers pages come from crawled sites and offer URLs from their
JSON-LD, so jobbot must never be steered to an internal address (server metadata, the local dashboard…).

- the URL must be http(s) on a real domain name (no IP literal, ``localhost`` or ``.local`` name) and every
  address it resolves to must be public (``ipaddress.is_global``);
- redirects are followed by hand, each hop checked again (3 at most);
- the body is streamed and cut at ``MAX_PAGE_BYTES``.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from jobbot.http import HostThrottle

MAX_REDIRECTS = 3
MAX_PAGE_BYTES = 3_000_000
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")


class UnsafeUrl(ValueError):
    """The page is not on a public address (or redirects to one that is not)."""


class PageGone(RuntimeError):
    """HTTP 404 / 410."""


def is_safe_url(url: str | None) -> bool:
    """Syntactic check: http(s) and a dotted domain name that is not an IP literal nor a local name."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return "." in host and not host.endswith(_LOCAL_SUFFIXES) and not is_ip_literal(host)


def is_ip_literal(host: str) -> bool:
    """IP literal, including the short / hexadecimal forms resolvers accept (``127.1``, ``0x7f.0.0.1``)."""
    if all(re.fullmatch(r"\d+|0x[0-9a-f]*", label) for label in host.split(".")):
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def resolves_public(host: str) -> bool:
    """Every address of ``host`` is a public one (False when it does not resolve)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    addresses = {info[4][0].split("%")[0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


def fetch_public_page(client: httpx.Client, url: str, *, throttle: HostThrottle | None = None,
                      headers: dict[str, str] | None = None) -> tuple[str, str]:
    """(final URL, text) of a public page. Raises ``UnsafeUrl``, ``PageGone`` or ``httpx.HTTPError``."""
    for _ in range(MAX_REDIRECTS + 1):
        host = (urlsplit(url).hostname or "") if is_safe_url(url) else ""
        if not host or not resolves_public(host):
            raise UnsafeUrl(f"adresse non publique refusée : {url[:200]}")
        if throttle is not None:
            throttle.wait(url)
        with client.stream("GET", url, headers=headers or {"Accept": "text/html"}, follow_redirects=False) as resp:
            if resp.is_redirect:
                url = urljoin(url, resp.headers.get("location", ""))
                continue
            if resp.status_code in (404, 410):
                raise PageGone(f"{url[:200]} : HTTP {resp.status_code}")
            resp.raise_for_status()
            return url, _read_capped(resp)
    raise UnsafeUrl(f"trop de redirections : {url[:200]}")


def _read_capped(resp: httpx.Response) -> str:
    chunks: list[bytes] = []
    size = 0
    for chunk in resp.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_PAGE_BYTES:
            break
    return b"".join(chunks)[:MAX_PAGE_BYTES].decode(resp.encoding or "utf-8", errors="replace")
