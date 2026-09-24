"""outreach.spontaneous — campaign safeguards (fake documents service, mailer and followup modules)."""

from __future__ import annotations

import dataclasses
import sys
import types
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from jobbot import clock
from jobbot.models import Application, ApplicationStatus, ApplyMethod, ContactEmail, SentEmail, TailoredDocuments
from jobbot.outreach import spontaneous
from jobbot.outreach.spontaneous import OPT_OUT_SENTENCE, CampaignReport, ensure_opt_out, run_spontaneous_campaign
from jobbot.pacing import Pacer

FOLLOWUP_DAYS = 11


class OutgoingEmail(BaseModel):  # mirrors the mail.mailer contract
    model_config = ConfigDict(frozen=True, extra="forbid")

    to: str
    subject: str
    body: str
    attachments: list[str] = Field(default_factory=list)
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    kind: Literal["application", "spontaneous", "followup"]
    application_id: int | None = None


class FakeMailer:
    def __init__(self, dry_run: bool = False, fail_for: set[str] | None = None):
        self.dry_run = dry_run
        self.fail_for = fail_for or set()
        self.sent: list[OutgoingEmail] = []

    def send(self, email: OutgoingEmail) -> SentEmail:
        if email.to in self.fail_for:
            raise RuntimeError("550 5.1.1 destinataire inconnu")
        self.sent.append(email)
        return SentEmail(to=email.to, subject=email.subject, message_id=f"<{len(self.sent)}.jobbot@gmail.com>",
                         kind=email.kind, sent_at=clock.now(), dry_run=self.dry_run)


@pytest.fixture
def docs_calls(tmp_path, monkeypatch):
    """Install fake documents.service / mail.mailer / followup modules; returns prepare_documents calls."""
    calls = []

    def prepare_documents(ctx, *, job=None, company=None):
        calls.append(company.name)
        if company.name == "Docs KO":
            raise RuntimeError("LLM indisponible")
        folder = tmp_path / company.id
        folder.mkdir(exist_ok=True)
        cv, letter = folder / "CV_Jean_DUPONT.pdf", folder / "Lettre_de_motivation_Jean_DUPONT.pdf"
        cv.write_bytes(b"%PDF-1.4 cv")
        letter.write_bytes(b"%PDF-1.4 lettre")
        docs = TailoredDocuments(
            company_id=company.id, cv_pdf=str(cv), letter_pdf=str(letter), letter_text="Lettre complète",
            email_subject=f"Candidature spontanée – {company.name}",
            email_body=f"Bonjour,\n\nVotre activité chez {company.name} m'intéresse.\n\nJean DUPONT",
            created_at=clock.now(),
        )
        ctx.repo.save_documents(docs)
        return docs

    def compute_next_followup(app, cfg, *, after=None):
        return app.applied_at + timedelta(days=FOLLOWUP_DAYS)

    for name, attrs in {
        "jobbot.documents.service": {"prepare_documents": prepare_documents},
        "jobbot.mail.mailer": {"OutgoingEmail": OutgoingEmail, "Mailer": FakeMailer},
        "jobbot.followup": {"compute_next_followup": compute_next_followup},
    }.items():
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
    return calls


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(spontaneous, "_build_pacer",
                        lambda ctx: Pacer(ctx.repo, ctx.settings.limits, sleep=waited.append))
    return waited


@pytest.fixture
def live_ctx(ctx):
    return dataclasses.replace(ctx, dry_run=False)


def _company(make_company, siren: str, name: str, contacts=None, status: str = "enriched", score: int = 5):
    default = [ContactEmail(email=f"recrutement@{siren}.fr", confidence=0.9, kind="recruitment")]
    return make_company(
        siren=siren, name=name, domain=f"{siren}.fr", status=status, contacts=default if contacts is None else contacts,
        raw={"outreach_score": score, "email_sources": {f"recrutement@{siren}.fr": f"https://{siren}.fr/carrieres"}},
    )


