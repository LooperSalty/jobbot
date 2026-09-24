"""Notification channels usable on a headless server: e-mail to yourself, ntfy and Telegram. Never raise.

- **E-mail** (``settings.notify_email``): only ``important`` / ``urgent`` notifications, at most
  ``settings.notify_email_max_per_day`` per day (action_log channel ``notify``), subject « [jobbot] … », sent to
  ``profile.identity.email`` through the usual ``Mailer`` (dry-run → ``.eml`` in output/outbox, like every e-mail).
- **ntfy** (``settings.notify_ntfy_topic``): ``POST https://ntfy.sh/<topic>`` (title / priority / tags as query
  parameters, so accents survive; message as UTF-8 body). Skipped in dry-run: nothing leaves the machine.
- **Telegram** (``settings.notify_telegram`` + ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` in .env):
  ``sendMessage`` of the bot; important / urgent only unless ``settings.notify_telegram_all``. The token is
  part of the API URL: it is never logged (httpx request logs are redacted too). Skipped in dry-run.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

import httpx

if TYPE_CHECKING:
    from jobbot.config import Secrets, Settings
    from jobbot.context import AppContext

log = logging.getLogger(__name__)

_TOKEN_IN_URL = re.compile(r"/bot[^/\s]+/")


class _RedactBotToken(logging.Filter):
    """httpx logs every request URL at INFO: hide the Telegram bot token it contains."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # malformed record: leave it to logging
            return True
        if "/bot" in text:
            record.msg, record.args = _TOKEN_IN_URL.sub("/bot***/", text), ()
        return True


for _name in ("httpx", "httpcore"):
    if not any(isinstance(f, _RedactBotToken) for f in logging.getLogger(_name).filters):
        logging.getLogger(_name).addFilter(_RedactBotToken())

Level = Literal["info", "important", "urgent"]
LEVELS: tuple[str, ...] = ("info", "important", "urgent")
LEVEL_FR = {"info": "information", "important": "important", "urgent": "urgent"}

NTFY_URL = "https://ntfy.sh"
NTFY_TIMEOUT_S = 10.0
NTFY_PRIORITY = {"info": "3", "important": "4", "urgent": "5"}  # ntfy: 3 default, 4 high, 5 max
NTFY_TAGS = {"info": "information_source", "important": "briefcase", "urgent": "rotating_light"}

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_TIMEOUT_S = 10.0
TELEGRAM_PREFIX = {"info": "", "important": "IMPORTANT — ", "urgent": "URGENT — "}

EMAIL_LEVELS = frozenset({"important", "urgent"})
EMAIL_CHANNEL = "notify"  # action_log channel (never a Pacer channel)
EMAIL_ACTION = "email"
EMAIL_ACTION_SIMULATED = "email_simulated"  # dry-run sends do not use the real daily budget
SUBJECT_PREFIX = "[jobbot] "


def normalize_level(level: str | None) -> str:
    return level if level in LEVELS else "info"


# --------------------------------------------------------------------------- ntfy


def send_ntfy(settings: Settings, title: str, message: str, level: str, *, dry_run: bool = False,
              post: Callable[..., Any] | None = None) -> bool:
    """Push to the ntfy topic (True when accepted). Never raises."""
    topic = getattr(settings, "notify_ntfy_topic", None)
    if not topic:
        return False
    if dry_run:
        log.info("[simulation] notification ntfy non envoyée : %s", title)
        return False
    params = {"title": title, "priority": NTFY_PRIORITY[normalize_level(level)],
              "tags": NTFY_TAGS[normalize_level(level)]}
    try:
        resp = (post or httpx.post)(f"{NTFY_URL}/{topic}", params=params, content=message.encode("utf-8"),
                                    headers={"Content-Type": "text/plain; charset=utf-8"}, timeout=NTFY_TIMEOUT_S)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.warning("Notification ntfy impossible : %s", exc)
        return False
    if resp.status_code >= 400:
        log.warning("Notification ntfy refusée (HTTP %s)", resp.status_code)
        return False
    return True


