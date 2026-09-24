"""Interview prep sheets + weekly report (jobbot.reports) and their hooks: inbox, pipeline, CLI, dashboard.

No network: FakeLLM, fake IMAP server, fake mailer. Dates are computed relative to the real clock.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobbot import cli, cli_support, clock, pipeline
from jobbot.config import Secrets
from jobbot.llm import FakeLLM
from jobbot.mail.messages import InboundMessage
from jobbot.models import Application, ApplicationEvent, ApplicationStatus, ApplyMethod, JobStatus
from jobbot.reports import interview, weekly
from jobbot.reports._logistics import extract_logistics

from .fixtures.orchestration.fakes import FakeMailer, store_job

FIX = Path(__file__).parent / "fixtures"
INVITE = ("Bonjour Jean,\n\nMerci pour votre candidature. Seriez-vous disponible pour un entretien en visio "
          "mardi 29 septembre à 10h ? Lien : https://meet.google.com/abc-defg-hij\n\nBien cordialement,\nAmélie")
runner = CliRunner()


# --------------------------------------------------------------------------- helpers


def with_reports(ctx, **updates):
    reports = ctx.settings.reports.model_copy(update=updates)
    return dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={"reports": reports}))


def make_app(ctx, **overrides) -> Application:
    base = dict(method=ApplyMethod.EMAIL, applied_at=clock.now() - timedelta(days=3), company_name="Acme Logiciels",
                job_title="Développeur Full-Stack", email_to="rh@acme-logiciels.fr")
    base.update(overrides)
    return ctx.repo.create_application(Application(**base))


def invitation(body: str = INVITE, **overrides) -> InboundMessage:
    base = dict(uid=1, folder="[Gmail]/Tous les messages", message_id="<r1@acme-logiciels.fr>",
                from_addr="amelie.durand@acme-logiciels.fr", from_name="Amélie Durand",
                subject="RE : Candidature", date=clock.now(), body_text=body)
    base.update(overrides)
    return InboundMessage(**base)


def job_with_app(ctx, make_job, **job_overrides):
    job = store_job(ctx, make_job(**job_overrides), status=JobStatus.APPLIED)
    return job, make_app(ctx, job_id=job.id, company_name=job.company, job_title=job.title)


@pytest.fixture
def notes(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(interview, "notify", lambda c, title, message: calls.append((title, message)))
    monkeypatch.setattr(weekly, "notify", lambda c, title, message: calls.append((title, message)))
    return calls


def last_full_week() -> tuple[date, datetime, datetime]:
    """(sunday, start, end) of the last complete ISO week, whatever today is."""
    sunday = weekly.previous_week_end()
    assert sunday.weekday() == 6 and sunday < clock.to_paris(clock.now()).date()
    return (sunday, *weekly.week_bounds(sunday))


# --------------------------------------------------------------------------- logistics


def test_extract_logistics_visio_invitation():
    found = extract_logistics(INVITE, contact="Amélie Durand <amelie.durand@acme-logiciels.fr>")
    assert found.date == "mardi 29 septembre" and found.time == "10h" and found.mode == "visio"
    assert found.link == "https://meet.google.com/abc-defg-hij" and found.place is None
    assert "Amélie Durand" in found.contact and found.found()
    assert found.short() == "mardi 29 septembre · 10h · visio"


def test_extract_logistics_on_site_and_phone():
    text = ("Nous vous proposons un rendez-vous le 02/10/2026 à 14 h 30 dans nos locaux au 12 rue des Jardins, "
            "57000 Metz. Mon numéro : 03 87 12 34 56. Pour info vous travaillerez 35h par semaine.")
    found = extract_logistics(text, exclude_phones=("+33 6 12 34 56 78",))
    assert found.date == "02/10/2026" and found.time == "14h30" and found.mode == "sur place"
    assert found.place == "12 rue des Jardins, 57000 Metz" and found.phone == "03 87 12 34 56"
    phone_call = extract_logistics("Je vous propose un entretien téléphonique demain à 9 heures. 06 12 34 56 78",
                                   exclude_phones=("+33 6 12 34 56 78",))
    assert (phone_call.date, phone_call.time, phone_call.mode, phone_call.phone) == ("demain", "9h", "téléphone", None)
    empty = extract_logistics("")
    assert not empty.found() and empty.short() == ""


# --------------------------------------------------------------------------- interview prep sheets


def test_prep_template_fallback_has_every_section(ctx, make_job, notes):
    job, app = job_with_app(ctx, make_job, description=make_job().description + " Kubernetes apprécié.")
    path = interview.build_interview_prep(ctx, app, message=invitation())
    today = clock.to_paris(clock.now()).date().isoformat()
    assert path == ctx.output_dir / "entretiens" / f"{today}-acme-logiciels.md"
    text = path.read_text(encoding="utf-8")
    for heading in ("## Logistique", "## L'entreprise", "## Ce que le poste demande", "## Tes expériences",
                    "## Questions probables", "## Questions à poser", "## Check-list"):
        assert heading in text, heading
    assert "mardi 29 septembre" in text and "https://meet.google.com/abc-defg-hij" in text
    assert "ACME INDUSTRIE" in text and "**Situation**" in text and "**Résultat**" in text
    assert "Kubernetes" in text.split("## Tes expériences")[0]  # listed as « à réviser », never claimed
    assert "Kubernetes" not in text.split("## Tes expériences")[1].split("## Questions probables")[0]
    assert "3 semaines en entreprise / 1 semaine en formation" in text  # alternance questions
    assert "modèle de secours" in text
    stored = json.loads(ctx.repo.kv_get(f"{interview.PREP_KV_PREFIX}{app.id}"))
    assert stored["path"] == str(path) and "10h" in stored["logistics"]
    assert ctx.repo.list_events(app.id) == []  # no event: the inbox already logged the interview
    assert notes and "Acme Logiciels" in notes[0][0] and "mardi 29 septembre" in notes[0][1]


def _llm_answer(profile_company: str = "ACME INDUSTRIE") -> str:
    return json.dumps({
        "company_summary": "Acme Logiciels édite des logiciels de gestion pour les PME du Grand Est.",
        "role_needs": ["Développer des fonctionnalités React / Node.js", "Travailler en méthode agile"],
        "star": [
            {"experience": f"Développeur Full-Stack — {profile_company}",
             "situation": "Stage de fin d'études chez ACME INDUSTRIE.",
             "task": "Concevoir seul une application intranet Next.js.",
             "action": "Modélisation PostgreSQL avec Prisma, API REST, gestion des droits.",
             "result": "Application mise en production (Docker) et utilisée quotidiennement."},
            {"experience": "Ingénieur DevOps — Amazon", "situation": "Chez Amazon pendant 10 ans.",
             "task": "Gérer Kubernetes.", "action": "Déploiements Kubernetes.", "result": "99,99 % de disponibilité."},
            {"experience": "Développeur Web — Indépendant", "situation": "Freelance depuis 2025.",
             "task": "Sites pour des TPE.", "action": "Déploiement sur Kubernetes.", "result": "Clients satisfaits."},
        ],
        "questions": [
            {"category": "technique", "question": "Pourquoi Next.js ?", "answer": "Je l'ai utilisé chez ACME INDUSTRIE."},
            {"category": "technique", "question": "Connaissez-vous Kubernetes ?",
             "answer": "Pas encore en projet : j'utilise Docker et j'apprendrais vite."},
            {"category": "alternance", "question": "Votre rythme ?", "answer": "3 semaines en entreprise."},
            {"category": "comportement", "question": "Un défaut ?", "answer": "J'ai géré 50 développeurs."},
            {"category": "motivation", "question": "Pourquoi nous ?", "answer": "Votre produit m'intéresse."},
            {"category": "comportement", "question": "Travail en équipe ?", "answer": "Équipe de 3-4 développeurs."},
        ],
        "questions_to_ask": ["Qui sera mon tuteur ?", "Quelle est la stack ?", "Prochaines étapes ?"],
    }, ensure_ascii=False)


def test_prep_with_llm_keeps_only_truthful_content(ctx, make_job, notes):
    llm = FakeLLM(lambda prompt, system, fast: _llm_answer())
    job, app = job_with_app(ctx, make_job)
    path = interview.build_interview_prep(dataclasses.replace(ctx, llm=llm), app, message=invitation())
    text = path.read_text(encoding="utf-8")
    assert llm.calls and llm.calls[0][2] is False  # quality model
    assert "PME du Grand Est" in text and "Travailler en méthode agile" in text
    assert "Concevoir seul une application intranet Next.js." in text
    assert "Amazon" not in text and "99,99" not in text and "50 développeurs" not in text
    assert "Déploiement sur Kubernetes" not in text  # real experience, but an unbacked skill claim
    assert "Pourquoi Next.js ?" in text and "Qui sera mon tuteur ?" in text and "IA vérifiée" in text
    assert "### 2." in text  # template STAR points complete the list


def test_prep_llm_failure_and_spontaneous_application(ctx, make_company, notes):
    company = make_company(description="Agence web messine spécialisée en React et Symfony.", naf_label="Programmation")
    ctx.repo.upsert_company(company)
    app = make_app(ctx, company_id=company.id, job_title="", method=ApplyMethod.EMAIL)
    broken = dataclasses.replace(ctx, llm=FakeLLM([]))  # LLMError on the first call
    path = interview.build_interview_prep(broken, app)
    text = path.read_text(encoding="utf-8")
    assert "Agence web messine" in text and "https://www.acme-logiciels.fr" in text
    assert "Aucune information logistique" in text and "Candidature spontanée" in text


def test_prep_reuses_stored_invitation_and_rows(ctx, make_job, notes):
    job, app = job_with_app(ctx, make_job)
    interview.build_interview_prep(ctx, app, message=invitation())
    ctx.repo.update_application(app.model_copy(update={"status": ApplicationStatus.INTERVIEW}))
    message = interview.generate_prep(ctx, app.id)  # CLI / dashboard path: no e-mail passed
    assert "Fiche d'entretien" in message
    text = interview.prep_path(ctx, app.id).read_text(encoding="utf-8")
    assert "mardi 29 septembre" in text
    rows = interview.interview_rows(ctx)
    assert rows[0]["id"] == app.id and rows[0]["statut"] == "Entretien" and rows[0]["fiche"].endswith(".md")
    assert "10h" in rows[0]["logistique"]
    assert "introuvable" in interview.generate_prep(ctx, 9999)


def test_prep_on_interview_setting_and_errors(ctx, make_job, notes, monkeypatch):
    job, app = job_with_app(ctx, make_job)
    assert interview.prep_on_interview(with_reports(ctx, interview_prep=False), app, invitation()) is None

    def boom(*a, **k):
        raise RuntimeError("disque plein")

    monkeypatch.setattr(interview, "build_interview_prep", boom)
    assert interview.prep_on_interview(ctx, app, invitation()) is None


def _imap_fakes():
    spec = importlib.util.spec_from_file_location("jobbot_reports_imap_fakes", FIX / "mail" / "imap_fakes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inbox_interview_reply_writes_prep_sheet(ctx, make_job, notes, monkeypatch):
    from jobbot.mail import inbox

    fakes = _imap_fakes()
    monkeypatch.setattr(inbox, "notify", lambda *a: None)
    mail_ctx = dataclasses.replace(ctx, secrets=Secrets(_env_file=None, smtp_user=fakes.ME,
                                                        smtp_password="abcd efgh ijkl mnop"))
    first = "<179016435516.26396.1.jobbot@gmail.com>"
    job, app = job_with_app(mail_ctx, make_job)
    mail_ctx.repo.update_application(app.model_copy(update={"email_message_id": first, "email_thread_ids": [first]}))
    raw = (FIX / "mail" / "human_reply_interview.eml").read_bytes()
    report = inbox.process_inbox(mail_ctx, imap_factory=lambda h, p: fakes.gmail({50: (raw, "900")}))
    assert report.matched == 1 and "fiche d'entretien" in report.updates[0]
    sheet = interview.prep_path(mail_ctx, app.id)
    assert sheet is not None and "mardi 29 septembre" in sheet.read_text(encoding="utf-8")
    off = with_reports(mail_ctx, interview_prep=False)
    assert inbox._interview_prep(off, app, invitation()) == ""


# --------------------------------------------------------------------------- weekly report


def test_week_bounds_and_labels():
    sunday = date(2026, 9, 27)
    start, end = weekly.week_bounds(sunday, now=datetime(2026, 10, 5, tzinfo=UTC))
    assert clock.to_paris(start) == datetime(2026, 9, 21, tzinfo=clock.PARIS)
    assert clock.to_paris(end) == datetime(2026, 9, 28, tzinfo=clock.PARIS)
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    assert weekly.week_bounds(None, now=now) == (start, now)  # current week, up to now
    assert weekly.week_label(sunday) == "2026-W39" and weekly.report_path_name(sunday) == "semaine-2026-39.md"
    with pytest.raises(ValueError, match="future"):
        weekly.week_bounds(date(2026, 12, 1), now=now)


def _seed_week(ctx, make_job, start: datetime) -> None:
    """4 offers found in the week (+1 older), 2 applications sent in it, 2 answers, 1 follow-up due soon."""
    at, old = start + timedelta(days=1, hours=10), start - timedelta(days=10)
    alpha = store_job(ctx, make_job(external_id="a", company="Alpha", found_at=at), status=JobStatus.APPLIED)
    store_job(ctx, make_job(external_id="b", source="lba", company="Beta", found_at=at), status=JobStatus.FILTERED_OUT)
    store_job(ctx, make_job(external_id="m", source="wttj", company="Manu", found_at=at), status=JobStatus.NEEDS_MANUAL)
    store_job(ctx, make_job(external_id="o", company="Old", found_at=old), status=JobStatus.APPLIED)
    store_job(ctx, make_job(external_id="r", company="Review", found_at=at), status=JobStatus.PENDING_REVIEW)
    make_app(ctx, job_id=alpha.id, company_name="Alpha", job_title=alpha.title, applied_at=at)
    gamma = make_app(ctx, company_name="Gamma", job_title="", method=ApplyMethod.LBA, applied_at=at,
                     status=ApplicationStatus.INTERVIEW, last_response_at=at + timedelta(hours=5))
    ctx.repo.add_event(ApplicationEvent(application_id=gamma.id, at=at + timedelta(hours=5), kind="interview",
                                        detail="Entretien (rh@gamma.fr) : visio jeudi"))
    delta = make_app(ctx, company_name="Delta", applied_at=old, status=ApplicationStatus.REJECTED, last_response_at=at)
    ctx.repo.add_event(ApplicationEvent(application_id=delta.id, at=at, kind="rejected", detail="Refus"))
    make_app(ctx, company_name="Epsilon", applied_at=old, next_followup_at=clock.now() + timedelta(days=2))


def test_weekly_report_content_and_counts(ctx, make_job, notes):
    sunday, start, end = last_full_week()
    _seed_week(ctx, make_job, start)
    report = weekly.build_weekly_report(ctx, sunday)
    path = Path(report.path)
    assert path == ctx.output_dir / "rapports" / weekly.report_path_name(sunday)
    assert report.offers == 4 and report.offers_per_source == {"france_travail": 2, "lba": 1, "wttj": 1}
    assert report.applications == 2 and report.applications_per_channel == {"E-mail": 1, "La Bonne Alternance": 1}
    assert (report.responses, report.interviews) == (2, 1)
    assert report.pending_manual == 2 and report.upcoming_followups == 1 and not report.emailed
    text = path.read_text(encoding="utf-8")
    assert "# Rapport hebdomadaire" in text and "Jean DUPONT" in text
    assert "## Justificatif de recherche d'emploi" in text and "| Alpha |" in text and "| Gamma |" in text
    assert "Candidature spontanée" in text and "Delta" in text and "Epsilon" in text and "Manu" in text
    assert "| Old |" not in text.split("## Justificatif")[1]
    assert "| E-mail | 1 | 3 | 1 | 33 % |" in text  # Alpha (no answer), Delta (refus), Epsilon (en attente)
    assert "| La Bonne Alternance | 1 | 1 | 1 | 100 % |" in text
    assert notes[0][0].startswith("Rapport hebdomadaire") and report.summary() in notes[0][1]
    assert report.summary().startswith(f"Semaine {sunday.isocalendar().week} : 4 offre(s), 2 candidature(s)")
    assert report.week == weekly.week_label(sunday) and report.start == start and report.end == end


def test_weekly_report_email_to_candidate(ctx, make_job, notes, monkeypatch):
    sunday, start, _ = last_full_week()
    mailer = FakeMailer(dry_run=True)
    report = weekly.build_weekly_report(ctx, sunday, send_email=True, mailer=mailer)
    assert report.emailed and mailer.sent[0].kind == "report" and mailer.sent[0].to == ctx.profile.identity.email
    assert "Rapport hebdomadaire" in mailer.sent[0].body and "[simulation]" in report.details[-1]
    from jobbot.mail.mailer import MailError

    failing = weekly.build_weekly_report(ctx, sunday, send_email=True, mailer=FakeMailer(error=MailError("refusé")))
    assert not failing.emailed and "refusé" in failing.details[-1]
    monkeypatch.setattr("jobbot.mail.mailer.build_mailer", lambda c: None)
    no_smtp = weekly.build_weekly_report(dataclasses.replace(ctx, dry_run=False), sunday, send_email=True)
    assert not no_smtp.emailed and "SMTP" in no_smtp.details[-1]


def test_mailer_accepts_report_kind(ctx, tmp_path):
    from jobbot.mail.mailer import Mailer, OutgoingEmail

    secrets = Secrets(_env_file=None, smtp_user="jean.dupont@example.com", smtp_password="x")
    mailer = Mailer(secrets, ctx.repo, ctx.profile, dry_run=True, outbox_dir=tmp_path)
    sent = mailer.send(OutgoingEmail(to="jean.dupont@example.com", subject="Rapport", body="Bilan", kind="report"))
    assert sent.kind == "report" and sent.dry_run and list(tmp_path.glob("*-report-*.eml"))


def test_weekly_due_rules(ctx):
    monday = date(2026, 9, 28)
    assert weekly.weekly_report_due(ctx, monday) == date(2026, 9, 27)
    assert weekly.weekly_report_due(ctx, monday + timedelta(days=2)) == date(2026, 9, 27)  # catch-up
    assert weekly.weekly_report_due(ctx, monday + timedelta(days=3)) is None  # too late
    friday = with_reports(ctx, weekly_report_day=4)
    assert weekly.weekly_report_due(friday, date(2026, 10, 2)) == date(2026, 10, 2)
    assert weekly.weekly_report_due(with_reports(ctx, weekly_report=False), monday) is None
    assert weekly.weekly_report_due(with_reports(ctx, weekly_report_day=9), monday) is None
    ctx.repo.kv_set(weekly.DONE_KV_PREFIX + "2026-W39", "x")
    assert weekly.weekly_report_due(ctx, monday) is None


def test_scheduled_report_runs_once_per_week(ctx, notes, monkeypatch):
    sunday, _, _ = last_full_week()
    monkeypatch.setattr(weekly, "_today", lambda: sunday + timedelta(days=1))
    mailers: list[FakeMailer] = []

    def factory() -> FakeMailer:
        mailers.append(FakeMailer(dry_run=True))
        return mailers[-1]

    report = weekly.scheduled_weekly_report(ctx, mailer_factory=factory)
    assert report is not None and report.week == weekly.week_label(sunday)
    assert mailers == [] and not report.emailed  # reports.weekly_email is off by default
    assert weekly.scheduled_weekly_report(ctx, mailer_factory=factory) is None  # once per week
    ctx.repo.kv_set(weekly.DONE_KV_PREFIX + report.week, "")
    monkeypatch.setattr(weekly, "wants_email", lambda c: True)
    mailed = weekly.scheduled_weekly_report(ctx, mailer_factory=factory)
    assert mailed.emailed and mailers[0].sent[0].kind == "report"


def test_pipeline_runs_weekly_after_the_daily_summary(ctx, monkeypatch):
    sunday, _, _ = last_full_week()
    monkeypatch.setattr(weekly, "_today", lambda: sunday + timedelta(days=1))
    order: list[str] = []
    monkeypatch.setattr("jobbot.notify.notify", lambda c, title, msg: order.append(title))
    monkeypatch.setattr(weekly, "notify", lambda c, title, msg: order.append(title))
    report = pipeline.run_daily(ctx, steps=[])
    assert isinstance(report.weekly, weekly.WeeklyReport) and report.summary() == "Rien à signaler."
    assert order[0].startswith("jobbot — bilan") and "hebdomadaire" in order[1]
    assert pipeline.run_daily(ctx, steps=[]).weekly is None  # already done this week

    def boom(c, **k):
        raise RuntimeError("base verrouillée")

    monkeypatch.setattr(weekly, "scheduled_weekly_report", boom)
    assert pipeline.run_daily(ctx, steps=[]).weekly == "Erreur : base verrouillée"


# --------------------------------------------------------------------------- CLI & dashboard


@pytest.fixture
def run_cli(ctx, project_root, monkeypatch):
    monkeypatch.setattr(cli_support, "build_app_context", lambda root, dry_run: ctx)
    monkeypatch.chdir(project_root)

    def invoke(*args: str):
        return runner.invoke(cli.app, ["--root", str(project_root), *args])

    yield invoke
    cli_support.teardown_logging()


def test_cli_prep_and_report(run_cli, ctx, make_job, notes):
    job, app = job_with_app(ctx, make_job)
    res = run_cli("prep", str(app.id))
    assert res.exit_code == 0, res.output
    assert "entretiens" in res.output and interview.prep_path(ctx, app.id).is_file()
    assert run_cli("prep", "4242").exit_code == 1
    res = run_cli("report", "--precedente")
    assert res.exit_code == 0, res.output
    assert "semaine-" in res.output and "Semaine" in res.output
    assert run_cli("report", "--date", "pas-une-date").exit_code == 1
    assert run_cli("report", "--date", "2999-01-01").exit_code == 1


def test_dashboard_interviews_page(ctx, make_job, project_root, monkeypatch, notes):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from jobbot import cli_tools

    job, app = job_with_app(ctx, make_job)
    ctx.repo.update_application(app.model_copy(update={"status": ApplicationStatus.INTERVIEW}))
    monkeypatch.setenv("JOBBOT_ROOT", str(project_root))
    at = AppTest.from_file(str(Path(cli_tools.DASHBOARD_APP)), default_timeout=60).run()
    at.sidebar.radio[0].set_value("Entretiens").run()
    assert not at.exception and not [e.value for e in at.error]
    at.button(key=f"prep-{app.id}").click().run()
    assert not at.exception and interview.prep_path(ctx, app.id) is not None
    assert any("Fiche d'entretien" in s.value for s in at.success)
    assert at.button(key=f"prep-{app.id}").label == "Régénérer la fiche"
