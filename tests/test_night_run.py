"""night.run — steps within the time budget, report, prepare ahead, GDPR purge, CLI / status / doctor / schedule."""

from __future__ import annotations

import dataclasses
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobbot import cli, cli_support, cli_tools, clock, cron
from jobbot.config import Settings
from jobbot.models import Application, ApplyMethod, ContactEmail, TailoredDocuments
from jobbot.night import budget, run
from jobbot.night.budget import Deadline, night_end
from jobbot.night.models import NightReport
from jobbot.night.prepare import prepare_ahead
from jobbot.night.purge import purge_personal_data
from jobbot.night.report import LAST_REPORT_KEY, load_last
from jobbot.night.run import StepOutcome, run_night
from jobbot.night.status import doctor_checks, queue_rows, status_lines

RECRUIT = ContactEmail(email="recrutement@exemple.fr", confidence=0.9, kind="recruitment")
NAMED = ContactEmail(email="lea.roux@exemple.fr", confidence=0.7, kind="hr", person_name="Léa ROUX")


class FakeClock:
    def __init__(self, start: datetime):
        self.at = start

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


@pytest.fixture
def fake_clock(monkeypatch):
    fake = FakeClock(clock.now())
    monkeypatch.setattr(budget, "_now", fake)
    return fake


@pytest.fixture
def notes(monkeypatch):
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr("jobbot.notify.notify", lambda c, title, message, level="info": seen.append(
        (title, message, level)))
    return seen


def _live(ctx):
    return dataclasses.replace(ctx, dry_run=False)


# --------------------------------------------------------------------------- time budget


def test_night_end():
    start = datetime(2026, 9, 27, 22, 30, tzinfo=clock.PARIS)  # late evening: until the next morning
    at_half_past_midnight = datetime(2026, 9, 28, 0, 30, tzinfo=clock.PARIS)
    assert night_end("06:30", start=at_half_past_midnight) == datetime(2026, 9, 28, 6, 30, tzinfo=clock.PARIS)
    assert night_end("06:30", start=start) == datetime(2026, 9, 28, 6, 30, tzinfo=clock.PARIS)
    afternoon = datetime(2026, 9, 28, 14, 0, tzinfo=clock.PARIS)
    assert night_end("06:30", start=afternoon) == afternoon + timedelta(hours=8)  # manual run: 8 h at most
    assert night_end("06:30", start=afternoon, budget_minutes=45) == afternoon + timedelta(minutes=45)


def test_deadline_share(fake_clock):
    deadline = Deadline(fake_clock() + timedelta(hours=4))
    assert deadline.share(0.25).end == fake_clock() + timedelta(hours=1)
    fake_clock.advance(hours=5)
    assert deadline.expired() and deadline.remaining_s() == 0


def _fake_steps(monkeypatch, fake_clock, calls, *, harvest_hours=0.0, fail=None):
    def step(name, fields):
        def run_step(ctx, deadline):
            calls.append(name)
            if name == fail:
                raise RuntimeError("API en panne")
            if name == "harvest":
                fake_clock.advance(hours=harvest_hours)
            return StepOutcome(fields)
        return run_step

    monkeypatch.setattr(run, "STEPS", {
        "purge": step("purge", {"purged": 1}),
        "harvest": step("harvest", {"companies_found": 120, "companies_new": 30, "relevant": 110, "irrelevant": 10}),
        "enrich": step("enrich", {"enriched": 40, "with_email": 25, "generic_emails": 20, "nominative_emails": 9}),
        "prioritize": step("prioritize", {"queue_ready": 87}),
        "prepare": step("prepare", {"docs_prepared": 25, "daily_cap": 20}),
    })


def test_run_night_runs_every_step_and_reports(ctx, monkeypatch, fake_clock, notes):
    calls: list[str] = []
    _fake_steps(monkeypatch, fake_clock, calls)
    live = _live(ctx)
    report = run_night(live, budget_minutes=60)
    assert calls == ["purge", "harvest", "enrich", "prioritize", "prepare"]
    assert not report.stopped_early and not report.errors
    expected = ("Nuit : +120 entreprises (110 pertinentes, 30 nouvelles), +29 adresses (dont 9 nominatives), "
                "25 dossiers prêts, file d'attente : 87 entreprises ≈ 5 jours d'envoi")
    assert report.summary() == expected
    assert notes == [("jobbot — nuit", expected, "important")]
    assert load_last(live.repo)["summary"] == expected
    assert not (live.root / "data" / run.LOCK_FILE).exists()


