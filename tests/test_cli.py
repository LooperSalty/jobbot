"""CLI (Typer) smoke tests for every command with fakes, the setup wizard, config commands, and the dashboard."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobbot import cli, cli_admin, cli_support, cli_tools
from jobbot.config import Secrets, load_profile, load_settings
from jobbot.models import ApplyMethod, ATSReport, JobStatus, KeywordHit
from jobbot.preferences import is_setup_completed

from .conftest import PROJECT_ROOT
from .fixtures.orchestration.fakes import FakeBrowser, FakeSMTP, live, result, store_job, write_docs

runner = CliRunner()
pytestmark = pytest.mark.filterwarnings("ignore::getpass.GetPassWarning")  # hidden input read from CliRunner stdin


@pytest.fixture
def run_cli(ctx, project_root, monkeypatch):
    """invoke(*args, input=None, app_ctx=None) with the test AppContext injected."""
    state = {"ctx": ctx}
    monkeypatch.setattr(cli_support, "build_app_context", lambda root, dry_run: state["ctx"])
    monkeypatch.chdir(project_root)

    def invoke(*args: str, input: str | None = None, app_ctx=None):
        if app_ctx is not None:
            state["ctx"] = app_ctx
        return runner.invoke(cli.app, ["--root", str(project_root), *args], input=input)

    yield invoke
    cli_support.teardown_logging()


def _ok(res):
    assert res.exit_code == 0, res.output
    return res.output


# --------------------------------------------------------------------------- global behaviour & wizard


def test_help_never_runs_the_wizard(run_cli, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    out = _ok(run_cli("--help"))
    assert "assist" in out and "schedule" in out
    out = _ok(run_cli("apply", "--help"))
    assert "--channel" in out and "Configuration de jobbot" not in out


def test_first_run_wizard_on_tty_then_command(run_cli, project_root, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    out = _ok(run_cli("status", input="\n" * 60))
    assert "Configuration terminée" in out and "Offres" in out
    assert is_setup_completed(project_root)


def test_first_run_without_tty_continues(run_cli, project_root):
    out = _ok(run_cli("status"))
    assert "Configuration de jobbot" not in out and not is_setup_completed(project_root)


def test_setup_section_reasks_on_invalid_value(run_cli, project_root):
    answers = "cdi, job\nalternance, cdi, stage\n" + "\n" * 20
    out = _ok(run_cli("setup", "--section", "recherche", input=answers))
    assert "valeurs inconnues" in out
    profile = load_profile(project_root / "config" / "profile.yaml")
    assert [c.value for c in profile.target.contract_types] == ["alternance", "cdi", "stage"]
    assert is_setup_completed(project_root)


def test_setup_full_with_secret_and_changes(run_cli, project_root):
    shutil.copy(PROJECT_ROOT / ".env.example", project_root / ".env.example")
    # Recherche (12) → defaults; Automatisation: mode review, warm-up 5, rest default; …; Comptes: SMTP password;
    # Offres de l'école (Classroom, 4) → defaults.
    answers = ("\n" * 12 + "review\n5\n" + "\n" * 6 + "\n" * 5 + "\n" * 3 + "\n" * 4 + "abcd efgh ijkl mnop\n"
               + "\n" * 5 + "\n" * 4)
    out = _ok(run_cli("setup", input=answers))
    assert "abcd" not in out
    settings = load_settings(project_root)
    assert settings.mode == "review" and settings.warmup_days == 5 and settings.setup_completed
    assert "SMTP_PASSWORD=abcd efgh ijkl mnop" in (project_root / ".env").read_text(encoding="utf-8")


def test_setup_errors(run_cli):
    assert run_cli("setup", "--section", "inexistante").exit_code == 1
    res = run_cli("setup", input="")
    assert res.exit_code == 1 and "interrompu" in res.output


def test_config_commands(run_cli, project_root):
    assert "limits.linkedin_per_day" in _ok(run_cli("config", "show", "Automatisation"))
    assert _ok(run_cli("config", "get", "limits.linkedin_per_day")).strip() == "15"
    _ok(run_cli("config", "set", "limits.linkedin_per_day", "9"))
    assert load_settings(project_root).limits.linkedin_per_day == 9
    assert "[simulation]" in _ok(run_cli("--dry-run", "config", "set", "limits.linkedin_per_day", "3"))
    assert load_settings(project_root).limits.linkedin_per_day == 9
    bad = run_cli("config", "set", "mode", "yolo")
    assert bad.exit_code == 1 and "Erreur" in bad.output
    _ok(run_cli("config", "set", "loc.nancy", "non"))
    assert all(loc.city != "Nancy" for loc in load_profile(project_root / "config" / "profile.yaml").target.locations)
    out = _ok(run_cli("config", "set", "SMTP_PASSWORD", "secret-123"))
    assert "secret-123" not in out
    assert _ok(run_cli("config", "get", "SMTP_PASSWORD")).strip() == "✓"
    assert _ok(run_cli("config", "get", "profile.identity.first_name")).strip() == "Jean"
    assert run_cli("config", "get", "nope.key").exit_code == 1
    assert run_cli("config", "set", "loc.metz_radius", "abc").exit_code == 1


# --------------------------------------------------------------------------- init, doctor, import-cv, login


def test_init_creates_folders(run_cli, project_root):
    shutil.copy(PROJECT_ROOT / ".env.example", project_root / ".env.example")
    out = _ok(run_cli("init"))
    assert (project_root / ".env").exists() and (project_root / "output").is_dir()
    assert "Étapes suivantes" in out and "Base de données prête" in out
    env_text = (project_root / ".env").read_text(encoding="utf-8")
    assert "HUNTER_API_KEY=\n" in env_text and Secrets(_env_file=project_root / ".env").hunter_api_key in (None, "")
    assert cli_admin.sanitized_env("A=1\nB_KEY=   # optionnel\n") == "A=1\n# optionnel\nB_KEY=\n"


def test_init_copies_the_fictitious_example_profile_when_missing(run_cli, project_root):
    example = PROJECT_ROOT / cli_admin.PROFILE_EXAMPLE
    profile = project_root / "config" / "profile.yaml"
    profile.unlink()
    shutil.copy(example, project_root / cli_admin.PROFILE_EXAMPLE)
    out = " ".join(_ok(run_cli("init")).split())  # Rich may wrap long lines
    assert profile.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")
    assert "profil FICTIF" in out and "jobbot import-cv" in out
    profile.write_text("identity: {first_name: Moi}\n", encoding="utf-8")
    assert "profil FICTIF" not in " ".join(_ok(run_cli("init")).split())
    assert profile.read_text(encoding="utf-8") == "identity: {first_name: Moi}\n"  # never overwritten


def test_init_profile_without_example(tmp_path):
    note = cli_admin.init_profile(tmp_path, "config/profile.yaml")
    assert note and "jobbot import-cv" in note and not (tmp_path / "config" / "profile.yaml").exists()


def test_example_profile_is_the_fictitious_test_profile(profile):
    assert load_profile(PROJECT_ROOT / cli_admin.PROFILE_EXAMPLE) == profile
    assert profile.identity.email.endswith("@example.com")


def test_doctor(run_cli, project_root, monkeypatch):
    monkeypatch.setattr(cli_admin, "chromium_path", lambda: "chromium.exe")
    monkeypatch.setattr(cli_admin, "chrome_installed", lambda: False)
    monkeypatch.setattr(cli_admin, "check_sources", lambda c: [cli_admin.Check("Source lba", "ok", "disponible")])
    out = _ok(run_cli("doctor", "--smtp"))
    assert "Diagnostic" in out and "SMTP_PASSWORD" in out and "Chromium" in out
    (project_root / "config" / "settings.yaml").write_text("mode: yolo\n", encoding="utf-8")
    assert run_cli("doctor").exit_code == 1


def test_doctor_checks_units(ctx, monkeypatch):
    sources = cli_admin.check_sources(dataclasses.replace(ctx, settings=ctx.settings.model_copy(
        update={"search": ctx.settings.search.model_copy(update={"sources": ["lba", "france_travail", "nope"]})})))
    assert [c.level for c in sources] == ["ok", "warn", "warn"]
    secrets = Secrets(_env_file=None, smtp_user="jean.dupont@example.com", smtp_password="abcd efgh")
    assert cli_admin.check_smtp(secrets, FakeSMTP).level == "ok"

    class Refused(FakeSMTP):
        def login(self, user, password):
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad")

    assert cli_admin.check_smtp(secrets, Refused).level == "error"
    assert cli_admin.check_smtp(Secrets(_env_file=None), FakeSMTP).level == "warn"
    env_checks = cli_admin.check_env(Secrets(_env_file=None, hunter_api_key="# optionnel", smtp_user="a@b.fr"))
    hunter = next(c for c in env_checks if "HUNTER" in c.name)
    assert hunter.detail.startswith("absent")  # Secrets drops comment-only values
    assert next(c for c in env_checks if "SMTP_USER" in c.name).level == "ok"
    raw_values = dataclasses.make_dataclass("RawSecrets", [("hunter_api_key", str)])("# optionnel")
    hunter = next(c for c in cli_admin.check_env(raw_values) if "HUNTER" in c.name)
    assert "commentaire" in hunter.detail
    assert cli_admin.check_llm(ctx.settings.model_copy(update={"llm": ctx.settings.llm.model_copy(
        update={"provider": "anthropic"})}), Secrets(_env_file=None)).level == "error"
    monkeypatch.setattr(cli_admin, "chromium_path", lambda: None)
    assert cli_admin.check_browser(ctx.settings)[0].level == "error"
    assert cli_admin.check_database(ctx).level == "ok" and cli_admin.check_python().level == "ok"


def test_import_cv_and_login(run_cli, project_root, tmp_path, monkeypatch):
    import jobbot.apply.browser as browser
    import jobbot.documents.importer as importer

    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(importer, "import_cv", lambda path, llm: {"identity": {"first_name": "Jean"}})
    out = _ok(run_cli("import-cv", str(pdf)))
    assert (project_root / "config" / "profile.imported.yaml").exists() and "relis-le" in out
    assert run_cli("import-cv", str(pdf)).exit_code == 1
    monkeypatch.setattr(browser, "login", lambda c, site, timeout_s=600: site == "linkedin")
    assert "Connecté" in _ok(run_cli("login", "linkedin"))
    assert run_cli("login", "hellowork").exit_code == 1


# --------------------------------------------------------------------------- daily commands


def test_step_commands(run_cli, ctx, make_job, monkeypatch):
    import jobbot.apply.service as service
    import jobbot.followup as followup
    import jobbot.mail.inbox as inbox
    import jobbot.matching.match as match
    import jobbot.outreach.companies as companies
    import jobbot.outreach.emails as emails
    import jobbot.outreach.spontaneous as spontaneous
    import jobbot.pipeline as pipeline
    import jobbot.sources.registry as registry

    calls: dict[str, object] = {}
    monkeypatch.setattr(registry, "build_sources", lambda c, names=None: calls.setdefault("sources", names) and [])
    monkeypatch.setattr(registry, "collect_jobs", lambda c, sources=None: registry.CollectReport(
        new=3, per_source={"lba": 3}, errors={"linkedin": "bloqué"}))
    monkeypatch.setattr(match, "score_new_jobs", lambda c: match.MatchReport(scored=2, shortlisted=1))
    monkeypatch.setattr(pipeline, "prepare_shortlisted", lambda c, limit=10, job_ids=None: pipeline.PrepareReport(
        prepared=1, details=["Acme — Dev : CV ATS 82/100"]))
    monkeypatch.setattr(service, "apply_to_jobs", lambda c, **kw: calls.setdefault("apply", kw) and
                        service.ApplyReport(applied=1, details=["ok"]))
    monkeypatch.setattr(companies, "discover_companies", lambda c, limit=300: companies.DiscoverReport(found=4, new=2))
    monkeypatch.setattr(emails, "enrich_companies", lambda c, limit=50: emails.EnrichReport(
        processed=2, with_email=1, without_email=1))
    monkeypatch.setattr(spontaneous, "run_spontaneous_campaign", lambda c, m, limit=None: spontaneous.CampaignReport(
        sent=calls.setdefault("mailer_dry", m.dry_run) and 1, skipped=0, failed=0))
    monkeypatch.setattr(inbox, "process_inbox", lambda c: inbox.InboxReport(scanned=5, matched=1, updates=["Entretien"]))
    monkeypatch.setattr(followup, "run_followups", lambda c, m: followup.FollowupReport(sent=1, manual=0, closed=0))
    store_job(ctx, make_job(), status=JobStatus.SHORTLISTED, docs=False)
    assert "linkedin : bloqué" in _ok(run_cli("search", "--source", "lba"))
    assert calls["sources"] == ["lba"]
    assert "Meilleures offres retenues" in _ok(run_cli("score"))
    assert "CV ATS 82/100" in _ok(run_cli("prepare", "-n", "3"))
    out = _ok(run_cli("--dry-run", "apply", "-c", "email", "-n", "2", "-j", "x"))
    assert "Mode simulation" in out and calls["apply"] == {"channels": ["email"], "limit": 2, "job_ids": ["x"]}
    assert run_cli("apply", "-c", "fax").exit_code == 1
    for command, text in (("discover", "Trouvées"), ("enrich", "Avec e-mail"), ("spontaneous", "Envoyées"),
                          ("inbox", "Entretien"), ("followup", "Relances")):
        assert text in _ok(run_cli(command)), command
    assert calls["mailer_dry"] is True


def test_run_command(run_cli, monkeypatch):
    import jobbot.pipeline as pipeline

    monkeypatch.setattr(pipeline, "run_daily", lambda c, steps=None: pipeline.DailyReport(
        apply=__import__("jobbot.apply.service", fromlist=["x"]).ApplyReport(applied=2), inbox="Erreur : IMAP"))
    out = _ok(run_cli("run", "-s", "apply"))
    assert "2 candidature(s) envoyée(s)" in out and "Erreur : IMAP" in out

    def busy(c, steps=None):
        raise pipeline.PipelineBusy("déjà en cours")

    monkeypatch.setattr(pipeline, "run_daily", busy)
    assert run_cli("run").exit_code == 2
    monkeypatch.undo()


def test_run_rejects_unknown_step(run_cli, monkeypatch):
    monkeypatch.setattr("jobbot.notify.notify", lambda *a: None)
    res = run_cli("run", "--step", "nope")
    assert res.exit_code == 1 and "inconnue" in res.output


def test_ats_command(run_cli, ctx, make_job, tmp_path, monkeypatch):
    job = store_job(ctx, make_job(), docs=False)
    docs = write_docs(ctx, job.id)
    report = ATSReport(score=82, keyword_score=80, title_score=100, section_score=90, format_score=70,
                       matched=[KeywordHit(keyword="React", weight=1)], missing=[KeywordHit(keyword="Scrum", weight=0.5)],
                       warnings=["Photo détectée"])
    ctx.repo.save_documents(docs.model_copy(update={"cv_ats": report, "letter_ats": report,
                                                    "truth_warnings": ["Compétence absente : Rust"]}))
    out = _ok(run_cli("ats", job.id))
    assert "82/100" in out and "Scrum (0.5)" in out and "Rust" in out and "Photo" in out
    assert "introuvable" in _ok(run_cli("ats", "nope"))
    other = store_job(ctx, make_job(external_id="z"), docs=False)
    assert "Aucun document" in _ok(run_cli("ats", other.id))
    import jobbot.documents.render as render
    import jobbot.matching.keywords as keywords

    monkeypatch.setattr(render, "pdf_text", lambda p: "Développeur React Node.js")
    monkeypatch.setattr(keywords, "extract_keywords", lambda j, llm=None, **kw: [KeywordHit(keyword="React", weight=1)])
    cv = tmp_path / "autre.pdf"
    cv.write_bytes(b"%PDF")
    assert "CV autre.pdf" in _ok(run_cli("ats", job.id, "--cv", str(cv)))


def test_assist_records_answers(run_cli, ctx, make_job, monkeypatch):
    import jobbot.apply.assisted as assisted
    import jobbot.apply.browser as browser

    fake = FakeBrowser()
    monkeypatch.setattr(browser, "open_browser", fake.open)
    method = ApplyMethod.EXTERNAL
    jobs = [store_job(ctx, make_job(external_id=f"e{n}", company=f"C{n}", apply_method=method),
                      status=JobStatus.NEEDS_MANUAL, score=90 - n) for n in range(4)]
    outcomes = iter([result(JobStatus.NEEDS_MANUAL, "pré-rempli"), result(JobStatus.EXPIRED, "fermée"),
                     result(JobStatus.NEEDS_MANUAL, "pré-rempli"), result(JobStatus.NEEDS_MANUAL, "pré-rempli")])
    monkeypatch.setattr(assisted, "assisted_apply", lambda *a: next(outcomes))
    out = _ok(run_cli("assist", input="o\nn\nq\n", app_ctx=live(ctx)))
    statuses = [ctx.repo.get_job(j.id).status for j in jobs]
    assert statuses == [JobStatus.APPLIED, JobStatus.EXPIRED, JobStatus.SKIPPED, JobStatus.NEEDS_MANUAL]
    assert ctx.repo.application_for_job(jobs[0].id).next_followup_at is not None
    assert "1 envoyées" in out and fake.opened == fake.closed == 1
    outcomes = iter([result(JobStatus.NEEDS_MANUAL, "pré-rempli")])
    store_job(ctx, make_job(external_id="e9", company="C9", apply_method=method), status=JobStatus.NEEDS_MANUAL)
    assert "[simulation]" in _ok(run_cli("assist", "-n", "1", input="o\n", app_ctx=ctx))


def test_approve_reject(run_cli, ctx, make_job, make_company):
    first = store_job(ctx, make_job(external_id="a", company="A"), status=JobStatus.PENDING_REVIEW)
    second = store_job(ctx, make_job(external_id="b", company="Bad Corp"), status=JobStatus.PENDING_REVIEW)
    company = make_company(status="pending_review")
    ctx.repo.upsert_company(company)
    assert "Offres à valider" in _ok(run_cli("approve"))
    _ok(run_cli("reject", second.id, "--blacklist"))
    assert ctx.repo.get_job(second.id).status == JobStatus.SKIPPED and ctx.repo.is_blacklisted("bad corp")
    out = _ok(run_cli("approve", "--all"))
    assert ctx.repo.get_job(first.id).status == JobStatus.APPROVED and "validée" in out
    assert ctx.repo.get_company(company.id).status == "approved"
    assert "introuvable" in _ok(run_cli("reject", "nope"))


# --------------------------------------------------------------------------- status, export, dashboard, schedule


def test_status_and_export(run_cli, ctx, make_job, tmp_path):
    store_job(ctx, make_job(), status=JobStatus.PENDING_REVIEW)
    store_job(ctx, make_job(external_id="m"), status=JobStatus.NEEDS_MANUAL)
    out = _ok(run_cli("status"))
    assert "À valider" in out and "jobbot assist" in out and "linkedin" in out
    xlsx = tmp_path / "export.xlsx"
    _ok(run_cli("export", "-o", str(xlsx)))
    from openpyxl import load_workbook

    assert load_workbook(xlsx).sheetnames == ["offres", "candidatures", "entreprises"]
    _ok(run_cli("export", "--format", "csv", "-o", str(tmp_path / "csv")))
    text = (tmp_path / "csv" / "offres.csv").read_text(encoding="utf-8-sig")
    assert text.startswith("id;source;offre") and "Acme Logiciels" in text
    assert "(vide)" in (tmp_path / "csv" / "candidatures.csv").read_text(encoding="utf-8-sig")


def test_dashboard_command(run_cli, project_root, monkeypatch):
    seen = {}

    def fake_run(cmd, env=None, check=False):
        seen.update(cmd=cmd, env=env)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli_tools.subprocess, "run", fake_run)
    _ok(run_cli("dashboard", "--port", "8600"))
    assert "streamlit" in seen["cmd"] and "8600" in seen["cmd"] and seen["env"]["JOBBOT_ROOT"] == str(project_root)
    monkeypatch.setattr(cli_tools.importlib.util, "find_spec", lambda name: None)
    assert run_cli("dashboard").exit_code == 1


def test_schedule_commands(run_cli, project_root, monkeypatch):
    commands: list[list[str]] = []

    def fake(cmd):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0 if "/Delete" not in cmd else 1, stdout="SUCCÈS", stderr="")

    monkeypatch.setattr(cli_tools, "run_command", fake)
    out = _ok(run_cli("schedule", "install", "--matin", "08:15"))
    assert "Tâches jobbot : OK" in out and "SUCCÈS" in out
    install = commands[0]
    assert install[install.index("-File") + 1].endswith("install_task.ps1")
    assert install[install.index("-Matin") + 1] == "08:15" and install[install.index("-Soir") + 1] == "18:00"
    _ok(run_cli("schedule", "remove"))
    assert commands[-1][-1] == "-Supprimer"
    out = _ok(run_cli("schedule", "show"))
    assert commands[-1][:2] == ["schtasks", "/Query"] and "jobbot-soir : OK" in out
    assert "[simulation]" in _ok(run_cli("--dry-run", "schedule", "install"))
    assert run_cli("schedule", "install", "--soir", "25:00").exit_code == 1
    assert cli_tools._encoding(["schtasks"]) in ("oem", "utf-8") and cli_tools._encoding(["powershell.exe"]) == "utf-8"


def test_real_context_building(project_root, monkeypatch):
    import jobbot.llm as llm

    def no_llm(settings, secrets):
        raise llm.LLMError("claude absent")

    monkeypatch.setattr(llm, "build_llm", no_llm)
    built = cli_support.build_app_context(project_root, True)
    try:
        assert isinstance(built.llm, llm.DisabledLLM) and built.dry_run
    finally:
        built.repo.close()
    (project_root / "config" / "settings.yaml").write_text("mode: yolo\n", encoding="utf-8")
    res = runner.invoke(cli.app, ["--root", str(project_root), "status"])
    cli_support.teardown_logging()
    assert res.exit_code == 1 and "Configuration invalide" in res.output


def test_print_report_variants():
    console = cli_support.make_console()
    with console.capture() as cap:
        cli_support.print_report(console, "Étape", "Erreur : panne")
        cli_support.print_report(console, "Vide", None)
        from jobbot.apply.service import ApplyReport

        cli_support.print_report(console, "Candidatures", ApplyReport(details=[f"l{i}" for i in range(45)]))
    text = cap.get()
    assert "Erreur : panne" in text and "… et 5 autre(s)" in text


# --------------------------------------------------------------------------- dashboard data & UI


def test_dashboard_data(ctx, make_job, make_company, project_root):
    from jobbot.dashboard import data

    job = store_job(ctx, make_job(), status=JobStatus.PENDING_REVIEW)
    manual = store_job(ctx, make_job(external_id="m", company="M"), status=JobStatus.NEEDS_MANUAL)
    ctx.repo.upsert_company(make_company(status="pending_review"))
    assert data.kpis(ctx)["a_valider"] == 2 and data.pipeline_counts(ctx)
    assert data.job_rows(ctx)[0]["cv_pdf"].endswith(".pdf") and data.ats_summary(ctx, job.id) is None
    assert "validée" in data.approve(ctx, job.id) and "écartée" in data.reject(ctx, job.id)
    assert "enregistrée" in data.mark_applied(ctx, manual.id) and "introuvable" in data.mark_applied(ctx, "x")
    assert data.application_rows(ctx)[0]["entreprise"] == "M" and data.timeline(ctx)[0]["evenement"] == "Candidature envoyée"
    assert data.company_rows(ctx)[0]["statut"] == "À valider" and data.source_stats(ctx)[0]["offres"] == 2
    assert data.pending_companies(ctx) and data.file_bytes(None) is None
    assert data.manual_followups(ctx) == []
    sections = data.settings_sections()
    questions = sections["Automatisation"]
    answers = {**data.current_values(project_root, questions), "mode": "review", "warmup_days": 7}
    assert data.save_answers(project_root, questions, answers) == (2, [])
    assert load_settings(project_root).mode == "review"
    bad = {**answers, "mode": "yolo"}
    saved, errors = data.save_answers(project_root, questions, bad)
    assert saved == 0 and errors and "choisir parmi" in errors[0]


def test_dashboard_app_renders_every_page(ctx, make_job, project_root, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    store_job(ctx, make_job(), status=JobStatus.PENDING_REVIEW)
    store_job(ctx, make_job(external_id="m", company="M"), status=JobStatus.NEEDS_MANUAL)
    monkeypatch.setenv("JOBBOT_ROOT", str(project_root))
    app_path = Path(cli_tools.DASHBOARD_APP)
    at = AppTest.from_file(str(app_path), default_timeout=60).run()
    assert not at.exception
    for page in ("Offres", "À valider", "Actions manuelles", "Candidatures", "Entreprises", "Sources", "Réglages"):
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, page
        assert not [e.value for e in at.error], page
    at.sidebar.radio[0].set_value("À valider").run()
    at.button(key=f"ok-{make_job().id}").click().run()
    assert ctx.repo.get_job(make_job().id).status == JobStatus.APPROVED
