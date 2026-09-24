"""Classroom web page reading: small JS snippets (raw text + links) and pure Python parsers.

Classroom class names are obfuscated and change often, so nothing relies on them: posts are found through
their permalinks (``/c/<course>/p|sa|a|m/<id>``), a post card is the largest ancestor of its permalink that
holds no other post, and author / date / body are read from the text lines (FR + EN). Without any permalink the
stream is split heuristically (largest list of text blocks) and posts get a hash of their text as id.
Material-icon ligatures (``more_vert``…), tab names and the comments block are dropped.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jobbot import clock
from jobbot.classroom.models import (
    CLASSROOM_HOST,
    Attachment,
    ClassroomPost,
    Course,
    hash_id,
    post_id_from_url,
    stream_fingerprint,
)
from jobbot.textutil import strip_accents

_LINKS_JS = """const linksOf = (root) => [...root.querySelectorAll('a[href]')].map(x => ({
    href: x.href, text: (x.innerText || '').trim().slice(0, 200),
    label: x.getAttribute('aria-label') || x.getAttribute('title') || ''}));"""

STREAM_JS = "(maxPosts) => {" + _LINKS_JS + r"""
  const re = /\/c\/([A-Za-z0-9_-]{4,})\/(p|sa|a|m)\/([A-Za-z0-9_-]{4,})/;
  const keyOf = (a) => { const m = (a.getAttribute('href') || '').match(re); return m ? m[2] + '/' + m[3] : null; };
  const firsts = new Map();
  for (const a of document.querySelectorAll('a[href]')) { const k = keyOf(a); if (k && !firsts.has(k)) firsts.set(k, a); }
  const stop = (el) => !el || ['BODY', 'HTML', 'MAIN'].includes(el.tagName) || el.getAttribute('role') === 'main';
  const card = (start) => {
    let node = start;
    while (!stop(node.parentElement)) {
      const keys = new Set([...node.parentElement.querySelectorAll('a[href]')].map(keyOf).filter(Boolean));
      if (keys.size > 1) break;
      node = node.parentElement;
    }
    return node;
  };
  const out = [];
  for (const a of firsts.values()) {
    if (out.length >= maxPosts) break;
    const c = card(a);
    out.push({href: a.href, text: (c.innerText || '').slice(0, 8000), links: linksOf(c)});
  }
  return out;
}"""

FALLBACK_JS = "(maxPosts) => {" + _LINKS_JS + r"""
  const root = document.querySelector('[role="main"]') || document.querySelector('main') || document.body;
  const texty = (el) => [...el.children].filter(k => (k.innerText || '').trim().length >= 40);
  let best = null, bestCount = 1;
  for (const el of root.querySelectorAll('*')) {
    if (el.children.length < 2) continue;
    const n = texty(el).length;
    if (n > bestCount) { best = el; bestCount = n; }
  }
  if (!best) return [];
  return texty(best).slice(0, maxPosts).map(k => ({href: '', text: k.innerText.slice(0, 8000), links: linksOf(k)}));
}"""

POST_JS = "() => {" + _LINKS_JS + r"""
  const root = document.querySelector('[role="main"]') || document.querySelector('main') || document.body;
  return {text: (root.innerText || '').slice(0, 40000), links: linksOf(root)};
}"""

READY_JS = r"""() => location.hostname !== 'classroom.google.com'
  || /\/c\/[A-Za-z0-9_-]{4,}\/(p|sa|a|m)\//.test([...document.querySelectorAll('a[href]')].map(a => a.href).join(' '))
  || ((document.querySelector('[role="main"]') || document.body).innerText || '').length > 400"""

SCROLL_JS = r"""() => {
  window.scrollTo(0, document.body.scrollHeight);
  for (const el of document.querySelectorAll('*')) {
    if (el.scrollHeight > el.clientHeight + 200 && /(auto|scroll)/.test(getComputedStyle(el).overflowY)) {
      el.scrollTop = el.scrollHeight;
    }
  }
}"""

SEE_MORE_RE = re.compile(r"^\s*(voir plus|afficher plus|lire la suite|see more|show more|read more)\s*$", re.I)
SEE_MORE_SELECTOR = "button, [role=button], a"

_LOGIN_TEXT_RE = re.compile(
    r"choisissez un compte|choose an account|utiliser votre compte google|use your google account|"
    r"connectez-vous|se connecter|sign in to|sign in with|connexion a google|to continue to classroom"
)
_NO_ACCESS_RE = re.compile(
    r"vous n'avez pas acces|you don't have access|impossible de trouver (?:ce|le) cours|class not found|"
    r"cours introuvable|demander l'acces|request access|ce cours a ete archive|this class (?:was|has been) archived"
)
_NAV_LINES = frozenset({
    "flux", "stream", "travaux et devoirs", "classwork", "personnes", "people", "notes", "grades", "accueil",
    "home", "agenda", "calendar", "a faire", "to-do", "to do", "inscrit", "enrolled", "google classroom",
    "classroom", "voir plus", "afficher plus", "see more", "show more", "afficher moins", "voir moins",
    "show less", "see less", "copier le lien", "copy link", "options", "partager", "share",
})
_ICON_RE = re.compile(r"^[a-z]+(?:_[a-z]+)+$|^(?:more_vert|people|assignment|class|announcement|description|"
                      r"attach_file|comment|send|close|menu|apps|add|settings|info|person|event|public|lock|"
                      r"link|book|drive_file|insert_drive_file|picture_as_pdf|open_in_new|youtube_activity)$")
_COMMENTS_RE = re.compile(
    r"^(?:\d+\s+)?(?:commentaires? (?:ajoutes? (?:au|a la) (?:cours|classe)|de la classe|du cours|prives?)|"
    r"class comments?|private comments?|ajouter un commentaire|add (?:a )?(?:class |private )?comment)"
)
_MONTHS = (("janv", 1), ("jan", 1), ("fevr", 2), ("fev", 2), ("feb", 2), ("mars", 3), ("mar", 3), ("avr", 4),
           ("apr", 4), ("mai", 5), ("may", 5), ("juin", 6), ("jun", 6), ("juil", 7), ("jul", 7), ("aout", 8),
           ("aug", 8), ("sept", 9), ("sep", 9), ("oct", 10), ("nov", 11), ("dec", 12))
_MONTH_RX = "(?:" + "|".join(m for m, _ in _MONTHS) + ")"
_DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_RX})[a-z]*\.?(?:\s+(\d{{4}}))?")
_MONTH_DAY_RE = re.compile(rf"\b({_MONTH_RX})[a-z]*\.?\s+(\d{{1,2}})\b(?:,?\s+(\d{{4}}))?")
_TIME_RE = re.compile(r"\b(\d{1,2})[:h](\d{2})\b")
_YESTERDAY_RE = re.compile(r"\b(?:hier|yesterday)\b")
_GOOGLE_HOSTS = ("google.com", "google.fr")  # accounts / edu / workspace pages shown instead of the course
_GOOGLE_INTERNAL = (*_GOOGLE_HOSTS, "gstatic.com", "googleusercontent.com", "youtube.com", "youtu.be",
                    "googleapis.com", "withgoogle.com")
_FILE_PATTERNS = (
    ("doc", re.compile(r"docs\.google\.com/document/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})")),
    ("slides", re.compile(r"docs\.google\.com/presentation/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})")),
    ("sheet", re.compile(r"docs\.google\.com/spreadsheets/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})")),
    ("drive", re.compile(r"drive\.google\.com/(?:u/\d+/)?file/d/([A-Za-z0-9_-]{10,})")),
    ("drive", re.compile(r"drive\.google\.com/(?:open|uc)\?(?:.*&)?id=([A-Za-z0-9_-]{10,})")),
)
_FORM_RE = re.compile(r"docs\.google\.com/forms/|forms\.gle/")


def fold(text: str) -> str:
    return " ".join(strip_accents(text or "").lower().replace("’", "'").split())


# --------------------------------------------------------------------------- page state


def login_wall(url: str, text: str, has_posts: bool = False) -> bool:
    """Google shows a sign-in / account chooser / marketing page instead of the course."""
    host = (urlsplit(url or "").hostname or "").lower()
    if host and host != CLASSROOM_HOST and host.endswith(_GOOGLE_HOSTS):
        return True
    return not has_posts and bool(_LOGIN_TEXT_RE.search(fold((text or "")[:4000])))


def no_access(text: str) -> bool:
    return bool(_NO_ACCESS_RE.search(fold((text or "")[:4000])))


# --------------------------------------------------------------------------- links


def unwrap_google_redirect(url: str) -> str:
    """'https://www.google.com/url?q=<target>&sa=D' → '<target>'."""
    parts = urlsplit(url or "")
    if (parts.hostname or "").endswith(_GOOGLE_HOSTS) and parts.path == "/url":
        query = parse_qs(parts.query)
        target = (query.get("q") or query.get("url") or [""])[0]
        if target.startswith(("http://", "https://")):
            return target
    return url


def google_file(url: str) -> tuple[str, str] | None:
    """(kind, file id) of a Google Docs / Slides / Sheets / Drive file link, else None."""
    return next(((kind, m.group(1)) for kind, rx in _FILE_PATTERNS if (m := rx.search(url or ""))), None)


def _is_internal(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _GOOGLE_INTERNAL)


def classify_links(raw_links: list[dict[str, Any]]) -> tuple[list[str], list[Attachment]]:
    """(outbound links, Google file attachments) from the ``{href, text, label}`` list of a post."""
    links: dict[str, None] = {}
    files: dict[str, Attachment] = {}
    for item in raw_links or []:
        url = unwrap_google_redirect(str(item.get("href") or "").strip())
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            continue
        found = None if _FORM_RE.search(url) else google_file(url)
        if found is not None:
            title = " ".join(str(item.get("label") or item.get("text") or "").split())[:200]
            files.setdefault(found[1], Attachment(title=title, url=url, kind=found[0], file_id=found[1]))
        elif _FORM_RE.search(url) or not _is_internal((parts.hostname or "").lower()):
            links.setdefault(url, None)
    return list(links), list(files.values())


# --------------------------------------------------------------------------- text


def _clean_lines(text: str) -> list[str]:
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    kept: list[str] = []
    for line in (line for line in lines if line):
        folded = fold(line)
        if _COMMENTS_RE.match(folded):
            break
        if folded in _NAV_LINES or _ICON_RE.match(line):
            continue
        kept.append(line)
    return kept


def is_date_line(line: str) -> bool:
    folded = fold(line)
    if len(folded) > 60 or len(folded.split()) > 8:
        return False
    return bool(_DAY_MONTH_RE.search(folded) or _MONTH_DAY_RE.search(folded) or _YESTERDAY_RE.search(folded)
                or _TIME_RE.fullmatch(folded.split("(")[0].strip()))


def _split_header(line: str) -> tuple[str, str]:
    """'Mme X 12 sept. more_vert' → ('Mme X', '12 sept.'); a bare date line → ('', date)."""
    folded = fold(line)
    starts = [m.start() for rx in (_DAY_MONTH_RE, _MONTH_DAY_RE, _YESTERDAY_RE, _TIME_RE) if (m := rx.search(folded))]
    start = min(starts) if starts and len(folded) == len(line) else 0
    date_text = " ".join(word for word in line[start:].split() if not _ICON_RE.match(word))
    return line[:start].strip(" ·•-,"), date_text


def parse_post_text(text: str) -> tuple[str, str, str]:
    """(author, date text, body) from the text of a post card / post page."""
    lines = _clean_lines(text)
    idx = next((i for i, line in enumerate(lines[:8]) if is_date_line(line)), None)
    if idx is None:
        return "", "", "\n".join(lines)
    author, date_text = _split_header(lines[idx])
    previous = lines[idx - 1] if idx >= 1 else ""
    if not author and previous and len(previous) <= 80 and "http" not in previous:
        author = previous
    return author, date_text, "\n".join(lines[idx + 1:])


def _month(token: str) -> int:
    return next(num for name, num in _MONTHS if token.startswith(name))


def parse_date_text(text: str, now: datetime | None = None) -> datetime | None:
    """'12 sept.', 'Sep 12, 2026', 'hier', '09:41' → aware UTC datetime (noon Paris for a bare date)."""
    folded, current = fold(text), clock.to_paris(now or clock.now())
    if _YESTERDAY_RE.search(folded):
        return (current - timedelta(days=1)).astimezone(clock.PARIS)
    match = _DAY_MONTH_RE.search(folded)
    day_month = (int(match.group(1)), _month(match.group(2)), match.group(3)) if match else None
    if day_month is None and (match := _MONTH_DAY_RE.search(folded)):
        day_month = (int(match.group(2)), _month(match.group(1)), match.group(3))
    if day_month is None:
        return current if _TIME_RE.search(folded) else None
    day, month, year = day_month
    try:
        when = current.replace(year=int(year) if year else current.year, month=month, day=day, hour=12, minute=0,
                               second=0, microsecond=0)
    except ValueError:
        return None
    if not year and when > current + timedelta(days=1):
        when = when.replace(year=when.year - 1)
    return when


# --------------------------------------------------------------------------- posts


def card_ref(card: dict[str, Any], course: Course) -> tuple[str, str, str]:
    """(post id, permalink or course URL, stream fingerprint) of a stream card."""
    href = str(card.get("href") or "")
    text = str(card.get("text") or "")
    post_id = post_id_from_url(href) or hash_id(text)
    return post_id, href if post_id_from_url(href) else course.url, stream_fingerprint(text)


def build_post(course: Course, card: dict[str, Any], page: dict[str, Any] | None = None,
               now: datetime | None = None) -> ClassroomPost:
    """Post from its stream card, completed by its own page when it was opened."""
    post_id, url, fingerprint = card_ref(card, course)
    source = page if page and str(page.get("text") or "").strip() else card
    author, date_text, body = parse_post_text(str(source.get("text") or ""))
    if not author and source is not card:
        author, date_text, _ = parse_post_text(str(card.get("text") or ""))
    links, attachments = classify_links([*(source.get("links") or []), *(card.get("links") or [])])
    links = [link for link in links if post_id_from_url(link) is None and link != course.url]
    return ClassroomPost(
        post_id=post_id, course_id=course.course_id, url=url, author=author, date_text=date_text,
        published_at=parse_date_text(date_text, now) if date_text else None, text=body.strip(), links=links,
        attachments=attachments, stream_hash=fingerprint,
    )
