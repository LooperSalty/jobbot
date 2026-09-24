"""What a post points to: Google files (read with the Google session), job-board offers, other web pages.

- Google Docs / Slides / Sheets → plain-text export, Drive files → download (PDF via pypdf, DOCX, text), both
  through the backend's ``Downloader`` (browser: ``context.request`` shares the logged-in cookies; API: Drive
  API with the OAuth token). Size-capped, never raises.
- Known job boards (HelloWork, LinkedIn, La Bonne Alternance, Indeed, WTTJ, France Travail, APEC, Free-Work)
  are recognised with ``sources.alerts.match_offer_url`` and read through their own source (description +
  apply method), never stored by this module.
- Any other page → httpx + ``html_to_text`` (truncated).
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from jobbot import clock
from jobbot.classroom.dom import google_file, unwrap_google_redirect
from jobbot.classroom.models import Attachment, ClassroomPost, LinkContent
from jobbot.http import make_client, request_with_retry
from jobbot.models import JobOffer, JobStatus
from jobbot.textutil import html_to_text, stable_id, truncate

if TYPE_CHECKING:
    from jobbot.context import AppContext

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 15
MAX_ATTACHMENTS = 6
MAX_ATTACHMENT_CHARS = 6000
MAX_LINKS = 6
MAX_PAGE_CHARS = 3500
MAX_BOARD_CHARS = 5000
PAGE_TIMEOUT_S = 15.0
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.I)
_SKIP_FETCH = re.compile(r"docs\.google\.com/forms/|forms\.gle/|meet\.google\.com|youtube\.com|youtu\.be|"
                         r"classroom\.google\.com|accounts\.google\.com")
EXPORT_URLS = {
    "doc": "https://docs.google.com/document/d/{id}/export?format=txt",
    "slides": "https://docs.google.com/presentation/d/{id}/export/txt",
    "sheet": "https://docs.google.com/spreadsheets/d/{id}/export?format=csv",
    "drive": "https://drive.google.com/uc?export=download&id={id}",
}


class Downloader(Protocol):
    def download(self, attachment: Attachment) -> tuple[str, bytes] | None:
        """(content type, bytes) of the attachment, or None when unreadable."""
        ...


def google_export_url(attachment: Attachment) -> str | None:
    template = EXPORT_URLS.get(attachment.kind)
    return template.format(id=attachment.file_id) if template and attachment.file_id else None


# --------------------------------------------------------------------------- bytes → text


def pdf_bytes_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES]).strip()
    except Exception as exc:  # pypdf raises many types on broken files
        log.info("PDF joint illisible : %s", exc)
        return ""


def docx_bytes_text(data: bytes) -> str:
    try:
        from docx import Document

        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs if p.text.strip())
    except Exception as exc:  # not a docx / corrupted
        log.info("Document Word joint illisible : %s", exc)
        return ""


def bytes_to_text(content_type: str, data: bytes) -> str:
    kind = (content_type or "").lower()
    if "pdf" in kind or data[:5] == b"%PDF-":
        return pdf_bytes_text(data)
    if "wordprocessingml" in kind or (data[:2] == b"PK" and "zip" not in kind and "text" not in kind):
        return docx_bytes_text(data)
    decoded = data.decode("utf-8", errors="replace")
    if "html" in kind:
        return html_to_text(decoded)
    return decoded.strip() if kind.startswith("text/") or "csv" in kind or "json" in kind else ""


def attachment_text(attachment: Attachment, downloader: Downloader) -> str:
    try:
        got = downloader.download(attachment)
    except Exception as exc:  # network / browser errors: the post is still usable
        log.info("Pièce jointe « %s » non lue : %s", attachment.title or attachment.kind, exc)
        return ""
    if got is None or len(got[1]) > MAX_DOWNLOAD_BYTES:
        return ""
    return truncate(bytes_to_text(*got), MAX_ATTACHMENT_CHARS)


def with_attachment_texts(post: ClassroomPost, downloader: Downloader) -> ClassroomPost:
    """Copy of ``post`` whose Google attachments carry their text (first ``MAX_ATTACHMENTS``)."""
    filled = [att.model_copy(update={"text": attachment_text(att, downloader)}) if i < MAX_ATTACHMENTS else att
              for i, att in enumerate(post.attachments)]
    return post.model_copy(update={"attachments": filled})


# --------------------------------------------------------------------------- linked pages & boards


def post_urls(post: ClassroomPost) -> list[str]:
    """Outbound links of the post then URLs written in its text (Google files excluded), deduplicated."""
    found = [*post.links, *(u.rstrip(".,;:!?") for u in _URL_IN_TEXT.findall(post.text))]
    urls: dict[str, None] = {}
    for url in (unwrap_google_redirect(u) for u in found):
        if google_file(url) is None:
            urls.setdefault(url, None)
    return list(urls)


def _board_stub(hit: Any) -> JobOffer:
    """Minimal offer for a recognised board URL (``alerts.match_offer_url`` hit), as an alert e-mail gives it."""
    return JobOffer(
        id=stable_id(hit.source, hit.external_id), source=hit.source, external_id=hit.external_id, title="",
        url=hit.url, found_at=clock.now(), raw={**hit.extra, "needs_details": True},
    )


def board_content(ctx: AppContext, url: str, hit: Any) -> LinkContent:
    """A job-board offer read through its own source (not stored)."""
    from jobbot.sources.registry import fetch_details_only

    stub = _board_stub(hit)
    job = fetch_details_only(ctx, stub) or stub
    if job.status == JobStatus.EXPIRED:
        return LinkContent(url=url, kind="board", title=job.title, text=f"Offre expirée sur {job.source}.", board=job)
    text = "\n".join(x for x in (job.title, job.company, job.location, job.description) if x)
    return LinkContent(url=url, kind="board", title=job.title, text=truncate(text, MAX_BOARD_CHARS), board=job)


def page_content(url: str, client: httpx.Client) -> LinkContent:
    try:
        resp = request_with_retry(client, "GET", url, retries=1)
    except httpx.HTTPError as exc:
        return LinkContent(url=url, kind="error", text=truncate(f"page injoignable : {exc}", 200))
    if resp.status_code >= 400:
        return LinkContent(url=url, kind="error", text=f"page indisponible (HTTP {resp.status_code})")
    text = bytes_to_text(resp.headers.get("content-type", ""), resp.content[:MAX_DOWNLOAD_BYTES])
    return LinkContent(url=url, kind="page", text=truncate(text, MAX_PAGE_CHARS))


def link_contents(ctx: AppContext, post: ClassroomPost, *, client: httpx.Client | None = None,
                  board_reader: Callable[[AppContext, str, Any], LinkContent] = board_content) -> list[LinkContent]:
    """Content of the first ``MAX_LINKS`` links of a post (boards through their source, pages through httpx)."""
    from jobbot.sources.alerts import match_offer_url

    contents: list[LinkContent] = []
    own_client = client is None
    http = client or make_client(timeout=PAGE_TIMEOUT_S)
    try:
        for url in post_urls(post)[:MAX_LINKS]:
            hit = match_offer_url(url)
            if hit is not None:
                contents.append(board_reader(ctx, url, hit))
            elif not _SKIP_FETCH.search(url):
                contents.append(page_content(url, http))
    finally:
        if own_client:
            http.close()
    return contents
