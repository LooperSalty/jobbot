"""Notification levels and the server channels: ntfy push, Telegram bot, e-mail to yourself (no network)."""

from __future__ import annotations

import dataclasses
import logging

import httpx
import respx

from jobbot import clock, notify, notify_remote, pipeline
from jobbot.apply.service import ApplyReport
from jobbot.classroom import process
from jobbot.mail.mailer import MailError

TOPIC = "jobbot-test-sujet"
NTFY = f"{notify_remote.NTFY_URL}/{TOPIC}"
TOKEN = "123456:jeton-factice-ABC"
TELEGRAM = notify_remote.TELEGRAM_URL.format(token=TOKEN)


def _with(ctx, *, dry_run: bool | None = None, secrets: dict | None = None, **settings):
    return dataclasses.replace(
        ctx, settings=ctx.settings.model_copy(update=settings),
        secrets=ctx.secrets.model_copy(update=secrets or {}),
        dry_run=ctx.dry_run if dry_run is None else dry_run)


class FakeMailer:
    def __init__(self, error: str | None = None):
        self.sent: list = []
        self.error = error

    def send(self, email):
        if self.error:
            raise MailError(self.error)
        self.sent.append(email)
        return email


# --------------------------------------------------------------------------- notify() dispatch


def test_notify_logs_the_level_and_calls_every_remote_channel(ctx, monkeypatch, caplog):
    calls: list = []
    monkeypatch.setattr(notify, "_side_effects_enabled", lambda: True)
    monkeypatch.setattr(notify, "_toast", lambda t, m: calls.append("toast"))
    monkeypatch.setattr(notify_remote, "send_ntfy", lambda *a, **k: calls.append(("ntfy", a[3])))
    monkeypatch.setattr(notify_remote, "send_telegram", lambda *a, **k: calls.append(("telegram", a[4])))

    def broken_email(*a, **k):
        raise RuntimeError("panne")  # one channel failing never blocks the others

    monkeypatch.setattr(notify_remote, "send_email_notice", broken_email)
    with caplog.at_level("INFO"):
        notify.notify(ctx, "Entretien — Acme", "mardi 10 h", level="urgent")
        notify.notify(ctx, "Bilan", "rien", level="n'importe quoi")
    assert calls == ["toast", ("ntfy", "urgent"), ("telegram", "urgent"), "toast", ("ntfy", "info"),
                     ("telegram", "info")]
    assert "Notification (urgent) : Entretien — Acme — mardi 10 h" in caplog.text


# --------------------------------------------------------------------------- ntfy


@respx.mock
def test_ntfy_push_with_priority_and_utf8(ctx):
    route = respx.post(NTFY).mock(return_value=httpx.Response(200, json={"id": "x"}))
    live = _with(ctx, dry_run=False, notify_ntfy_topic=TOPIC)
    assert notify_remote.send_ntfy(live.settings, "Réponse positive — Acme", "Rappel prévu à 14 h", "important")
    request = route.calls[0].request
    assert request.url.params["title"] == "Réponse positive — Acme" and request.url.params["priority"] == "4"
    assert request.content.decode("utf-8") == "Rappel prévu à 14 h"
    assert notify_remote.send_ntfy(live.settings, "t", "m", "urgent") and route.calls[1].request.url.params[
        "priority"] == "5"


@respx.mock
def test_ntfy_is_skipped_or_swallowed(ctx):
    route = respx.post(NTFY).mock(side_effect=[httpx.ConnectError("hors ligne"), httpx.Response(500)])
    settings = _with(ctx, notify_ntfy_topic=TOPIC).settings
    assert not notify_remote.send_ntfy(ctx.settings, "t", "m", "urgent")  # no topic
    assert not notify_remote.send_ntfy(settings, "t", "m", "urgent", dry_run=True) and not route.called
    assert not notify_remote.send_ntfy(settings, "t", "m", "urgent")  # network error: never raises
    assert not notify_remote.send_ntfy(settings, "t", "m", "urgent")  # HTTP 500


# --------------------------------------------------------------------------- Telegram


@respx.mock
def test_telegram_important_only_unless_all(ctx):
    route = respx.post(TELEGRAM).mock(return_value=httpx.Response(200, json={"ok": True}))
    live = _with(ctx, dry_run=False, notify_telegram=True, secrets={"telegram_bot_token": TOKEN,
                                                                    "telegram_chat_id": "42"})
    assert not notify_remote.send_telegram(live.settings, live.secrets, "Bilan", "3 offres", "info")
    assert notify_remote.send_telegram(live.settings, live.secrets, "Entretien", "mardi", "urgent")
    payload = route.calls[0].request.read().decode("utf-8")
    assert '"chat_id":"42"' in payload.replace(" ", "") and "URGENT — Entretien" in payload
    all_levels = _with(live, notify_telegram_all=True)
    assert notify_remote.send_telegram(all_levels.settings, all_levels.secrets, "Bilan", "3 offres", "info")
    assert not notify_remote.send_telegram(live.settings, live.secrets, "E", "m", "urgent", dry_run=True)
    off = _with(live, notify_telegram=False)
    assert not notify_remote.send_telegram(off.settings, off.secrets, "E", "m", "urgent")
    missing = _with(live, secrets={"telegram_chat_id": None})
    assert not notify_remote.send_telegram(missing.settings, missing.secrets, "E", "m", "urgent")
    assert route.call_count == 2


