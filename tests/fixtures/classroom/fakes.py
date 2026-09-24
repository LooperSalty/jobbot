"""Test doubles for the Classroom watcher: a scripted backend, fictitious posts and LLM answers."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from jobbot import clock
from jobbot.classroom.models import ClassroomPost, CourseFetch, stream_fingerprint
from jobbot.context import AppContext

COURSE_URL = "https://classroom.google.com/c/MTIzNDU2Nzg5MDEy"  # fictitious course (base64 of 123456789012)
POST_URL = f"{COURSE_URL}/p/NTU1MDAwMTExMjIy"
LI_URL = "https://www.linkedin.com/jobs/view/4410135372/"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def deadline_in(days: int) -> str:
    return clock.to_paris(clock.now() + timedelta(days=days)).strftime("%d/%m/%Y")


def post_text() -> str:
    return ("Deux offres d'alternance de nos entreprises partenaires.\n"
            "1) Acme Logiciels (Metz) recherche un développeur full-stack React / Node.js / TypeScript en alternance "
            f"(Bac+3 à Bac+5, 24 mois). Candidature à recrutement@acme-logiciels.fr avant le {deadline_in(10)}.\n"
            "2) Beta Services (Nancy) cherche un technicien support en alternance, niveau BTS SIO. Postuler : "
            + LI_URL)


def offers_payload() -> dict[str, Any]:
    return {"is_offer_post": True, "offers": [
        {"title": "Développeur full-stack React / Node.js en alternance", "company": "Acme Logiciels",
         "location": "Metz (57)", "contract_type": "alternance", "target_level": "Bac+3 à Bac+5",
         "description": ("Développement d'applications web React, Node.js, TypeScript et PostgreSQL, Docker, Git. "
                         "Alternance de 24 mois."),
         "skills": ["React", "Node.js", "TypeScript", "PostgreSQL"], "contact_email": "recrutement@acme-logiciels.fr",
         "apply_instructions": "CV + lettre", "deadline": deadline_in(10)},
        {"title": "Technicien support informatique en alternance", "company": "Beta Services",
         "location": "Nancy (54)", "contract_type": "alternance", "target_level": "BTS SIO",
         "description": "Support utilisateurs et parc informatique.", "apply_url": LI_URL},
    ]}


def responder(payload: dict[str, Any] | None = None):
    """FakeLLM responder: the extraction prompt gets ``payload``, everything else '{}' (template fallbacks)."""
    answer = json.dumps(payload or offers_payload())

    def respond(prompt: str, system: str | None, fast: bool) -> str:
        return answer if "is_offer_post" in prompt else "{}"
    return respond


def make_post(**updates: Any) -> ClassroomPost:
    text = updates.pop("text", None) or post_text()
    base = dict(post_id="p-555000111222", course_id="123456789012", url=POST_URL, author="Mme Claire Exemple",
                date_text="12 sept.", published_at=clock.now(), text=text, links=[LI_URL],
                stream_hash=stream_fingerprint(text))
    return ClassroomPost(**{**base, **updates})


def with_classroom(ctx: AppContext, *, urls: str | None = COURSE_URL, **classroom: Any) -> AppContext:
    secrets = ctx.secrets.model_copy(update={"classroom_course_urls": urls})
    settings = ctx.settings.model_copy(update={"classroom": ctx.settings.classroom.model_copy(update=classroom)})
    return dataclasses.replace(ctx, secrets=secrets, settings=settings)


class FakeBackend:
    """Returns the scripted posts, honouring the watcher's ``wants`` callback like the real backends."""

    name = "fake"

    def __init__(self, posts: list[ClassroomPost] | None = None, *, login_required: bool = False,
                 error: str | None = None):
        self.posts = list(posts or [])
        self.login_required = login_required
        self.error = error
        self.calls = 0

    def fetch(self, ctx: Any, courses: list[Any], wants: Any) -> list[CourseFetch]:
        self.calls += 1
        out = []
        for course in courses:
            if self.login_required or self.error:
                out.append(CourseFetch(course=course, login_required=self.login_required, error=self.error))
                continue
            posts = [p for p in self.posts if wants(course.course_id, p.post_id, p.stream_hash)]
            out.append(CourseFetch(course=course, posts=posts, seen_in_stream=len(self.posts)))
        return out


def fast_documents(monkeypatch: Any) -> None:
    """Real tailoring / letter code, no Chromium (PDF rendering replaced by a tiny PDF)."""
    import jobbot.documents.service as docs_service

    def fake_pdf(cv: Any, out_path: Path) -> tuple[Path, Any]:
        Path(out_path).write_bytes(PDF_BYTES)
        return Path(out_path), cv

    def fake_letter(letter: Any, profile: Any, out_path: Path, *, recipient: str | None = None) -> Path:
        Path(out_path).write_bytes(PDF_BYTES)
        return Path(out_path)

    monkeypatch.setattr(docs_service, "render_cv_pdf_fitted", fake_pdf)
    monkeypatch.setattr(docs_service, "render_letter_pdf", fake_letter)
    monkeypatch.setattr(docs_service, "pdf_text", lambda path: "")
    monkeypatch.setattr(docs_service, "pdf_pages", lambda path: 1)