def _store(ctx, *companies):
    for company in companies:
        ctx.repo.upsert_company(company)


def _outreach(ctx, **values):
    outreach = ctx.settings.outreach.model_copy(update=values)
    return dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={"outreach": outreach}))


# --------------------------------------------------------------------------- ensure_opt_out


def test_ensure_opt_out_appends_source_and_stop_once():
    body = ensure_opt_out("Bonjour,\n\nMa candidature.\n\nJean", "la page Carrières de votre site")
    assert body.endswith(OPT_OUT_SENTENCE)
    assert "PS : J'ai trouvé votre adresse sur la page Carrières de votre site." in body
    assert ensure_opt_out(body, "la page Carrières de votre site") == body  # idempotent
    already = "Bonjour.\nUn simple « non merci » et je ne vous recontacterai pas."
    assert ensure_opt_out(already) == already
    assert ensure_opt_out("", None) == f"PS : {OPT_OUT_SENTENCE}"


# --------------------------------------------------------------------------- sending


def test_sends_application_and_records_everything(live_ctx, make_company, docs_calls, sleeps):
    ctx = live_ctx
    _store(ctx, _company(make_company, "111111111", "Alpha Web", score=9),
           _company(make_company, "222222222", "Beta Soft", score=3))
    mailer = FakeMailer()

    report = run_spontaneous_campaign(ctx, mailer)

    assert report == CampaignReport(sent=2, skipped=0, failed=0, details=report.details)
    assert docs_calls == ["Alpha Web", "Beta Soft"]  # best score first
    first = mailer.sent[0]
    assert first.to == "recrutement@111111111.fr" and first.kind == "spontaneous"
    assert [Path(p).name for p in first.attachments] == ["CV_Jean_DUPONT.pdf", "Lettre_de_motivation_Jean_DUPONT.pdf"]
    assert "STOP" in first.body and "la page Carrières de votre site" in first.body
    assert first.subject == "Candidature spontanée – Alpha Web"
    assert len(sleeps) == 1  # one human pause between the two e-mails, none before the first

    apps = ctx.repo.list_applications()
    alpha = next(a for a in apps if a.company_name == "Alpha Web")
    assert alpha.method == ApplyMethod.EMAIL and alpha.status == ApplicationStatus.APPLIED
    assert alpha.email_message_id == "<1.jobbot@gmail.com>" and alpha.email_thread_ids == ["<1.jobbot@gmail.com>"]
    assert alpha.next_followup_at - alpha.applied_at == timedelta(days=FOLLOWUP_DAYS)
    assert alpha.company_id and alpha.cv_path.endswith("CV_Jean_DUPONT.pdf")
    assert [e.kind for e in ctx.repo.list_events(alpha.id)] == ["applied"]
    assert {c.status for c in ctx.repo.list_companies()} == {"contacted"}
    assert Pacer(ctx.repo, ctx.settings.limits).used_today("spontaneous") == 2


def test_followup_fallback_when_module_missing(live_ctx, make_company, docs_calls, sleeps, monkeypatch):
    monkeypatch.setitem(sys.modules, "jobbot.followup", None)
    _store(live_ctx, _company(make_company, "111111111", "Alpha Web"))
    run_spontaneous_campaign(live_ctx, FakeMailer())
    app = live_ctx.repo.list_applications()[0]
    assert app.next_followup_at - app.applied_at == timedelta(days=live_ctx.settings.followup.first_after_days)


