"""Classroom watcher: new post → offers → score → documents → immediate application (dry-run end to end),
auto-apply decisions, retries, login alerts, preview and the pipeline step. No network, no browser."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime

import pytest

from jobbot import clock, pipeline
from jobbot.classroom import process, state
from jobbot.classroom.preview import preview_classroom
from jobbot.classroom.watch import LOGIN_MESSAGE, NOT_CONFIGURED, run_classroom
from jobbot.llm import FakeLLM
from jobbot.models import ApplyMethod, JobStatus

from .fixtures.classroom.fakes import (
    FakeBackend,
    fast_documents,
    make_post,
    offers_payload,
    responder,
    with_classroom,
)
from .fixtures.orchestration.fakes import FakeApplier, applied, install_appliers, live, result, store_job, with_limits

NOON = datetime(2026, 9, 24, 12, 0, tzinfo=clock.PARIS)


@pytest.fixture
def notes(monkeypatch):
    """Captured notifications; no geocoding, no board fetch, no Chromium."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("jobbot.notify.notify", lambda c, title, message: sent.append((title, message)))
    monkeypatch.setattr("jobbot.matching.match.geocode", lambda query, client=None: None)
    monkeypatch.setattr("jobbot.sources.registry.fetch_details_only", lambda c, job: None)
    fast_documents(monkeypatch)
    return sent


@pytest.fixture
def cctx(ctx):
    """Dry-run context with the fictitious course configured, the extraction LLM and a warm-up in progress."""
    warm = ctx.settings.model_copy(update={"setup_date": date.today().isoformat(), "warmup_days": 5})
    return with_classroom(dataclasses.replace(ctx, settings=warm, llm=FakeLLM(responder())))


# --------------------------------------------------------------------------- end to end


def test_new_post_with_two_offers_dry_run_end_to_end(cctx, notes):
    backend = FakeBackend([make_post()])
    report = run_classroom(cctx, backend=backend, now=NOON)
    assert (report.posts, report.new_posts, report.offers) == (1, 1, 2), report
    assert (report.simulated, report.filtered_out, report.failed) == (1, 1, 0), report.details
    outbox = list((cctx.output_dir / "outbox").glob("*.eml"))
    assert len(outbox) == 1 and "recrutement@acme-logiciels.fr" in outbox[0].read_text(encoding="utf-8")
    jobs = {j.company: j for j in cctx.repo.list_jobs(source="classroom")}
    acme, beta = jobs["Acme Logiciels"], jobs["Beta Services"]
    assert acme.apply_method == ApplyMethod.EMAIL and acme.status == JobStatus.APPROVED  # warm-up ignored
    assert acme.match_score >= 60 and "Offre relayée par l'école (+5 points)" in acme.match_reasons
    assert beta.status == JobStatus.FILTERED_OUT and "BTS SIO" in beta.match_reasons[0]
    docs = cctx.repo.latest_documents(job_id=acme.id)
    assert "par l'intermédiaire de Exemple Digital School" in docs.email_body
    assert [title for title, _ in notes] == ["Classroom : candidature simulée (--dry-run)", "Classroom : offre écartée"]
    assert state.last_check(cctx.repo)["ok"] is True
    again = run_classroom(cctx, backend=backend, now=NOON)
    assert (again.new_posts, again.offers, again.simulated) == (0, 0, 0)
    assert len(list((cctx.output_dir / "outbox").glob("*.eml"))) == 1 and len(notes) == 2


def test_live_application_is_immediate_without_long_pause(cctx, notes, monkeypatch):
    email = FakeApplier("email", [applied(ApplyMethod.EMAIL, email_to="recrutement@acme-logiciels.fr",
                                          mid="<a1@gmail.com>")])
    install_appliers(monkeypatch, {"email": email})
    monkeypatch.setattr("jobbot.pacing.Pacer.between_applications", lambda self: pytest.fail("pause inattendue"))
    report = run_classroom(live(cctx), backend=FakeBackend([make_post()]), now=NOON)
    assert report.applied == 1 and len(email.calls) == 1
    job = cctx.repo.get_job(email.calls[0])
    assert job.status == JobStatus.APPLIED and cctx.repo.application_for_job(job.id).email_message_id
    assert notes[0][0] == "Classroom : candidature envoyée"


