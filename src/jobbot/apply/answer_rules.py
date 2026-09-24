"""Deterministic answers to common FR/EN application-form questions (no I/O, no LLM).

Each rule reads the normalised question label and returns a ``Candidate`` (one or more values to try,
in order) or ``None``. ``jobbot.apply.answers.FormAnswerer`` coerces candidates to the field kind/options
(see ``jobbot.apply.coerce``) and falls back to remembered answers and the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobbot.config import Profile
from jobbot.models import JobOffer
from jobbot.textutil import normalize


@dataclass(frozen=True)
class Candidate:
    values: tuple[str, ...]
    confidence: float
    source: str = "rule"  # rule | profile


@dataclass(frozen=True)
class RuleInput:
    text: str  # normalised label, hyphens replaced by spaces
    kind: str
    options: tuple[str, ...]
    profile: Profile
    job: JobOffer | None


# --------------------------------------------------------------------------- label helpers

_STAR = re.compile(r"\*")
_NOISE = re.compile(r"\((?:obligatoire|required|optionnel|optional|facultatif)\)|\b(?:obligatoire|required)\s*$", re.I)


def clean_label(label: str) -> str:
    """Strip required markers, trailing colons and LinkedIn's duplicated (visually hidden) label text."""
    text = _NOISE.sub(" ", _STAR.sub(" ", label or ""))
    text = re.sub(r"\s+", " ", text).strip(" : \t")
    words = text.split(" ")
    half = len(words) // 2
    if half and len(words) % 2 == 0 and words[:half] == words[half:]:
        text = " ".join(words[:half])
    return text


def rule_text(label: str) -> str:
    return normalize(clean_label(label)).replace("-", " ")


_YES_NO_START = re.compile(
    r"^(etes vous|avez vous|as tu|es tu|pouvez vous|seriez vous|acceptez vous|accepteriez vous|parlez vous|"
    r"disposez vous|possedez vous|habitez vous|souhaitez vous|est ce que|are you|do you|have you|can you|"
    r"would you|will you|did you|is your|etes|avez)\b"
)


def is_yes_no_question(text: str) -> bool:
    return bool(_YES_NO_START.search(text))


# --------------------------------------------------------------------------- identity

_IDENTITY: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (key, re.compile(pattern))
    for key, pattern in (
        ("country_code", r"country code|indicatif"),
        ("first_name", r"\b(prenom|first ?name|given name|forename)\b"),
        ("last_name", r"\b(nom de famille|last ?name|surname|family name)\b|^nom$"),
        ("full_name", r"^(name|your name|votre nom|nom et prenom|nom prenom|prenom et nom|prenom nom)$|full name|nom complet"),
        ("email", r"\b(e ?mail|courriel|adresse electronique|mail)\b"),
        ("phone", r"\b(telephone|phone|mobile|portable|tel)\b"),
        ("postal_code", r"code postal|postal code|\bzip\b"),
        ("city", r"\b(ville|city|localite|town|commune|localisation|location)\b|lieu de residence|ou habitez"),
        ("country", r"\b(pays|country)\b"),
        ("linkedin", r"linkedin"),
        ("github", r"github"),
        ("portfolio", r"portfolio|site web|site internet|website|personal site|\burl\b"),
        ("address", r"\b(adresse|address)\b"),
    )
)


def identity_values(key: str, profile: Profile) -> tuple[str, ...]:
    """Values to try for a well-known identity field (first one wins after coercion)."""
    i = profile.identity
    digits = re.sub(r"\D", "", i.phone)
    national = "0" + digits[2:] if digits.startswith("33") else digits
    table: dict[str, tuple[str | None, ...]] = {
        "first_name": (i.first_name,), "last_name": (i.last_name,), "full_name": (i.full_name,),
        "email": (i.email,), "phone": (i.phone, national), "postal_code": (i.postal_code,),
        "city": (i.city,), "country": (i.country, "France"), "country_code": ("France (+33)", "+33", "France"),
        "linkedin": (i.linkedin,), "github": (i.github,), "portfolio": (i.portfolio, i.github),
        "address": (f"{i.postal_code} {i.city}".strip(),),
    }
    return tuple(v for v in table.get(key, ()) if v)


