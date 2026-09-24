"""Company website → short French summary (``Company.description``) used to personalise spontaneous e-mails
and letters: 2-3 sentences on the activity, the clients and the technologies when visible (LLM fast model),
falling back to the page's meta description when the LLM is unavailable or answers badly.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from urllib.parse import urlparse

from jobbot.llm import LLMClient, LLMError
from jobbot.textutil import html_to_text, truncate

log = logging.getLogger(__name__)

MAX_SITE_TEXT = 4000
MIN_SUMMARY_CHARS = 40
MAX_SUMMARY_CHARS = 600
MAX_META_CHARS = 400
ABOUT_PAGE = re.compile(r"a-propos|apropos|about|qui-sommes|notre-histoire|presentation|l-agence|societe|"
                        r"entreprise|equipe|team|expertise|services|solutions|savoir-faire", re.I)
_REFUSAL = re.compile(r"je ne (?:peux|dispose)|d[ée]sol[ée]|aucune information|pas d'information|"
                      r"texte (?:fourni )?ne (?:contient|permet)|en tant qu'(?:ia|assistant)", re.I)
SYSTEM = "Tu résumes des sites web d'entreprises de façon factuelle, sobre et concise, en français."
PROMPT = """Voici le texte du site web de l'entreprise « {name} ».
Résume en 2 ou 3 phrases, en français, ce que fait cette entreprise : son activité, le type de clients \
et les technologies utilisées si elles apparaissent. N'invente rien : utilise uniquement ce que dit le texte.
Réponds uniquement par le résumé (pas de titre, pas de guillemets, pas de liste).

Texte du site :
\"\"\"
{text}
\"\"\""""


def meta_description(html: str) -> str | None:
    """``<meta name="description">`` (or ``og:description``) of a page, when it says something."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    for attrs in ({"name": "description"}, {"property": "og:description"}, {"name": "twitter:description"}):
        tag = soup.find("meta", attrs=attrs)
        content = " ".join(str(tag.get("content") or "").split()) if tag is not None else ""
        if len(content) >= 30:
            return truncate(content, MAX_META_CHARS)
    return None


def site_text(pages: Iterable[tuple[str, str]], limit: int = MAX_SITE_TEXT) -> str:
    """Visible text of the home page (first page) and of the about/services pages, truncated."""
    chunks: list[str] = []
    for index, (url, html) in enumerate(pages):
        if index and not ABOUT_PAGE.search(urlparse(url).path):
            continue
        text = html_to_text(html)
        if text:
            chunks.append(text)
        if sum(len(c) for c in chunks) >= limit:
            break
    return "\n".join(chunks)[:limit]


def _clean(answer: str) -> str | None:
    text = " ".join((answer or "").replace("*", " ").split()).strip(" \"'«»“”")
    if not len(text) >= MIN_SUMMARY_CHARS or text[:1] in "{[" or _REFUSAL.search(text):
        return None
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    cut = text[:MAX_SUMMARY_CHARS]
    end = cut.rfind(". ")
    return cut[: end + 1] if end >= MIN_SUMMARY_CHARS else truncate(text, MAX_SUMMARY_CHARS)


def summarize_site(llm: LLMClient | None, company_name: str, text: str, meta: str | None) -> str | None:
    """2-3 French sentences about the company (LLM fast), else the meta description, else None."""
    body = (text or "").strip()
    if llm is not None and len(body) >= 200:
        try:
            answer = llm.complete(PROMPT.format(name=company_name, text=body[:MAX_SITE_TEXT]), system=SYSTEM,
                                  fast=True)
            summary = _clean(answer)
            if summary:
                return summary
            log.info("Résumé du site de %s inutilisable, repli sur la meta description", company_name)
        except LLMError as exc:
            log.info("Résumé du site de %s impossible (%s), repli sur la meta description", company_name, exc)
    return meta or None
