"""outreach.spontaneous (v2) — named contacts: greeting, unverified-guess cap & bounce pause, retry after bounce;
outreach._compose unit tests. Fakes (documents, mailer, follow-up) come from test_outreach_spontaneous."""

# ruff: noqa: F811 — pytest fixtures imported from test_outreach_spontaneous are "redefined" as parameters

from __future__ import annotations

import dataclasses

import pytest

from jobbot.models import ApplicationStatus, ContactEmail
from jobbot.outreach._compose import (
    GENERIC_GREETING,
    OPT_OUT_SENTENCE,
    ensure_opt_out,
    greeting_for,
    source_sentence,
    with_greeting,
)
from jobbot.outreach.selection import UNVERIFIED_ACTION
from jobbot.outreach.spontaneous import run_spontaneous_campaign
from tests.test_outreach_spontaneous import (  # noqa: F401 — fixtures reused
    FakeMailer,
    _company,
    _outreach,
    _store,
    docs_calls,
    live_ctx,
    sleeps,
)

JULIE = ContactEmail(email="julie.martin@111111111.fr", confidence=0.6, kind="hr", person_name="Julie MARTIN")
LUC_GUESS = ContactEmail(email="luc.verdier@111111111.fr", confidence=0.3, source="pattern", kind="leader",
                         person_name="Luc VERDIER")


def test_country_limit_excludes_french_and_unknown_companies(live_ctx, make_company, docs_calls, sleeps):
    ctx = _outreach(live_ctx, country="LU")
    french = _company(make_company, "111111111", "Alpha")
    unknown = _company(make_company, "222222222", "Beta")
    lux = _company(make_company, "333333333", "Gamma")
    french = french.model_copy(update={"raw": {**french.raw, "country": "FR"}})
    lux = lux.model_copy(update={"raw": {**lux.raw, "country": "LU"}})
    _store(ctx, french, unknown, lux)
    mailer = FakeMailer()

    run_spontaneous_campaign(ctx, mailer)

    assert [mail.to for mail in mailer.sent] == ["recrutement@333333333.fr"]


def test_excluded_address_is_not_used_even_if_already_saved(live_ctx, make_company, docs_calls, sleeps):
    contact = ContactEmail(email="retractation@111111111.fr", kind="generic")
    _store(live_ctx, _company(make_company, "111111111", "Alpha", contacts=[contact]))
    mailer = FakeMailer()
    run_spontaneous_campaign(live_ctx, mailer)
    assert not mailer.sent


def test_cdd_cdi_does_not_send_old_alternance_draft(live_ctx, make_company, docs_calls, sleeps, monkeypatch):
    from jobbot.models import ContractType
    from jobbot.outreach import spontaneous

    target = live_ctx.profile.target.model_copy(update={"contract_types": [ContractType.CDD, ContractType.CDI]})
    ctx = dataclasses.replace(live_ctx, profile=live_ctx.profile.model_copy(update={"target": target}))
    _store(ctx, _company(make_company, "111111111", "Alpha"))
    original = spontaneous._docs_for
    monkeypatch.setattr(spontaneous, "_docs_for", lambda *args: original(*args).model_copy(
        update={"email_subject": "Candidature en alternance"}))
    mailer = FakeMailer()
    result = run_spontaneous_campaign(ctx, mailer)
    assert not mailer.sent and result.failed == 1


def _guess_company(make_company, siren: str, name: str):
    guess = LUC_GUESS.model_copy(update={"email": f"luc.verdier@{siren}.fr"})
    company = _company(make_company, siren, name, contacts=[guess])
    return company.model_copy(update={"raw": {**company.raw, "email_sources": {guess.email: "pattern:search"}}})


def _bounce(ctx, email: str) -> None:
    """What the inbox does on a hard bounce: application BOUNCED + address blacklisted."""
    app = ctx.repo.find_applications_by_email(email)[0]
    ctx.repo.update_application(app.model_copy(update={"status": ApplicationStatus.BOUNCED, "next_followup_at": None}))
    ctx.repo.blacklist_add(email, "adresse invalide (bounce)")


# --------------------------------------------------------------------------- greeting


def test_named_contact_is_greeted_by_name_and_role_address_formally(live_ctx, make_company, docs_calls, sleeps):
    named = _company(make_company, "111111111", "Alpha", contacts=[JULIE], score=9)
    role = _company(make_company, "222222222", "Beta")
    _store(live_ctx, named, role)
    mailer = FakeMailer()
    run_spontaneous_campaign(live_ctx, mailer)
    role_mail, named_mail = mailer.sent  # priority: recruitment address (+20) before a named HR contact (+15)
    assert named_mail.to == JULIE.email and named_mail.body.startswith("Bonjour Julie Martin,\n")
    assert "Bonjour,\n" not in named_mail.body and named_mail.body.endswith(OPT_OUT_SENTENCE)
    assert role_mail.body.startswith(f"{GENERIC_GREETING}\n\nVotre activité chez Beta")


