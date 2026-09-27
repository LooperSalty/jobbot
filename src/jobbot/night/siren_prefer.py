"""Preference between registry companies answering to the same employer name (pure, no I/O): the tie-breaks of
:func:`jobbot.night.siren_match.decide` (no clear margin) and of :mod:`jobbot.night.siren_remote` (no local agency).

The first criterion that tells two companies apart decides:

1. **proximity of group siblings** — two IT companies with the offer's base name, one of them regional (« Acme
   Est » / « Acme Nord » / « Acme »): the head office clearly closer to the offer (``CLOSER_KM``) wins — the offer's
   place beats the literal qualifier;
2. **name** — ``NAME_GAP`` points of similarity or more;
3. **activity** — IT / Syntec NAF (62, 63.1, 58.2, 71.12B, 70.22Z) > any other > holding (64, 70.10Z);
4. **size** — clearly more open establishments (twice as many and 2 more at least), then a headcount tranche
   ``TRANCHE_GAP`` steps larger (unknown values never decide);
5. **head office in the target region**.

A company is preferred only when it wins against every other one (:func:`winner`), else nobody is.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

IT, OTHER, HOLDING = 2, 1, 0  # activity tiers
STRONG_NAME = 88.0  # exact name, sigle, initials, name contained with one extra word
NAME_GAP = 5.0  # points of name similarity that tell two companies apart
CLOSER_KM = 50.0  # a sibling's head office this much closer to the offer wins
TRANCHE_GAP = 2  # headcount tranche steps that tell two companies apart


@dataclass(frozen=True)
class Traits:
    """What the tie-breaks look at in one registry company."""

    tier: int  # IT / OTHER / HOLDING
    name: float  # name similarity with the offer's employer
    sibling: bool = False  # same base name as the offer's employer (regional qualifier, legal form dropped)
    regional: bool = False  # its name ends with a region or a city (« Acme Est »)
    hq_km: float | None = None  # head office → offer
    establishments: int = 0  # open establishments (0: unknown)
    tranche: int = -1  # headcount tranche rank (-1: unknown)
    hq_in_region: bool = False  # head office in the region of the offer / of the target departments


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def by_proximity(a: Traits, b: Traits) -> int:
    if not (a.tier == b.tier == IT and a.sibling and b.sibling and (a.regional or b.regional)):
        return 0
    if a.hq_km is None or b.hq_km is None or abs(a.hq_km - b.hq_km) < CLOSER_KM:
        return 0
    return _sign(b.hq_km - a.hq_km)


def by_name(a: Traits, b: Traits) -> int:
    gap = a.name - b.name
    return _sign(gap) if abs(gap) >= NAME_GAP else 0


def by_tier(a: Traits, b: Traits) -> int:
    return _sign(a.tier - b.tier)


def by_establishments(a: Traits, b: Traits) -> int:
    small, large = sorted((a.establishments, b.establishments))
    if small <= 0 or large < 2 * small or large - small < 2:
        return 0
    return _sign(a.establishments - b.establishments)


def by_tranche(a: Traits, b: Traits) -> int:
    if a.tranche < 0 or b.tranche < 0 or abs(a.tranche - b.tranche) < TRANCHE_GAP:
        return 0
    return _sign(a.tranche - b.tranche)


def by_region(a: Traits, b: Traits) -> int:
    return _sign(int(a.hq_in_region) - int(b.hq_in_region))


CRITERIA: tuple[Callable[[Traits, Traits], int], ...] = (
    by_proximity, by_name, by_tier, by_establishments, by_tranche, by_region,
)


def compare(a: Traits, b: Traits) -> int:
    """1: ``a`` preferred, -1: ``b`` preferred, 0: nothing tells them apart."""
    for criterion in CRITERIA:
        sign = criterion(a, b)
        if sign:
            return sign
    return 0


def winner(traits: Sequence[Traits]) -> int | None:
    """Index of the company preferred over every other one, None when there is none."""
    for i, mine in enumerate(traits):
        if all(compare(mine, other) > 0 for j, other in enumerate(traits) if j != i):
            return i
    return None


def operating_it(traits: Traits) -> bool:
    """An operating IT company with a strong name: a holding never wins over it."""
    return traits.tier == IT and traits.name >= STRONG_NAME