def test_never_sends_unverified_guess_and_skips_used_or_blacklisted_addresses(live_ctx, make_company, docs_calls, sleeps):
    ctx = _outreach(live_ctx, allow_unverified_guesses=False)
    guess_only = _company(make_company, "111111111", "Guess Only", contacts=[
        ContactEmail(email="luc.verdier@111111111.fr", confidence=0.4, source="pattern", kind="leader")])
    fallback = _company(make_company, "222222222", "Fallback", contacts=[
        ContactEmail(email="rh@222222222.fr", confidence=0.9, kind="recruitment"),
        ContactEmail(email="contact@222222222.fr", confidence=0.8, kind="generic"),
        ContactEmail(email="p.dupuis@222222222.fr", confidence=0.4, source="pattern", kind="leader", verified=True)])
    ctx.repo.blacklist_add("rh@222222222.fr", "STOP reçu")
    ctx.repo.create_application(Application(method=ApplyMethod.EMAIL, applied_at=clock.now() - timedelta(days=200),
                                            company_name="Ancien nom", email_to="contact@222222222.fr"))
    _store(ctx, guess_only, fallback)
    mailer = FakeMailer()

    report = run_spontaneous_campaign(ctx, mailer)

    assert [e.to for e in mailer.sent] == ["p.dupuis@222222222.fr"]  # verified guess, others excluded
    assert report.sent == 1 and report.skipped == 1
    assert ctx.repo.get_company(guess_only.id).status == "no_email"
    assert "registre public" in mailer.sent[0].body  # GDPR art. 14 source line for a guessed address


def test_company_level_dedup_blacklist_and_exclusion(live_ctx, make_company, docs_calls, sleeps):
    ctx = live_ctx
    recent = _company(make_company, "111111111", "Acme Logiciels")
    ctx.repo.create_application(Application(method=ApplyMethod.LINKEDIN_EASY_APPLY, applied_at=clock.now(),
                                            company_name="ACME Logiciels SAS", job_title="Dev"))
    black = _company(make_company, "222222222", "Noire")
    ctx.repo.blacklist_add("222222222.fr")
    excluded = _company(make_company, "333333333", "Evitee")
    ctx = dataclasses.replace(ctx, profile=ctx.profile.model_copy(update={
        "target": ctx.profile.target.model_copy(update={"exclude_companies": ["EVITEE"]})}))
    _store(ctx, recent, black, excluded)
    mailer = FakeMailer()

    report = run_spontaneous_campaign(ctx, mailer)

    assert mailer.sent == [] and docs_calls == []
    assert report.skipped == 3 and report.sent == 0
    assert any("moins de 60 jours" in d for d in report.details)
    assert any("liste noire" in d for d in report.details) and any("exclue" in d for d in report.details)


def test_daily_quota_stops_the_campaign(live_ctx, make_company, docs_calls, sleeps):
    ctx = dataclasses.replace(live_ctx, settings=live_ctx.settings.model_copy(update={
        "limits": live_ctx.settings.limits.model_copy(update={"spontaneous_per_day": 1})}))
    _store(ctx, _company(make_company, "111111111", "Alpha", score=9), _company(make_company, "222222222", "Beta"))
    mailer = FakeMailer()
    report = run_spontaneous_campaign(ctx, mailer)
    assert report.sent == 1 and len(mailer.sent) == 1

    report = run_spontaneous_campaign(ctx, mailer, limit=5)  # explicit limit cannot bypass the quota
    assert report.sent == 0 and "Quota journalier atteint" in report.details[-1]


def test_limit_caps_the_number_of_companies(live_ctx, make_company, docs_calls, sleeps):
    _store(live_ctx, *[_company(make_company, f"{i}" * 9, f"Boite {i}") for i in range(1, 4)])
    mailer = FakeMailer()
    assert run_spontaneous_campaign(live_ctx, mailer, limit=2).sent == 2 and len(mailer.sent) == 2


def test_failures_are_isolated(live_ctx, make_company, docs_calls, sleeps):
    ctx = live_ctx
    _store(ctx, _company(make_company, "111111111", "Docs KO", score=9),
           _company(make_company, "222222222", "Bounce", score=8),
           _company(make_company, "333333333", "Ok", score=7))
    mailer = FakeMailer(fail_for={"recrutement@222222222.fr"})

    report = run_spontaneous_campaign(ctx, mailer)

    assert (report.sent, report.failed) == (1, 2)
    assert [e.to for e in mailer.sent] == ["recrutement@333333333.fr"]
    assert ctx.repo.count_actions_today("spontaneous", "apply") == 1  # failures never consume quota
    assert ctx.repo.get_company(_company(make_company, "222222222", "Bounce").id).status == "enriched"
    assert any("envoi échoué" in d for d in report.details) and any("préparation impossible" in d for d in report.details)


