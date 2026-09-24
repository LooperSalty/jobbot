"""Text normalisation helpers shared by matching, dedup and scraping."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^a-z0-9+#./ -]+")
_LEGAL_SUFFIXES = re.compile(
    r"\b(sas|sasu|sarl|eurl|sa|sci|sca|snc|scop|group|groupe|france|holding|inc|ltd|gmbh)\b\.?"
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+'-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,24}")


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Lowercase, strip accents, keep chars meaningful for tech keywords (c#, c++, node.js, ci/cd)."""
    t = strip_accents(text or "").lower().replace("’", "'")
    t = _NON_WORD.sub(" ", t)
    return _WS.sub(" ", t).strip()


def normalize_company(name: str) -> str:
    t = normalize(name)
    t = _LEGAL_SUFFIXES.sub(" ", t)
    return _WS.sub(" ", t).strip()


def dedup_key(company: str, title: str) -> str:
    """Key used to detect the same offer published on several boards."""
    title_n = re.sub(r"\b(h|f|h/f|f/h|m/f|hf|fh)\b", " ", normalize(title))
    title_n = re.sub(r"[()/-]", " ", title_n)
    return f"{normalize_company(company)}|{_WS.sub(' ', title_n).strip()}"


def stable_id(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def html_to_text(html: str) -> str:
    """Cheap HTML → text (keeps line breaks at block elements)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for br in soup.find_all(["br", "p", "li", "div", "h1", "h2", "h3", "h4", "tr"]):
        br.insert_after("\n")
    text = soup.get_text(" ")
    lines = (_WS.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def slugify(text: str, max_len: int = 60) -> str:
    t = re.sub(r"[^a-z0-9]+", "-", strip_accents(text or "").lower()).strip("-")
    return t[:max_len].strip("-") or "x"


def extract_emails(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in EMAIL_RE.findall(text or ""):
        seen.setdefault(m.strip(".").lower(), None)
    return list(seen)
