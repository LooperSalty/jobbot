"""Keys of the job APIs travel in URLs (Adzuna ``app_id`` / ``app_key`` query parameters, Jooble ``/api/<key>``):
httpx logs every request URL at INFO, so those values are masked in the ``httpx`` / ``httpcore`` records (and in
jobbot's own retry warnings). Installed once, when a keyed source module is imported.
"""

from __future__ import annotations

import logging
import re

_PATTERNS = (
    (re.compile(r"(app_(?:id|key)=)[^&\s\"']+", re.I), r"\1***"),
    (re.compile(r"(jooble\.org/api/)[^/?\s\"']+", re.I), r"\1***"),
)
_LOGGERS = ("httpx", "httpcore", "jobbot.http")


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactApiKeys(logging.Filter):
    """Masks job-API keys found in a log record's final message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # malformed record: leave it to logging
            return True
        if "app_" in text or "jooble.org/api/" in text:
            record.msg, record.args = redact(text), ()
        return True


def install() -> None:
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactApiKeys) for f in logger.filters):
            logger.addFilter(RedactApiKeys())
