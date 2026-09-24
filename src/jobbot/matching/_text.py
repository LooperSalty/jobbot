"""Text helpers private to the matching package.

``fold`` strips accents while keeping the string length (one output char per input char), so offsets found
in a folded/lowercased copy can be applied to the original text (case-sensitive matching, title cleaning).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_SPACES = "\u00a0\u202f\u2007\u2009\u200a\t\r"
_QUOTES = "\u2019\u2018\u02bc`\u00b4"
_DASHES = "\u2013\u2014\u2010\u2011\u2212"
_TRANSLATE = str.maketrans(
    {**dict.fromkeys(_SPACES, " "), **dict.fromkeys(_QUOTES, "'"), **dict.fromkeys(_DASHES, "-"),
     "\u200b": None, "\ufeff": None}
)
_LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st",
    "\ufb06": "st", "\u0153": "oe", "\u0152": "OE", "\u00e6": "ae", "\u00c6": "AE", "\u00df": "ss",
}
_WS = re.compile(r"[ ]+")


def prep(text: str) -> str:
    """Unify spaces, quotes, dashes and expand ligatures (may change the length)."""
    t = (text or "").translate(_TRANSLATE)
    for lig, rep in _LIGATURES.items():
        if lig in t:
            t = t.replace(lig, rep)
    return t


@lru_cache(maxsize=4096)
def _fold_char(c: str) -> str:
    if c.isascii():
        return c
    base = "".join(ch for ch in unicodedata.normalize("NFKD", c) if not unicodedata.combining(ch))
    return base if len(base) == 1 else " "


def fold(text: str) -> str:
    """Accent-strip, one char per char (same length as ``text``)."""
    return "".join(_fold_char(c) for c in text)


def lower_same_len(text: str) -> str:
    low = text.lower()
    if len(low) == len(text):
        return low
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in text)


def norm(text: str) -> str:
    """Lowercase, accent-free, single-spaced (punctuation kept) — the general comparison form."""
    t = lower_same_len(fold(prep(text))).replace("\n", " ")
    return _WS.sub(" ", t).strip()


def norm_lines(text: str) -> list[str]:
    """``norm`` applied line by line (empty lines dropped)."""
    return [n for n in (norm(line) for line in prep(text).splitlines()) if n]


def words(text: str) -> list[str]:
    """Alphanumeric tokens of a normalized string."""
    return re.findall(r"[a-z0-9][a-z0-9+#.']*[a-z0-9+#]|[a-z0-9]", text)


# --------------------------------------------------------------------------- titles

GENDER_RX = re.compile(
    r"\(\s*(?:h|f|m|x)\s*[/-]\s*(?:h|f|m|x)(?:\s*[/-]\s*(?:x|nb|d|h|f))*\s*\)"
    r"|\b(?:h|f|m)\s*/\s*(?:h|f|m)(?:\s*/\s*(?:x|nb|d))?\b"
    r"|\((?:e|ne|se|euse|trice|eure|le)\)|[·.](?:e|se|euse|trice|ne)\b",
    re.I,
)
TITLE_NOISE_RX = re.compile(
    r"\b(?:en\s+)?(?:alternance|apprentissage|alternants?|alternantes?|apprentie?s?|stages?|stagiaires?|cdi|cdd|"
    r"interim|freelance|junior|confirmee?|senior|work\s*study|apprenticeship|contrat\s+pro(?:fessionnalisation)?|"
    r"contrat\s+de\s+professionnalisation|urgent|h\s*/\s*f|f\s*/\s*h|full\s+remote|teletravail|hybride|remote)\b"
    r"|\b\d+\s*mois\b|\bbac\s*\+\s*\d\b|\b(?:19|20)\d{2}\b",
    re.I,
)
_TITLE_SPLIT = re.compile(r"\s+[-|/:]\s+|\s*[|,;]\s*|\s+-\s*|\s*-\s+")
_UNIFY = (
    (re.compile(r"\bfull\s*-?\s*stack\b"), "full stack"),
    (re.compile(r"\bback\s*-?\s*end\b"), "backend"),
    (re.compile(r"\bfront\s*-?\s*end\b"), "frontend"),
    (re.compile(r"\bdeveloppeu(?:se|rs|ses)\b"), "developpeur"),
    (re.compile(r"\bdevelopper\b"), "developpeur"),
    (re.compile(r"\bdev\b"), "developpeur"),
    (re.compile(r"\bingenieures?\b|\bingenieurs\b"), "ingenieur"),
    (re.compile(r"\bconceptrices?\b|\bconcepteurs\b"), "concepteur"),
    (re.compile(r"\bdevelopers\b"), "developer"),
)

TITLE_FAMILIES: dict[str, tuple[str, ...]] = {
    "fullstack": ("developpeur full stack", "developpeur web full stack", "full stack developer", "full stack engineer",
                  "ingenieur full stack", "concepteur developpeur full stack", "full stack"),
    "frontend": ("developpeur frontend", "frontend developer", "integrateur web", "developpeur react",
                 "developpeur angular", "developpeur vue", "developpeur next", "developpeur javascript",
                 "developpeur typescript"),
    "backend": ("developpeur backend", "backend developer", "developpeur java", "developpeur php", "developpeur python",
                "developpeur node", "developpeur .net", "developpeur c#", "developpeur symfony", "developpeur go"),
    "mobile": ("developpeur mobile", "developpeur flutter", "developpeur android", "developpeur ios",
               "mobile developer"),
    "ia": ("developpeur ia", "ingenieur ia", "developpeur intelligence artificielle", "ai engineer", "ml engineer",
           "ingenieur machine learning", "developpeur llm"),
    "devops": ("ingenieur devops", "devops"),
    "software": ("ingenieur logiciel", "ingenieur developpement", "ingenieur etudes et developpement",
                 "software engineer",
                 "developpeur logiciel", "developpeur d'applications", "concepteur developpeur d'applications",
                 "concepteur developpeur", "analyste programmeur", "analyste developpeur", "developpeur informatique",
                 "developpeur web", "programmeur", "software developer", "developer", "developpeur",
                 "ingenieur etudes"),
}


def unify_title_words(text: str) -> str:
    t = text
    for rx, rep in _UNIFY:
        t = rx.sub(rep, t)
    return t


def title_norm(text: str) -> str:
    """``norm`` + title spelling unification (full-stack → full stack…), punctuation → spaces."""
    t = unify_title_words(norm(text))
    t = re.sub(r"[^a-z0-9+#.' ]+", " ", t)
    t = re.sub(r"\.(?![a-z0-9])", " ", t)
    return _WS.sub(" ", t).strip()


def normalize_title(title: str) -> str:
    """'Alternance DEVELOPPEUR WEB FULLSTACK - Metz (H/F)' → 'developpeur web full stack'."""
    t = lower_same_len(fold(prep(title)))
    t = GENDER_RX.sub(" ", t)
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t)
    t = unify_title_words(t)
    parts = [_clean_title_part(p) for p in _TITLE_SPLIT.split(t)]
    parts = [p for p in parts if p]
    with_family = [p for p in parts if family_of(p)]
    candidates = with_family or sorted(parts, key=len, reverse=True)
    return candidates[0] if candidates else ""


_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)")


def display_title(title: str) -> str:
    """Readable core title with accents kept: 'Développeur Full-Stack React / Node.js en alternance (H/F)'
    → 'Développeur Full-Stack React'."""
    raw = prep(title).replace("\n", " ")
    low = lower_same_len(fold(raw))
    spans = [m.span() for rx in (GENDER_RX, _BRACKETS, TITLE_NOISE_RX) for m in rx.finditer(low)]
    chars_raw, chars_low = list(raw), list(low)
    for start, end in spans:
        chars_raw[start:end] = " " * (end - start)
        chars_low[start:end] = " " * (end - start)
    raw, low = "".join(chars_raw), "".join(chars_low)
    bounds, prev = [], 0
    for sep in _TITLE_SPLIT.finditer(low):
        bounds.append((prev, sep.start()))
        prev = sep.end()
    bounds.append((prev, len(low)))
    parts = [(s, e) for s, e in bounds if low[s:e].strip(" -|/,:;.")]
    if not parts:
        return ""
    family = [(s, e) for s, e in parts if family_of(_clean_title_part(unify_title_words(low[s:e])))]
    start, end = (family or sorted(parts, key=lambda p: len(low[p[0]:p[1]].strip()), reverse=True))[0]
    text = _WS.sub(" ", raw[start:end]).strip(" -|/,:;.")
    return re.sub(r"^(?:en|de|pour)\s+|\s+(?:en|de|pour|et)$", "", text, flags=re.I).strip()


def _clean_title_part(part: str) -> str:
    p = TITLE_NOISE_RX.sub(" ", part)
    p = re.sub(r"[^a-z0-9+#.' ]+", " ", p)
    p = re.sub(r"\.(?![a-z0-9#])", " ", p)
    p = re.sub(r"^(?:en|de|du|d'|pour|a)\s+|\s+(?:en|de|du|pour|a|et)$", " ", p.strip())
    return _WS.sub(" ", p).strip(" '")


def family_of(normalized_title: str) -> str | None:
    """Title family (fullstack, backend, software…) of an already normalized title, else None."""
    padded = f" {normalized_title} "
    for family, keys in TITLE_FAMILIES.items():
        if any(f" {k} " in padded or padded.startswith(f" {k}") for k in keys):
            return family
    return None