# --------------------------------------------------------------------------- unverified guesses


def test_unverified_guess_is_sent_logged_and_capped_per_day(live_ctx, make_company, docs_calls, sleeps):
    ctx = _outreach(live_ctx, max_unverified_per_day=1)
    first, second = _guess_company(make_company, "111111111", "Alpha"), _guess_company(make_company, "222222222", "Beta")
    _store(ctx, first.model_copy(update={"raw": {**first.raw, "outreach_score": 9}}), second)
    mailer = FakeMailer()

    report = run_spontaneous_campaign(ctx, mailer)

    assert [e.to for e in mailer.sent] == ["luc.verdier@111111111.fr"] and report.sent == 1 and report.skipped == 1
    body = mailer.sent[0].body
    assert body.startswith("Bonjour Luc Verdier,")
    assert "J'ai déduit votre adresse de votre nom, relevé sur votre profil professionnel public" in body
    assert ctx.repo.count_actions_today("spontaneous", UNVERIFIED_ACTION) == 1
    assert any("quota d'adresses devinées" in d for d in report.details)
    assert ctx.repo.get_company(second.id).status == "enriched"  # deferred to tomorrow, not "no_email"
    assert "devinée, non vérifiée" in ctx.repo.list_applications()[0].notes


def test_dry_run_counts_simulated_guesses_against_the_cap(ctx, make_company, docs_calls, sleeps):
    ctx = _outreach(ctx, max_unverified_per_day=1)
    _store(ctx, _guess_company(make_company, "111111111", "Alpha"), _guess_company(make_company, "222222222", "Beta"))
    report = run_spontaneous_campaign(ctx, FakeMailer())
    assert (report.sent, report.skipped) == (1, 1)
    assert ctx.repo.count_actions_today("spontaneous", UNVERIFIED_ACTION) == 0  # nothing persisted in dry-run


def test_guesses_paused_when_recent_bounce_rate_is_high(live_ctx, make_company, docs_calls, sleeps):
    old = _company(make_company, "333333333", "Ancienne")
    _store(live_ctx, old)
    run_spontaneous_campaign(live_ctx, FakeMailer())
    _bounce(live_ctx, "recrutement@333333333.fr")  # 1 bounce out of 1 recent spontaneous e-mail
    _store(live_ctx, _guess_company(make_company, "111111111", "Alpha"),
           _company(make_company, "222222222", "Beta"))
    mailer = FakeMailer()

    report = run_spontaneous_campaign(live_ctx, mailer)

    assert [e.to for e in mailer.sent] == ["recrutement@222222222.fr"]  # verified addresses still go out
    assert report.details[0].startswith("Info : adresses devinées en pause")
    assert any(d.startswith("Alpha : adresses devinées en pause") for d in report.details)


def test_guesses_disabled_mark_company_without_usable_address(live_ctx, make_company, docs_calls, sleeps):
    ctx = _outreach(live_ctx, allow_unverified_guesses=False)
    company = _guess_company(make_company, "111111111", "Alpha")
    _store(ctx, company)
    report = run_spontaneous_campaign(ctx, FakeMailer())
    assert report.skipped == 1 and ctx.repo.get_company(company.id).status == "no_email"


# --------------------------------------------------------------------------- retry after bounce


def test_retry_after_bounce_with_next_best_address(live_ctx, make_company, docs_calls, sleeps):
    company = _company(make_company, "111111111", "Alpha", contacts=[
        ContactEmail(email="recrutement@111111111.fr", confidence=0.9, kind="recruitment"), JULIE,
        ContactEmail(email="contact@111111111.fr", confidence=0.8, kind="generic")])
    _store(live_ctx, company)
    mailer = FakeMailer()
    assert run_spontaneous_campaign(live_ctx, mailer).sent == 1 and mailer.sent[0].to == "recrutement@111111111.fr"
    assert run_spontaneous_campaign(live_ctx, mailer).sent == 0  # no bounce: company already contacted

    _bounce(live_ctx, "recrutement@111111111.fr")
    report = run_spontaneous_campaign(live_ctx, mailer)

    assert report.sent == 1 and "nouvel essai après un rebond" in report.details[-1]
    assert report.details[0].startswith("Info : adresses devinées en pause")  # 1 bounce / 1 recent e-mail
    retry = mailer.sent[-1]
    assert retry.to == JULIE.email and retry.body.startswith("Bonjour Julie Martin,")
    assert docs_calls == ["Alpha"]  # documents reused, greeting adapted to the new recipient
    new_app = live_ctx.repo.find_applications_by_email(JULIE.email)[0]
    events = live_ctx.repo.list_events(new_app.id)
    assert [e.kind for e in events] == ["applied", "retry_after_bounce"]
    assert "recrutement@111111111.fr" in events[1].detail and "2/2" in events[1].detail
    assert live_ctx.repo.get_company(company.id).status == "contacted"

    _bounce(live_ctx, JULIE.email)  # second bounce: max_attempts_per_company (2) reached
    assert run_spontaneous_campaign(live_ctx, mailer).sent == 0 and len(mailer.sent) == 2
    more = _outreach(live_ctx, max_attempts_per_company=3)
    assert run_spontaneous_campaign(more, mailer).sent == 1 and mailer.sent[-1].to == "contact@111111111.fr"


