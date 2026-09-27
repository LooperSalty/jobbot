"""Address guessing and deliverability checks: name patterns, MX lookup, Hunter.io (optional).

No SMTP ``RCPT TO`` probing: port 25 is blocked on French residential lines and probing hurts reputation.
Hunter answers ``accept_all``/``unknown`` are treated as *unverifiable*.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable

import dns.exception
import dns.resolver
import httpx

from jobbot import clock
from jobbot.db import Repository
from jobbot.http import make_client, request_with_retry
from jobbot.models import ContactEmail
from jobbot.outreach._extract import classify_email
from jobbot.outreach._patterns import apply_pattern, default_guesses, learned_pattern

log = logging.getLogger(__name__)

HUNTER_BASE = "https://api.hunter.io/v2"
HUNTER_MAX_CONFIDENCE = 0.85
VERIFIER_POLLS = 3
HUNTER_MONTHLY_CAPS = {"search": 20, "verify": 40}  # free plan = 50 credits/month (verify = 0.5), keep a margin
_CREDIT_LOCK = threading.Lock()  # parallel enrichment workers never overspend the budget


def guess_patterns(
    first: str, last: str, domain: str, *, pattern: str | None = None, repo: Repository | None = None
) -> list[str]:
    """Most likely nominative addresses, most probable first. The domain's learned pattern (``pattern``, else
    ``kv["email_pattern:<domain>"]`` when ``repo`` is given) comes first, then the priors
    (prenom.nom > p.nom > pnom > prenom...)."""
    learned = pattern or learned_pattern(repo, domain)
    first_choice = apply_pattern(learned, first, last, domain) if learned else []
    guesses = default_guesses(first, last, domain)
    if not guesses:
        return []
    return list(dict.fromkeys([*first_choice, *guesses]))


def take_hunter_credit(repo: Repository, kind: str) -> bool:
    """Reserve one Hunter call of ``kind`` (search | verify) in this month's budget (kv ``hunter_<kind>:YYYY-MM``)."""
    key = hunter_month_key(kind)
    cap = HUNTER_MONTHLY_CAPS[kind]
    with _CREDIT_LOCK:
        used = int(repo.kv_get(key, "0") or 0)
        if used >= cap:
            log.info("Budget Hunter mensuel (%s) atteint : %d/%d", kind, used, cap)
            return False
        repo.kv_set(key, str(used + 1))
    return True


def hunter_month_key(kind: str) -> str:
    return f"hunter_{kind}:{clock.to_paris(clock.now()):%Y-%m}"


def _make_resolver() -> dns.resolver.Resolver:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5.0
    return resolver


def has_mx(domain: str) -> bool:
    """True when ``domain`` accepts mail: MX records (null MX excluded) or an implicit MX (A record)."""
    name = (domain or "").strip().lower().rstrip(".")
    if not name or "." not in name:
        return False
    resolver = _make_resolver()
    try:
        answers = resolver.resolve(name, "MX")
    except dns.resolver.NXDOMAIN:
        return False
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
        return _has_a(resolver, name)
    except dns.exception.DNSException as exc:
        log.info("Résolution MX impossible pour %s : %s", name, exc)
        return False
    return any(str(a.exchange).rstrip(".") for a in answers)  # RFC 7505 null MX ("MX 0 .") → no host


def _has_a(resolver: dns.resolver.Resolver, name: str) -> bool:
    try:
        resolver.resolve(name, "A")
        return True
    except dns.exception.DNSException:
        return False


# --------------------------------------------------------------------------- Hunter.io


def _hunter_get(client: httpx.Client, path: str, params: dict, api_key: str) -> httpx.Response:
    # Key in a header, never in the URL (request URLs end up in logs). No retry: Hunter's 429 means
    # "monthly credits exhausted" and 403 "rate limited" — retrying would only burn time.
    return request_with_retry(
        client, "GET", f"{HUNTER_BASE}{path}", params=params, headers={"X-API-KEY": api_key}, retries=0,
    )