def identity_key(text: str) -> str | None:
    """Which identity field a short label designates (``None`` for real questions)."""
    if len(text.split()) > 7 or is_yes_no_question(text):
        return None
    return next((key for key, pattern in _IDENTITY if pattern.search(text)), None)


def identity(r: RuleInput) -> Candidate | None:
    key = identity_key(r.text)
    values = identity_values(key, r.profile) if key else ()
    return Candidate(values, 0.95) if values else None


# --------------------------------------------------------------------------- skills & years

_ALIASES = (
    ("nodejs", "node.js"), ("node", "node.js"), ("reactjs", "react"), ("react.js", "react"), ("nextjs", "next.js"),
    ("js", "javascript"), ("ts", "typescript"), ("postgres", "postgresql"), ("dotnet", ".net"), ("csharp", "c#"),
    ("c sharp", "c#"), ("html", "html5"), ("css", "css3"), ("vuejs", "vue.js"),
)
_VERSION = re.compile(r"\s+(\d+(\.\d+)*\+?|es\d+\+?)$")


def _boundary(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9+#.])" + re.escape(term) + r"(?![a-z0-9+#]|\.[a-z])")


def _apply_aliases(text: str) -> str:
    for alias, canon in _ALIASES:
        text = _boundary(alias).sub(canon, text)
    return text


def profile_vocab(profile: Profile) -> set[str]:
    terms = {k for k in profile.years_experience if k != "default"} | set(profile.all_skills())
    return {_VERSION.sub("", normalize(t)) for t in terms} - {""}


def find_skill(text: str, vocab: set[str]) -> str | None:
    """Longest profile skill mentioned in ``text`` (word-boundary aware, aliases resolved)."""
    t = _apply_aliases(text)
    for term in sorted(vocab, key=len, reverse=True):
        if len(term) >= 2 and _boundary(term).search(t):
            return term
    return None


_TOPIC = re.compile(r"\b(?:avec|en|with|in|using|sur|dans)\s+(?:le |la |l |les |du |des |the )?([a-z0-9+#./ ]{2,40}?)\s*$")
_GENERIC_TOPIC = re.compile(
    r"domaine|field|secteur|industry|poste|role|position|metier|informatique|\bit\b|developpement|development|"
    r"programmation|programming|software|logiciel|\bweb\b|professionnel|professional|total|\bwork\b|travail|france|annees"
)


def mentioned_topic(text: str) -> str | None:
    m = _TOPIC.search(text)
    return m.group(1).strip() if m else None


_YEARS = re.compile(
    r"(combien|nombre) d annees|annees d experience|ans d experience|years? of (\w+ )?experience|how many years|"
    r"experience (en annees|in years)"
)


def years(r: RuleInput) -> Candidate | None:
    if not _YEARS.search(r.text):
        return None
    table = {_VERSION.sub("", normalize(k)): v for k, v in r.profile.years_experience.items()}
    default = table.get("default", 0)
    skill = find_skill(r.text, profile_vocab(r.profile))
    if skill:
        return Candidate((str(table.get(skill, default)),), 0.9)
    topic = mentioned_topic(r.text)
    if topic and not _GENERIC_TOPIC.search(topic):
        return Candidate(("0",), 0.8)  # skill absent from the profile: the honest answer
    return Candidate((str(default),), 0.85)


_SKILL_Q = re.compile(
    r"^(avez vous|as tu|possedez vous|do you have|have you|are you|etes vous|connaissez vous|maitrisez vous|"
    r"savez vous|can you)\b.*\b(experience|connaissance|knowledge|familiar|familier|maitris\w*|worked|travaille|"
    r"utilise|used|competences?|skills?|proficient|experienced)\b"
)


def skill_yes_no(r: RuleInput) -> Candidate | None:
    if not _SKILL_Q.search(r.text):
        return None
    if find_skill(r.text, profile_vocab(r.profile)):
        return Candidate(("Oui",), 0.85)
    topic = mentioned_topic(r.text)
    if topic and not _GENERIC_TOPIC.search(topic):
        return Candidate(("Non",), 0.75)
    return None
