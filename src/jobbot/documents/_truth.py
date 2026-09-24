"""The master profile as a "truth base", and checks that a generated text claims nothing beyond it.

A claim is unsupported when it names a tech skill (lexicon-canonical, broader skills implied: Next.js ⇒ React ⇒
JavaScript), a soft skill or a language absent from the profile, or a number (optionally with a unit: %, €, ans)
that the profile never states.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from jobbot.config import Profile
from jobbot.documents._format import strip_emphasis
from jobbot.matching import lexicon
from jobbot.matching._text import family_of, norm, normalize_title

_THOUSANDS = re.compile(r"(?<=\d)[ \u00a0\u202f](?=\d{3}\b)")
_NUMBER = re.compile(r"(?<![\w.+-])(\d+(?:[.,]\d+)?)(?:\s*(%|k€|€|k\b|ans?\b|annees?\b))?")
_UNITS = {"annees": "an", "annee": "an", "ans": "an", "an": "an", "%": "%", "€": "€", "k€": "k€", "k": "k€"}


@dataclass(frozen=True)
class TruthBase:
    text: str
    norm: str
    skills: frozenset[str]
    soft: frozenset[str]
    languages: frozenset[str]
    numbers: frozenset[str]  # bare numbers ("210") and number+unit claims ("60%")
    families: frozenset[str]


def truth_text(profile: Profile) -> str:
    """Everything the candidate can claim (``Profile.full_text`` + dates, interests, alternance, identity)."""
    parts = [profile.full_text(), " ".join(profile.interests), profile.identity.city, profile.identity.postal_code]
    parts += [profile.identity.driving_license or ""]
    for exp in profile.experiences:
        parts.append(f"{exp.title} {exp.company} {exp.location} {exp.start} {exp.end}")
    for ed in profile.education:
        parts.append(f"{ed.degree} {ed.school} {ed.location} {ed.start} {ed.end} {ed.details}")
    alt = profile.alternance
    if alt is not None:
        parts += [alt.school, alt.degree, f"RNCP niveau {alt.rncp_level}", f"{alt.duration_months} mois",
                  alt.start_date, alt.rhythm]
    parts += [f"{k} {v}" for k, v in profile.answers.items()]
    return "\n".join(p for p in parts if p)


def number_claims(text: str) -> set[str]:
    """{'210', '60%', '2an'}: bare numbers plus number+unit claims (thousand separators removed)."""
    t = _THOUSANDS.sub("", norm(strip_emphasis(text)))
    claims: set[str] = set()
    for value, unit in _NUMBER.findall(t):
        number = value.replace(",", ".")
        claims.add(number)
        if unit:
            claims.add(number + _UNITS.get(unit, unit))
    return claims


def _profile_families(profile: Profile) -> frozenset[str]:
    titles = [*profile.target.job_titles, profile.headline, *(e.title for e in profile.experiences)]
    return frozenset(f for f in (family_of(normalize_title(t)) for t in titles) if f)


def build_truth(profile: Profile) -> TruthBase:
    text = truth_text(profile)
    skills = set(lexicon.count_skills(text, lenient=True))
    for item in profile.all_skills():
        canonical = lexicon.canonicalize(item)
        if canonical in lexicon.SKILLS:
            skills.add(canonical)
    quality_text = text + "\n" + " ".join(profile.qualities)
    return TruthBase(
        text=text,
        norm=norm(text),
        skills=frozenset(lexicon.expand_with_ancestors(skills)),
        soft=frozenset(lexicon.find_soft_skills(quality_text)),
        languages=frozenset(lexicon.find_languages(text)),
        numbers=frozenset(number_claims(text)),
        families=_profile_families(profile),
    )


def unsupported_skills(text: str, truth: TruthBase, *, lenient: bool = False,
                       allowed: frozenset[str] = frozenset()) -> list[str]:
    """Tech skills and languages of ``text`` absent from the profile. ``lenient`` (skill lists) also checks soft
    skills; in running text, adjectives such as « motivé » are self-assessments, not verifiable claims."""
    plain = strip_emphasis(text)
    found = set(lexicon.count_skills(plain, lenient=lenient))
    if lenient:
        canonical = lexicon.canonicalize(plain)
        found |= {canonical} if canonical in lexicon.SKILLS else set()
    bad = sorted(found - truth.skills - allowed)
    if lenient:
        bad += sorted(lexicon.find_soft_skills(plain) - truth.soft)
    bad += sorted(lexicon.find_languages(plain) - truth.languages)
    return bad


def unsupported_numbers(text: str, truth: TruthBase, *, allowed: frozenset[str] = frozenset()) -> list[str]:
    known = truth.numbers | allowed
    return sorted(c for c in number_claims(text) if c not in known)


_DEGREES = (
    ("Master", re.compile(r"\bmasters?\b")), ("Mastère", re.compile(r"\bmastere\b")),
    ("Licence", re.compile(r"\blicence\b")), ("Bachelor", re.compile(r"\bbachelor\b")),
    ("Doctorat", re.compile(r"\bdoctorat\b|\bphd\b")), ("MBA", re.compile(r"\bmba\b")),
    ("diplôme d'ingénieur", re.compile(r"\b(?:diplome|titre|ecole) d'ingenieurs?\b")),
    ("BTS", re.compile(r"\bbts\b")), ("DUT", re.compile(r"\bdut\b")),
)


def unsupported_degrees(text: str, truth: TruthBase) -> list[str]:
    """Degree names ("Master", "Licence"…) the profile never mentions."""
    n = norm(strip_emphasis(text))
    return [label for label, rx in _DEGREES if rx.search(n) and not rx.search(truth.norm)]


def unsupported_claims(text: str, truth: TruthBase, *, lenient: bool = False,
                       allowed_skills: frozenset[str] = frozenset(),
                       allowed_numbers: frozenset[str] = frozenset()) -> list[str]:
    """French descriptions of every claim of ``text`` the profile does not back."""
    claims = [f"compétence « {s} »" for s in unsupported_skills(text, truth, lenient=lenient, allowed=allowed_skills)]
    claims += [f"chiffre « {n} »" for n in unsupported_numbers(text, truth, allowed=allowed_numbers)]
    claims += [f"diplôme « {d} »" for d in unsupported_degrees(text, truth)]
    return claims


_STOPWORDS = frozenset({"de", "des", "du", "d", "la", "le", "les", "l", "et", "en", "a", "au", "aux", "un", "une",
                        "pour", "par", "sur", "avec", "dans", "the", "of", "and"})
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*")
PHRASE_WINDOW = 4  # extra words allowed between the words of a phrase ("maintenance corrective et évolutive")


def _stem(word: str) -> str:
    return word[: max(4, len(word) - 2)]


def _in_window(stems: list[str], words: list[str]) -> bool:
    size = len(stems) + PHRASE_WINDOW
    for start in range(max(1, len(words) - size + 1)):
        window = words[start:start + size]
        if all(any(w.startswith(s) for w in window) for s in stems):
            return True
    return False


def phrase_supported(phrase: str, truth: TruthBase) -> bool:
    """A free phrase ("maintenance évolutive") is supported when the profile says it: exact, fuzzy, or all its
    words close together ("maintenance corrective et évolutive"); accents ignored."""
    p = norm(phrase)
    if len(p) < 3:
        return False
    if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", truth.norm):
        return True
    if len(p) >= 6 and fuzz.partial_ratio(p, truth.norm) >= 90:
        return True
    stems = [_stem(w) for w in _WORD.findall(p) if w not in _STOPWORDS]
    return len(stems) >= 2 and _in_window(stems, _WORD.findall(truth.norm))


def similar(a: str, b: str, threshold: float = 85.0) -> bool:
    """Loose equality of two short labels (titles, degrees, schools): shortened forms are accepted."""
    na, nb = norm(strip_emphasis(a)), norm(strip_emphasis(b))
    if not na or not nb:
        return na == nb
    return na == nb or fuzz.token_set_ratio(na, nb) >= threshold
