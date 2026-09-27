"""Employer names against registry names (pure, no I/O) — used by :mod:`jobbot.night.siren_match`.

- **Cleaning**: the offer's employer name without « H/F », legal forms, « groupe », « France », a trailing region or
  city, acronym dots, ampersands (« T&S I.T. » → « t&s it », « ts it »).
- **Similarity** against every registry name (raison sociale, sigle, nom complet, enseignes, noms commerciaux):
  equality without punctuation → 100, rapidfuzz ratio / token_sort_ratio, a name contained in the other
  (``SUBSET_SCORE``, fewer points per extra word), initials (« SIE » = « Société pour l'Informatique
  Embarquée ») → ``ACRONYM_SCORE``.
- **Group siblings**: :func:`base_name` drops a regional qualifier (« Acme Nord », « Acme Est » → « acme »);
  :func:`sigle_match` tells a name given as the registry's sigle.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from jobbot.matching.geo import KNOWN_PLACES
from jobbot.outreach._registry import name_candidates
from jobbot.textutil import strip_accents

SUBSET_SCORE = 88.0  # one name contained in the other, one extra word…
SUBSET_STEP = 4.0  # …minus this per further extra word
ACRONYM_SCORE = 90.0  # « SIE » = initials of « Société pour l'Informatique Embarquée »
SHORT_NAME = 4  # characters (without spaces) of an acronym-like name

_GENDER = re.compile(r"\(?\b[hfm]\s*/\s*[hfm]\b\)?")
_ACRONYM_DOTS = re.compile(r"\b(?:[a-z0-9]\.){2,}")
_LEGAL = re.compile(r"\b(?:sas|sasu|sarl|eurl|sa|sci|sca|snc|scop|selarl|group|groupe|france|holding|inc|ltd|gmbh)\b")
_STOP = frozenset({"a", "au", "aux", "d", "de", "des", "du", "en", "et", "l", "la", "le", "les", "pour", "sur",
                   "and", "of", "the"})
_REGIONS = ("nord", "sud", "est", "ouest", "centre", "grand est", "lorraine", "alsace", "moselle",
            "meurthe et moselle", "ile de france")
_TAIL = re.compile(r"\s+(?:agence\s+(?:de\s+|d\s+)?)?(?:"
                   + "|".join(re.escape(p) for p in sorted({*_REGIONS, *KNOWN_PLACES}, key=len, reverse=True))
                   + r")$")
_PAREN = re.compile(r"\(([^)]+)\)\s*$")


def _undotted(text: str) -> str:
    """Lowercase, no accent, no « H/F », acronym dots removed ('i.t.' → 'it')."""
    t = _GENDER.sub(" ", strip_accents(text or "").lower())
    return _ACRONYM_DOTS.sub(lambda m: m.group(0).replace(".", ""), t)


def words(name: str) -> str:
    """Comparable form: 'T&S I.T. (H/F)' → 't s it', 'Exemple S.A.S.' → 'exemple'."""
    t = re.sub(r"[^a-z0-9]+", " ", _undotted(name))
    stripped = " ".join(_LEGAL.sub(" ", t).split())
    return stripped or " ".join(t.split())


def compact(text: str) -> str:
    return text.replace(" ", "")


def cleaned_name(name: str) -> str:
    """Registry query form: no « H/F », legal form, « groupe », « France », trailing region / city; '&' kept."""
    t = re.sub(r"\s*&\s*", "&", re.sub(r"[^a-z0-9&]+", " ", _undotted(name)))
    t = " ".join(_LEGAL.sub(" ", t).split())
    while (shorter := _TAIL.sub("", t)) != t and len(compact(shorter)) >= 3:
        t = shorter
    return " ".join(t.split())


def query_variants(name: str) -> list[str]:
    """Cleaned alternatives of ``name`` worth their own query ('T&S I.T.' → ['t&s it', 'ts it'])."""
    plain = " ".join(strip_accents(name or "").lower().split())
    cleaned = cleaned_name(name)
    out: list[str] = []
    for variant in (cleaned, cleaned.replace("&", "")):
        variant = " ".join(variant.split())
        if len(compact(variant)) >= 2 and variant != plain and variant not in out:
            out.append(variant)
    return out


def wanted_names(name: str) -> list[str]:
    """Comparable forms of the offer's employer name and of its variants."""
    forms = [words(name), *(words(v) for v in query_variants(name))]
    return [f for f in dict.fromkeys(forms) if f]


def is_short(wanted: list[str]) -> bool:
    return bool(wanted) and min(len(compact(w)) for w in wanted) <= SHORT_NAME


def registry_names(item: dict) -> list[str]:
    """Every name of a registry result: raison sociale, sigle, nom complet, enseignes, noms commerciaux."""
    names = [*name_candidates(item), str(item.get("nom_complet") or "")]
    for place in [item.get("siege") or {}, *(item.get("matching_etablissements") or [])]:
        names.extend(str(e) for e in place.get("liste_enseignes") or [])
    return list(dict.fromkeys(n.strip() for n in names if n and n.strip()))


def _initials(text: str) -> str:
    tokens = [w for w in text.split() if w not in _STOP]
    return "".join(w[0] for w in tokens) if len(tokens) >= 2 else ""


def _acronym_of(short: str, long: str) -> bool:
    return " " not in short and 2 <= len(short) <= 6 and _initials(long) == short


def _contained(a: str, b: str) -> float:
    """One name inside the other ('acme' in 'acme nord'): fewer points per extra word."""
    ta, tb = set(a.split()), set(b.split())
    small = ta if len(ta) <= len(tb) else tb
    if len(compact(" ".join(small - _STOP))) < 4 or fuzz.token_set_ratio(a, b) < 100:
        return 0.0
    extra = len(ta ^ tb)
    return max(0.0, SUBSET_SCORE - SUBSET_STEP * max(0, extra - 1))


def pair_score(a: str, b: str) -> float:
    """Similarity (0-100) of two comparable names (:func:`words`)."""
    if not a or not b:
        return 0.0
    if compact(a) == compact(b):
        return 100.0
    score = max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))
    if _acronym_of(a, b) or _acronym_of(b, a):
        score = max(score, ACRONYM_SCORE)
    return max(score, _contained(a, b))


def name_score(wanted: list[str], item: dict) -> float:
    names = [w for w in (words(n) for n in registry_names(item)) if w]
    return max((pair_score(a, b) for a in wanted for b in names), default=0.0)


# --------------------------------------------------------------------------- group siblings, sigle


def legal_name(item: dict) -> str:
    return str(item.get("nom_raison_sociale") or item.get("nom_complet") or "").strip()


def base_name(name: str) -> str:
    """Comparable name without its regional qualifier: 'ACME EST' / 'Acme Nord' / 'ACME FRANCE' → 'acme'."""
    return words(cleaned_name(name))


def is_regional(name: str) -> bool:
    """A name ending with a region or a city (« Acme Est », « Acme Metz »)."""
    return bool(name) and words(name) != base_name(name)


def sigle_match(wanted: list[str], item: dict) -> bool:
    """The offer's employer name is the registry's sigle (``sigle`` or « nom complet (SIGLE) »)."""
    paren = _PAREN.search(str(item.get("nom_complet") or ""))
    sigles = [s for s in (item.get("sigle"), paren.group(1) if paren else None) if s and str(s).strip()]
    forms = {compact(w) for w in wanted}
    return any(compact(words(str(s))) in forms for s in sigles)
