"""Sending ramp of spontaneous e-mails: start + step × full weeks, capped, back to start on bounces (+ notify)."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from jobbot import clock, ramp
from jobbot.config import Limits, SpontaneousRamp
from jobbot.models import Application, ApplicationStatus, ApplyMethod
from jobbot.pacing import Pacer


def _spontaneous(repo, days_ago: float, *, bounced: bool = False, company_id: str = "c1") -> Application:
    app = Application(method=ApplyMethod.EMAIL, company_id=company_id, company_name=f"Entreprise {company_id}",
                      applied_at=clock.now() - timedelta(days=days_ago), email_to=f"rh@{company_id}.example",
                      status=ApplicationStatus.BOUNCED if bounced else ApplicationStatus.APPLIED)
    return repo.create_application(app)


def test_ramp_settings_are_validated():
    assert Limits().spontaneous_ramp == SpontaneousRamp(enabled=True, start=20, step_per_week=5, max=40,
                                                        max_bounce_rate=0.05)
    with pytest.raises(ValueError, match="start"):
        SpontaneousRamp(start=50, max=40)


def test_no_email_yet_means_the_start_cap(repo):
    state = ramp.ramp_state(repo, Limits())
    assert state.cap == 20 and state.weeks == 0 and state.since is None
    assert "démarre au premier envoi" in ramp.describe(state, Limits())


@pytest.mark.parametrize(("days", "cap"), [(0, 20), (6.9, 20), (7.1, 25), (15, 30), (22, 35), (29, 40), (200, 40)])
def test_cap_grows_with_full_weeks_since_the_first_real_email(repo, days, cap):
    _spontaneous(repo, days, company_id="premier")
    assert ramp.spontaneous_cap(repo, Limits()) == cap


def test_offer_applications_and_lba_relays_do_not_start_the_ramp(repo):
    repo.create_application(Application(method=ApplyMethod.EMAIL, job_id="offre-1", company_name="X",
                                        applied_at=clock.now() - timedelta(days=30)))
    repo.create_application(Application(method=ApplyMethod.LBA, company_id="c9", company_name="Y",
                                        applied_at=clock.now() - timedelta(days=30)))
    assert ramp.spontaneous_cap(repo, Limits()) == 20


def test_disabled_ramp_uses_the_fixed_quota(repo):
    limits = Limits(spontaneous_per_day=12, spontaneous_ramp=SpontaneousRamp(enabled=False))
    _spontaneous(repo, 60)
    state = ramp.ramp_state(repo, limits)
    assert state.cap == 12 and not state.enabled
    assert "désactivée" in ramp.describe(state, limits)


def test_high_bounce_rate_falls_back_to_start(repo):
    _spontaneous(repo, 30, company_id="ancien")
    for i in range(9):
        _spontaneous(repo, 1, company_id=f"ok{i}")
    _spontaneous(repo, 1, bounced=True, company_id="rebond")  # 1/10 = 10 % > 5 %
    state = ramp.ramp_state(repo, Limits())
    assert state.cap == 20 and state.reset_due
    assert state.sent == 10 and state.bounced == 1
    assert "ramené au départ" in ramp.describe(state, Limits())


def test_check_reset_records_notifies_once_and_restarts_the_weeks(ctx, monkeypatch):
    live = dataclasses.replace(ctx, dry_run=False)
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr("jobbot.notify.notify", lambda c, title, message, level="info": notes.append((level, message)))
    _spontaneous(live.repo, 30, company_id="ancien")
    _spontaneous(live.repo, 2, bounced=True, company_id="rebond")
    state = ramp.check_reset(live)
    assert state.cap == 20 and not state.reset_due
    assert notes and notes[0][0] == "important" and "20/jour" in notes[0][1]
    assert live.repo.kv_get(ramp.RESET_KEY)
    ramp.check_reset(live)
    assert len(notes) == 1  # already at the start: nothing more to reset
    later = clock.now() + timedelta(days=8)  # a week after the reset, bounces now older than 7 days
    cfg = live.settings.limits.spontaneous_ramp
    assert ramp.ramp_state(live.repo, live.settings.limits, now=later).cap == min(cfg.max, cfg.start + cfg.step_per_week)


def test_check_reset_in_dry_run_changes_nothing(ctx, monkeypatch):
    monkeypatch.setattr("jobbot.notify.notify", lambda *a, **k: pytest.fail("pas de notification en simulation"))
    _spontaneous(ctx.repo, 30, company_id="ancien")
    _spontaneous(ctx.repo, 1, bounced=True, company_id="rebond")
    assert ramp.check_reset(ctx).reset_due
    assert ctx.repo.kv_get(ramp.RESET_KEY) is None


def test_pacer_reads_the_ramp_for_the_spontaneous_channel_only(repo):
    _spontaneous(repo, 15, company_id="premier")
    pacer = Pacer(repo, Limits(), sleep=lambda s: None)
    assert pacer.quota("spontaneous") == 30
    assert pacer.quota("linkedin") == 15
    assert pacer.remaining("spontaneous") == 30