def test_edited_post_is_processed_again_and_failures_are_bounded(cctx, notes, monkeypatch):
    backend = FakeBackend([make_post()])
    run_classroom(cctx, backend=backend, now=NOON)
    beta = next(j for j in cctx.repo.list_jobs(source="classroom") if j.company == "Beta Services")
    edited = make_post(text=make_post().text + "\nMise à jour : Beta Services accepte aussi un Bac+3 à Bac+5.")
    changed = offers_payload()
    changed["offers"][1]["target_level"] = "BTS SIO ou Bac+3 à Bac+5"
    ctx2 = dataclasses.replace(cctx, llm=FakeLLM(responder(changed)))
    report = run_classroom(ctx2, backend=FakeBackend([edited]), now=NOON)
    assert report.new_posts == 1 and cctx.repo.get_job(beta.id).status != JobStatus.FILTERED_OUT
    import jobbot.classroom.watch as watch

    def broken(c, post):
        raise RuntimeError("LLM en panne")

    monkeypatch.setattr(watch, "offers_for_post", broken)
    other = make_post(post_id="p-555000111233", text="Offre : Delta Data recrute un développeur Python.")
    for _ in range(state.MAX_POST_FAILURES):
        failed = run_classroom(cctx, backend=FakeBackend([other]), now=NOON)
        assert failed.errors and "LLM en panne" in failed.errors[0]
    assert run_classroom(cctx, backend=FakeBackend([other]), now=NOON).errors == []  # given up: not read again


# --------------------------------------------------------------------------- decisions & retries


def _ready(ctx, make_job, **updates):
    job = make_job(source="classroom", external_id=updates.pop("external_id", "p-9-1"),
                   raw={"relayed_by_school": True, "school_bonus": 5.0}, **updates)
    return store_job(ctx, job, status=JobStatus.DOCS_READY, score=updates.get("match_score", 80))


def test_review_decisions(cctx, make_job, notes):
    job = _ready(cctx, make_job)
    assert process.review_reason(cctx, job) is None
    off = with_classroom(cctx, auto_apply=False)
    assert "auto_apply" in process.review_reason(off, job)
    review = dataclasses.replace(cctx, settings=cctx.settings.model_copy(update={"mode": "review"}))
    assert "mode review" in process.review_reason(review, job)
    strict = with_classroom(cctx, min_score=90)
    assert "seuil" in process.review_reason(strict, job) and process.auto_threshold(strict) == 90
    docs = cctx.repo.latest_documents(job_id=job.id)
    cctx.repo.save_documents(docs.model_copy(update={"truth_warnings": ["Compétence absente : Rust"]}))
    assert "CV à vérifier" in process.review_reason(cctx, job)
    outcome = process.submit(off, job)
    assert outcome.kind == "pending_review" and cctx.repo.get_job(job.id).status == JobStatus.PENDING_REVIEW
    process.notify_outcome(off, job, outcome)
    assert f"jobbot approve {job.id}" in notes[-1][1]


def test_quota_defers_then_retry_applies(cctx, make_job, notes, monkeypatch):
    email = FakeApplier("email", [applied(ApplyMethod.EMAIL, email_to="rh@acme-logiciels.fr", mid="<b@gmail.com>")])
    install_appliers(monkeypatch, {"email": email})
    job = _ready(cctx, make_job)
    full = with_limits(live(cctx), email_applications_per_day=0)
    outcome = process.submit(full, job)
    assert outcome.kind == "deferred" and "quota" in outcome.detail and not email.calls
    stored = cctx.repo.get_job(job.id)
    assert stored.status == JobStatus.APPROVED and stored.raw["classroom_auto"] is True
    retried = process.retry_approved(live(cctx))
    assert [o.kind for _, o in retried] == ["applied"] and cctx.repo.get_job(job.id).status == JobStatus.APPLIED
    assert process.retry_approved(live(cctx)) == []


def test_channel_problem_retries_are_capped(cctx, make_job, monkeypatch):
    busy = FakeApplier("email", [result(JobStatus.SKIPPED, "Quota journalier atteint pour email")] * 10)
    install_appliers(monkeypatch, {"email": busy})
    job = _ready(cctx, make_job)
    assert process.submit(live(cctx), job).kind == "deferred"
    for _ in range(process.MAX_ATTEMPTS + 2):
        process.retry_approved(live(cctx))
    assert len(busy.calls) == process.MAX_ATTEMPTS
    assert cctx.repo.get_job(job.id).raw["classroom_attempts"] == process.MAX_ATTEMPTS