def test_time_budget_stops_cleanly(ctx, monkeypatch, fake_clock, notes):
    calls: list[str] = []
    _fake_steps(monkeypatch, fake_clock, calls, harvest_hours=3)
    report = run_night(_live(ctx), budget_minutes=120)
    assert calls == ["purge", "harvest"]
    assert report.stopped_early and report.companies_found == 120 and report.enriched == 0
    assert report.duration_min == 180.0
    assert any("non lancée (heure de fin atteinte)" in d for d in report.details)
    assert "arrêt à l'heure prévue" in report.summary()
    assert notes  # the report is sent anyway


def test_a_failing_step_never_stops_the_night(ctx, monkeypatch, fake_clock, notes):
    calls: list[str] = []
    _fake_steps(monkeypatch, fake_clock, calls, fail="harvest")
    report = run_night(_live(ctx), budget_minutes=60)
    assert calls == ["purge", "harvest", "enrich", "prioritize", "prepare"]
    assert report.errors == ["Collecte des entreprises : API en panne"]
    assert report.enriched == 40 and "1 étape(s) en erreur" in report.summary()


def test_dry_run_keeps_no_report(ctx, monkeypatch, fake_clock, notes):
    _fake_steps(monkeypatch, fake_clock, [])
    report = run_night(ctx, budget_minutes=10)
    assert report.dry_run and ctx.repo.kv_get(LAST_REPORT_KEY) is None
    assert notes[0][0] == "jobbot — nuit (simulation)"


def test_real_steps_on_an_empty_database_with_zero_budget(ctx, notes):
    report = run_night(_live(ctx), budget_minutes=0)
    assert report.stopped_early and report.companies_found == 0
    assert report.daily_cap == 20 and report.queue_ready == 0


def test_report_text_without_cap():
    report = NightReport(companies_found=3, relevant=2, companies_new=1, queue_ready=4, daily_cap=0)
    assert report.summary().endswith("file d'attente : 4 entreprises")
    assert NightReport(queue_ready=20, daily_cap=20).queue_days == 1
    assert NightReport(queue_ready=0, daily_cap=20).summary().endswith("file d'attente : 0 entreprises")


# --------------------------------------------------------------------------- prepare ahead


@pytest.fixture
def fake_documents(monkeypatch, tmp_path):
    prepared: list[str] = []

    def prepare_documents(ctx, *, job=None, company=None):
        prepared.append(company.name)
        cv = tmp_path / f"{company.id}.pdf"
        cv.write_bytes(b"%PDF-1.4")
        docs = TailoredDocuments(company_id=company.id, cv_pdf=str(cv), created_at=clock.now())
        ctx.repo.save_documents(docs)
        return docs

    module = types.ModuleType("jobbot.documents.service")
    module.prepare_documents = prepare_documents
    monkeypatch.setitem(sys.modules, "jobbot.documents.service", module)
    return prepared


def _ready(ctx, make_company, count: int):
    companies = [make_company(siren=f"9000001{i:02d}", name=f"Exemple {i:02d}", status="enriched",
                              contacts=[RECRUIT.model_copy(update={"email": f"rh@exemple{i}.fr"})],
                              domain=f"exemple{i}.fr",
                              raw={"outreach_score": 50 - i, "enriched_at": clock.iso(clock.now())})
                 for i in range(count)]
    for company in companies:
        ctx.repo.upsert_company(company)
    return companies


def test_prepare_ahead_top_cap_plus_margin(ctx, make_company, fake_documents):
    live = _live(ctx)
    companies = _ready(live, make_company, 6)
    live.repo.save_documents(TailoredDocuments(company_id=companies[0].id, cv_pdf=__file__, created_at=clock.now()))
    result = prepare_ahead(live, Deadline(clock.now() + timedelta(hours=1)), daily_cap=2)  # 2 + margin 5 = 7
    assert result.already_ready == 1 and result.prepared == 5
    assert fake_documents == [f"Exemple {i:02d}" for i in range(1, 6)]
    again = prepare_ahead(live, Deadline(clock.now() + timedelta(hours=1)), daily_cap=2)
    assert again.prepared == 0 and again.already_ready == 6


def test_prepare_ahead_dry_run_and_deadline(ctx, make_company, fake_documents):
    _ready(ctx, make_company, 3)
    planned = prepare_ahead(ctx, Deadline(clock.now() + timedelta(hours=1)), daily_cap=1)
    assert planned.planned == 3 and planned.prepared == 0 and fake_documents == []
    stopped = prepare_ahead(_live(ctx), Deadline(clock.now() - timedelta(seconds=1)), daily_cap=1)
    assert stopped.stopped and stopped.prepared == 0


