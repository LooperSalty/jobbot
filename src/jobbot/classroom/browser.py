"""Browser backend (default, zero setup): the course stream read in a separate, logged-in Chrome profile.

``jobbot login classroom`` opens classroom.google.com once for a manual Google login (never automated: no
password, no 2FA). Each run opens every course URL headless, waits for the stream, scrolls until
``classroom.max_posts`` posts, and opens the page of each post not seen yet (or whose card changed) to read
its full text, links and attachments (Google files read with ``context.request``, which shares the session).
Google asking to log in while headless → one headful retry when a display exists (never on a server without
screen); still asked → ``login_required`` (the watcher notifies « jobbot login classroom »).

Portable (Windows desktop and headless Linux server): no OS-specific call here. Every Classroom browser
context — watcher and ``jobbot login classroom`` — is created by ``open_classroom_browser`` only, the single
place to extend (e.g. importing a session exported from a desktop browser as a Playwright ``storage_state``).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

from jobbot.classroom import dom
from jobbot.classroom.content import MAX_DOWNLOAD_BYTES, Downloader, google_export_url, with_attachment_texts
from jobbot.classroom.models import Attachment, ClassroomPost, Course, CourseFetch, Wants
from jobbot.textutil import truncate

log = logging.getLogger(__name__)

Opener = Callable[..., AbstractContextManager[BrowserContext]]

NAV_TIMEOUT_MS = 30000
STREAM_WAIT_MS = 20000
SCROLL_PAUSE_MS = 1200
MAX_SCROLLS = 15
POST_SETTLE_MS = 1500
BETWEEN_POSTS_MS = 700
DOWNLOAD_TIMEOUT_MS = 30000
MAX_SEE_MORE = 5


@contextmanager
def open_classroom_browser(ctx: Any, *, headless: bool) -> Iterator[BrowserContext]:
    """THE Classroom browser context: persistent profile ``classroom.browser_profile`` (never the LinkedIn /
    HelloWork one), Google-friendly launch flags. Single creation point, used by the watcher and the login."""
    from jobbot.apply.browser import GOOGLE_LAUNCH, open_browser

    with open_browser(ctx, headless=headless, profile_dir=ctx.settings.classroom.browser_profile,
                      launch_options=GOOGLE_LAUNCH) as context:
        yield context


def display_available() -> bool:
    """A visible window can be opened (always on Windows / macOS; X11 or Wayland display on Linux)."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


class ContextDownloader:
    """Google Docs export / Drive download through the browser context (same cookies as the page)."""

    def __init__(self, context: BrowserContext):
        self.context = context

    def download(self, attachment: Attachment) -> tuple[str, bytes] | None:
        url = google_export_url(attachment)
        if url is None:
            return None
        resp = self.context.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS, max_redirects=5, fail_on_status_code=False)
        if resp.status != 200 or "accounts.google.com" in resp.url:
            return None
        if int(resp.headers.get("content-length") or 0) > MAX_DOWNLOAD_BYTES:
            return None
        return resp.headers.get("content-type", ""), resp.body()


# --------------------------------------------------------------------------- one course


def _wait_for_stream(page: Page) -> None:
    try:
        page.wait_for_function(dom.READY_JS, timeout=STREAM_WAIT_MS)
    except PlaywrightError:
        log.info("Flux Classroom lent à s'afficher : lecture de ce qui est chargé.")


def _stream_cards(page: Page, max_posts: int) -> list[dict[str, Any]]:
    """Post cards of the stream (permalink ones; heuristic blocks when none), newest first."""
    cards = page.evaluate(dom.STREAM_JS, max_posts)
    stalled = 0
    for _ in range(MAX_SCROLLS):
        if len(cards) >= max_posts or stalled >= 2:
            break
        page.evaluate(dom.SCROLL_JS)
        page.wait_for_timeout(SCROLL_PAUSE_MS)
        more = page.evaluate(dom.STREAM_JS, max_posts)
        stalled = stalled + 1 if len(more) <= len(cards) else 0
        cards = more if len(more) > len(cards) else cards
    if not cards:
        log.info("Aucun lien de publication dans le flux : découpage heuristique du flux.")
        cards = page.evaluate(dom.FALLBACK_JS, max_posts)
    return list(cards)[:max_posts]


