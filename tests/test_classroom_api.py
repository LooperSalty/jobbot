"""Optional Classroom API backend: REST listing → posts, Drive downloads, OAuth errors (respx, no network)."""

from __future__ import annotations

import builtins
import json

import httpx
import pytest
import respx

from jobbot.classroom import api
from jobbot.classroom.models import Attachment, parse_course_url

from .fixtures.classroom.fakes import COURSE_URL, with_classroom

COURSE = parse_course_url(COURSE_URL)
ANNOUNCEMENTS = f"{api.API_BASE}/courses/123456789012/announcements"
MATERIALS = f"{api.API_BASE}/courses/123456789012/courseWorkMaterials"
DOC_ID = "1AbCdEfGhIjKlMnOpQrSt"
PDF_ID = "1ZyXwVuTsRqPoNmLkJiHg"
ANNOUNCEMENT = {
    "courseId": "123456789012", "id": "555000111222", "text": "Offre d'alternance : Acme Logiciels recrute.",
    "state": "PUBLISHED", "alternateLink": f"{COURSE_URL}/p/NTU1MDAwMTExMjIy",
    "creationTime": "2026-09-12T08:30:00.000Z", "updateTime": "2026-09-13T09:00:00.000Z",
    "materials": [
        {"driveFile": {"driveFile": {"id": DOC_ID, "title": "Fiche de poste",
                                     "alternateLink": f"https://docs.google.com/document/d/{DOC_ID}/edit"},
                       "shareMode": "VIEW"}},
        {"link": {"url": "https://jobs.example.org/42", "title": "Offre"}},
        {"form": {"formUrl": "https://docs.google.com/forms/d/e/1FAIpQLSf/viewform", "title": "Candidature"}},
        {"youtubeVideo": {"id": "x", "alternateLink": "https://www.youtube.com/watch?v=x"}},
    ],
}
MATERIAL = {
    "courseId": "123456789012", "id": "555000111244", "title": "Offres de stage", "description": "Voir le PDF.",
    "alternateLink": f"{COURSE_URL}/m/NTU1MDAwMTExMjQ0/details", "creationTime": "2026-09-01T08:00:00Z",
    "updateTime": "2026-09-01T08:00:00Z",
    "materials": [{"driveFile": {"driveFile": {"id": PDF_ID, "title": "offres.pdf"}}}],
}


def _backend(token="tok"):
    return api.ApiBackend(token_provider=lambda ctx: token, client=httpx.Client())


@respx.mock
def test_api_lists_announcements_and_materials(ctx):
    ctx = with_classroom(ctx, backend="api", max_posts=10)
    listing = respx.get(ANNOUNCEMENTS).mock(return_value=httpx.Response(200, json={"announcements": [ANNOUNCEMENT]}))
    respx.get(MATERIALS).mock(return_value=httpx.Response(200, json={"courseWorkMaterial": [MATERIAL]}))
    export = respx.get(f"{api.DRIVE_BASE}/files/{DOC_ID}/export").mock(
        return_value=httpx.Response(200, text="Missions : API REST", headers={"content-type": "text/plain"}))
    respx.get(f"{api.DRIVE_BASE}/files/{PDF_ID}").mock(return_value=httpx.Response(
        200, content=b"%PDF-broken", headers={"content-type": "application/pdf"}))
    fetch = _backend().fetch(ctx, [COURSE], lambda cid, pid, fp: True)[0]
    assert fetch.seen_in_stream == 2 and not fetch.login_required and fetch.error is None
    first, second = fetch.posts
    assert first.post_id == "p-555000111222" and first.url.endswith("/p/NTU1MDAwMTExMjIy")
    assert first.links == ["https://jobs.example.org/42", "https://docs.google.com/forms/d/e/1FAIpQLSf/viewform"]
    assert first.attachments[0].text == "Missions : API REST" and first.published_at is not None
    assert second.post_id == "m-555000111244" and second.text == "Offres de stage\nVoir le PDF."
    assert second.attachments[0].file_id == PDF_ID and second.attachments[0].text == ""
    request = listing.calls[0].request
    assert request.headers["authorization"] == "Bearer tok" and request.url.params["orderBy"] == "updateTime desc"
    assert export.calls[0].request.url.params["mimeType"] == "text/plain"