def test_prepare_ahead_never_replaces_reviewed_documents(ctx, make_company, fake_documents):
    live = _live(ctx)
    approved = _ready(live, make_company, 1)[0]
    live.repo.upsert_company(approved.model_copy(update={"status": "approved"}))
    result = prepare_ahead(live, Deadline(clock.now() + timedelta(hours=1)), daily_cap=1)
    assert result.prepared == 0 and fake_documents == []


# --------------------------------------------------------------------------- atomic writes (day / night overlap)


def test_update_company_is_a_read_modify_write(repo, make_company):
    company = make_company(status="enriched")
    repo.upsert_company(company)
    assert repo.update_company("absent", lambda c: pytest.fail("jamais appelé")) is None
    assert repo.update_company(company.id, lambda c: None) == company  # nothing written
    stored = repo.update_company(company.id, lambda c: c.model_copy(update={"raw": {**c.raw, "k": 1}}))
    assert stored.raw["k"] == 1 and repo.get_company(company.id).raw["k"] == 1
    with pytest.raises(RuntimeError):
        repo.update_company(company.id, lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    repo.upsert_company(company.model_copy(update={"name": "Encore utilisable"}))  # no transaction left open
    assert repo.get_company(company.id).name == "Encore utilisable"


def test_prioritize_never_undoes_a_contact_made_meanwhile(ctx, make_company, monkeypatch):
    from jobbot.night import prioritize as step

    company = _ready(_live(ctx), make_company, 1)[0]

    def score_while_the_day_sends(c, profile, repo, cfg=None):
        stored = repo.get_company(c.id)
        repo.upsert_company(stored.model_copy(update={"status": "contacted", "raw": {**stored.raw,
                                                                                     "contacted_at": "maintenant"}}))
        return 0.0, ["Exclue : déjà contactée"]

    monkeypatch.setattr(step, "company_priority", score_while_the_day_sends)
    step.prioritize(ctx, Deadline(clock.now() + timedelta(hours=1)))
    stored = ctx.repo.get_company(company.id)
    assert stored.status == "contacted" and stored.raw["contacted_at"] == "maintenant"


def test_night_lock_outlives_the_longest_night(ctx, monkeypatch, fake_clock, notes):
    import os
    import time as real_time

    _fake_steps(monkeypatch, fake_clock, [])
    lock = ctx.path(f"{ctx.settings.paths.data_dir}/{run.LOCK_FILE}")
    lock.write_text("123", encoding="utf-8")
    seven_hours_ago = real_time.time() - 7 * 3600
    os.utime(lock, (seven_hours_ago, seven_hours_ago))
    from jobbot.pipeline import PipelineBusy

    with pytest.raises(PipelineBusy):
        run_night(ctx, budget_minutes=5)  # a 7-hour-old night may still be running (8 h max)


# --------------------------------------------------------------------------- GDPR purge


def test_purge_named_contacts_of_old_companies_never_contacted(ctx, make_company):
    live = _live(ctx)
    old = clock.now() - timedelta(days=400)
    stale = make_company(siren="900000201", name="Ancienne", status="enriched", found_at=old,
                         contacts=[RECRUIT, NAMED], leaders=["Paul MARTIN"],
                         raw={"people": [{"name": "Léa ROUX"}], "leaders_detail": [{"first": "Paul"}],
                              "source_urls": {RECRUIT.email: "https://exemple.fr/contact",
                                              NAMED.email: "https://exemple.fr/equipe"}})
    young = make_company(siren="900000202", name="Recente", status="enriched", contacts=[NAMED])
    written = make_company(siren="900000203", name="Ecrite", status="enriched", found_at=old, contacts=[NAMED])
    for company in (stale, young, written):
        live.repo.upsert_company(company)
    live.repo.create_application(Application(method=ApplyMethod.EMAIL, company_id=written.id,
                                             company_name="Ecrite", applied_at=clock.now()))
    assert purge_personal_data(ctx) == 1  # dry-run: counted only
    assert live.repo.get_company(stale.id).contacts == [RECRUIT, NAMED]
    assert purge_personal_data(live) == 1
    purged = live.repo.get_company(stale.id)
    assert purged.contacts == [RECRUIT] and purged.leaders == []
    assert "people" not in purged.raw and "leaders_detail" not in purged.raw
    assert purged.raw["source_urls"] == {RECRUIT.email: "https://exemple.fr/contact"}
    assert live.repo.get_company(young.id).contacts == [NAMED]
    assert live.repo.get_company(written.id).contacts == [NAMED]
    assert purge_personal_data(live) == 0
    from jobbot.night.store import merge_company

    refreshed = merge_company(purged, stale.model_copy(update={"leaders": ["Paul MARTIN"]}))
    assert refreshed.leaders == [] and "leaders_detail" not in refreshed.raw  # the registry never brings them back


# --------------------------------------------------------------------------- status, doctor, dashboard, CLI


def test_status_lines_queue_and_last_night(ctx, make_company, notes, monkeypatch, fake_clock):
    _ready(ctx, make_company, 3)
    lines = status_lines(ctx)
    assert lines[0].startswith("Candidatures spontanées : plafond du jour 20/jour")
    assert lines[1] == "File d'attente : 3 entreprise(s) prête(s) ≈ 1 jour d'envoi"
    assert lines[2] == "Nuit prochaine : 0 entreprise(s) à enrichir, 3 dossier(s) à préparer"
    assert lines[3].startswith("Dernière nuit : jamais lancée")
    rows = queue_rows(ctx)
    assert [r["rang"] for r in rows] == [1, 2, 3] and rows[0]["dossier_pret"] is False
    _fake_steps(monkeypatch, fake_clock, [])
    run_night(_live(ctx), budget_minutes=5)
    assert "Nuit : +120 entreprises" in status_lines(ctx)[3]


def test_doctor_checks(ctx):
    night, searx = doctor_checks(ctx)
    assert night.level == "warn" and "jamais lancée" in night.detail
    assert searx.level == "ok" and "non configuré" in searx.detail
    from jobbot.outreach import _searxng

    configured = dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={
        "search_engine_url": "http://127.0.0.1:8888"}))
    _searxng.start_backoff(ctx.repo, 6, "bing : CAPTCHA")
    level, detail = doctor_checks(configured)[1].level, doctor_checks(configured)[1].detail
    assert level == "warn" and "CAPTCHA" in detail and "Wikidata" in detail


