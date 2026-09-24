"""Immutable models shared by the inbox reader, the classifier and the platform parsers."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReplyCategory = Literal[
    "interview", "rejection", "positive", "info_request", "auto_reply", "bounce", "unsubscribe", "other"
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InboundMessage(_Frozen):
    """One e-mail read over IMAP (headers decoded, bodies extracted, bounce/auto flags computed)."""

    uid: int
    folder: str
    message_id: str
    from_addr: str
    from_name: str = ""
    subject: str = ""
    date: datetime | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    body_text: str = ""
    body_html: str = ""
    auto_submitted: bool = False  # machine-generated (Auto-Submitted, Precedence, X-Autoreply, empty Return-Path...)
    is_bounce: bool = False  # permanent delivery failure (DSN 5.x.x / mailer-daemon)
    bounced_recipient: str | None = None
    gm_thread_id: str | None = None  # Gmail X-GM-THRID (decimal)


class ReplyClassification(_Frozen):
    category: ReplyCategory
    summary: str
    confidence: float


class InboxReport(_Frozen):
    scanned: int
    matched: int
    updates: list[str] = Field(default_factory=list)