def _hunter_error(resp: httpx.Response) -> str:
    return {
        401: "clé API Hunter invalide",
        403: "limite de débit Hunter atteinte",
        429: "crédits Hunter épuisés pour ce mois",
        451: "la personne a demandé à Hunter de ne plus traiter ses données",
    }.get(resp.status_code, f"HTTP {resp.status_code}")


def _hunter_kind(item: dict, email: str) -> str:
    department = str(item.get("department") or "").lower()
    position = str(item.get("position") or "").lower()
    if department in ("hr", "human resources") or re.search(r"recrut|rh\b|ressources humaines|talent|human", position):
        return "hr"
    if item.get("type") == "generic":
        return classify_email(email) or "generic"
    if department in ("executive", "management") or re.search(r"ceo|fondat|founder|g[ée]rant|pr[ée]sident|directeur g", position):
        return "leader"
    return "named"


def hunter_contact(item: dict) -> ContactEmail | None:
    """One Hunter ``emails[]`` item → :class:`ContactEmail` (None when invalid or unusable)."""
    email = str(item.get("value") or "").strip().lower()
    status = str((item.get("verification") or {}).get("status") or "")
    if not email or status == "invalid" or classify_email(email) is None:
        return None
    name = " ".join(p for p in (item.get("first_name"), item.get("last_name")) if p) or None
    try:
        confidence = min(HUNTER_MAX_CONFIDENCE, max(0.0, float(item.get("confidence") or 0) / 100))
    except (TypeError, ValueError):
        confidence = 0.0
    kind = _hunter_kind(item, email)
    if kind in ("leader", "named") and _is_tech(item):
        kind = "tech"
    return ContactEmail(
        email=email, confidence=round(confidence, 2), source="hunter", kind=kind,
        person_name=name, verified=status == "valid",
    )


_TECH_POSITION = re.compile(r"\bcto\b|directeur technique|responsable technique|tech lead|lead dev|head of engineering",
                            re.I)


def _is_tech(item: dict) -> bool:
    return bool(_TECH_POSITION.search(str(item.get("position") or "")))


def hunter_domain_data(domain: str, api_key: str, *, client: httpx.Client | None = None) -> dict:
    """Raw Hunter ``domain-search`` data (``emails``, ``pattern``, ``organization``…); {} on any error."""
    if not api_key or not domain:
        return {}
    own = client is None
    http = client or make_client()
    try:
        resp = _hunter_get(http, "/domain-search", {"domain": domain, "limit": 10}, api_key)
        if resp.status_code != 200:
            log.warning("Hunter domain-search %s : %s", domain, _hunter_error(resp))
            return {}
        data = resp.json().get("data")
        return data if isinstance(data, dict) else {}
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        log.warning("Hunter domain-search %s impossible : %s", domain, exc)
        return {}
    finally:
        if own:
            http.close()


def hunter_domain_search(domain: str, api_key: str, *, client: httpx.Client | None = None) -> list[ContactEmail]:
    """Addresses Hunter knows for ``domain`` (free plan: first 10). Errors → [] (logged, never raised)."""
    items = hunter_domain_data(domain, api_key, client=client).get("emails") or []
    return [c for c in (hunter_contact(i) for i in items if isinstance(i, dict)) if c is not None]


def hunter_verify(
    email: str, api_key: str, *, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep
) -> str:
    """Hunter verdict: valid | invalid | accept_all | webmail | disposable | unknown | claimed | error."""
    if not api_key:
        return "error"
    own = client is None
    http = client or make_client()
    try:
        for _ in range(VERIFIER_POLLS):
            resp = _hunter_get(http, "/email-verifier", {"email": email}, api_key)
            if resp.status_code == 202:  # verification still running
                sleep(3.0)
                continue
            if resp.status_code == 451:
                return "claimed"
            if resp.status_code != 200:
                log.warning("Hunter email-verifier : %s", _hunter_error(resp))
                return "error"
            return str((resp.json().get("data") or {}).get("status") or "unknown")
        return "unknown"
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Hunter email-verifier impossible : %s", exc)
        return "error"
    finally:
        if own:
            http.close()
