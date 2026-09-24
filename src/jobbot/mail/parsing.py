"""Raw RFC 5322 bytes → InboundMessage: RFC 2047 headers, multipart bodies, DSN bounces, quote stripping."""

from __future__ import annotations

import contextlib
import email
import email.policy
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from jobbot.mail.messages import InboundMessage
from jobbot.textutil import extract_emails, html_to_text

log = logging.getLogger(__name__)

_MSGID_TOKEN = re.compile(r"<[^<>\s]+>")
_BOUNCE_FROM = re.compile(r"(mailer-daemon|postmaster|mail delivery (subsystem|system))", re.IGNORECASE)
_DELAY_SUBJECT = re.compile(r"(\(delay\)|delayed|retard|temporaire|will retry|nouvelle tentative)", re.IGNORECASE)
_AUTO_PRECEDENCE = {"auto_reply", "bulk", "junk", "list"}
_QUOTE_MARKERS = re.compile(
    r"^(>"
    r"|le [^\n]{5,160}(\n[^\n]{0,100})?a [ée]crit\s?:"
    r"|on [^\n]{5,160}(\n[^\n]{0,100})?wrote\s?:"
    r"|de\s?:\s"
    r"|from:\s"
    r"|envoy[ée] de mon "
    r"|-{2,}\s?(message d'origine|original message|message transf[ée]r[ée]|forwarded message)"
    r"|_{10,})",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class BounceInfo:
    hard: bool = False
    soft: bool = False
    recipient: str | None = None
    original_id: str | None = None  # Message-ID of the undelivered e-mail (attached to the DSN)


# --------------------------------------------------------------------------- headers


def decode_value(value: str) -> str:
    """Decode RFC 2047 encoded-words ('=?utf-8?q?...?=') and flatten whitespace."""
    text = value or ""
    if "=?" in text:
        with contextlib.suppress(LookupError, ValueError, HeaderParseError):  # keep the raw text
            text = str(make_header(decode_header(text)))
    return re.sub(r"\s+", " ", text).strip()


def header(msg: Message, name: str) -> str:
    try:
        raw = msg.get(name)
    except Exception:  # malformed header: the policy parser can raise on access
        log.debug("En-tête %s illisible", name, exc_info=True)
        return ""
    return decode_value(str(raw)) if raw is not None else ""


def msg_ids(value: str) -> list[str]:
    """All '<id>' tokens of a Message-ID / In-Reply-To / References header, in order, deduplicated."""
    seen: dict[str, None] = {}
    for token in _MSGID_TOKEN.findall(value or ""):
        seen.setdefault(token, None)
    return list(seen)


def first_msg_id(value: str) -> str | None:
    ids = msg_ids(value)
    if ids:
        return ids[0]
    bare = (value or "").strip()
    return f"<{bare}>" if bare and " " not in bare and "@" in bare else None


def sender_address(raw_headers: bytes) -> str:
    """Lower-cased From address of a raw header block (cheap, used to skip our own sent copies)."""
    try:
        msg = email.message_from_bytes(raw_headers, policy=email.policy.compat32)
        return parseaddr(decode_value(str(msg.get("From", ""))))[1].strip().lower()
    except Exception:
        return ""


def _date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- bodies


def _leaf_parts(part: Message) -> list[Message]:
    """Leaf parts, without descending into attached messages (bounces carry our original mail)."""
    if part.get_content_maintype() == "message":
        return []
    if part.is_multipart():
        leaves: list[Message] = []
        for sub in part.get_payload():
            leaves.extend(_leaf_parts(sub))
        return leaves
    return [part]


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()  # type: ignore[attr-defined]
        if isinstance(content, str):
            return content
    except Exception:
        pass
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_bodies(msg: Message) -> tuple[str, str]:
    texts: list[str] = []
    htmls: list[str] = []
    for part in _leaf_parts(msg):
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            texts.append(_part_text(part))
        elif ctype == "text/html":
            htmls.append(_part_text(part))
    text = "\n".join(t for t in texts if t.strip()).replace("\r\n", "\n").strip()
    html = "\n".join(h for h in htmls if h.strip()).strip()
    if not text and html:
        text = html_to_text(html)
    return text, html


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply (cut at '>' lines, 'Le … a écrit :', 'De :', '-----Original…')."""
    body = (text or "").replace("\r\n", "\n")
    match = _QUOTE_MARKERS.search(body)
    new = body[: match.start()] if match else body
    return new.strip() or body.strip()


# --------------------------------------------------------------------------- auto / bounce


def is_auto_submitted(msg: Message) -> bool:
    auto = header(msg, "Auto-Submitted").lower()
    return (
        auto not in ("", "no")
        or bool(header(msg, "X-Autoreply"))
        or bool(header(msg, "X-Autorespond"))
        or header(msg, "Precedence").lower() in _AUTO_PRECEDENCE
        or header(msg, "Return-Path") == "<>"
        or bool(header(msg, "List-Unsubscribe"))
        or bool(header(msg, "List-Id"))
    )


def _dsn_blocks(msg: Message) -> list[Message]:
    """Per-recipient header blocks of every message/delivery-status part."""
    blocks: list[Message] = []
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            payload = part.get_payload()
            blocks.extend(payload if isinstance(payload, list) else [])
    return blocks


def _dsn_fields(msg: Message) -> tuple[list[str], list[str], list[str]]:
    blocks = _dsn_blocks(msg)
    rcpts = [
        str(b.get("Final-Recipient") or b.get("Original-Recipient")).split(";", 1)[-1].strip().strip("<>").lower()
        for b in blocks
        if b.get("Final-Recipient") or b.get("Original-Recipient")
    ]
    statuses = [str(b["Status"]).strip() for b in blocks if b.get("Status")]
    actions = [str(b["Action"]).strip().lower() for b in blocks if b.get("Action")]
    return rcpts, statuses, actions


def _original_message_id(msg: Message) -> str | None:
    """Message-ID of the original e-mail carried by a DSN (message/rfc822 or text/rfc822-headers part)."""
    for part in msg.walk():
        ctype = part.get_content_type()
        try:
            if ctype == "message/rfc822":
                inner = part.get_payload()
                inner = inner[0] if isinstance(inner, list) and inner else inner
                return first_msg_id(str(inner.get("Message-ID", ""))) if isinstance(inner, Message) else None
            if ctype == "text/rfc822-headers":
                headers = email.message_from_string(_part_text(part), policy=email.policy.compat32)
                return first_msg_id(str(headers.get("Message-ID", "")))
        except Exception:
            log.debug("Message d'origine illisible dans le rapport de non-distribution", exc_info=True)
    return None


def bounce_info(msg: Message, from_addr: str, from_name: str, subject: str, text: str) -> BounceInfo:
    is_report = (
        msg.get_content_type() == "multipart/report"
        and str(msg.get_param("report-type") or "").lower() == "delivery-status"
    )
    if not (is_report or _BOUNCE_FROM.search(f"{from_addr} {from_name}")):
        return BounceInfo()
    rcpts, statuses, actions = _dsn_fields(msg)
    if not rcpts and header(msg, "X-Failed-Recipients"):
        rcpts = [a.strip().lower() for a in header(msg, "X-Failed-Recipients").split(",") if a.strip()]
    if not rcpts:
        rcpts = [a for a in extract_emails(text) if not _BOUNCE_FROM.search(a) and a != from_addr]
    if statuses or actions:
        hard = any(s.startswith("5") for s in statuses) or "failed" in actions
    else:
        hard = not _DELAY_SUBJECT.search(subject)
    return BounceInfo(hard=hard, soft=not hard, recipient=rcpts[0] if rcpts else None,
                      original_id=_original_message_id(msg))


# --------------------------------------------------------------------------- entry point


def parse_message(raw: bytes, *, uid: int, folder: str, gm_thread_id: str | None = None) -> InboundMessage:
    msg = email.message_from_bytes(raw or b"", policy=email.policy.default)
    from_name, from_addr = parseaddr(header(msg, "From"))
    from_addr = from_addr.strip().lower()
    subject = header(msg, "Subject")
    text, html = extract_bodies(msg)
    bounce = bounce_info(msg, from_addr, from_name, subject, text)
    message_id = first_msg_id(header(msg, "Message-ID")) or ""
    references = msg_ids(header(msg, "References"))
    if bounce.original_id and bounce.original_id not in references:
        references = [*references, bounce.original_id]  # lets thread matching find the application
    return InboundMessage(
        uid=uid,
        folder=folder,
        message_id=message_id,
        from_addr=from_addr,
        from_name=decode_value(from_name),
        subject=subject,
        date=_date(header(msg, "Date")),
        in_reply_to=first_msg_id(header(msg, "In-Reply-To")),
        references=references,
        body_text=text,
        body_html=html,
        auto_submitted=is_auto_submitted(msg) or bounce.soft,
        is_bounce=bounce.hard,
        bounced_recipient=bounce.recipient,
        gm_thread_id=gm_thread_id,
    )
