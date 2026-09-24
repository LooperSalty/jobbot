"""E-mail: sending (Gmail SMTP) and reading replies (Gmail IMAP)."""

from jobbot.mail.inbox import (
    InboundMessage,
    InboxReport,
    ReplyClassification,
    classify_reply,
    fetch_new_messages,
    process_inbox,
)
from jobbot.mail.mailer import Mailer, MailError, MailFatalError, OutgoingEmail, build_mailer, reply_subject

__all__ = [
    "InboundMessage",
    "InboxReport",
    "MailError",
    "MailFatalError",
    "Mailer",
    "OutgoingEmail",
    "ReplyClassification",
    "build_mailer",
    "classify_reply",
    "fetch_new_messages",
    "process_inbox",
    "reply_subject",
]
