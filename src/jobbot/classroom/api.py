"""Optional API backend (``classroom.backend: api``): Classroom REST API + Drive API with an OAuth desktop flow.

Setup: a Google Cloud « Desktop app » OAuth client saved as ``config/google_oauth_client.json`` (gitignored),
``pip install google-auth-oauthlib`` (imported lazily), then ``jobbot login classroom`` (consent in the
browser → token in ``data/classroom_token.json``, gitignored). Reads ``courses.announcements.list`` and
``courses.courseWorkMaterials.list`` (text, ``alternateLink``, materials: driveFile / link / form); Drive files
are exported (Docs → text) or downloaded with the token.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from jobbot import clock
from jobbot.classroom.content import MAX_DOWNLOAD_BYTES, with_attachment_texts
from jobbot.classroom.dom import classify_links, google_file
from jobbot.classroom.models import (
    Attachment,
    ClassroomPost,
    Course,
    CourseFetch,
    Wants,
    encode_id,
    post_id_from_url,
    stream_fingerprint,
)
from jobbot.http import make_client
from jobbot.textutil import truncate

log = logging.getLogger(__name__)

API_BASE = "https://classroom.googleapis.com/v1"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"
SCOPES = (
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)
CLIENT_FILE = "config/google_oauth_client.json"
TOKEN_FILE = "data/classroom_token.json"
MISSING_DEPENDENCY = ("Le mode API de Classroom demande le paquet google-auth-oauthlib : installe-le dans "
                      "l'environnement de jobbot (python -m pip install \"jobbot[classroom]\" ou "
                      "google-auth-oauthlib), ou repasse classroom.backend à browser.")
EXPORT_MIME = {"doc": "text/plain", "slides": "text/plain", "sheet": "text/csv"}
RESPONSE_KEYS = {"announcements": "announcements", "courseWorkMaterials": "courseWorkMaterial"}  # list responses
TIMEOUT_S = 30.0


class ClassroomAuthError(RuntimeError):
    """No usable OAuth token (French message telling what to do)."""


# --------------------------------------------------------------------------- OAuth


def _google_modules() -> tuple[Any, Any, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise ClassroomAuthError(MISSING_DEPENDENCY) from exc
    return Credentials, Request, InstalledAppFlow


def load_token(ctx: Any) -> str:
    """Valid access token (refreshed and saved when expired), else ClassroomAuthError."""
    credentials_cls, request_cls, _ = _google_modules()
    path = ctx.path(TOKEN_FILE)
    if not path.exists():
        raise ClassroomAuthError("Autorisation Google Classroom absente : lance « jobbot login classroom ».")
    creds = credentials_cls.from_authorized_user_file(str(path), list(SCOPES))
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise ClassroomAuthError("Autorisation Google Classroom expirée : relance « jobbot login classroom ».")
        try:
            creds.refresh(request_cls())
        except Exception as exc:  # google.auth.exceptions.RefreshError and transport errors
            raise ClassroomAuthError(f"Autorisation Google Classroom refusée ({exc}) : relance "
                                     "« jobbot login classroom ».") from exc
        path.write_text(creds.to_json(), encoding="utf-8")
    return str(creds.token)


def authorize(ctx: Any) -> tuple[bool, str]:
    """Interactive OAuth consent (desktop flow, local redirect). (ok, French message)."""
    try:
        _, _, flow_cls = _google_modules()
    except ClassroomAuthError as exc:
        return False, str(exc)
    client = ctx.path(CLIENT_FILE)
    if not client.exists():
        return False, (f"Client OAuth absent : crée un identifiant « Application de bureau » dans Google Cloud "
                       f"(API Classroom et Drive activées) et enregistre son JSON sous {CLIENT_FILE}.")
    try:
        creds = flow_cls.from_client_secrets_file(str(client), list(SCOPES)).run_local_server(port=0)
    except Exception as exc:  # user closed the window, port busy…
        return False, f"Autorisation Google non obtenue : {exc}"
    ctx.path(TOKEN_FILE).write_text(creds.to_json(), encoding="utf-8")
    return True, f"Autorisation Google Classroom enregistrée ({TOKEN_FILE})."


# --------------------------------------------------------------------------- REST → posts


def _materials(item: dict[str, Any]) -> list[dict[str, str]]:
    """Materials as the ``{href, text, label}`` links ``dom.classify_links`` understands."""
    out: list[dict[str, str]] = []
    for material in item.get("materials") or []:
        drive = (material.get("driveFile") or {}).get("driveFile") or {}
        link = material.get("link") or {}
        form = material.get("form") or {}
        if drive.get("id"):
            url = drive.get("alternateLink") or f"https://drive.google.com/file/d/{drive['id']}/view"
            out.append({"href": url, "text": drive.get("title") or "", "label": ""})
        elif link.get("url"):
            out.append({"href": link["url"], "text": link.get("title") or "", "label": ""})
        elif form.get("formUrl"):
            out.append({"href": form["formUrl"], "text": form.get("title") or "", "label": ""})
    return out


def item_to_post(item: dict[str, Any], course: Course, kind: str) -> ClassroomPost:
    """An announcement (kind "p") or a course-work material (kind "m") → ClassroomPost."""
    text = item.get("text") if kind == "p" else "\n".join(x for x in (item.get("title"), item.get("description")) if x)
    link = item.get("alternateLink") or f"{course.url}/{kind}/{encode_id(str(item.get('id') or ''))}"
    links, attachments = classify_links(_materials(item))
    stamp = item.get("updateTime") or item.get("creationTime")
    published: datetime | None = clock.parse_dt(item.get("creationTime")) if item.get("creationTime") else None
    return ClassroomPost(
        post_id=post_id_from_url(link) or f"{kind}-{item.get('id')}", course_id=course.course_id, url=link,
        date_text=str(stamp or ""), published_at=published, text=str(text or "").strip(), links=links,
        attachments=attachments, stream_hash=stream_fingerprint(f"{text}\n{stamp}"),
    )


class ApiDownloader:
    """Drive API: Google Docs exported as text, other files downloaded (``alt=media``)."""

    def __init__(self, client: httpx.Client, token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    def download(self, attachment: Attachment) -> tuple[str, bytes] | None:
        found = google_file(attachment.url)
        if found is None:
            return None
        kind, file_id = found
        if kind in EXPORT_MIME:
            url, params = f"{DRIVE_BASE}/files/{file_id}/export", {"mimeType": EXPORT_MIME[kind]}
        else:
            url, params = f"{DRIVE_BASE}/files/{file_id}", {"alt": "media"}
        resp = self.client.get(url, params=params, headers=self.headers)
        if resp.status_code != 200 or len(resp.content) > MAX_DOWNLOAD_BYTES:
            return None
        return resp.headers.get("content-type", ""), resp.content


class ApiBackend:
    name = "api"

    def __init__(self, token_provider: Callable[[Any], str] = load_token, client: httpx.Client | None = None):
        self.token_provider = token_provider
        self.client = client

    def fetch(self, ctx: Any, courses: list[Course], wants: Wants) -> list[CourseFetch]:
        try:
            token = self.token_provider(ctx)
        except ClassroomAuthError as exc:
            log.warning("%s", exc)
            return [CourseFetch(course=c, login_required=True, error=str(exc)) for c in courses]
        client = self.client or make_client(timeout=TIMEOUT_S)
        try:
            return [self._course(ctx, client, token, course, wants) for course in courses]
        finally:
            if self.client is None:
                client.close()

    def _course(self, ctx: Any, client: httpx.Client, token: str, course: Course, wants: Wants) -> CourseFetch:
        limit = ctx.settings.classroom.max_posts
        try:
            items = [*((i, "p") for i in self._list(client, token, course, "announcements", limit)),
                     *((i, "m") for i in self._list(client, token, course, "courseWorkMaterials", limit))]
        except ClassroomAuthError as exc:
            return CourseFetch(course=course, login_required=True, error=str(exc))
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return CourseFetch(course=course, error=truncate(f"API Classroom : {exc}", 200))
        items.sort(key=lambda pair: str(pair[0].get("updateTime") or pair[0].get("creationTime") or ""),
                   reverse=True)
        posts = [item_to_post(item, course, kind) for item, kind in items[:limit]]
        downloader = ApiDownloader(client, token)
        wanted = [with_attachment_texts(p, downloader) for p in posts
                  if wants(course.course_id, p.post_id, p.stream_hash)]
        return CourseFetch(course=course, posts=wanted, seen_in_stream=len(posts))

    def _list(self, client: httpx.Client, token: str, course: Course, resource: str,
              limit: int) -> list[dict[str, Any]]:
        resp = client.get(f"{API_BASE}/courses/{course.course_id}/{resource}",
                          params={"pageSize": str(min(limit, 100)), "orderBy": "updateTime desc"},
                          headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 401:
            raise ClassroomAuthError("Autorisation Google Classroom refusée : relance « jobbot login classroom ».")
        if resp.status_code in (403, 404):
            raise RuntimeError(f"accès refusé au cours ou API non activée (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return list(resp.json().get(RESPONSE_KEYS[resource]) or [])
