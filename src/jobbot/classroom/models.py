"""Classroom data (posts, attachments, fetched links) and identifiers (course / post ids from URLs).

Classroom web URLs carry URL-safe base64 encodings of the numeric ids: ``/c/<course>`` (course),
``/c/<course>/p/<id>`` or ``/sa/<id>`` (announcement), ``/a/<id>/details`` (assignment), ``/m/<id>/details``
(material). Ids are decoded to the numeric form when possible so the browser and API backends agree.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from jobbot.models import JobOffer
from jobbot.textutil import normalize

CLASSROOM_HOST = "classroom.google.com"
_COURSE_PATH_RE = re.compile(r"^(?:/u/\d+)?/c/([A-Za-z0-9_-]{4,})(?:/|$)")
_POST_PATH_RE = re.compile(r"/c/([A-Za-z0-9_-]{4,})/(p|sa|a|m)/([A-Za-z0-9_-]{4,})")
POST_KINDS = {"p": "p", "sa": "p", "a": "a", "m": "m"}  # announcement, assignment, material
_URL_SPLIT_RE = re.compile(r"[\s,;]+")
_DIGITS_RE = re.compile(r"\d+")
_COMMENT_WORDS_RE = re.compile(r"\b(?:commentaires?|comments?|de la classe|class)\b")

Wants = Callable[[str, str, str], bool]  # (course id, post id, stream fingerprint) → read this post?


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Course(Frozen):
    url: str  # normalised course URL (private: never printed)
    key: str  # base64 path segment
    course_id: str  # numeric id when decodable, else the key


class Attachment(Frozen):
    title: str = ""
    url: str
    kind: str = "other"  # doc | slides | sheet | drive | form | other
    file_id: str | None = None
    text: str = ""  # extracted content (filled by the backend, which holds the Google session)


class ClassroomPost(Frozen):
    post_id: str  # "p-<numeric id>" (announcement), "a-…", "m-…", or "h-<hash>" (no permalink found)
    course_id: str
    url: str  # permalink (or the course URL when none)
    author: str = ""
    date_text: str = ""
    published_at: datetime | None = None
    text: str = ""
    links: list[str] = Field(default_factory=list)  # outbound links (Google redirects unwrapped)
    attachments: list[Attachment] = Field(default_factory=list)
    stream_hash: str = ""  # cheap "did the stream card change" fingerprint


class CourseFetch(Frozen):
    course: Course
    posts: list[ClassroomPost] = Field(default_factory=list)
    seen_in_stream: int = 0
    login_required: bool = False
    error: str | None = None


class LinkContent(Frozen):
    url: str
    kind: str = "page"  # board | page | error
    title: str = ""
    text: str = ""
    board: JobOffer | None = None  # full offer read through the board's own source (LinkedIn, HelloWork, LBA…)


# --------------------------------------------------------------------------- identifiers


def decode_id(segment: str) -> str:
    """URL-safe base64 of a numeric id → the numeric id; anything else is returned unchanged."""
    raw = (segment or "").strip().rstrip("=")
    if not raw or raw.isdigit():
        return raw
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("ascii")
    except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors
        return raw
    return decoded if decoded.isdigit() else raw


def encode_id(numeric: str) -> str:
    return base64.urlsafe_b64encode(str(numeric).encode("ascii")).decode("ascii").rstrip("=")


def parse_course_url(url: str) -> Course | None:
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != CLASSROOM_HOST:
        return None
    match = _COURSE_PATH_RE.match(parts.path)
    if not match:
        return None
    key = match.group(1)
    return Course(url=f"https://{CLASSROOM_HOST}/c/{key}", key=key, course_id=decode_id(key))


def parse_course_urls(value: str | None) -> tuple[list[Course], int]:
    """(valid courses, number of invalid entries) from the comma-separated ``CLASSROOM_COURSE_URLS``."""
    courses: dict[str, Course] = {}
    invalid = 0
    for entry in (e for e in _URL_SPLIT_RE.split(value or "") if e):
        course = parse_course_url(entry)
        if course is None:
            invalid += 1
        else:
            courses.setdefault(course.course_id, course)
    return list(courses.values()), invalid


def save_course_urls(root: Path, value: object) -> None:
    """Wizard setter: validated ``CLASSROOM_COURSE_URLS`` written to .env (normalised, never echoed)."""
    from jobbot.config import ConfigError
    from jobbot.preferences import set_secret

    courses, invalid = parse_course_urls(str(value))
    if invalid or not courses:
        raise ConfigError("Lien de cours invalide : attendu https://classroom.google.com/c/… (séparés par des "
                          "virgules)")
    set_secret(root, "CLASSROOM_COURSE_URLS", ",".join(c.url for c in courses))


def post_id_from_url(url: str) -> str | None:
    match = _POST_PATH_RE.search(urlsplit(url or "").path)
    if not match:
        return None
    return f"{POST_KINDS[match.group(2)]}-{decode_id(match.group(3))}"


def hash_id(text: str) -> str:
    return "h-" + hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]


def stream_fingerprint(text: str) -> str:
    """Hash of a stream card without dates / comment counts (they change without the post changing)."""
    flat = _COMMENT_WORDS_RE.sub(" ", _DIGITS_RE.sub(" ", normalize(text)))
    return hashlib.sha1(" ".join(flat.split()).encode("utf-8")).hexdigest()[:16]


def text_hash(post: ClassroomPost) -> str:
    """Fingerprint of what the offers are extracted from (text + attachments)."""
    parts = [normalize(post.text), *sorted(a.url for a in post.attachments), *sorted(post.links)]
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]