def _expand(page: Page) -> None:
    """Click « Voir plus » / « See more » so the whole text is in the DOM."""
    try:
        buttons = page.locator(dom.SEE_MORE_SELECTOR).filter(has_text=dom.SEE_MORE_RE)
        for i in range(min(buttons.count(), MAX_SEE_MORE)):
            if buttons.nth(i).is_visible():
                buttons.nth(i).click(timeout=3000)
                page.wait_for_timeout(300)
    except PlaywrightError as exc:
        log.debug("Bouton « Voir plus » non cliqué : %s", exc)


def _open_post(page: Page, url: str) -> dict[str, Any] | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(POST_SETTLE_MS)
        _expand(page)
        return page.evaluate(dom.POST_JS)
    except PlaywrightError as exc:
        log.info("Page de la publication illisible (%s) : texte du flux utilisé.", truncate(str(exc), 120))
        return None


def _read_posts(page: Page, course: Course, cards: list[dict[str, Any]], wants: Wants,
                downloader: Downloader) -> list[ClassroomPost]:
    posts: list[ClassroomPost] = []
    for card in cards:
        post_id, url, fingerprint = dom.card_ref(card, course)
        if not wants(course.course_id, post_id, fingerprint):
            continue
        opened = _open_post(page, url) if url != course.url else None
        post = dom.build_post(course, card, opened)
        posts.append(with_attachment_texts(post, downloader))
        page.wait_for_timeout(BETWEEN_POSTS_MS)
    return posts


def _read_course(page: Page, course: Course, wants: Wants, max_posts: int, downloader: Downloader) -> CourseFetch:
    page.goto(course.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    _wait_for_stream(page)
    page.wait_for_timeout(POST_SETTLE_MS)
    text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 5000)")
    has_posts = bool(page.evaluate(dom.STREAM_JS, 1))
    if dom.login_wall(page.url, text, has_posts):
        return CourseFetch(course=course, login_required=True)
    if not has_posts and dom.no_access(text):
        return CourseFetch(course=course, error="cours inaccessible avec ce compte Google (lien ou compte à vérifier)")
    cards = _stream_cards(page, max_posts)
    posts = _read_posts(page, course, cards, wants, downloader)
    return CourseFetch(course=course, posts=posts, seen_in_stream=len(cards))


def fetch_course(context: BrowserContext, course: Course, wants: Wants, *, max_posts: int,
                 downloader: Downloader | None = None) -> CourseFetch:
    """Read one course stream in ``context`` (never raises: errors are reported in the result)."""
    page = context.new_page()
    try:
        return _read_course(page, course, wants, max_posts, downloader or ContextDownloader(context))
    except PlaywrightError as exc:
        log.warning("Flux Classroom illisible : %s", truncate(str(exc), 200))
        return CourseFetch(course=course, error=truncate(f"lecture du flux impossible : {exc}", 200))
    finally:
        try:
            page.close()
        except PlaywrightError:
            log.debug("Onglet Classroom déjà fermé")


# --------------------------------------------------------------------------- backend


class BrowserBackend:
    name = "browser"

    def __init__(self, opener: Opener = open_classroom_browser,
                 downloader_factory: Callable[[BrowserContext], Downloader] = ContextDownloader):
        self.opener = opener
        self.downloader_factory = downloader_factory

    def fetch(self, ctx: Any, courses: list[Course], wants: Wants) -> list[CourseFetch]:
        cfg = ctx.settings.classroom
        results = self._round(ctx, courses, wants, headless=cfg.headless)
        blocked = [r.course for r in results if r.login_required]
        if not (cfg.headless and blocked):
            return results
        if not display_available():
            log.info("Google demande une connexion et aucun écran n'est disponible : pas de nouvel essai visible.")
            return results
        log.info("Google demande une connexion en mode invisible : nouvel essai avec une fenêtre visible.")
        retried = {r.course.course_id: r for r in self._round(ctx, blocked, wants, headless=False)
                   if r.error is None or r.login_required}  # a failed retry keeps the « reconnecte-toi » signal
        return [retried.get(r.course.course_id, r) for r in results]

    def _round(self, ctx: Any, courses: list[Course], wants: Wants, *, headless: bool) -> list[CourseFetch]:
        try:
            with self.opener(ctx, headless=headless) as context:
                downloader = self.downloader_factory(context)
                return [fetch_course(context, course, wants, max_posts=ctx.settings.classroom.max_posts,
                                     downloader=downloader) for course in courses]
        except Exception as exc:  # Playwright missing, profile already in use, browser crash…
            log.warning("Navigateur Classroom indisponible : %s", truncate(str(exc), 200))
            detail = truncate(f"navigateur indisponible : {exc}", 200)
            return [CourseFetch(course=course, error=detail) for course in courses]
