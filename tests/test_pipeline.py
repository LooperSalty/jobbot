"""pipeline.run_daily: end-to-end dry run with fakes, step isolation, lock, prepare step, summary."""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from jobbot import pipeline
from jobbot.models import ApplyMethod, JobStatus
from jobbot.outreach.companies import DiscoverReport
from jobbot.outreach.spontaneous import CampaignReport
from jobbot.sources.base import SearchQuery

from .fixtures.orchestration.fakes import (
    PDF_BYTES,
    FakeApplier,
    FakeSource,
    applied,
    install_appliers,
    live,
    store_job,
    with_settings,
    write_docs,
)


def _fast_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real tailoring/letter code but skip Chromium (PDF rendering)."""
    import jobbot.documents.service as docs_service

    def fake_pdf(cv, out_path: Path):
        Path(out_path).write_bytes(PDF_BYTES)
        return Path(out_path), cv

    def fake_letter(letter, profile, out_path: Path, *, recipient=None):
        Path(out_path).write_bytes(PDF_BYTES)
        return Path(out_path)

    monkeypatch.setattr(docs_service, "render_cv_pdf_fitted", fake_pdf)
    monkeypatch.setattr(docs_service, "render_letter_pdf", fake_letter)
    monkeypatch.setattr(docs_service, "pdf_text", lambda path: "")
    monkeypatch.setattr(docs_service, "pdf_pages", lambda path: 1)


def _track_status(monkeypatch, job_id: str, ctx, history: list[JobStatus]) -> None:
    for name, func in list(pipeline.STEP_FUNCTIONS.items()):
        def wrapped(c, shared, _func=func):
            out = _func(c, shared)
            job = ctx.repo.get_job(job_id)
            if job is not None and (not history or history[-1] != job.status):
                history.append(job.status)
            return out
        monkeypatch.setitem(pipeline.STEP_FUNCTIONS, name, wrapped)


def test_run_daily_end_to_end_dry_run(ctx, make_job, monkeypatch):
    import jobbot.outreach.companies as companies
    import jobbot.sources.registry as registry

    job = make_job()
    real_collect = registry.collect_jobs
    monkeypatch.setattr(registry, "collect_jobs", lambda c: real_collect(
        c, sources=[FakeSource([job])], queries=[SearchQuery(keywords="développeur full-stack")]))
    monkeypatch.setattr(companies, "discover_companies", lambda c, **kw: DiscoverReport(found=0, new=0))
    _fast_documents(monkeypatch)
    email = FakeApplier("email", [applied(ApplyMethod.EMAIL, email_to="rh@acme-logiciels.fr", mid="<x1@gmail.com>")])
    install_appliers(monkeypatch, {"email": email})
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr("jobbot.notify.notify", lambda c, title, msg: notified.append((title, msg)))
    history: list[JobStatus] = []
    _track_status(monkeypatch, job.id, ctx, history)

    report = pipeline.run_daily(ctx)

    assert report.errors() == {}, report.errors()
    assert history == [JobStatus.NEW, JobStatus.SHORTLISTED, JobStatus.DOCS_READY, JobStatus.APPLIED]
    assert report.collect.new == 1 and report.score.shortlisted == 1 and report.prepare.prepared == 1
    assert report.apply.applied == 1 and email.calls == [job.id]
    app = ctx.repo.application_for_job(job.id)
    assert app is not None and app.next_followup_at is not None and app.email_message_id == "<x1@gmail.com>"
    docs = ctx.repo.latest_documents(job_id=job.id)
    assert Path(docs.cv_pdf).name == "CV_Jean_DUPONT.pdf" and docs.cv_ats is not None
    assert notified and "simulation" in notified[0][0] and "1 candidature(s) envoyée(s)" in notified[0][1]
    assert not (ctx.root / "data" / pipeline.LOCK_FILE).exists()


def test_steps_are_isolated_and_ordered(ctx, monkeypatch):
    calls: list[str] = []

    def boom(c, shared):
        calls.append("collect")
        raise RuntimeError("source en panne")

    def fine(name):
        def run(c, shared):
            calls.append(name)
            return CampaignReport(sent=2, skipped=0, failed=0) if name == "spontaneous" else None
        return run

    monkeypatch.setitem(pipeline.STEP_FUNCTIONS, "collect", boom)
    monkeypatch.setitem(pipeline.STEP_FUNCTIONS, "spontaneous", fine("spontaneous"))
    monkeypatch.setattr("jobbot.notify.notify", lambda *a: None)
    report = pipeline.run_daily(ctx, steps=["spontaneous", "collect"])
    assert calls == ["collect", "spontaneous"]
    assert report.collect == "Erreur : source en panne" and report.spontaneous.sent == 2 and report.apply is None
    assert "2 spontanée(s)" in report.summary() and "1 étape(s) en erreur : collect" in report.summary()
    with pytest.raises(ValueError, match="inconnue"):
        pipeline.run_daily(ctx, steps=["nope"])
    assert pipeline.validate_steps(None) == list(pipeline.STEPS)


def test_lock_prevents_parallel_runs(ctx, monkeypatch):
    monkeypatch.setattr("jobbot.notify.notify", lambda *a: None)
    lock = ctx.root / "data" / pipeline.LOCK_FILE
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("123", encoding="utf-8")
    with pytest.raises(pipeline.PipelineBusy):
        pipeline.run_daily(ctx, steps=[])
    old = time.time() - pipeline.LOCK_STALE_S - 10
    os.utime(lock, (old, old))
    assert pipeline.run_daily(ctx, steps=[]).summary() == "Rien à signaler."
    assert not lock.exists()


def test_prepare_shortlisted_best_first_and_isolated(ctx, make_job, monkeypatch):
    import jobbot.documents.service as docs_service
    import jobbot.sources.registry as registry

    best = store_job(ctx, make_job(external_id="a", company="A"), status=JobStatus.SHORTLISTED, score=90, docs=False)
    broken = store_job(ctx, make_job(external_id="b", company="B"), status=JobStatus.SHORTLISTED, score=80,
                       docs=False)
    expired = store_job(ctx, make_job(external_id="c", company="C"), status=JobStatus.SHORTLISTED, score=70,
                        docs=False)
    store_job(ctx, make_job(external_id="d", company="D"), status=JobStatus.SHORTLISTED, score=60, docs=False)

    def ensure(c, job):
        return job.model_copy(update={"status": JobStatus.EXPIRED}) if job.id == expired.id else job

    def prepare(c, job=None, company=None):
        if job.id == broken.id:
            raise RuntimeError("LLM en panne")
        c.repo.update_job_status(job.id, JobStatus.DOCS_READY)
        return write_docs(c, job.id)

    monkeypatch.setattr(registry, "ensure_details", ensure)
    monkeypatch.setattr(docs_service, "prepare_documents", prepare)
    report = pipeline.prepare_shortlisted(ctx, limit=3)
    assert (report.prepared, report.failed, report.skipped) == (1, 1, 1)
    assert report.details[0].startswith("A — ") and "LLM en panne" in report.details[1]
    assert ctx.repo.get_job(best.id).status == JobStatus.DOCS_READY
    only = pipeline.prepare_shortlisted(ctx, job_ids=[broken.id, "missing"])
    assert only.failed == 1 and only.prepared == 0


def test_prepare_rescores_after_full_description(ctx, make_job, monkeypatch):
    import jobbot.documents.service as docs_service
    import jobbot.sources.registry as registry

    card = dict(source="linkedin", description="", raw={"needs_details": True})
    senior = store_job(ctx, make_job(external_id="s", company="S", **card), status=JobStatus.SHORTLISTED, score=70,
                       docs=False)
    fine = store_job(ctx, make_job(external_id="f", company="F", **card), status=JobStatus.SHORTLISTED, score=65,
                     docs=False)
    full = make_job().description

    def ensure(c, job):
        extra = " Expérience : 7 ans d'expérience exigés." if job.id == senior.id else ""
        return job.model_copy(update={"description": full + extra})

    def prepare(c, job=None, company=None):
        c.repo.update_job_status(job.id, JobStatus.DOCS_READY)
        return write_docs(c, job.id)

    monkeypatch.setattr(registry, "ensure_details", ensure)
    monkeypatch.setattr(docs_service, "prepare_documents", prepare)
    report = pipeline.prepare_shortlisted(ctx)
    assert (report.prepared, report.skipped) == (1, 1)
    assert "annonce complète" in report.details[0]
    dropped = ctx.repo.get_job(senior.id)
    assert dropped.status == JobStatus.FILTERED_OUT
    assert dropped.match_score == 0
    kept = ctx.repo.get_job(fine.id)
    assert kept.status == JobStatus.DOCS_READY
    assert kept.match_score != 65


def test_prepare_skips_company_already_applied(ctx, make_job, monkeypatch):
    from jobbot import clock
    from jobbot.models import Application

    job = store_job(ctx, make_job(external_id="a"), status=JobStatus.SHORTLISTED, docs=False)
    ctx.repo.create_application(Application(method=ApplyMethod.EMAIL, applied_at=clock.now(),
                                            company_name="Acme Logiciels"))
    report = pipeline.prepare_shortlisted(ctx)
    assert report.skipped == 1 and "moins de 60 jours" in report.details[0]
    assert ctx.repo.get_job(job.id).status == JobStatus.SKIPPED  # leaves the queue, never blocks it


def test_prepare_gives_up_after_repeated_failures(ctx, make_job, monkeypatch):
    import jobbot.documents.service as docs_service
    import jobbot.sources.registry as registry

    job = store_job(ctx, make_job(external_id="x"), status=JobStatus.SHORTLISTED, docs=False)
    monkeypatch.setattr(registry, "ensure_details", lambda c, j: j)

    def broken(c, job=None, company=None):
        raise RuntimeError("rendu PDF impossible")

    monkeypatch.setattr(docs_service, "prepare_documents", broken)
    for _ in range(pipeline.PREPARE_MAX_FAILURES - 1):
        assert pipeline.prepare_shortlisted(ctx).failed == 1
        assert ctx.repo.get_job(job.id).status == JobStatus.SHORTLISTED
    last = pipeline.prepare_shortlisted(ctx)
    assert last.failed == 1
    assert "mise de côté" in last.details[0]
    stored = ctx.repo.get_job(job.id)
    assert stored.status == JobStatus.FAILED
    assert stored.raw["prepare_failures"] == pipeline.PREPARE_MAX_FAILURES


def test_spontaneous_step_uses_effective_mode(ctx, monkeypatch):
    import jobbot.outreach.spontaneous as spontaneous

    seen: list[str] = []

    def campaign(c, mailer, *, limit=None):
        seen.append(c.settings.mode)
        return CampaignReport(sent=0, skipped=0, failed=0)

    monkeypatch.setattr(spontaneous, "run_spontaneous_campaign", campaign)
    monkeypatch.setattr("jobbot.notify.notify", lambda *a: None)
    warm = with_settings(ctx, setup_date=date.today().isoformat(), warmup_days=5)
    pipeline.run_daily(warm, steps=["spontaneous"])
    pipeline.run_daily(ctx, steps=["spontaneous"])
    assert seen == ["review", "auto"]
    assert pipeline.with_effective_mode(ctx) is ctx


# --------------------------------------------------------------------------- LBA relay (spontaneous)

RECRUITER_SIRET = "85190096900011"
RECRUITER_DETAIL = (f"https://labonnealternance.apprentissage.beta.gouv.fr/api/_private/jobs/recruteurs_lba/"
                    f"{RECRUITER_SIRET}")


def _recruiter(ctx, make_company, **raw):
    company = make_company(source="lba_recruteur", contacts=[], siren=RECRUITER_SIRET[:9],
                           raw={"siret": RECRUITER_SIRET, "hasEmail": True, **raw})
    ctx.repo.upsert_company(company)
    write_docs(ctx, company_id=company.id)
    return company


def _recruiter_detail(**updates):
    import json

    path = Path(__file__).parent / "fixtures" / "sources_api" / "lba_detail_recruiter.json"
    return {**json.loads(path.read_text(encoding="utf-8")), **updates}


@respx.mock
def test_lba_relay_applies_once_and_records(ctx, make_company):
    from jobbot.apply.lba import APPLY_URL
    from jobbot.apply.lba_recruiters import run_lba_recruiters

    company = _recruiter(ctx, make_company)
    respx.get(RECRUITER_DETAIL).mock(return_value=httpx.Response(200, json=_recruiter_detail()))
    post = respx.post(APPLY_URL).mock(return_value=httpx.Response(202, json={"id": "1"}))
    report = run_lba_recruiters(live(ctx))
    assert report.applied == 1, report.details
    app = ctx.repo.list_applications()[0]
    assert app.method == ApplyMethod.LBA and app.company_id == company.id and app.next_followup_at is not None
    assert ctx.repo.get_company(company.id).status == "contacted"
    assert run_lba_recruiters(live(ctx)).applied == 0 and post.call_count == 1


@respx.mock
def test_lba_relay_review_dry_run_and_refusals(ctx, make_company, monkeypatch):
    from jobbot.apply.lba import APPLY_URL
    from jobbot.apply.lba_recruiters import DONE_FLAG, run_lba_recruiters

    company = _recruiter(ctx, make_company)
    route = respx.get(RECRUITER_DETAIL).mock(return_value=httpx.Response(200, json=_recruiter_detail()))
    post = respx.post(APPLY_URL).mock(return_value=httpx.Response(202, json={}))
    review = run_lba_recruiters(with_settings(live(ctx), mode="review"))
    assert review.pending_review == 1 and ctx.repo.get_company(company.id).status == "pending_review"
    ctx.repo.upsert_company(ctx.repo.get_company(company.id).model_copy(update={"status": "approved"}))
    dry = run_lba_recruiters(ctx)
    assert dry.skipped == 1 and "[dry-run]" in dry.details[0] and not post.called
    route.mock(return_value=httpx.Response(200, json=_recruiter_detail(contact={"hasEmail": False})))
    refused = run_lba_recruiters(live(ctx))
    assert refused.skipped == 1 and ctx.repo.get_company(company.id).raw[DONE_FLAG] is True
    assert run_lba_recruiters(live(ctx)).details == []


def test_lba_relay_review_queue_is_capped(ctx, make_company, monkeypatch):
    import jobbot.apply.lba_recruiters as relay

    for n in range(relay.REVIEW_QUEUE_MAX + 3):
        company = make_company(name=f"Recruteur {n}", siren=f"{n:09d}", source="lba_recruteur", contacts=[],
                               raw={"siret": f"{n:09d}00011", "hasEmail": True})
        ctx.repo.upsert_company(company)
        write_docs(ctx, company_id=company.id)
    report = relay.run_lba_recruiters(with_settings(ctx, mode="review"))
    assert report.pending_review == relay.REVIEW_QUEUE_MAX
    assert relay.run_lba_recruiters(with_settings(ctx, mode="review")).pending_review == 0


def test_lba_relay_skips_blocked_and_merges_campaign(ctx, make_company, monkeypatch):
    import jobbot.apply.lba_recruiters as relay
    import jobbot.outreach.spontaneous as spontaneous

    company = _recruiter(ctx, make_company)
    ctx.repo.blacklist_add(company.name)
    report = relay.run_lba_recruiters(live(ctx))
    assert report.skipped == 1 and "liste noire" in report.details[0]
    monkeypatch.setattr(spontaneous, "run_spontaneous_campaign",
                        lambda c, m, limit=None: CampaignReport(sent=2, skipped=1, failed=0, details=["mail ok"]))

    def boom(c, limit=None):
        raise RuntimeError("LBA en panne")

    monkeypatch.setattr(relay, "run_lba_recruiters", boom)
    merged = pipeline.spontaneous_applications(ctx, None)
    assert (merged.sent, merged.skipped, merged.failed) == (2, 1, 1)
    assert merged.details[0].startswith("LBA — erreur") and merged.details[-1] == "mail ok"


def test_summary_texts():
    from jobbot.apply.service import ApplyReport
    from jobbot.followup import FollowupReport
    from jobbot.mail.messages import InboxReport

    report = pipeline.DailyReport(
        apply=ApplyReport(applied=2, needs_manual=1, pending_review=3),
        inbox=InboxReport(scanned=4, matched=1, updates=[]), followup=FollowupReport(sent=1, manual=0, closed=0),
        enrich="Erreur : x",
    )
    text = report.summary()
    assert "2 candidature(s) envoyée(s), 1 à finir à la main, 3 à valider" in text
    assert "1 réponse(s)" in text and "1 relance(s)" in text and "enrich" in text
