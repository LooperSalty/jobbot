"""Test doubles for the orchestration tests (apply service, pipeline, CLI). No network, no browser, no SMTP."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jobbot import clock
from jobbot.apply.base import ApplyResult
from jobbot.context import AppContext
from jobbot.models import ApplyMethod, JobOffer, JobStatus, SentEmail, TailoredDocuments

PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def with_settings(ctx: AppContext, **updates: Any) -> AppContext:
    """Copy of ``ctx`` with top-level settings replaced (nested models: pass full copies)."""
    return dataclasses.replace(ctx, settings=ctx.settings.model_copy(update=updates))


def with_limits(ctx: AppContext, **limits: Any) -> AppContext:
    return with_settings(ctx, limits=ctx.settings.limits.model_copy(update=limits))


def live(ctx: AppContext) -> AppContext:
    """Same context but not in dry-run."""
    return dataclasses.replace(ctx, dry_run=False)


def write_docs(ctx: AppContext, job_id: str | None = None, company_id: str | None = None) -> TailoredDocuments:
    folder = ctx.root / "output" / "test" / (job_id or company_id or "x")
    folder.mkdir(parents=True, exist_ok=True)
    cv = folder / "CV_Jean_DUPONT.pdf"
    letter = folder / "Lettre_de_motivation_Jean_DUPONT.pdf"
    cv.write_bytes(PDF_BYTES)
    letter.write_bytes(PDF_BYTES)
    docs = TailoredDocuments(
        job_id=job_id, company_id=company_id, cv_pdf=str(cv), letter_pdf=str(letter),
        letter_text="Madame, Monsieur,\n\nJe souhaite rejoindre votre équipe.\n\nJean DUPONT",
        email_subject="Candidature – Développeur – Jean DUPONT", email_body="Bonjour,\n\nVoici ma candidature.\n\nJean",
        created_at=clock.now(),
    )
    ctx.repo.save_documents(docs)
    return docs


def store_job(ctx: AppContext, job: JobOffer, *, status: JobStatus = JobStatus.DOCS_READY, score: float = 80,
              docs: bool = True) -> JobOffer:
    stored = job.model_copy(update={"status": status, "match_score": score})
    ctx.repo.save_job(stored)
    if docs:
        write_docs(ctx, job_id=stored.id)
    return stored


class FakeApplier:
    """Returns scripted results; records every job it was asked to apply to."""

    def __init__(self, channel: str, results: list[ApplyResult] | None = None, *, can: bool = True,
                 raises: Exception | None = None, fatal_after: str | None = None):
        self.channel = channel
        self.results = list(results or [])
        self.can = can
        self.raises = raises
        self.fatal: str | None = None
        self.fatal_after = fatal_after
        self.calls: list[str] = []

    def can_apply(self, job: JobOffer) -> bool:
        return self.can

    def apply(self, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        self.calls.append(job.id)
        if self.raises is not None:
            raise self.raises
        if self.fatal_after:
            self.fatal = self.fatal_after
        if self.results:
            return self.results.pop(0)
        return applied(job.apply_method)


def applied(method: ApplyMethod, *, email_to: str | None = None, mid: str | None = None) -> ApplyResult:
    return ApplyResult(ok=True, status=JobStatus.APPLIED, method=method, detail="Envoyée (test).",
                       email_to=email_to, email_message_id=mid)


def result(status: JobStatus, detail: str = "", *, ok: bool = False,
           method: ApplyMethod = ApplyMethod.LINKEDIN_EASY_APPLY) -> ApplyResult:
    return ApplyResult(ok=ok, status=status, method=method, detail=detail)


def install_appliers(monkeypatch: Any, appliers: dict[str, Any]) -> list[str]:
    """Replace ``service.make_applier``; returns the list of channels built (in order)."""
    from jobbot.apply import service

    built: list[str] = []

    def fake(ctx: AppContext, channel: str, res: Any) -> Any:
        built.append(channel)
        return appliers.get(channel)

    monkeypatch.setattr(service, "make_applier", fake)
    return built


class FakeBrowser:
    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0

    @contextmanager
    def open(self, ctx: AppContext, *, headless: bool | None = None) -> Iterator[str]:
        self.opened += 1
        try:
            yield "browser-context"
        finally:
            self.closed += 1


class FakeSource:
    name = "fake"

    def __init__(self, jobs: list[JobOffer]):
        self.jobs = jobs

    def available(self) -> tuple[bool, str]:
        return True, ""

    def search(self, query: Any) -> Iterator[JobOffer]:
        yield from self.jobs

    def fetch_details(self, job: JobOffer) -> JobOffer:
        return job


class FakeMailer:
    def __init__(self, dry_run: bool = False, error: Exception | None = None):
        self.dry_run = dry_run
        self.error = error
        self.sent: list[Any] = []

    def send(self, email: Any) -> SentEmail:
        if self.error is not None:
            raise self.error
        self.sent.append(email)
        return SentEmail(to=email.to, subject=email.subject, message_id=f"<m{len(self.sent)}@gmail.com>",
                         kind=email.kind, sent_at=clock.now(), dry_run=self.dry_run)


class FakeSMTP:
    """smtplib.SMTP stand-in (records messages)."""

    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float = 0):
        self.host, self.port = host, port
        self.messages: list[Any] = []
        self.logged: tuple[str, str] | None = None
        FakeSMTP.instances.append(self)

    def ehlo(self) -> None: ...

    def starttls(self, context: Any = None) -> None: ...

    def login(self, user: str, password: str) -> None:
        self.logged = (user, password)

    def send_message(self, msg: Any, from_addr: str | None = None, to_addrs: Any = None) -> dict:
        self.messages.append(msg)
        return {}

    def noop(self) -> tuple[int, bytes]:
        return 250, b"OK"

    def quit(self) -> None: ...


def fixture_path(name: str) -> Path:
    return Path(__file__).parent / name
