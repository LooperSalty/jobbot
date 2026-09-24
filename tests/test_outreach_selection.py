"""outreach.selection — contact order, nominative / unverified policy, bounce pause, history & retries."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from jobbot import clock
from jobbot.models import Application, ApplicationStatus, ApplyMethod, ContactEmail
from jobbot.outreach import selection as sel

RH = ContactEmail(email="recrutement@acme.fr", confidence=0.9, kind="recruitment")
CONTACT = ContactEmail(email="contact@acme.fr", confidence=0.8, kind="generic")
JULIE = ContactEmail(email="julie.martin@acme.fr", confidence=0.6, kind="hr", person_name="Julie Martin")
KARIM = ContactEmail(email="karim.benali@acme.fr", confidence=0.35, source="pattern", kind="tech",
                     person_name="Karim Benali")
LUC = ContactEmail(email="luc.verdier@acme.fr", confidence=0.4, source="pattern", kind="leader",
                   person_name="Luc Verdier", verified=True)
OTHER = ContactEmail(email="sophie.leroy@acme.fr", confidence=0.6, kind="named")


def _ctx(ctx, **outreach):
    settings = ctx.settings.model_copy(update={"outreach": ctx.settings.outreach.model_copy(update=outreach)})
    return dataclasses.replace(ctx, settings=settings)


def _app(company, status=ApplicationStatus.APPLIED, days_ago=1, email="x@acme.fr", **extra) -> Application:
    base = dict(company_id=company.id, method=ApplyMethod.EMAIL, status=status, company_name=company.name,
                applied_at=clock.now() - timedelta(days=days_ago), email_to=email)
    return Application(**{**base, **extra})


# --------------------------------------------------------------------------- classification & order


def test_order_recruitment_hr_tech_leader_generic_named(make_company):
    contacts = [OTHER, CONTACT, LUC, KARIM, JULIE, RH]
    assert [c.email for c in sel.sort_contacts(contacts)] == [
        RH.email, JULIE.email, KARIM.email, LUC.email, CONTACT.email, OTHER.email]
    large = [c.email for c in sel.sort_contacts(contacts, small_company=False)]
    assert large.index(CONTACT.email) < large.index(LUC.email)  # leaders of larger companies after contact@


def test_nominative_and_guess_detection():
    assert not sel.is_nominative(RH) and not sel.is_nominative(CONTACT)
    assert all(sel.is_nominative(c) for c in (JULIE, KARIM, LUC, OTHER))
    assert not sel.is_nominative(ContactEmail(email="rh@acme.fr", kind="hr", person_name="Service RH"))
    assert sel.is_guess(KARIM) and not sel.is_guess(LUC) and not sel.is_guess(JULIE)


def test_usable_honours_settings(settings):
    cfg = settings.outreach
    assert all(sel.usable(c, cfg) for c in (RH, JULIE, KARIM, LUC))
    no_names = cfg.model_copy(update={"allow_nominative": False})
    assert [c for c in (RH, CONTACT, JULIE, KARIM, LUC) if sel.usable(c, no_names)] == [RH, CONTACT]
    no_guess = cfg.model_copy(update={"allow_unverified_guesses": False})
    assert not sel.usable(KARIM, no_guess) and sel.usable(LUC, no_guess)


# --------------------------------------------------------------------------- picking


def test_pick_prefers_named_hr_over_contact_and_skips_used_addresses(ctx, make_company):
    company = make_company(contacts=[CONTACT, JULIE, KARIM])
    assert sel.pick_contact(ctx, company).contact == JULIE
    ctx.repo.blacklist_add(JULIE.email, "bounce")
    assert sel.pick_contact(ctx, company).contact == KARIM
    ctx.repo.create_application(_app(company, email=KARIM.email))
    assert sel.pick_contact(ctx, company).contact == CONTACT
    ctx.repo.blacklist_add("acme.fr")
    assert sel.pick_contact(ctx, company) == sel.Pick(reason="aucune adresse utilisable")


def test_pick_without_nominative_contacts(ctx, make_company):
    company = make_company(contacts=[JULIE, CONTACT])
    assert sel.pick_contact(_ctx(ctx, allow_nominative=False), company).contact == CONTACT


def test_unverified_guess_daily_cap_and_bounce_pause(ctx, make_company):
    ctx = _ctx(ctx, max_unverified_per_day=1)
    only_guess = make_company(contacts=[KARIM])
    with_fallback = make_company(siren="222222222", contacts=[KARIM.model_copy(update={"email": "k@b.fr"}), CONTACT])
    assert sel.pick_contact(ctx, only_guess).contact == KARIM
    assert sel.unverified_left(ctx.repo, ctx.settings.outreach) == 1
    sel.record_unverified(ctx.repo, "someone@x.fr")
    assert sel.unverified_left(ctx.repo, ctx.settings.outreach) == 0
    capped = sel.pick_contact(ctx, only_guess)
    assert capped.contact is None and capped.deferred and "quota" in capped.reason
    assert sel.pick_contact(ctx, with_fallback).contact == CONTACT  # verified address instead of waiting
    assert sel.pick_contact(ctx, only_guess, paused="pause").reason == "pause"
    fresh = _ctx(ctx, max_unverified_per_day=3)
    assert sel.pick_contact(fresh, only_guess, simulated=2).contact is None  # dry-run count included


def test_guesses_paused_by_recent_bounce_rate(make_company, settings):
    company = make_company()
    cfg = settings.outreach
    sent = [_app(company) for _ in range(9)]
    assert sel.guesses_paused([], cfg) is None
    assert sel.guesses_paused([*sent, _app(company, ApplicationStatus.BOUNCED)], cfg).startswith("adresses devinées")
    old_bounce = _app(company, ApplicationStatus.BOUNCED, days_ago=45)
    job_bounce = _app(company, ApplicationStatus.BOUNCED, job_id="job-1")
    assert sel.guesses_paused([*sent, old_bounce, job_bounce], cfg) is None  # out of window / not spontaneous
    assert sel.bounce_stats([*sent, old_bounce]) == (0, 9)


# --------------------------------------------------------------------------- history & retries


def test_company_history_states(make_company, settings):
    company = make_company()
    cfg = settings.outreach
    assert sel.company_history([], company, cfg).state == "fresh"
    assert sel.company_history([_app(company, days_ago=90)], company, cfg).state == "fresh"
    linkedin = Application(method=ApplyMethod.LINKEDIN_EASY_APPLY, applied_at=clock.now(),
                           company_name="ACME Logiciels SAS")
    blocked = sel.company_history([linkedin], company, cfg)
    assert blocked.state == "blocked" and "moins de 60 jours" in blocked.reason
    bounced = _app(company, ApplicationStatus.BOUNCED, email="old@acme.fr")
    retry = sel.company_history([bounced], company, cfg)
    assert retry.state == "retry" and retry.bounced == bounced and retry.attempts == 1
    twice = [bounced, _app(company, ApplicationStatus.BOUNCED, days_ago=0)]
    exhausted = sel.company_history(twice, company, cfg)
    assert exhausted.state == "blocked" and "maximum 2" in exhausted.reason
    assert sel.company_history(twice, company, cfg.model_copy(update={"max_attempts_per_company": 3})).state == "retry"
    assert sel.company_history([bounced, _app(company, days_ago=0)], company, cfg).state == "blocked"


def test_bounced_companies_need_contacted_status_attempts_and_a_new_address(ctx, make_company):
    company = make_company(status="contacted", contacts=[RH, CONTACT])
    ctx.repo.upsert_company(company)
    bounced = ctx.repo.create_application(_app(company, ApplicationStatus.BOUNCED, email=RH.email))
    ctx.repo.blacklist_add(RH.email, "bounce")
    apps = ctx.repo.list_applications()
    assert [c.id for c in sel.bounced_companies(ctx, apps)] == [company.id]
    ctx.repo.blacklist_add(CONTACT.email, "stop")
    assert sel.bounced_companies(ctx, apps) == []  # no address left
    other = make_company(siren="999999999", status="enriched", contacts=[CONTACT])
    ctx.repo.upsert_company(other)
    ctx.repo.create_application(_app(other, ApplicationStatus.BOUNCED))
    assert sel.bounced_companies(ctx, ctx.repo.list_applications()) == []  # not "contacted": normal flow
    assert bounced.id is not None


@pytest.mark.parametrize("kind", ["hr", "tech", "leader", "named", "unknown"])
def test_contact_tier_defaults(kind):
    tier = sel.contact_tier(ContactEmail(email="a.b@acme.fr", kind=kind))
    assert 0 < tier <= sel.TIER["named"]
