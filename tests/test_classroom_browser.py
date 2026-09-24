"""Classroom browser backend: real headless Chromium against fictitious Classroom pages served on the real URLs
with ``context.route`` (everything else aborted: no network), plus the backend logic with fakes."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from jobbot.classroom import browser as cb
from jobbot.classroom.models import Attachment, CourseFetch, parse_course_url

from .fixtures.classroom.fakes import COURSE_URL, with_classroom

FIX = Path(__file__).parent / "fixtures" / "classroom"
COURSE = parse_course_url(COURSE_URL)
DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWx"
REMINDER_PAGE = ("<div role='main'><div>M. Paul Exemple</div><div>10 sept.</div>"
                 "<div>Rappel : réunion d'information sur les soutenances jeudi à 14h en salle 12.</div></div>")
OLD_PAGE = ("<div role='main'><div>Mme Claire Exemple</div><div>2 sept.</div><div>Ancienne publication : offre de "
            "stage chez Gamma Web, voir le lien.</div></div>")


class FakeDownloader:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.asked: list[str] = []

    def download(self, attachment: Attachment):
        self.asked.append(attachment.file_id or "")
        data = self.files.get(attachment.file_id or "")
        return ("text/plain", data) if data is not None else None


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        b = playwright.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def bctx(chromium):
    context = chromium.new_context(locale="fr-FR", viewport={"width": 1280, "height": 720})
    context.set_default_timeout(8000)
    context.route("**/*", lambda route: route.abort())
    yield context
    context.close()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    for name, value in (("STREAM_WAIT_MS", 3000), ("SCROLL_PAUSE_MS", 250), ("POST_SETTLE_MS", 50),
                        ("BETWEEN_POSTS_MS", 0)):
        monkeypatch.setattr(cb, name, value)


def serve(context, pattern: str, *, file: str | None = None, body: str = "", status: int = 200,
          headers: dict[str, str] | None = None, hits: list[str] | None = None) -> None:
    html = (FIX / file).read_text(encoding="utf-8") if file else body

    def handler(route):
        if hits is not None:
            hits.append(route.request.url)
        route.fulfill(status=status, body=html, headers=headers or {}, content_type="text/html; charset=utf-8")

    context.route(re.compile(pattern), handler)


def _serve_course(context, hits=None) -> None:
    serve(context, r"/c/MTIzNDU2Nzg5MDEy$", file="stream.html")
    serve(context, r"/p/NTU1MDAwMTExMjIy$", file="post_offers.html", hits=hits)
    serve(context, r"/p/NTU1MDAwMTExMjMz$", body=REMINDER_PAGE, hits=hits)
    serve(context, r"/p/NTU1MDAwMTExMjQ0$", body=OLD_PAGE, hits=hits)


# --------------------------------------------------------------------------- real browser


@pytest.mark.browser
def test_stream_scroll_permalinks_see_more_and_attachments(bctx):
    _serve_course(bctx)
    downloader = FakeDownloader({DOC_ID: "Fiche de poste : React, Node.js, TypeScript".encode()})
    fetch = cb.fetch_course(bctx, COURSE, lambda cid, pid, fp: True, max_posts=3, downloader=downloader)
    assert fetch.error is None and not fetch.login_required and fetch.seen_in_stream == 3  # 3rd after scrolling
    assert [p.post_id for p in fetch.posts] == ["p-555000111222", "p-555000111233", "p-555000111244"]
    offer = fetch.posts[0]
    assert offer.author == "Mme Claire Exemple" and offer.date_text == "12 sept. (Modifié : 13 sept.)"
    assert "recrutement@acme-logiciels.fr" in offer.text and "Voir plus" not in offer.text  # expanded
    assert "merci" not in offer.text  # comments dropped
    assert offer.links == ["https://www.acme-logiciels.example/carrieres"]
    assert [(a.kind, a.file_id) for a in offer.attachments] == [("doc", DOC_ID)]
    assert offer.attachments[0].text.startswith("Fiche de poste") and offer.published_at is not None
    assert fetch.posts[1].text.startswith("Rappel") and fetch.posts[1].author == "M. Paul Exemple"
    assert fetch.posts[2].links == ["https://jobs.gamma-web.example/stage-dev"]  # from its stream card
    assert all(p.stream_hash for p in fetch.posts)


@pytest.mark.browser
def test_only_wanted_posts_are_opened(bctx):
    hits: list[str] = []
    _serve_course(bctx, hits)
    wanted = cb.fetch_course(bctx, COURSE, lambda cid, pid, fp: pid != "p-555000111233", max_posts=2,
                             downloader=FakeDownloader({}))
    assert [p.post_id for p in wanted.posts] == ["p-555000111222"]
    assert len(hits) == 1 and hits[0].endswith("/p/NTU1MDAwMTExMjIy")


@pytest.mark.browser
def test_login_wall_and_no_access(bctx):
    # Playwright does not route the target of a fulfilled redirect: the account chooser is served on the course
    # URL itself (the accounts.google.com host check is covered by test_classroom_core).
    serve(bctx, r"/c/MTIzNDU2Nzg5MDEy$", file="account_chooser.html")
    assert cb.fetch_course(bctx, COURSE, lambda *a: True, max_posts=5, downloader=FakeDownloader({})).login_required
    bctx.unroute("**/*")
    bctx.route("**/*", lambda route: route.abort())
    serve(bctx, r"/c/MTIzNDU2Nzg5MDEy$", body="<div role='main'>Vous n'avez pas accès à ce cours.</div>")
    fetch = cb.fetch_course(bctx, COURSE, lambda *a: True, max_posts=5, downloader=FakeDownloader({}))
    assert not fetch.login_required and "inaccessible" in fetch.error


@pytest.mark.browser
def test_fallback_without_permalinks(bctx):
    serve(bctx, r"/c/MTIzNDU2Nzg5MDEy$", file="stream_nolinks.html")
    fetch = cb.fetch_course(bctx, COURSE, lambda *a: True, max_posts=5, downloader=FakeDownloader({}))
    assert len(fetch.posts) == 2 and all(p.post_id.startswith("h-") and p.url == COURSE_URL for p in fetch.posts)
    first = fetch.posts[0]
    assert (first.author, first.date_text) == ("Mme Claire Exemple", "11 sept.")
    assert "jobs@delta-data.example" in first.text


@pytest.mark.browser
def test_unreachable_page_is_reported(bctx):
    fetch = cb.fetch_course(bctx, COURSE, lambda *a: True, max_posts=5, downloader=FakeDownloader({}))
    assert fetch.error and "lecture du flux impossible" in fetch.error


# --------------------------------------------------------------------------- backend logic (no browser)


def test_backend_retries_headful_once_when_google_asks_to_log_in(ctx, monkeypatch):
    opened: list[bool] = []

    @contextmanager
    def opener(c, *, headless):
        opened.append(headless)
        yield f"context-{headless}"

    def fake_fetch(context, course, wants, *, max_posts, downloader):
        return CourseFetch(course=course, login_required=context == "context-True")

    monkeypatch.setattr(cb, "fetch_course", fake_fetch)
    monkeypatch.setattr(cb, "display_available", lambda: True)
    backend = cb.BrowserBackend(opener=opener, downloader_factory=lambda context: None)
    results = backend.fetch(with_classroom(ctx), [COURSE], lambda *a: True)
    assert opened == [True, False] and not results[0].login_required
    opened.clear()
    backend.fetch(with_classroom(ctx, headless=False), [COURSE], lambda *a: True)
    assert opened == [False]

    @contextmanager
    def broken(c, *, headless):
        raise RuntimeError("profil déjà utilisé")
        yield  # pragma: no cover

    failed = cb.BrowserBackend(opener=broken).fetch(with_classroom(ctx), [COURSE], lambda *a: True)
    assert "navigateur indisponible : profil déjà utilisé" in failed[0].error


def test_headless_server_never_loses_the_login_signal(ctx, monkeypatch):
    """Linux server without screen: no headful retry; a retry that cannot open a window keeps login_required."""
    opened: list[bool] = []

    @contextmanager
    def opener(c, *, headless):
        opened.append(headless)
        if not headless:
            raise RuntimeError("Missing X server or $DISPLAY")
        yield "headless-context"

    monkeypatch.setattr(cb, "fetch_course", lambda context, course, wants, **kw: CourseFetch(
        course=course, login_required=True))
    backend = cb.BrowserBackend(opener=opener, downloader_factory=lambda context: None)
    monkeypatch.setattr(cb, "display_available", lambda: False)
    assert backend.fetch(with_classroom(ctx), [COURSE], lambda *a: True)[0].login_required and opened == [True]
    monkeypatch.setattr(cb, "display_available", lambda: True)
    opened.clear()
    result = backend.fetch(with_classroom(ctx), [COURSE], lambda *a: True)[0]
    assert opened == [True, False] and result.login_required and result.error is None


def test_display_detection(monkeypatch):
    monkeypatch.setattr(cb.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not cb.display_available()
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert cb.display_available()
    monkeypatch.setattr(cb.sys, "platform", "win32")
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert cb.display_available()


def test_context_downloader_uses_the_google_export_urls():
    calls: list[str] = []

    def get(url, **kwargs):
        calls.append(url)
        if "presentation" in url:
            return SimpleNamespace(status=200, url="https://accounts.google.com/signin", headers={}, body=lambda: b"")
        size = "99999999" if "spreadsheets" in url else "12"
        return SimpleNamespace(status=200, url=url, headers={"content-type": "text/plain", "content-length": size},
                               body=lambda: b"Missions : dev")

    downloader = cb.ContextDownloader(SimpleNamespace(request=SimpleNamespace(get=get)))
    doc = Attachment(url="u", kind="doc", file_id="1Doc")
    assert downloader.download(doc) == ("text/plain", b"Missions : dev")
    assert calls[-1] == "https://docs.google.com/document/d/1Doc/export?format=txt"
    assert downloader.download(Attachment(url="u", kind="slides", file_id="1Pres")) is None  # login redirect
    assert downloader.download(Attachment(url="u", kind="sheet", file_id="1Big")) is None  # too large
    assert downloader.download(Attachment(url="u", kind="other")) is None
    assert downloader.download(Attachment(url="u", kind="drive", file_id="1Pdf")) is not None
    assert calls[-1] == "https://drive.google.com/uc?export=download&id=1Pdf"


class FakeLoginPage:
    def __init__(self, url: str, cookies: list[dict[str, str]]):
        self.url = url
        self.context = SimpleNamespace(cookies=lambda url: cookies)

    def goto(self, url, **kwargs):
        return None

    def is_closed(self) -> bool:
        return False


def test_login_classroom_uses_the_separate_profile_and_google_session(ctx, monkeypatch):
    import jobbot.apply.browser as shared

    page = FakeLoginPage("https://classroom.google.com/h", [{"name": "SID", "value": "x"}])
    seen = {}
    helper_calls: list[bool] = []
    real_helper = cb.open_classroom_browser

    @contextmanager
    def fake_open(c, *, headless=None, profile_dir=None, launch_options=None):
        seen.update(headless=headless, profile_dir=profile_dir, launch_options=launch_options)
        yield SimpleNamespace(pages=[page])

    def spy_helper(c, *, headless):
        helper_calls.append(headless)
        return real_helper(c, headless=headless)

    monkeypatch.setattr(shared, "open_browser", fake_open)
    monkeypatch.setattr(cb, "open_classroom_browser", spy_helper)
    assert shared.login(ctx, "classroom", timeout_s=5)
    assert helper_calls == [False]  # the single Classroom context helper, also used by the watcher
    assert seen == {"headless": False, "profile_dir": "data/browser-profile-classroom",
                    "launch_options": shared.GOOGLE_LAUNCH}
    assert not shared.is_logged_in(FakeLoginPage("https://accounts.google.com/signin", [{"name": "SID", "value": "x"}]),
                                   "classroom")
    assert not shared.is_logged_in(FakeLoginPage("https://classroom.google.com/h", []), "classroom")
    shared.login(ctx, "linkedin", timeout_s=0)
    assert seen["profile_dir"] is None and seen["launch_options"] is None


def test_open_classroom_browser_uses_the_separate_profile(ctx, monkeypatch):
    import jobbot.apply.browser as shared

    seen = {}

    @contextmanager
    def fake_open(c, *, headless=None, profile_dir=None, launch_options=None):
        seen.update(headless=headless, profile_dir=profile_dir, launch_options=launch_options)
        yield "context"

    monkeypatch.setattr(shared, "open_browser", fake_open)
    with cb.open_classroom_browser(ctx, headless=True) as context:
        assert context == "context"
    assert seen["profile_dir"] == "data/browser-profile-classroom" != ctx.settings.paths.browser_profile
    assert seen["launch_options"] == shared.GOOGLE_LAUNCH and seen["headless"] is True