# --------------------------------------------------------------------------- Telegram


def send_telegram(settings: Settings, secrets: Secrets, title: str, message: str, level: str, *,
                  dry_run: bool = False, post: Callable[..., Any] | None = None) -> bool:
    """Message through the Telegram bot (True when accepted). Never raises, never logs the token."""
    token = getattr(secrets, "telegram_bot_token", None)
    chat_id = getattr(secrets, "telegram_chat_id", None)
    lvl = normalize_level(level)
    if not (getattr(settings, "notify_telegram", False) and token and chat_id):
        return False
    if lvl == "info" and not getattr(settings, "notify_telegram_all", False):
        return False
    if dry_run:
        log.info("[simulation] notification Telegram non envoyée : %s", title)
        return False
    payload = {"chat_id": chat_id, "text": f"{TELEGRAM_PREFIX[lvl]}{title}\n{message}",
               "disable_web_page_preview": True}
    try:
        resp = (post or httpx.post)(TELEGRAM_URL.format(token=token), json=payload, timeout=TELEGRAM_TIMEOUT_S)
    except (httpx.HTTPError, OSError, ValueError) as exc:  # the message may hold the URL: type only
        log.warning("Notification Telegram impossible (%s)", type(exc).__name__)
        return False
    if resp.status_code >= 400:
        log.warning("Notification Telegram refusée (HTTP %s)", resp.status_code)
        return False
    return True


# --------------------------------------------------------------------------- e-mail to yourself


def _email_body(message: str, level: str) -> str:
    return (f"{message}\n\n—\nNiveau : {LEVEL_FR[normalize_level(level)]}. Envoyé par jobbot (réglage notify_email ; "
            "pour ne plus recevoir ces e-mails : jobbot config set notify_email false).")


def email_budget_left(ctx: AppContext) -> int:
    action = EMAIL_ACTION_SIMULATED if ctx.dry_run else EMAIL_ACTION
    used = ctx.repo.count_actions_today(EMAIL_CHANNEL, action)
    return max(0, int(ctx.settings.notify_email_max_per_day) - used)


def send_email_notice(ctx: AppContext, title: str, message: str, level: str, *,
                      mailer_factory: Callable[[AppContext], Any] | None = None) -> bool:
    """E-mail an important / urgent notification to the candidate (True when sent or simulated). Never raises."""
    if not getattr(ctx.settings, "notify_email", False) or normalize_level(level) not in EMAIL_LEVELS:
        return False
    try:
        return _send_email(ctx, title, message, level, mailer_factory)
    except Exception:  # a notification never breaks the caller
        log.warning("Notification par e-mail impossible", exc_info=True)
        return False


def _send_email(ctx: AppContext, title: str, message: str, level: str,
                mailer_factory: Callable[[AppContext], Any] | None) -> bool:
    from jobbot.mail.mailer import MailError, OutgoingEmail, build_mailer

    if email_budget_left(ctx) <= 0:
        log.info("Notification par e-mail non envoyée : maximum de %d par jour atteint.",
                 ctx.settings.notify_email_max_per_day)
        return False
    mailer = (mailer_factory or build_mailer)(ctx)
    if mailer is None:
        log.info("Notification par e-mail non envoyée : SMTP non configuré.")
        return False
    email = OutgoingEmail(to=ctx.profile.identity.email, subject=SUBJECT_PREFIX + title,
                          body=_email_body(message, level), kind="notification")
    action = EMAIL_ACTION_SIMULATED if ctx.dry_run else EMAIL_ACTION
    try:
        mailer.send(email)
    except MailError as exc:
        log.warning("Notification par e-mail non envoyée : %s", exc)
        ctx.repo.log_action(EMAIL_CHANNEL, action, False, str(exc)[:200])
        return False
    ctx.repo.log_action(EMAIL_CHANNEL, action, True, title[:200])
    return True