@respx.mock
def test_telegram_never_logs_the_token(ctx, caplog):
    respx.post(TELEGRAM).mock(side_effect=[httpx.Response(200, json={"ok": True}),
                                           httpx.ConnectError(f"échec vers {TELEGRAM}"), httpx.Response(401)])
    live = _with(ctx, notify_telegram=True, secrets={"telegram_bot_token": TOKEN, "telegram_chat_id": "42"})
    with caplog.at_level(logging.DEBUG):
        assert notify_remote.send_telegram(live.settings, live.secrets, "E", "m", "urgent")
        assert not notify_remote.send_telegram(live.settings, live.secrets, "E", "m", "urgent")
        assert not notify_remote.send_telegram(live.settings, live.secrets, "E", "m", "urgent")
    assert TOKEN not in caplog.text and "Telegram" in caplog.text
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: POST %s", (TELEGRAM,), None)
    notify_remote._RedactBotToken().filter(record)
    assert TOKEN not in record.getMessage() and "/bot***/" in record.getMessage()


# --------------------------------------------------------------------------- e-mail to yourself


def test_email_notice_levels_cap_and_subject(ctx):
    mailer = FakeMailer()
    on = _with(ctx, notify_email=True, notify_email_max_per_day=2)
    send = lambda c, level="important": notify_remote.send_email_notice(  # noqa: E731
        c, "Entretien — Acme", "mardi 10 h", level, mailer_factory=lambda _c: mailer)
    assert not send(ctx)  # setting off
    assert not send(on, "info")  # info never e-mailed
    assert send(on) and send(on, "urgent")
    assert not send(on)  # daily cap reached
    assert [e.subject for e in mailer.sent] == ["[jobbot] Entretien — Acme"] * 2
    assert mailer.sent[0].to == ctx.profile.identity.email and mailer.sent[0].kind == "notification"
    assert "mardi 10 h" in mailer.sent[0].body and "notify_email false" in mailer.sent[0].body
    assert ctx.repo.count_actions_today("notify", "email_simulated") == 2  # dry-run: own budget
    assert ctx.repo.count_actions_today("notify", "email") == 0


def test_email_notice_failures_never_raise(ctx):
    on = _with(ctx, dry_run=False, notify_email=True)
    assert not notify_remote.send_email_notice(on, "t", "m", "urgent", mailer_factory=lambda c: None)
    assert not notify_remote.send_email_notice(on, "t", "m", "urgent",
                                               mailer_factory=lambda c: FakeMailer(error="SMTP refusé"))
    today = clock.to_paris(clock.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    assert on.repo.count_actions_since("notify", "email", today, ok_only=False) == 1  # failure recorded
    assert on.repo.count_actions_today("notify", "email") == 0  # but never counted as sent

    def boom(c):
        raise RuntimeError("bug")

    assert not notify_remote.send_email_notice(on, "t", "m", "urgent", mailer_factory=boom)


# --------------------------------------------------------------------------- levels chosen by the callers


def test_classroom_outcome_levels(ctx, make_job, monkeypatch):
    levels: list[str] = []
    monkeypatch.setattr("jobbot.notify.notify", lambda c, t, m, level="info": levels.append(level))
    job = make_job(source="classroom")
    for kind in ("applied", "needs_manual", "pending_review", "filtered_out", "simulated"):
        process.notify_outcome(ctx, job, process.Outcome(kind=kind, detail="détail"))
    assert levels == ["important", "important", "important", "info", "info"]


def test_expired_session_alert_is_urgent_and_daily(ctx, monkeypatch):
    sent: list = []
    monkeypatch.setattr("jobbot.notify.notify", lambda c, t, m, level="info": sent.append((t, level, m)))
    report = ApplyReport(details=["Acme — Dev : session LinkedIn absente : lance « jobbot login linkedin » "
                                  "(sera retentée)"])
    pipeline._alert_expired_session(ctx, report)
    pipeline._alert_expired_session(ctx, report)
    pipeline._alert_expired_session(ctx, ApplyReport(details=["Acme : envoyée"]))
    assert len(sent) == 1 and sent[0][:2] == ("Session à renouveler", "urgent")
    assert "session export" in sent[0][2]