def test_rejected_recipient_is_blacklisted(live_ctx, make_company, docs_calls, sleeps):
    _store(live_ctx, _company(make_company, "222222222", "Bounce"))
    mailer = FakeMailer(fail_for={"recrutement@222222222.fr"})
    report = run_spontaneous_campaign(live_ctx, mailer)
    assert report.failed == 1 and live_ctx.repo.is_blacklisted("recrutement@222222222.fr")
    assert run_spontaneous_campaign(live_ctx, mailer).failed == 0  # never retried


def test_smtp_auth_error_aborts_the_campaign(live_ctx, make_company, docs_calls, sleeps):
    _store(live_ctx, _company(make_company, "111111111", "Alpha", score=9), _company(make_company, "222222222", "Beta"))

    class AuthKo(FakeMailer):
        def send(self, email):
            raise RuntimeError("(535, b'5.7.8 Username and Password not accepted')")

    report = run_spontaneous_campaign(live_ctx, AuthKo())
    assert (report.sent, report.failed) == (0, 1) and len(report.details) == 1
    assert "Campagne interrompue" in report.details[0]
    assert docs_calls == ["Alpha"] and not live_ctx.repo.is_blacklisted("recrutement@111111111.fr")


def test_mail_fatal_error_class_aborts_even_with_unknown_wording(live_ctx, make_company, docs_calls, sleeps,
                                                                 monkeypatch):
    fatal = type("MailFatalError", (RuntimeError,), {})
    monkeypatch.setattr(sys.modules["jobbot.mail.mailer"], "MailFatalError", fatal, raising=False)
    _store(live_ctx, _company(make_company, "111111111", "Alpha", score=9), _company(make_company, "222222222", "Beta"))

    class Blocked(FakeMailer):
        def send(self, email):
            raise fatal("Gmail a bloqué le message comme indésirable")

    report = run_spontaneous_campaign(live_ctx, Blocked())
    assert report.failed == 1 and docs_calls == ["Alpha"] and "Campagne interrompue" in report.details[-1]


def test_missing_cv_file_fails_cleanly(live_ctx, make_company, docs_calls, sleeps, monkeypatch):
    company = _company(make_company, "111111111", "Alpha")
    _store(live_ctx, company)
    broken = TailoredDocuments(company_id=company.id, cv_pdf="C:/nulle-part/cv.pdf", created_at=clock.now())
    monkeypatch.setattr(spontaneous, "_prepare", lambda ctx, c: broken)
    report = run_spontaneous_campaign(live_ctx, FakeMailer())
    assert report.failed == 1 and "CV introuvable" in report.details[0]


def test_record_failure_after_send_still_marks_company_contacted(live_ctx, make_company, docs_calls, sleeps,
                                                                 monkeypatch):
    company = _company(make_company, "111111111", "Alpha")
    _store(live_ctx, company)

    def boom(*args, **kwargs):
        raise RuntimeError("disque plein")

    monkeypatch.setattr(spontaneous, "_record_sent", boom)
    report = run_spontaneous_campaign(live_ctx, FakeMailer())
    assert report.sent == 1 and "suivi non enregistré" in report.details[0]
    assert live_ctx.repo.get_company(company.id).status == "contacted"  # never sent twice


# --------------------------------------------------------------------------- review mode / dry-run / no mailer