def test_retry_after_bounce_goes_through_review_mode(live_ctx, make_company, docs_calls, sleeps):
    company = _company(make_company, "111111111", "Alpha", contacts=[
        ContactEmail(email="recrutement@111111111.fr", confidence=0.9, kind="recruitment"), JULIE])
    _store(live_ctx, company)
    mailer = FakeMailer()
    run_spontaneous_campaign(live_ctx, mailer)
    _bounce(live_ctx, "recrutement@111111111.fr")
    review = dataclasses.replace(live_ctx, settings=live_ctx.settings.model_copy(update={"mode": "review"}))

    report = run_spontaneous_campaign(review, mailer)
    pending = live_ctx.repo.get_company(company.id)
    assert pending.status == "pending_review" and pending.raw["review_contact"] == JULIE.email
    assert "nouvel essai" in report.details[-1] and len(mailer.sent) == 1

    live_ctx.repo.upsert_company(pending.model_copy(update={"status": "approved"}))
    assert run_spontaneous_campaign(review, mailer).sent == 1 and mailer.sent[-1].to == JULIE.email
    kinds = [e.kind for e in live_ctx.repo.list_events(live_ctx.repo.find_applications_by_email(JULIE.email)[0].id)]
    assert kinds == ["applied", "retry_after_bounce"] and docs_calls == ["Alpha"]


# --------------------------------------------------------------------------- composition


@pytest.mark.parametrize(("body", "expected_start"), [
    ("Madame, Monsieur,\n\nTexte.", "Bonjour Julie Martin,\n\nTexte."),
    ("Bonjour,\n\nTexte.", "Bonjour Julie Martin,\n\nTexte."),
    ("Bonjour Madame, Monsieur,\nTexte.", "Bonjour Julie Martin,\nTexte."),
    ("Bonjour Luc Verdier,\n\nTexte.", "Bonjour Julie Martin,\n\nTexte."),
    ("Objet : Candidature\nMadame, Monsieur,\n\nTexte.", "Objet : Candidature\nBonjour Julie Martin,\n\nTexte."),
    ("Bonjour, je suis Jean.", "Bonjour Julie Martin,\n\nBonjour, je suis Jean."),
    ("Texte sans formule.", "Bonjour Julie Martin,\n\nTexte sans formule."),
    ("", "Bonjour Julie Martin,"),
])
def test_with_greeting(body, expected_start):
    assert with_greeting(body, "Bonjour Julie Martin,") == expected_start


def test_greeting_for_contacts():
    assert greeting_for(JULIE) == "Bonjour Julie Martin,"
    assert greeting_for(LUC_GUESS) == "Bonjour Luc Verdier,"
    assert greeting_for(ContactEmail(email="rh@acme.fr", kind="recruitment", person_name="Service RH")) == GENERIC_GREETING
    assert greeting_for(ContactEmail(email="j.m@acme.fr", kind="named")) == GENERIC_GREETING  # no name known
    assert greeting_for(ContactEmail(email="x.y@acme.fr", kind="hr", person_name="Équipe Talents")) == GENERIC_GREETING


def test_source_sentences(make_company):
    company = make_company(raw={"email_sources": {
        "a.b@acme.fr": "pattern:website", "c.d@acme.fr": "pattern:hunter", "rh@acme.fr": "https://acme.fr/carrieres",
        "h@acme.fr": "hunter"}})
    guess = ContactEmail(email="a.b@acme.fr", source="pattern", kind="hr")
    assert "relevé sur votre site internet" in source_sentence(company, guess)
    assert "Hunter.io" in source_sentence(company, guess.model_copy(update={"email": "c.d@acme.fr"}))
    assert "registre public" in source_sentence(company, guess.model_copy(update={"email": "z@acme.fr"}))
    scraped = ContactEmail(email="rh@acme.fr", kind="recruitment")
    assert source_sentence(company, scraped) == "J'ai trouvé votre adresse sur la page Carrières de votre site."
    assert "Hunter.io" in source_sentence(company, ContactEmail(email="h@acme.fr", kind="generic"))
    assert source_sentence(company, ContactEmail(email="m@acme.fr", source="manual")) is None
    body = ensure_opt_out("Texte", sentence=source_sentence(company, guess))
    assert "PS : J'ai déduit votre adresse" in body and body.endswith(OPT_OUT_SENTENCE)
    assert ensure_opt_out(body, sentence="J'ai trouvé votre adresse ailleurs.") == body  # idempotent
