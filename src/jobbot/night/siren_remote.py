"""Employer without any local agency (pure, no I/O): an IT services company (ESN, consulting) recruits for the
offer's city while it is registered elsewhere — its consultants work at the clients' sites.

Judged only when no operating IT company answering to that name has an establishment in the zone
(:func:`jobbot.night.siren_match.blocks_remote`), on the results of the query without place. The best company is
accepted only when:

- its name is strong (``STRONG_NAME``: exact, sigle, initials, name contained with one extra word);
- its NAF is IT / Syntec (a holding never: :func:`jobbot.night.siren_match.without_holdings`);
- the tie-breaks (:mod:`jobbot.night.siren_prefer`) prefer it over every other namesake — a group sibling closer
  to the offer first (« Acme Est » over « Acme Nord » or « Acme » for an offer in Metz), then the name, the
  activity, the number of establishments / the headcount, the head office in the target region;
- a common name (``GENERIC_HOMONYMS`` namesakes or more, or more results than the page, spread over
  ``GENERIC_ACTIVITIES`` NAF divisions or more, holdings aside) is the company's sigle.

The company keeps its head office (address, SIRET) but works where the offer is (city, postal code, coordinates:
distance and priority), with a French note « Pas d'agence locale enregistrée : candidature au siège (recrute pour
<ville de l'offre>) ».
"""

from __future__ import annotations

from dataclasses import replace

from jobbot.night.siren_match import (
    HOLDING, IT, NAME_MIN, Match, OfferGroup, Zone, activity_points, capped_name, name_score, sibling_notes,
    traits_of, usable, wanted_names, without_holdings,
)
from jobbot.night.siren_names import sigle_match
from jobbot.night.siren_prefer import NAME_GAP, STRONG_NAME, winner

GENERIC_HOMONYMS = 4  # namesakes (distinct SIREN, same name score)…
GENERIC_ACTIVITIES = 3  # …in at least this many NAF divisions: a common name, accepted through its sigle only


def absent_note(group: OfferGroup) -> str:
    where = group.city or group.postal_code or "la ville de l'offre"
    return f"Pas d'agence locale enregistrée : candidature au siège (recrute pour {where})"


def _similar(group: OfferGroup, zone: Zone, items: list[dict], wanted: list[str]) -> list[tuple[Match, float]]:
    """Plausible namesakes wherever they are (one per SIREN), each at its head office, with its raw similarity."""
    out: dict[str, tuple[Match, float]] = {}
    for item in items:
        similarity = name_score(wanted, item) if isinstance(item, dict) and usable(item) else 0.0
        if similarity < NAME_MIN or str(item["siren"]) in out:
            continue
        name = capped_name(similarity, wanted, item)
        out[str(item["siren"])] = (Match(
            siren=str(item["siren"]), item=item, place=item.get("siege") or {}, score=name + activity_points(item),
            name_score=name, traits=traits_of(group, zone, item, name), remote=True), similarity)
    return list(out.values())


def is_common_name(similar: list[tuple[Match, float]], total: int, shown: int) -> bool:
    """Many unrelated companies bear that very name (holdings of a group aside)."""
    if not similar:
        return False
    top = max(similarity for _, similarity in similar)
    namesakes = [m for m, similarity in similar if similarity >= top - NAME_GAP]
    divisions = {str(m.item.get("activite_principale") or "")[:2] for m in namesakes if m.traits.tier != HOLDING}
    many = len(namesakes) >= GENERIC_HOMONYMS or total > shown
    return many and len(divisions) >= GENERIC_ACTIVITIES


def decide_remote(group: OfferGroup, zone: Zone, items: list[dict], total: int = 0) -> Match | None:
    """The IT company recruiting for the offer's place from its head office, only with a clear preference."""
    wanted = wanted_names(group.name)
    similar = _similar(group, zone, items, wanted)
    pool = without_holdings(sorted((m for m, _ in similar), key=lambda m: -m.score))
    index = winner([m.traits for m in pool]) if pool else None
    if index is None:
        return None
    best = pool[index]
    if best.traits.tier != IT or best.name_score < STRONG_NAME:
        return None
    if is_common_name(similar, total, len(items)) and not sigle_match(wanted, best.item):
        return None  # a common name without a local agency: never guess
    others = [m for m in pool if m is not best]
    return replace(best, notes=(absent_note(group), *sibling_notes(best, others)))