def test_review_mode_prepares_without_sending_then_sends_approved(live_ctx, make_company, docs_calls, sleeps):
    ctx = dataclasses.replace(live_ctx, settings=live_ctx.settings.model_copy(update={"mode": "review"}))
    company = _company(make_company, "111111111", "Alpha")
    _store(ctx, company)
    mailer = FakeMailer()

    report = run_spontaneous_campaign(ctx, mailer)
    assert mailer.sent == [] and report.sent == 0 and report.skipped == 1
    pending = ctx.repo.get_company(company.id)
    assert pending.status == "pending_review" and pending.raw["review_contact"] == "recrutement@111111111.fr"
    assert "en attente de validation" in report.details[0]

    ctx.repo.upsert_company(pending.model_copy(update={"status": "approved"}))  # user validated in the dashboard
    report = run_spontaneous_campaign(ctx, mailer)
    assert report.sent == 1 and docs_calls == ["Alpha"]  # documents reused, not regenerated
    assert ctx.repo.get_company(company.id).status == "contacted"


def test_review_mode_without_mailer_only_prepares(live_ctx, make_company, docs_calls, sleeps):
    ctx = dataclasses.replace(live_ctx, settings=live_ctx.settings.model_copy(update={"mode": "review"}))
    _store(ctx, _company(make_company, "111111111", "Alpha"),
           _company(make_company, "222222222", "Beta", status="approved"))
    report = run_spontaneous_campaign(ctx, None)
    assert report.sent == 0 and report.skipped == 2
    assert any("SMTP non configuré" in d for d in report.details)


def test_no_mailer_in_auto_mode(live_ctx, make_company, docs_calls, sleeps):
    _store(live_ctx, _company(make_company, "111111111", "Alpha"))
    report = run_spontaneous_campaign(live_ctx, None)
    assert report == CampaignReport(sent=0, skipped=0, failed=0, details=report.details)
    assert docs_calls == [] and "SMTP" in report.details[0]


def test_dry_run_simulates_without_persisting(ctx, make_company, docs_calls, sleeps):
    assert ctx.dry_run
    company = _company(make_company, "111111111", "Alpha")
    _store(ctx, company)
    real_mailer = FakeMailer(dry_run=False)
    report = run_spontaneous_campaign(ctx, real_mailer)
    assert report.sent == 1 and real_mailer.sent == [] and report.details[0].startswith("[simulation]")

    outbox_mailer = FakeMailer(dry_run=True)  # a dry-run mailer writes .eml files: allowed
    run_spontaneous_campaign(ctx, outbox_mailer)
    assert len(outbox_mailer.sent) == 1 and "STOP" in outbox_mailer.sent[0].body
    assert ctx.repo.list_applications() == [] and ctx.repo.get_company(company.id).status == "enriched"
    assert ctx.repo.count_actions_today("spontaneous", "apply") == 0 and sleeps == []


def test_unexpected_error_is_contained(live_ctx, make_company, docs_calls, sleeps, monkeypatch):
    _store(live_ctx, _company(make_company, "111111111", "Alpha"))

    def boom(ctx, company):
        raise KeyError("bug")

    monkeypatch.setattr(spontaneous, "_skip_reason", boom)
    report = run_spontaneous_campaign(live_ctx, FakeMailer())
    assert report.failed == 1 and "erreur inattendue" in report.details[0]


def test_default_subject_and_body_when_documents_have_none(live_ctx, make_company, docs_calls, sleeps, monkeypatch):
    company = _company(make_company, "111111111", "Alpha", contacts=[
        ContactEmail(email="contact@111111111.fr", kind="generic", source="manual")])
    _store(live_ctx, company)
    cv = live_ctx.root / "cv.pdf"
    cv.write_bytes(b"%PDF")
    bare = TailoredDocuments(company_id=company.id, cv_pdf=str(cv), created_at=clock.now())
    monkeypatch.setattr(spontaneous, "_prepare", lambda ctx, c: bare)
    mailer = FakeMailer()
    run_spontaneous_campaign(live_ctx, mailer)
    email = mailer.sent[0]
    assert email.subject.startswith("Candidature spontanée – alternance") and "Jean DUPONT" in email.subject
    assert "Alpha" in email.body and email.body.endswith(OPT_OUT_SENTENCE)
    assert "trouvé votre adresse" not in email.body  # manual contact: no invented source
    assert email.attachments == [str(cv)]