def test_cli_night_command(ctx, project_root, monkeypatch, fake_clock, notes):
    seen = {}

    def fake_run(app, *, budget_minutes=None):
        seen.update(dry_run=app.dry_run, budget=budget_minutes)
        return NightReport(companies_found=4, relevant=4, companies_new=2, daily_cap=20, queue_ready=10)

    monkeypatch.setattr(cli_support, "build_app_context", lambda root, dry_run: ctx)
    monkeypatch.setattr(run, "run_night", fake_run)
    result = CliRunner().invoke(cli.app, ["--root", str(project_root), "night", "--budget", "30", "--dry-run"])
    cli_support.teardown_logging()
    assert result.exit_code == 0, result.output
    assert seen == {"dry_run": True, "budget": 30}
    assert "Nuit : +4 entreprises" in result.output and "Entreprises trouvées" in result.output


def test_cron_night_line_runs_with_low_priority():
    root = Path("/home/jobuser/jobbot")
    jobs = cron.build_jobs(root, Settings(), {"night": "01:15"}, xvfb=True, exe="jobbot", path_env="/bin",
                           low_priority=cron.LOW_PRIORITY)
    night = next(j for j in jobs if j.name == "night")
    assert night.schedule == "15 1 * * *"
    assert "flock -n /tmp/jobbot-night.lock nice -n 10 ionice -c3 jobbot night >>" in night.command
    assert "xvfb-run" not in night.command and night.line().endswith("# jobbot:night")
    assert [j.name for j in cron.build_jobs(root, Settings(night={"enabled": False}), exe="jobbot",
                                            path_env="/bin")] == ["run", "soir", "classroom"]
    assert cron.low_priority_prefix(lambda name: f"/usr/bin/{name}") == cron.LOW_PRIORITY
    assert cron.low_priority_prefix(lambda name: None) == ""


def test_windows_night_task(project_root):
    create = cli_tools.night_commands(project_root, "install", "00:45")[0]
    assert create[:5] == ["schtasks", "/Create", "/F", "/TN", "jobbot-nuit"]
    assert create[create.index("/SC") + 1] == "DAILY" and create[create.index("/ST") + 1] == "00:45"
    assert create[-1].endswith(f'--root "{project_root}" night')
    assert cli_tools.night_commands(project_root, "remove") == [["schtasks", "/Delete", "/F", "/TN", "jobbot-nuit"]]
    (project_root / "config" / "settings.yaml").write_text("night:\n  enabled: false\n", encoding="utf-8")
    assert cli_tools.night_commands(project_root, "install") == []
