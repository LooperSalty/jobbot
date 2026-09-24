"""Follow-ups + notifications tests (fake mailer, fake subprocess, no network)."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from jobbot import clock, followup, notify
from jobbot.config import FollowupSettings, Limits, Secrets
from jobbot.followup import (
    FollowupReport,
    add_working_days,
    compute_next_followup,
    holidays_moselle,
    is_working_day,
    run_followups,
    write_followup,
)
from jobbot.llm import FakeLLM, LLMError
from jobbot.mail.mailer import SUBJECT_KV_PREFIX, Mailer, MailError, MailFatalError
from jobbot.models import Application, ApplicationStatus, ApplyMethod, SentEmail
from jobbot.pacing import Pacer

CFG = FollowupSettings()
FIRST_ID = "<first.jobbot@gmail.com>"


def app_(**overrides) -> Application:
    base = dict(
        id=3, job_id="job-1", method=ApplyMethod.EMAIL, applied_at=datetime(2026, 9, 23, 8, tzinfo=UTC),
        company_name="Acme Logiciels", job_title="Développeur Full-Stack (alternance)", email_to="rh@acme.fr",
        email_message_id=FIRST_ID, email_thread_ids=[FIRST_ID],
    )
    base.update(overrides)
    return Application(**base)


class FakeMailer:
    def __init__(self, *, dry_run: bool = False, fail: Exception | None = None):
        self.dry_run = dry_run
        self.fail = fail
        self.sent = []

    def send(self, email):
        if self.fail is not None:
            raise self.fail
        self.sent.append(email)
        n = len(self.sent)
        return SentEmail(to=email.to, subject=email.subject, message_id=f"<fu{n}.jobbot@gmail.com>",
                         in_reply_to=email.in_reply_to, kind="followup", application_id=email.application_id,
                         sent_at=clock.now(), dry_run=self.dry_run)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def live_ctx(ctx, monkeypatch, sleeps):
    """Non-dry-run context whose pacer never sleeps (pauses are recorded in ``sleeps``)."""
    monkeypatch.setattr(followup, "_build_pacer",
                        lambda c: Pacer(c.repo, c.settings.limits, sleep=sleeps.append))
    return dataclasses.replace(ctx, dry_run=False, llm=FakeLLM(LLMErrorResponder()))


class LLMErrorResponder:
    def __call__(self, prompt, system, fast):
        raise LLMError("LLM indisponible")


def due_app(repo, **overrides) -> Application:
    base = dict(applied_at=clock.now() - timedelta(days=10), next_followup_at=clock.now() - timedelta(hours=1), id=None)
    base.update(overrides)
    return repo.create_application(app_(**base))


# --------------------------------------------------------------------------- calendar


def test_moselle_holidays_and_working_days():
    hol = holidays_moselle(2027)
    assert date(2027, 3, 26) in hol and date(2027, 3, 29) in hol  # Good Friday (Moselle), Easter Monday
    assert date(2026, 12, 26) in holidays_moselle(2026) and date(2026, 5, 14) in holidays_moselle(2026)  # Ascension
    assert add_working_days(date(2027, 3, 25), 1) == date(2027, 3, 30)
    assert add_working_days(date(2026, 9, 25), 1) == date(2026, 9, 28)  # Friday → Monday
    assert not is_working_day(date(2026, 11, 11)) and is_working_day(date(2026, 11, 12))


# --------------------------------------------------------------------------- scheduling


def test_first_followup_is_seven_working_days_on_a_good_slot():
    nxt = compute_next_followup(app_(), CFG)
    local = clock.to_paris(nxt)
    assert local.date() == date(2026, 10, 1)  # Wed 23/09 + 7 working days = Thu 01/10
    assert 9 <= local.hour < 11 and local.weekday() in (1, 2, 3)


def test_monday_and_friday_slots_move_to_midweek():
    monday = compute_next_followup(app_(applied_at=datetime(2026, 9, 24, 8, tzinfo=UTC)), CFG)  # Mon 05/10 → Tue
    assert clock.to_paris(monday).date() == date(2026, 10, 6)
    cfg = CFG.model_copy(update={"first_after_days": 3})
    friday = compute_next_followup(app_(applied_at=datetime(2026, 9, 23, 8, tzinfo=UTC)), cfg)  # Mon 28/09 → Tue
    assert clock.to_paris(friday).date() == date(2026, 9, 29)


def test_second_followup_and_give_up_checkpoint():
    after = datetime(2026, 10, 1, 8, tzinfo=UTC)
    second = compute_next_followup(app_(followup_count=1, status=ApplicationStatus.FOLLOWED_UP), CFG, after=after)
    assert clock.to_paris(second).date() >= add_working_days(date(2026, 10, 1), 8) - timedelta(days=1)
    done = app_(followup_count=2, status=ApplicationStatus.FOLLOWED_UP,
                last_contact_at=datetime(2026, 10, 13, tzinfo=UTC))
    give_up = compute_next_followup(done, CFG)
    assert give_up >= datetime(2026, 10, 23, 8, tzinfo=UTC)  # applied + 30 days at least


def test_no_followup_when_closed_or_answered():
    assert compute_next_followup(app_(status=ApplicationStatus.REJECTED), CFG) is None
    assert compute_next_followup(app_(last_response_at=clock.now()), CFG) is None


def test_give_up_caps_a_late_followup():
    cfg = CFG.model_copy(update={"give_up_after_days": 3})
    nxt = compute_next_followup(app_(), cfg)
    assert nxt == datetime(2026, 9, 26, 8, tzinfo=UTC)


# --------------------------------------------------------------------------- writing


def test_template_followups_in_french(profile):
    subject, body = write_followup(profile, app_(), None, number=1)
    assert subject == "Re: Candidature – Développeur Full-Stack (alternance)"
    assert body.startswith("Bonjour,") and "23 septembre 2026" in body and "Jean DUPONT" in body
    assert "octobre 2026" in body and "?" in body and "non merci" not in body
    _, last = write_followup(profile, app_(), None, number=2)
    assert "dernier message" in last.lower() and "relancerai plus" in last


def test_spontaneous_followup_has_opt_out(profile):
    subject, body = write_followup(profile, app_(job_id=None, job_title=""), None, number=1)
    assert "spontanée" in subject and "candidature spontanée en alternance" in body
    assert "non merci" in body


def test_llm_followup_is_validated(profile):
    good = ("Bonjour,\n\nJe me permets de revenir vers vous concernant ma candidature du 23 septembre. "
            "Mon projet de fin d'études est désormais en production. Auriez-vous un moment pour en parler ?\n\n"
            "Bien cordialement,")
    _, body = write_followup(profile, app_(job_id=None), FakeLLM(good), number=1)
    assert body.startswith("Bonjour,") and "Jean DUPONT\n+33 6 12 34 56 78" in body  # signature appended
    assert body.rstrip().endswith("en réponse suffit.")  # spontaneous → opt-out line last
    _, fallback = write_followup(profile, app_(), FakeLLM("Bonjour [Nom], ..." * 10), number=1)
    assert "[" not in fallback and "Je me permets" in fallback
    _, err = write_followup(profile, app_(), FakeLLM([]), number=1)  # LLMError → template
    assert "Je me permets" in err


# --------------------------------------------------------------------------- run


def test_run_followups_sends_in_thread(live_ctx, tmp_path):
    cv = tmp_path / "CV.pdf"
    cv.write_bytes(b"%PDF")
    app = due_app(live_ctx.repo, cv_path=str(cv))
    live_ctx.repo.kv_set(SUBJECT_KV_PREFIX + FIRST_ID, "Candidature – Développeur Full-Stack")
    mailer = FakeMailer()
    report = run_followups(live_ctx, mailer)
    assert isinstance(report, FollowupReport) and (report.sent, report.manual, report.closed) == (1, 0, 0)
    mail = mailer.sent[0]
    assert mail.subject == "Re: Candidature – Développeur Full-Stack" and mail.kind == "followup"
    assert mail.in_reply_to == FIRST_ID and mail.references == [FIRST_ID] and mail.attachments == [str(cv)]
    stored = live_ctx.repo.get_application(app.id)
    assert stored.status == ApplicationStatus.FOLLOWED_UP and stored.followup_count == 1
    assert stored.email_thread_ids == [FIRST_ID, "<fu1.jobbot@gmail.com>"]
    assert stored.next_followup_at > clock.now() and stored.last_contact_at is not None
    assert [e.kind for e in live_ctx.repo.list_events(app.id)] == ["followup_sent"]
    assert Pacer(live_ctx.repo, live_ctx.settings.limits).used_today("followup") == 1


def test_second_followup_threads_on_last_message_without_cv(live_ctx, tmp_path, sleeps):
    cv = tmp_path / "CV.pdf"
    cv.write_bytes(b"%PDF")
    due_app(live_ctx.repo, followup_count=1, status=ApplicationStatus.FOLLOWED_UP, cv_path=str(cv),
            email_thread_ids=[FIRST_ID, "<fu0@gmail.com>"])
    due_app(live_ctx.repo, email_to="jobs@beta.fr", company_name="Beta")
    mailer = FakeMailer()
    report = run_followups(live_ctx, mailer)
    assert report.sent == 2 and len(sleeps) == 1  # one human pause between the two e-mails
    second = next(m for m in mailer.sent if m.to == "rh@acme.fr")
    assert second.in_reply_to == "<fu0@gmail.com>" and second.references == [FIRST_ID, "<fu0@gmail.com>"]
    assert second.attachments == [] and "dernier message" in second.body.lower()


def test_run_followups_manual_close_and_cancel(live_ctx):
    manual = due_app(live_ctx.repo, method=ApplyMethod.LINKEDIN_EASY_APPLY, email_to=None, email_message_id=None,
                     email_thread_ids=[])
    old = due_app(live_ctx.repo, applied_at=clock.now() - timedelta(days=40))
    maxed = due_app(live_ctx.repo, followup_count=2, status=ApplicationStatus.FOLLOWED_UP)
    answered = due_app(live_ctx.repo, last_response_at=clock.now() - timedelta(days=1))
    live_ctx.repo.blacklist_add("black@list.fr")
    black = due_app(live_ctx.repo, email_to="black@list.fr")
    mailer = FakeMailer()
    report = run_followups(live_ctx, mailer)
    assert (report.sent, report.manual, report.closed) == (0, 1, 2) and mailer.sent == []
    repo = live_ctx.repo
    assert [e.kind for e in repo.list_events(manual.id)] == ["manual_followup"]
    assert repo.get_application(manual.id).followup_count == 1
    assert repo.get_application(manual.id).next_followup_at > clock.now()
    for closed in (old, maxed):
        assert repo.get_application(closed.id).status == ApplicationStatus.NO_RESPONSE
    for cancelled in (answered, black):
        assert repo.get_application(cancelled.id).next_followup_at is None
    assert any("liste noire" in d for d in report.details)


def test_unthreaded_followup_is_not_a_fake_reply(live_ctx):
    due_app(live_ctx.repo, email_message_id=None, email_thread_ids=[])
    mailer = FakeMailer()
    run_followups(live_ctx, mailer)
    mail = mailer.sent[0]
    assert mail.in_reply_to is None and mail.references == []
    assert mail.subject == "Relance – Candidature – Développeur Full-Stack (alternance)"


def test_run_followups_without_mailer_is_manual(live_ctx):
    app = due_app(live_ctx.repo)
    report = run_followups(live_ctx, None)
    assert report.manual == 1 and "rh@acme.fr" in report.details[0]
    assert live_ctx.repo.list_events(app.id)[0].kind == "manual_followup"


def test_run_followups_errors_quota_and_fatal(live_ctx):
    due_app(live_ctx.repo)
    due_app(live_ctx.repo, email_to="b@beta.fr", company_name="Beta")
    report = run_followups(live_ctx, FakeMailer(fail=MailError("boîte pleine")))
    assert report.sent == 0 and sum("boîte pleine" in d for d in report.details) == 2
    fatal = run_followups(live_ctx, FakeMailer(fail=MailFatalError("identifiants refusés")))
    assert sum("reportée" in d for d in fatal.details) == 1  # the second one is not even attempted
    limited = dataclasses.replace(live_ctx, settings=live_ctx.settings.model_copy(
        update={"limits": Limits(followups_per_day=0)}))
    quota = run_followups(limited, FakeMailer())
    assert quota.sent == 0 and any("Quota" in d for d in quota.details)


def test_run_followups_dry_run_does_not_touch_the_application(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(followup, "_build_pacer", lambda c: Pacer(c.repo, c.settings.limits, sleep=lambda s: None))
    app = due_app(ctx.repo)
    creds = Secrets(_env_file=None, smtp_user="jean.dupont@example.com", smtp_password="x" * 16)
    mailer = Mailer(creds, ctx.repo, ctx.profile, dry_run=True, outbox_dir=tmp_path / "outbox")
    report = run_followups(ctx, mailer)
    assert report.sent == 1 and "[simulation]" in report.details[0]
    assert len(list((tmp_path / "outbox").glob("*.eml"))) == 1
    stored = ctx.repo.get_application(app.id)
    assert stored.followup_count == 0 and stored.status == ApplicationStatus.APPLIED
    real_mailer = FakeMailer()
    again = run_followups(ctx, real_mailer)  # dry-run context + real mailer → nothing is sent
    assert real_mailer.sent == [] and "[simulation]" in again.details[0]


def test_run_followups_unexpected_error_is_isolated(live_ctx, monkeypatch):
    due_app(live_ctx.repo)
    monkeypatch.setattr(followup, "write_followup", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    report = run_followups(live_ctx, FakeMailer())
    assert report.sent == 0 and "erreur inattendue" in report.details[0]


# --------------------------------------------------------------------------- notify


def test_notify_logs_only_under_pytest(ctx, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    with caplog.at_level("INFO"):
        notify.notify(ctx, "Entretien", "Acme veut vous rencontrer\nmardi")
    assert calls == [] and "Acme veut vous rencontrer mardi" in caplog.text


def test_notify_toast_and_jarvis(ctx, monkeypatch, tmp_path):
    script = tmp_path / "jarvis_notify.py"
    script.write_text("print('ok')", encoding="utf-8")
    settings = ctx.settings.model_copy(update={"notify_jarvis": True, "jarvis_script": str(script)})
    jctx = dataclasses.replace(ctx, settings=settings)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(notify, "_side_effects_enabled", lambda: True)
    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    monkeypatch.setattr(notify.sys, "platform", "win32")
    monkeypatch.setattr(notify.shutil, "which", lambda name: "C:/powershell.exe")
    notify.notify(jctx, "Entretien <b>", "x" * 500)
    toast, jarvis = calls
    assert "-EncodedCommand" in toast[0] and toast[1]["timeout"] == notify.TOAST_TIMEOUT_S
    assert toast[1]["env"]["JOBBOT_TOAST_TITLE"] == "Entretien <b>"  # escaped inside PowerShell, not here
    assert len(toast[1]["env"]["JOBBOT_TOAST_MESSAGE"]) == notify.MAX_MESSAGE_CHARS
    assert jarvis[0][:4] == [sys.executable, str(script), "--type", "tell"] and jarvis[1]["timeout"] == 15


def test_notify_never_raises(ctx, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("powershell", 10)

    monkeypatch.setattr(notify, "_side_effects_enabled", lambda: True)
    monkeypatch.setattr(notify.sys, "platform", "win32")
    monkeypatch.setattr(notify.shutil, "which", lambda name: "C:/powershell.exe")
    monkeypatch.setattr(notify.subprocess, "run", boom)
    jctx = dataclasses.replace(ctx, settings=ctx.settings.model_copy(
        update={"notify_jarvis": True, "jarvis_script": str(Path(__file__))}))
    notify.notify(jctx, "t", "m")  # timeout on both calls: swallowed
    monkeypatch.setattr(notify, "_clean", lambda *a: (_ for _ in ()).throw(ValueError("x")))
    notify.notify(ctx, "t", "m")  # even an internal bug is swallowed


def test_side_effects_switch(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JOBBOT_NO_TOAST", "1")
    assert notify._side_effects_enabled() is False
    monkeypatch.setenv("JOBBOT_NO_TOAST", "0")
    assert notify._side_effects_enabled() is True
