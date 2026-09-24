"""Skill lexicon: canonical names ⇄ FR/EN variants, and word-boundary-aware skill detection.

Matching rules (see docs/research/ats-and-documents.md §6.2):
- text is accent-folded, lowercased, hyphens → spaces; longest variants are matched first and their span is
  masked, so "React Native" never also counts "React" and "Tailwind CSS" never also counts "CSS";
- tech-safe boundaries: "Java" never matches inside "JavaScript", "C" never inside a word, "C#", "C++",
  ".NET", "Node.js", "CI/CD" work, "Go" never matches "Google";
- ambiguous spellings ("C", "R", "Go", "Vue", "Express", "Spring", "Oracle", "Vite"…) are case-sensitive AND
  need a tech context: a list of skills ("Python, Go, Rust") or a word like "langage", "stack", "framework".
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from jobbot.matching._skills_data import TECH
from jobbot.matching._soft_data import LANGUAGES, RELATED_GROUPS, SOFT
from jobbot.matching._text import fold, lower_same_len, prep

SKILLS: dict[str, list[str]] = {spec.canonical: list(spec.variants) for spec in TECH}
"""Canonical tech skill → lowercase variants (FR/EN). Ambiguous spellings live in ``CASE_SENSITIVE``."""
CASE_SENSITIVE: dict[str, tuple[str, ...]] = {spec.canonical: spec.cs for spec in TECH if spec.cs}
SOFT_SKILLS: dict[str, list[str]] = {name: list(variants) for name, variants in SOFT.items()}
LANGUAGE_SKILLS: dict[str, list[str]] = {name: list(variants) for name, variants in LANGUAGES.items()}
PARENTS: dict[str, str] = {spec.canonical: spec.parent for spec in TECH if spec.parent}
_CATEGORY: dict[str, str] = {spec.canonical: spec.cat for spec in TECH}

NO_CONTEXT_CS = frozenset({"AI", "ML", "SOLID"})  # uppercase acronyms that are unambiguous as-is

_LB = r"(?<![\w+#.@])"
_LB_DOT = r"(?<![\w+.@])"  # ".NET" may follow "C#" ("C#.NET")
_RB = r"(?![\w+#]|\.(?![nN][eE][tT]\b)[A-Za-z0-9])"
_RB_SHORT = r"(?![&'])"  # "C'est", "R&D"
_TOKEN = re.compile(r"[a-z0-9]+")
_SEPARATORS = frozenset(",;/|•·+()&:")
_JOINER_BEFORE = re.compile(r"(?:^|\s)(?:et|and|ou|or)$")
_JOINER_AFTER = re.compile(r"(?:et|and|ou|or)\b")
_CTX_RX = re.compile(
    r"\b(?:langages?|language|frameworks?|stack|technos?|technologies?|environnement technique|librairies?|"
    r"library|libraries|programmation|programming|developpement en|developper en|coder en|en langage|"
    r"competences techniques)\b"
)
_FILLER = frozenset(
    ["langage", "langages", "language", "framework", "frameworks", "librairie", "library", "outil", "outils", "de", "des", "du", "d", "la", "le", "les", "l", "et", "en", "avec", "base", "bases", "serveur", "serveurs", "server", "servers", "lts", "x", "plus", "avance", "avancee", "avances", "bonne", "bonnes", "maitrise", "connaissance", "connaissances", "notions", "programmation", "developpement", "sur", "pour", "via", "moderne"]
)
_VERSION = re.compile(r"v?\d+[a-z]?|\d+x")


@dataclass(frozen=True)
class _Matcher:
    canonical: str
    rx: re.Pattern[str]
    anchor: str
    length: int
    cs: bool
    ctx: bool


Hit = tuple[str, int, int]


def _haystacks(text: str) -> tuple[str, str]:
    """(case-kept, lowercase) folded copies of ``text`` with identical length."""
    t = prep(text).replace("\n", " | ").replace("-", " ").replace("_", " ")
    t = re.sub(r" {2,}", " ", t)
    raw = fold(t)
    return raw, lower_same_len(raw)


def _variant_key(variant: str) -> str:
    return _haystacks(variant)[1].strip(" |")


def _matcher(canonical: str, variant: str, *, cs: bool, ctx: bool) -> _Matcher | None:
    key = _haystacks(variant)[0].strip(" |") if cs else _variant_key(variant)
    anchor = _TOKEN.search(key.lower())
    if not key or anchor is None:
        return None
    lb = _LB_DOT if key.startswith(".") else _LB
    rb = _RB + (_RB_SHORT if len(key) <= 2 else "")
    return _Matcher(canonical, re.compile(lb + re.escape(key) + rb), anchor.group(), len(key), cs, ctx)


def _build_table(entries: Iterable[tuple[str, Iterable[str], Iterable[str]]]) -> tuple[_Matcher, ...]:
    rows: list[_Matcher] = []
    for canonical, variants, cs_variants in entries:
        ambiguous = {v.lower() for v in cs_variants}
        for v in {canonical.lower(), *variants}:
            if v in ambiguous and v not in variants:
                continue  # "c", "go", "vite"… only exist as case-sensitive, context-checked spellings
            m = _matcher(canonical, v, cs=False, ctx=False)
            if m:
                rows.append(m)
        for v in cs_variants:
            m = _matcher(canonical, v, cs=True, ctx=v not in NO_CONTEXT_CS)
            if m:
                rows.append(m)
    unique = {(m.canonical, m.rx.pattern, m.cs): m for m in rows}
    return tuple(sorted(unique.values(), key=lambda m: (-m.length, m.canonical)))


_TECH_TABLE = _build_table((s.canonical, s.variants, s.cs) for s in TECH)
_SOFT_TABLE = _build_table((name, variants, ()) for name, variants in SOFT.items())
_LANG_TABLE = _build_table((name, variants, ()) for name, variants in LANGUAGES.items())


def _build_term_index() -> dict[str, str]:
    index: dict[str, str] = {}
    sources = [(s.canonical, (*s.variants, *s.cs)) for s in TECH]
    sources += [(name, variants) for name, variants in {**SOFT, **LANGUAGES}.items()]
    for canonical, variants in sources:
        for v in (canonical, *variants):
            index.setdefault(_variant_key(v), canonical)
    return index


_TERM_INDEX = _build_term_index()


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _has_separator(low: str, start: int, end: int) -> bool:
    before = low[max(0, start - 12) : start].rstrip()
    after = low[end : end + 12].lstrip()
    if (before and before[-1] in _SEPARATORS) or (after and after[0] in _SEPARATORS):
        return True
    return bool(_JOINER_BEFORE.search(before) or _JOINER_AFTER.match(after))


def _in_tech_context(low: str, start: int, end: int, hits: list[Hit]) -> bool:
    if _CTX_RX.search(low[max(0, start - 40) : end + 40]):
        return True
    if not _has_separator(low, start, end):
        return False
    return any(s < end + 50 and e > start - 50 for _, s, e in hits)


def _apply(m: _Matcher, raw: str, low: str, hits: list[Hit], check_ctx: bool) -> tuple[str, str]:
    spans = [
        (x.start(), x.end())
        for x in m.rx.finditer(raw if m.cs else low)
        if not check_ctx or _in_tech_context(low, x.start(), x.end(), hits)
    ]
    if not spans:
        return raw, low
    hits.extend((m.canonical, s, e) for s, e in spans)
    return _mask(raw, spans), _mask(low, spans)


def _scan(table: tuple[_Matcher, ...], raw: str, low: str, lenient: bool) -> tuple[list[Hit], str, str]:
    """Match every row of ``table``; returns hits and the masked haystacks."""
    hits: list[Hit] = []
    if not low.strip(" |"):
        return hits, raw, low
    tokens = set(_TOKEN.findall(low))
    deferred: list[_Matcher] = []
    for m in table:
        if m.anchor not in tokens:
            continue
        if m.ctx and not lenient:
            deferred.append(m)
            continue
        raw, low = _apply(m, raw, low, hits, check_ctx=False)
    for m in deferred:
        raw, low = _apply(m, raw, low, hits, check_ctx=True)
    return hits, raw, low


# --------------------------------------------------------------------------- public API


def count_skills(text: str, *, lenient: bool = False) -> Counter[str]:
    """Canonical tech skill → number of mentions. ``lenient`` accepts ambiguous spellings without context
    (use it for a CV "Compétences" section, which is a list by construction)."""
    raw, low = _haystacks(text or "")
    hits, _, _ = _scan(_TECH_TABLE, raw, low, lenient)
    return Counter(canonical for canonical, _, _ in hits)


def find_skills(text: str) -> set[str]:
    """Canonical tech skills mentioned in ``text`` (word-boundary aware, ambiguity-safe)."""
    return set(count_skills(text))


def find_soft_skills(text: str) -> set[str]:
    raw, low = _haystacks(text or "")
    return {c for c, _, _ in _scan(_SOFT_TABLE, raw, low, True)[0]}


def find_languages(text: str) -> set[str]:
    raw, low = _haystacks(text or "")
    return {c for c, _, _ in _scan(_LANG_TABLE, raw, low, True)[0]}


def canonicalize(term: str) -> str:
    """'Postgres' → 'PostgreSQL', 'react.js' → 'React', 'Next.js 15' → 'Next.js', 'esprit d'équipe' →
    'Travail en équipe'. Unknown terms come back cleaned (trimmed, single spaces)."""
    cleaned = " ".join((term or "").split()).strip(" ,;:•*|")
    if not cleaned:
        return ""
    hit = _TERM_INDEX.get(_variant_key(cleaned))
    if hit:
        return hit
    return _single_canonical(cleaned) or cleaned


def _single_canonical(term: str) -> str | None:
    raw, low = _haystacks(term)
    found: set[str] = set()
    for table in (_TECH_TABLE, _SOFT_TABLE, _LANG_TABLE):
        hits, raw, low = _scan(table, raw, low, True)
        found.update(c for c, _, _ in hits)
    if len(found) != 1:
        return None
    rest = [t for t in _TOKEN.findall(low) if t not in _FILLER and not _VERSION.fullmatch(t)]
    return next(iter(found)) if not rest else None


def is_known(term: str) -> bool:
    """True when ``term`` canonicalizes to a lexicon entry (tech, soft skill or language)."""
    c = canonicalize(term)
    return c in SKILLS or c in SOFT_SKILLS or c in LANGUAGE_SKILLS


def skill_category(canonical: str) -> str:
    """KeywordHit category of a canonical name: hard | tool | soft | language."""
    if canonical in SOFT_SKILLS:
        return "soft"
    if canonical in LANGUAGE_SKILLS:
        return "language"
    return _CATEGORY.get(canonical, "hard")


def ancestors(skill: str) -> set[str]:
    """Broader skills satisfied by ``skill`` (Next.js → React → JavaScript)."""
    out: set[str] = set()
    current = PARENTS.get(skill)
    while current and current not in out:
        out.add(current)
        current = PARENTS.get(current)
    return out


def _build_descendants() -> dict[str, set[str]]:
    desc: dict[str, set[str]] = {}
    for child in PARENTS:
        for anc in ancestors(child):
            desc.setdefault(anc, set()).add(child)
    return desc


_DESCENDANTS = _build_descendants()


def descendants(skill: str) -> set[str]:
    """Narrower skills that satisfy ``skill`` (SQL ← PostgreSQL, MySQL…)."""
    return set(_DESCENDANTS.get(skill, set()))


def _build_related() -> dict[str, set[str]]:
    rel: dict[str, set[str]] = {}
    for group in RELATED_GROUPS:
        for name in group:
            rel.setdefault(name, set()).update(g for g in group if g != name)
    return rel


_RELATED = _build_related()


def related(skill: str) -> set[str]:
    """Sibling skills (partial credit only): React ↔ Vue.js, MySQL ↔ MariaDB…"""
    return set(_RELATED.get(skill, set()))


def expand_with_ancestors(skills: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for s in skills:
        out.add(s)
        out.update(ancestors(s))
    return out