def test_external_offer_needs_manual_with_link(cctx, make_job, notes):
    job = _ready(cctx, make_job, apply_method=ApplyMethod.EXTERNAL, contact_email=None,
                 apply_url="https://careers.acme.example/42")
    outcome = process.submit(live(cctx), job)
    assert outcome.kind == "needs_manual" and cctx.repo.get_job(job.id).status == JobStatus.NEEDS_MANUAL
    process.notify_outcome(cctx, job, outcome)
    assert "https://careers.acme.example/42" in notes[-1][1]


def test_duplicate_and_company_dedup(cctx, make_job, notes):
    from jobbot.models import Application

    cctx.repo.create_application(Application(method=ApplyMethod.EMAIL, applied_at=clock.now(),
                                             company_name="Acme Logiciels"))
    report = run_classroom(cctx, backend=FakeBackend([make_post()]), now=NOON)
    assert report.skipped == 1 and "moins de 60 jours" in " ".join(report.details)


# --------------------------------------------------------------------------- guards & alerts


def test_guards_not_configured_disabled_hours_busy(cctx, ctx, notes):
    assert run_classroom(with_classroom(ctx, urls=None), backend=FakeBackend()).details == [NOT_CONFIGURED]
    assert "désactivée" in run_classroom(with_classroom(cctx, enabled=False)).details[0]
    backend = FakeBackend([make_post()])
    assert "plage horaire" in run_classroom(cctx, backend=backend, now=NOON.replace(hour=23)).details[0]
    assert backend.calls == 0
    with state.classroom_lock(cctx):
        assert "déjà en cours" in run_classroom(cctx, backend=backend, now=NOON).details[0]


def test_login_required_notifies_at_most_every_six_hours(cctx, notes):
    backend = FakeBackend(login_required=True)
    first = run_classroom(cctx, backend=backend, now=NOON)
    second = run_classroom(cctx, backend=backend, now=NOON)
    assert first.errors == [LOGIN_MESSAGE] and second.errors == [LOGIN_MESSAGE]
    assert [t for t, _ in notes] == ["Classroom : reconnecte-toi"] and "jobbot login classroom" in notes[0][1]
    check = state.last_check(cctx.repo)
    assert check["ok"] is False and check["login_required"] is True
    broken = run_classroom(cctx, backend=FakeBackend(error="navigateur indisponible : x"), now=NOON)
    assert broken.errors == ["Classroom : navigateur indisponible : x"]


# --------------------------------------------------------------------------- preview & pipeline


def test_preview_is_read_only(cctx, notes):
    reminder = make_post(post_id="p-555000111233", text="Rappel : réunion jeudi à 14h en salle 12.", links=[])
    llm = FakeLLM(lambda p, s, f: (responder()(p, s, f) if "Deux offres" in p
                                   else '{"is_offer_post": false, "offers": []}'))
    rows, lines = preview_classroom(dataclasses.replace(cctx, llm=llm), limit=5,
                                    backend=FakeBackend([make_post(), reminder]))
    assert lines == ["2 publication(s) dans le flux, 2 analysée(s)."]
    verdicts = {row.company or row.title: row.verdict for row in rows}
    assert verdicts["Acme Logiciels"] == "candidature immédiate (e-mail)"
    assert verdicts["Beta Services"].startswith("écartée") and "BTS" in verdicts["Beta Services"]
    assert verdicts["Rappel : réunion jeudi à 14h en salle 12."] == "pas une offre"
    assert cctx.repo.list_jobs(source="classroom") == [] and state.load_seen(cctx.repo, "123456789012") == {}
    assert preview_classroom(with_classroom(cctx, urls=None))[1] == [NOT_CONFIGURED]
    assert preview_classroom(cctx, backend=FakeBackend(login_required=True))[1] == [LOGIN_MESSAGE]


def test_pipeline_step_runs_first_and_ignores_hours(cctx, notes, monkeypatch):
    import jobbot.classroom.watch as watch

    backend = FakeBackend([make_post()])
    monkeypatch.setattr(watch, "make_backend", lambda c: backend)
    assert pipeline.STEPS[0] == "classroom"
    report = pipeline.run_daily(cctx, steps=["classroom"])
    assert report.classroom.offers == 2 and "école : 2 offre(s), 1 candidature(s)" in report.summary()
    assert pipeline.DailyReport(classroom=watch.ClassroomReport()).summary() == "Rien à signaler."
