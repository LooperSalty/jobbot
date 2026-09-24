"""Contract for appliers (one per channel)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from jobbot.models import ApplyMethod, JobOffer, JobStatus, TailoredDocuments


class ApplyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    status: JobStatus  # APPLIED | NEEDS_MANUAL | FAILED | SKIPPED | EXPIRED
    method: ApplyMethod
    detail: str = ""
    email_to: str | None = None
    email_message_id: str | None = None  # when the application was an email (threading for follow-ups)


class Applier(Protocol):
    channel: str  # pacing channel name: linkedin | hellowork | lba | email

    def can_apply(self, job: JobOffer) -> bool: ...

    def apply(self, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        """Submit the application. Never raises for expected failures: return ok=False + status."""
        ...
