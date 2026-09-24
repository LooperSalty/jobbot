"""Coerce free answers to what a form field accepts (integers, one of the options, yes/no...)."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence

from rapidfuzz import fuzz

from jobbot.textutil import normalize

YES = frozenset({"oui", "yes", "true", "vrai", "y", "o"})
NO = frozenset({"non", "no", "false", "faux", "n"})
FUZZY_MIN = 85

_PLACEHOLDER = re.compile(r"^(select|selectionne|choisi|choose|please select|veuillez|-|\.\.\.)|^$")
_PREFER_NOT = re.compile(
    r"prefere(r)? ne pas|prefer not|decline|don t wish|do not wish|ne souhaite pas|not to (say|answer|disclose)|"
    r"ne se prononce pas|sans reponse"
)


def is_placeholder(option: str) -> bool:
    return bool(_PLACEHOLDER.search(normalize(option)))


def yes_no(text: str) -> bool | None:
    words = normalize(text).replace(",", " ").split()
    if not words:
        return None
    if words[0] in YES:
        return True
    if words[0] in NO:
        return False
    return None


def is_binary(options: Sequence[str]) -> bool:
    real = [o for o in options if not is_placeholder(o)]
    return len(real) == 2 and {yes_no(o) for o in real} == {True, False}


def to_int_str(value: str) -> str | None:
    """'32 000 €' → '32000', '2 ans' → '2', 'aucun' → None."""
    compact = re.sub(r"(?<=\d)[\s.,](?=\d{3}\b)", "", value or "")
    m = re.search(r"\d+", compact)
    return m.group(0) if m else None


def _range(option: str) -> tuple[float, float] | None:
    t = normalize(option)
    nums = [int(x) for x in re.findall(r"\d+", t)]
    if re.search(r"moins d ?(un|une|1)\b|less than (one|1|a)\b", t):
        return (0, 0.999)
    if not nums:
        return (0, 0) if re.search(r"aucun|none|pas d experience|no experience|jamais|never", t) else None
    if re.search(r"moins|less|under|inferieur|<", t):
        return (0, nums[0] - 0.001)
    if re.search(r"plus|more|over|au moins|at least|et \+|\+|>|superieur|or more|ou plus", t):
        return (nums[0], math.inf)
    return (nums[0], nums[1]) if len(nums) >= 2 else (nums[0], nums[0])


def numeric_option(n: int, options: Sequence[str]) -> str | None:
    for option in options:
        bounds = _range(option)
        if bounds and bounds[0] <= n <= bounds[1]:
            return option
    return None


def match_option(value: str, options: Sequence[str]) -> str | None:
    """Best option for a free value: exact → yes/no → numeric range → containment → fuzzy."""
    opts = [o for o in options if o and not is_placeholder(o)]
    target = normalize(value)
    if not opts or not target:
        return None
    exact = next((o for o in opts if normalize(o) == target), None)
    if exact:
        return exact
    yn = yes_no(value)
    if yn is not None:
        found = next((o for o in opts if yes_no(o) is yn), None)
        if found:
            return found
    if re.fullmatch(r"\d+", target):
        return numeric_option(int(target), opts)
    contained = next((o for o in opts if len(normalize(o)) >= 3 and _contains(target, normalize(o))), None)
    contained = contained or next((o for o in opts if len(target) >= 3 and _contains(normalize(o), target)), None)
    if contained:
        return contained
    best = max(opts, key=lambda o: fuzz.token_set_ratio(target, normalize(o)))
    return best if fuzz.token_set_ratio(target, normalize(best)) >= FUZZY_MIN else None


def _contains(haystack: str, needle: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack))


def coerce(kind: str, options: Sequence[str], values: Sequence[str]) -> str | list[str] | None:
    """First candidate value that the field can take, converted to the field's format."""
    for value in values:
        out = _coerce_one(kind, options, value)
        if out not in (None, "", []):
            return out
    return None


def _coerce_one(kind: str, options: Sequence[str], value: str) -> str | list[str] | None:
    value = (value or "").strip()
    if not value or kind == "file":
        return None
    if kind == "number":
        return to_int_str(value)
    if kind in ("select", "radio"):
        return match_option(value, options)
    if kind == "checkbox" and options:
        parts = [p.strip() for p in re.split(r"[;|]", value) if p.strip()]
        picked = [o for o in (match_option(p, options) for p in parts) if o]
        return list(dict.fromkeys(picked)) or None
    if kind == "checkbox":
        return _yes_no_label(yes_no(value))
    return value


def _yes_no_label(flag: bool | None) -> str | None:
    if flag is None:
        return None
    return "Oui" if flag else "Non"


def prefer_not_option(options: Sequence[str]) -> str | None:
    return next((o for o in options if _PREFER_NOT.search(normalize(o))), None)


def level_option(options: Sequence[str], target: int, level_of: Callable[[str], int | None]) -> str | None:
    """Option whose level equals ``target``, else the highest level below it (never above: stay honest)."""
    scored = [(level_of(o), o) for o in options if not is_placeholder(o)]
    scored = [(lvl, o) for lvl, o in scored if lvl is not None]
    exact = next((o for lvl, o in scored if lvl == target), None)
    if exact:
        return exact
    below = [(lvl, o) for lvl, o in scored if lvl < target]
    return max(below, key=lambda x: x[0])[1] if below else None
