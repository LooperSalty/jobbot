"""Cross-module safeguards added at the v2 integration: settings validation, truthfulness gate before any
automatic sending (offers, spontaneous e-mails, LBA relay), dashboard labels for the v2 data, weekly report in
« jobbot run »."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from jobbot import cli, cli_support, clock
from jobbot.apply import lba_recruiters, service
from jobbot.config import ConfigError, ReportSettings, Settings
from jobbot.dashboard import data
from jobbot.models import Application, ApplicationEvent, ApplyMethod, ContactEmail, JobStatus, TailoredDocuments
from jobbot.outreach import spontaneous
from jobbot.pacing import Pacer
from jobbot.preferences import set_value
from jobbot.reports.weekly import WeeklyReport, wants_email

from .fixtures.orchestration.fakes import FakeMailer, install_appliers, live, store_job, with_settings, write_docs

WARNING = "Compétence non justifiée : Rust"


def _untruthful(ctx, *, job_id=None, company_id=None) -> TailoredDocuments:
    docs = write_docs(ctx, job_id=job_id, company_id=company_id)
    flagged = docs.model_copy(update={"truth_warnings": [WARNING], "created_at": clock.now()})
    ctx.repo.save_documents(flagged)
    return flagged


# --------------------------------------------------------------------------- settings


def test_report_settings_defaults_and_bounds(project_root):
    assert ReportSettings().weekly_email is False
    with pytest.raises(ValidationError):
        ReportSettings(weekly_report_day=7)
    with pytest.raises(ValidationError):
        Settings.model_validate({"outreach": {"max_bounce_rate": 2}})
    with pytest.raises(ConfigError):
        set_value(project_root, "reports.weekly_report_day", "9")
    assert set_value(project_root, "reports.weekly_email", "true") is True


def test_wants_email_follows_setting(ctx):
    assert wants_email(ctx) is False
    on = with_settings(ctx, reports=ctx.settings.reports.model_copy(update={"weekly_email": True}))
    assert wants_email(on) is True


# --------------------------------------------------------------------------- truthfulness gate


def test_auto_mode_never_sends_a_cv_with_truth_warnings(ctx, make_job, monkeypatch):
    clean = store_job(ctx, make_job(external_id="a", company="Propre"), score=90)
    flagged = store_job(ctx, make_job(external_id="b", company="Douteuse"), score=95, docs=False)
    _untruthful(ctx, job_id=flagged.id)
    recorder = _Recorder()
    install_appliers(monkeypatch, {"email": recorder})
    report = service.apply_to_jobs(ctx)
    assert (report.applied, report.pending_review) == (1, 1)
    assert recorder.calls == [clean.id]
    held = ctx.repo.get_job(flagged.id)
    assert held.status == JobStatus.PENDING_REVIEW and "CV à vérifier" in held.raw["apply_detail"]
    assert "Rust" in held.raw["apply_detail"]
    assert ctx.repo.get_job(clean.id).status == JobStatus.APPLIED


def test_approved_job_with_warnings_is_the_users_decision(ctx, make_job):
    job = store_job(ctx, make_job(external_id="c"), status=JobStatus.APPROVED, score=30, docs=False)
    _untruthful(ctx, job_id=job.id)
    assert [j.id for j in service.select_jobs_to_apply(ctx)] == [job.id]
    assert service.truth_warnings(ctx, job) == [WARNING]


class _Recorder:
    channel = "email"
    fatal = None

    def __init__(self):
        self.calls = []

    def can_apply(self, job):
        return True

    def apply(self, job, docs):
        from .fixtures.orchestration.fakes import applied

        self.calls.append(job.id)
        return applied(ApplyMethod.EMAIL, email_to="rh@x.fr", mid="<x1@gmail.com>")


def test_spontaneous_campaign_holds_untruthful_documents(ctx, make_company, monkeypatch):
    company = make_company(status="enriched")
    ctx.repo.upsert_company(company)
    monkeypatch.setattr(spontaneous, "_prepare", lambda c, comp: _untruthful(c, company_id=comp.id))
    monkeypatch.setattr(spontaneous, "_build_pacer", lambda c: Pacer(c.repo, c.settings.limits, sleep=lambda s: None))
    mailer = FakeMailer()
    report = spontaneous.run_spontaneous_campaign(live(ctx), mailer)
    assert mailer.sent == [] and report.sent == 0
    stored = ctx.repo.get_company(company.id)
    assert stored.status == "pending_review" and stored.raw["review_contact"] == "recrutement@acme-logiciels.fr"
    assert any("CV à vérifier" in d for d in report.details)


def test_lba_relay_holds_untruthful_documents(ctx, make_company):
    company = make_company(source="lba_recruteur", status="enriched", raw={"hasEmail": True, "siret": "12345678900011"})
    ctx.repo.upsert_company(company)
    _untruthful(ctx, company_id=company.id)
    report = lba_recruiters.run_lba_recruiters(ctx)
    assert report.pending_review == 1 and report.applied == 0
    assert "CV à vérifier" in report.details[0]
    assert ctx.repo.get_company(company.id).status == "pending_review"


# --------------------------------------------------------------------------- dashboard labels (v2 data)


def test_company_row_shows_chosen_contact_kind_and_guess(make_company):
    guess = ContactEmail(email="julie.martin@acme.fr", confidence=0.35, source="pattern", kind="hr",
                         person_name="Julie Martin")
    generic = ContactEmail(email="contact@acme.fr", confidence=0.8, kind="generic")
    company = make_company(contacts=[generic, guess], status="pending_review",
                           raw={"review_contact": "julie.martin@acme.fr", "people": [{"name": "Julie Martin"}]},
                           description="Éditeur de logiciels à Metz.")
    row = data.company_row(company)
    assert row["contact"] == "julie.martin@acme.fr" and row["type_contact"] == "RH (nommé)"
    assert row["adresse_devinee"] is True and row["personnes_connues"] == 1
    assert "adresse devinée" in data.contact_label(row) and "Julie Martin" in data.contact_label(row)
    assert data.best_contact(company.model_copy(update={"raw": {}})).email == "julie.martin@acme.fr"
    tech = company.model_copy(update={"contacts": [ContactEmail(email="cto@acme.fr", kind="tech")], "raw": {}})
    assert data.company_row(tech)["type_contact"] == "Responsable technique"
    assert data.contact_label(data.company_row(company.model_copy(update={"contacts": [], "raw": {}}))) == "—"


def test_dashboard_review_and_companies_pages_show_v2_contacts(ctx, project_root, make_company, monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from jobbot import cli_tools

    guess = ContactEmail(email="julie.martin@acme.fr", source="pattern", kind="hr", person_name="Julie Martin")
    company = make_company(contacts=[guess], status="pending_review", raw={"review_contact": guess.email})
    ctx.repo.upsert_company(company)
    monkeypatch.setenv("JOBBOT_ROOT", str(project_root))
    at = AppTest.from_file(str(Path(cli_tools.DASHBOARD_APP)), default_timeout=60).run()
    at.sidebar.radio[0].set_value("À valider").run()
    assert not at.exception and not [e.value for e in at.error]
    assert any("adresse devinée non vérifiée" in m.value for m in at.markdown)
    at.sidebar.radio[0].set_value("Entreprises").run()
    assert not at.exception and not [e.value for e in at.error]
    at.sidebar.radio[0].set_value("À valider").run()
    at.button(key=f"cok-{company.id}").click().run()
    assert ctx.repo.get_company(company.id).status == "approved"


def test_timeline_uses_french_event_labels(ctx):
    app = ctx.repo.create_application(Application(method=ApplyMethod.EMAIL, applied_at=clock.now(),
                                                  company_name="Acme", company_id="c1"))
    ctx.repo.add_event(ApplicationEvent(application_id=app.id, at=clock.now(), kind="retry_after_bounce",
                                        detail="Nouvel essai"))
    assert data.timeline(ctx)[0]["evenement"] == "Nouvel essai (autre adresse)"


# --------------------------------------------------------------------------- Luxembourg places known offline


@pytest.mark.parametrize("location, kept, reason", [
    ("Kirchberg, Luxembourg", True, "3 km de Luxembourg"),
    ("Strassen, Luxembourg", True, "4 km de Luxembourg"),
    ("Ettelbruck, Luxembourg", False, "trop loin"),
])
def test_luxembourg_business_areas_get_a_distance_check(profile, location, kept, reason):
    from jobbot.matching.geo import lookup_known_place
    from jobbot.matching.match import match_job
    from jobbot.models import ContractType, JobOffer

    assert lookup_known_place(location) is not None
    job = JobOffer(id=location, source="free_work", external_id=location, title="Développeur Full-Stack junior (H/F)",
                   company="X", location=location, description="React, Node.js, TypeScript, PostgreSQL.",
                   contract_type=ContractType.CDI, found_at=clock.now())
    score, reasons = match_job(job, profile)
    assert (score > 0) is kept
    assert any(reason in r for r in reasons)


# --------------------------------------------------------------------------- CLI run prints the weekly report


def _weekly(path: str) -> WeeklyReport:
    now = clock.now()
    return WeeklyReport(week="2026-W39", start=datetime(2026, 9, 21, tzinfo=now.tzinfo), end=now, path=path,
                        offers=4, applications=2)


@pytest.mark.parametrize("weekly, expected", [
    (_weekly("output/rapports/semaine-2026-39.md"), "Semaine 39 : 4 offre(s)"),
    ("Erreur : disque plein", "disque plein"),
])
def test_run_prints_weekly_report(ctx, project_root, monkeypatch, weekly, expected):
    from jobbot import pipeline

    monkeypatch.setattr(cli_support, "build_app_context", lambda root, dry_run: ctx)
    monkeypatch.setattr(pipeline, "run_daily", lambda c, steps=None: pipeline.DailyReport(weekly=weekly))
    monkeypatch.chdir(project_root)
    res = CliRunner().invoke(cli.app, ["--root", str(project_root), "run", "--steps", "score"])
    cli_support.teardown_logging()
    assert res.exit_code == 0, res.output
    assert "Rapport hebdomadaire" in res.output and expected in res.output


def test_status_shows_guessed_addresses_used_today(ctx, project_root, monkeypatch):
    from jobbot.outreach.selection import record_unverified

    record_unverified(ctx.repo, "julie.martin@acme.fr")
    monkeypatch.setattr(cli_support, "build_app_context", lambda root, dry_run: ctx)
    monkeypatch.chdir(project_root)
    res = CliRunner().invoke(cli.app, ["--root", str(project_root), "status"])
    cli_support.teardown_logging()
    assert res.exit_code == 0, res.output
    line = next(li for li in res.output.splitlines() if "adresses devinées" in li)
    assert "1" in line and str(ctx.settings.outreach.max_unverified_per_day) in line


def test_run_without_weekly_report_prints_nothing_extra(ctx, project_root, monkeypatch):
    from jobbot import pipeline

    monkeypatch.setattr(cli_support, "build_app_context", lambda root, dry_run: ctx)
    monkeypatch.setattr(pipeline, "run_daily", lambda c, steps=None: pipeline.DailyReport())
    monkeypatch.chdir(project_root)
    res = CliRunner().invoke(cli.app, ["--root", str(project_root), "run"])
    cli_support.teardown_logging()
    assert res.exit_code == 0 and "Rapport hebdomadaire" not in res.output