@respx.mock
def test_api_respects_wants_and_limits(ctx):
    ctx = with_classroom(ctx, backend="api", max_posts=1)
    respx.get(ANNOUNCEMENTS).mock(return_value=httpx.Response(200, json={"announcements": [ANNOUNCEMENT]}))
    respx.get(MATERIALS).mock(return_value=httpx.Response(200, json={}))
    fetch = _backend().fetch(ctx, [COURSE], lambda cid, pid, fp: False)[0]
    assert fetch.posts == [] and fetch.seen_in_stream == 1


@respx.mock
def test_api_errors(ctx):
    ctx = with_classroom(ctx, backend="api")
    respx.get(ANNOUNCEMENTS).mock(return_value=httpx.Response(401))
    assert _backend().fetch(ctx, [COURSE], lambda *a: True)[0].login_required
    respx.get(ANNOUNCEMENTS).mock(return_value=httpx.Response(403))
    assert "accès refusé" in _backend().fetch(ctx, [COURSE], lambda *a: True)[0].error
    respx.get(ANNOUNCEMENTS).mock(return_value=httpx.Response(500))
    assert "HTTP 500" in _backend().fetch(ctx, [COURSE], lambda *a: True)[0].error

    def no_token(c):
        raise api.ClassroomAuthError("Autorisation Google Classroom absente")

    got = api.ApiBackend(token_provider=no_token).fetch(ctx, [COURSE], lambda *a: True)[0]
    assert got.login_required and "absente" in got.error


@respx.mock
def test_api_downloader():
    client = httpx.Client()
    downloader = api.ApiDownloader(client, "tok")
    respx.get(f"{api.DRIVE_BASE}/files/{DOC_ID}/export").mock(return_value=httpx.Response(404))
    assert downloader.download(Attachment(url=f"https://docs.google.com/document/d/{DOC_ID}/edit", kind="doc")) is None
    assert downloader.download(Attachment(url="https://example.org/x")) is None
    sheet = f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit"
    route = respx.get(f"{api.DRIVE_BASE}/files/{DOC_ID}/export").mock(
        return_value=httpx.Response(200, text="a;b", headers={"content-type": "text/csv"}))
    assert downloader.download(Attachment(url=sheet, kind="sheet")) == ("text/csv", b"a;b")
    assert route.calls[-1].request.url.params["mimeType"] == "text/csv"


def test_missing_dependency_and_token_errors(ctx, monkeypatch):
    real_import = builtins.__import__

    def no_google(name, *args, **kwargs):
        if name.startswith("google"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_google)
    with pytest.raises(api.ClassroomAuthError, match="google-auth-oauthlib"):
        api.load_token(ctx)
    ok, message = api.authorize(ctx)
    assert not ok and "google-auth-oauthlib" in message
    monkeypatch.setattr(builtins, "__import__", real_import)

    class Creds:
        valid, expired, refresh_token, token = False, True, None, "x"

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            return cls()

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            raise AssertionError("client file absent: never reached")

    monkeypatch.setattr(api, "_google_modules", lambda: (Creds, object, Flow))
    with pytest.raises(api.ClassroomAuthError, match="jobbot login classroom"):
        api.load_token(ctx)  # no token file
    ctx.path(api.TOKEN_FILE).write_text(json.dumps({"token": "x"}), encoding="utf-8")
    with pytest.raises(api.ClassroomAuthError, match="expirée"):
        api.load_token(ctx)
    ok, message = api.authorize(ctx)
    assert not ok and api.CLIENT_FILE in message


def test_token_refresh_and_authorize(ctx, monkeypatch):
    class Creds:
        def __init__(self):
            self.valid, self.expired, self.refresh_token, self.token = False, True, "r", "old"

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            return cls()

        def refresh(self, request):
            self.valid, self.token = True, "fresh"

        def to_json(self):
            return json.dumps({"token": self.token})

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            assert "classroom.announcements.readonly" in scopes[0]
            return cls()

        def run_local_server(self, port=0):
            return Creds()

    monkeypatch.setattr(api, "_google_modules", lambda: (Creds, lambda: None, Flow))
    ctx.path(api.TOKEN_FILE).write_text("{}", encoding="utf-8")
    assert api.load_token(ctx) == "fresh" and "fresh" in ctx.path(api.TOKEN_FILE).read_text(encoding="utf-8")
    ctx.path(api.CLIENT_FILE).write_text("{}", encoding="utf-8")
    ok, message = api.authorize(ctx)
    assert ok and api.TOKEN_FILE in message
