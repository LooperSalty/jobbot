"""Nominative address patterns (Hunter syntax: ``{first}.{last}``, ``{f}{last}``, ``{first}``…).

Pure helpers + kv persistence: infer a domain's pattern from any named address seen (website, JSON-LD, Hunter),
remember it under ``kv["email_pattern:<domain>"]`` and render it for a person. A learned pattern is far more
reliable than the generic priors (research §6.6: first.last ≈ 48 % overall, first name only in small teams).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable

from jobbot.db import Repository

log = logging.getLogger(__name__)

KV_PREFIX = "email_pattern:"
# Every pattern we can recognise, most common first (ties in inference resolve to the earliest one).
TEMPLATES = (
    "{first}.{last}", "{f}.{last}", "{f}{last}", "{first}", "{first}{last}", "{first}-{last}", "{first}_{last}",
    "{last}.{first}", "{last}", "{last}{f}", "{first}.{l}", "{first}{l}", "{last}.{f}", "{last}{first}",
    "{f}-{last}", "{last}-{first}", "{last}_{first}", "{f}{l}",
)
# Prior order used when nothing was learned (research §6.6), as (template, dashed compound names).
DEFAULT_ORDER = (
    ("{first}.{last}", False), ("{first}.{last}", True), ("{f}.{last}", False), ("{f}{last}", False),
    ("{first}", False), ("{first}{last}", False), ("{first}-{last}", False), ("{first}_{last}", False),
    ("{last}.{first}", False), ("{last}", False), ("{last}{f}", False), ("{first}.{l}", False),
)
_PLACEHOLDER = re.compile(r"\{(first|last|f|l)\}")
_VALID_PATTERN = re.compile(r"(?:\{(?:first|last|f|l)\}|[._-])+")
_VALID_LOCAL = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
_TWO_WORDS = re.compile(r"([a-z]{2,})([._-])([a-z]{2,})")


def norm_name(value: str) -> str:
    """ASCII, lower-case, only letters, spaces, hyphens and apostrophes ("Élodie" → "elodie")."""
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z\- ']", "", text).strip()


def is_valid_pattern(pattern: str | None) -> bool:
    return bool(pattern) and bool(_VALID_PATTERN.fullmatch(pattern)) and bool(_PLACEHOLDER.search(pattern))


def _parts(first: str, last: str, dashed: bool) -> tuple[str, str] | None:
    first_n = norm_name(first.split()[0] if first and first.split() else "")
    last_n = norm_name(last)
    if not first_n or not last_n:
        return None
    if dashed:
        return re.sub(r"[\s']", "-", first_n), re.sub(r"[\s']", "-", last_n)
    return re.sub(r"[\s'\-]", "", first_n), re.sub(r"[\s'\-]", "", last_n)


def render_local(pattern: str, first: str, last: str, *, dashed: bool = False) -> str | None:
    """Local part of ``pattern`` for this person ("{f}{last}", "Jean", "Dupont" → "jdupont")."""
    if not is_valid_pattern(pattern):
        return None
    parts = _parts(first, last, dashed)
    if parts is None or not parts[0] or not parts[1]:
        return None
    values = {"first": parts[0], "last": parts[1], "f": parts[0][0], "l": parts[1][0]}
    local = _PLACEHOLDER.sub(lambda m: values[m.group(1)], pattern)
    return local if _VALID_LOCAL.fullmatch(local) else None


def apply_pattern(pattern: str, first: str, last: str, domain: str) -> list[str]:
    """Addresses of this person under ``pattern`` (compact, then hyphenated compound names)."""
    domain = (domain or "").strip().lower()
    if not domain:
        return []
    locals_ = (render_local(pattern, first, last, dashed=d) for d in (False, True))
    return list(dict.fromkeys(f"{x}@{domain}" for x in locals_ if x))


def default_guesses(first: str, last: str, domain: str) -> list[str]:
    domain = (domain or "").strip().lower()
    if not domain:
        return []
    locals_ = (render_local(t, first, last, dashed=d) for t, d in DEFAULT_ORDER)
    return list(dict.fromkeys(f"{x}@{domain}" for x in locals_ if x))


# --------------------------------------------------------------------------- inference


def infer_pattern(local: str, first: str, last: str) -> str | None:
    """Pattern that turns (first, last) into ``local`` ("jdupont", "Jean", "DUPONT" → "{f}{last}")."""
    local = (local or "").strip().lower()
    for template in TEMPLATES:
        if any(render_local(template, first, last, dashed=d) == local for d in (False, True)):
            return template
    return None


def infer_from_local(local: str) -> str | None:
    """Unknown person: "sylvie.martin" is assumed first.last (the dominant order in France)."""
    match = _TWO_WORDS.fullmatch((local or "").strip().lower())
    return f"{{first}}{match.group(2)}{{last}}" if match else None


def infer_domain_pattern(named_locals: Iterable[str], names: Iterable[tuple[str, str]]) -> str | None:
    """Most voted pattern among named local parts (a local matched to a known person counts double)."""
    people = [(f, ln) for f, ln in names if f and ln]
    votes: Counter[str] = Counter()
    for local in named_locals:
        template = next((t for f, ln in people if (t := infer_pattern(local, f, ln))), None)
        if template:
            votes[template] += 2
        elif (guess := infer_from_local(local)) is not None:
            votes[guess] += 1
    if not votes:
        return None
    best = max(votes.values())
    return next(t for t in (*TEMPLATES, *votes) if votes.get(t) == best)


def person_for_local(local: str, names: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    """The (first, last) whose name renders to ``local`` under any known pattern (initials-only excluded)."""
    local = (local or "").strip().lower()
    if len(local) < 3:
        return None
    for first, last in names:
        template = infer_pattern(local, first, last)
        if template and template not in ("{f}{l}",):
            return first, last
    return None


# --------------------------------------------------------------------------- persistence


def learned_pattern(repo: Repository | None, domain: str | None) -> str | None:
    if repo is None or not domain:
        return None
    value = repo.kv_get(f"{KV_PREFIX}{domain.strip().lower()}")
    return value if is_valid_pattern(value) else None


def remember_pattern(repo: Repository, domain: str, pattern: str) -> bool:
    """Store the pattern of ``domain``; False (nothing stored) when it is not a valid pattern."""
    if not domain or not is_valid_pattern(pattern):
        return False
    key = f"{KV_PREFIX}{domain.strip().lower()}"
    if repo.kv_get(key) != pattern:
        repo.kv_set(key, pattern)
        log.info("Format d'adresse appris pour %s : %s", domain, pattern)
    return True
