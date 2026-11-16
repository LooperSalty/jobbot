"""apply/service (routing, quotas, dedup, records), apply/email_apply and apply/lba."""

from __future__ import annotations

import base64
import json
from datetime import date

import httpx
import pytest
import respx

from jobbot import clock
from jobbot.apply import service
from jobbot.apply.email_apply import EmailApplier
from jobbot.apply.lba import (
    APPLY_URL,
    KV_PREFIX,
    LbaApplier,
    build_attachment,
    contract_start,
    fit_message,
    normalize_phone,
)
from jobbot.config import Secrets
from jobbot.mail.mailer import Mailer, MailError, MailFatalError
from jobbot.models import Application, ApplyMethod, JobStatus

from .fixtures.orchestration.fakes import (
    FakeApplier,
    FakeBrowser,
    FakeMailer,
    FakeSMTP,
    applied,
    fixture_path,
    install_appliers,
    live,
    result,
    store_job,
    with_limits,
    with_settings,
    write_docs,
)

LBA_SITE = "https://labonnealternance.apprentissage.beta.gouv.fr"
OFFER_ID = "6aaa5a821124e472788457ab"


def _job(make_job, n: int, **kw):
    kw.setdefault("company", f"Entreprise {n}")
    return make_job(external_id=f"ext-{n}", **kw)


# --------------------------------------------------------------------------- selection


def test_channel_of(make_job):
    assert service.channel_of(make_job(apply_method=ApplyMethod.LINKEDIN_EASY_APPLY)) == "linkedin"
    assert service.channel_of(make_job(apply_method=ApplyMethod.MANUAL)) == "external"
    assert service.channel_of(make_job(apply_method=ApplyMethod.LBA)) == "lba"


def test_select_auto_mode(ctx, make_job):
    high = store_job(ctx, _job(make_job, 1), score=80)
    store_job(ctx, _job(make_job, 2), score=50)
    approved = store_job(ctx, _job(make_job, 3), status=JobStatus.APPROVED, score=30)
    store_job(ctx, _job(make_job, 4), status=JobStatus.NEW, score=99)
    assert [j.id for j in service.select_jobs_to_apply(ctx)] == [high.id, approved.id]
    assert len(service.select_jobs_to_apply(ctx, limit=1)) == 1


def test_select_review_and_warmup(ctx, make_job):
    store_job(ctx, _job(make_job, 1), score=95)
    approved = store_job(ctx, _job(make_job, 2), status=JobStatus.APPROVED)
    review = with_settings(ctx, mode="review")
    assert [j.id for j in service.select_jobs_to_apply(review)] == [approved.id]
    warm = with_settings(ctx, setup_date=date.today().isoformat(), warmup_days=3)
    assert [j.id for j in service.select_jobs_to_apply(warm)] == [approved.id]


def test_select_skips_duplicates(ctx, make_job):
    job = store_job(ctx, _job(make_job, 1, company="Acme"))
    ctx.repo.create_application(Application(method=ApplyMethod.EMAIL, applied_at=clock.now(), company_name="Acme"))
    assert service.select_jobs_to_apply(ctx) == []
    assert service.dedup_reason(ctx, job)[1] == JobStatus.SKIPPED


# --------------------------------------------------------------------------- routing & records


def test_apply_routes_records_and_reviews(ctx, make_job):
    email_job = store_job(ctx, _job(make_job, 1), score=90)
    external = store_job(ctx, _job(make_job, 2, apply_method=ApplyMethod.EXTERNAL), score=85)
    low = store_job(ctx, _job(make_job, 3), score=50)
    fake = FakeApplier("email", [applied(ApplyMethod.EMAIL, email_to="rh@acme.fr", mid="<a1@gmail.com>")])
    report = _run(ctx, {"email": fake})
    assert (report.applied, report.needs_manual, report.pending_review) == (1, 1, 1)
    app = ctx.repo.application_for_job(email_job.id)
    assert app.email_message_id == "<a1@gmail.com>" and app.email_thread_ids == ["<a1@gmail.com>"]
    assert app.next_followup_at is not None and app.next_followup_at > app.applied_at
    assert [e.kind for e in ctx.repo.list_events(app.id)] == ["applied"]
    assert ctx.repo.get_job(email_job.id).status == JobStatus.APPLIED
    assert ctx.repo.get_job(external.id).status == JobStatus.NEEDS_MANUAL
    assert "assist" in ctx.repo.get_job(external.id).raw["apply_detail"]
    assert ctx.repo.get_job(low.id).status == JobStatus.PENDING_REVIEW


def test_supported_external_offer_is_recorded_and_obeys_quota(ctx, make_job):
    url = "https://jobs.lever.co/company/123/apply"
    first = store_job(ctx, _job(make_job, 1, url=url, apply_url=url, apply_method=ApplyMethod.EXTERNAL), score=90)
    second = store_job(ctx, _job(make_job, 2, url=url, apply_url=url, apply_method=ApplyMethod.EXTERNAL), score=80)
    fake = FakeApplier("external", [applied(ApplyMethod.EXTERNAL)])
    report = _run(with_limits(ctx, external_per_day=1), {"external": fake})
    assert report.applied == 1 and report.skipped == 1
    assert ctx.repo.application_for_job(first.id) is not None
    assert ctx.repo.get_job(second.id).status == JobStatus.DOCS_READY


def test_only_unattempted_manual_external_offers_are_retried(ctx, make_job):
    url = "https://jobs.lever.co/company/123/apply"
    one = store_job(ctx, _job(make_job, 1, url=url, apply_url=url, apply_method=ApplyMethod.EXTERNAL),
                    status=JobStatus.NEEDS_MANUAL, score=90)
    two = store_job(ctx, _job(make_job, 2, url=url, apply_url=url, apply_method=ApplyMethod.EXTERNAL),
                    status=JobStatus.NEEDS_MANUAL, score=90)
    service.set_job_status(ctx, one, JobStatus.NEEDS_MANUAL, service.EXTERNAL_DETAIL)
    service.set_job_status(ctx, two, JobStatus.NEEDS_MANUAL, "Envoi tenté mais confirmation absente")
    fake = FakeApplier("external", [applied(ApplyMethod.EXTERNAL)])
    report = _run(ctx, {"external": fake})
    assert report.applied == 1 and fake.calls == [one.id]
    assert ctx.repo.get_job(two.id).status == JobStatus.NEEDS_MANUAL


def _run(ctx, appliers, **kw):
    mp = pytest.MonkeyPatch()
    try:
        install_appliers(mp, appliers)
        return service.apply_to_jobs(ctx, **kw)
    finally:
        mp.undo()


def test_quota_blocks_channel_and_keeps_status(ctx, make_job):
    first = store_job(ctx, _job(make_job, 1), score=90)
    second = store_job(ctx, _job(make_job, 2), score=80)
    fake = FakeApplier("email")
    report = _run(with_limits(ctx, email_applications_per_day=1), {"email": fake})
    assert (report.applied, report.skipped) == (1, 1)
    assert fake.calls == [first.id]
    assert ctx.repo.get_job(second.id).status == JobStatus.DOCS_READY
    assert any("quota" in d for d in report.details)


def test_same_company_twice_in_one_run(ctx, make_job):
    store_job(ctx, _job(make_job, 1, company="Acme"), score=90)
    second = store_job(ctx, _job(make_job, 2, company="ACME SAS"), score=80)
    report = _run(ctx, {"email": FakeApplier("email")})
    assert (report.applied, report.skipped) == (1, 1)
    assert ctx.repo.get_job(second.id).status == JobStatus.SKIPPED


@pytest.mark.parametrize("detail", ["session absente — lance « jobbot login linkedin »",
                                    "limite quotidienne de candidatures LinkedIn atteinte, réessaie demain"])
def test_login_problem_blocks_channel_without_touching_jobs(ctx, make_job, detail):
    method = ApplyMethod.LINKEDIN_EASY_APPLY
    jobs = [store_job(ctx, _job(make_job, n, source="linkedin", apply_method=method), score=90 - n) for n in (1, 2)]
    fake = FakeApplier("linkedin", [result(JobStatus.NEEDS_MANUAL, detail)])
    report = _run(ctx, {"linkedin": fake})
    assert fake.calls == [jobs[0].id] and report.skipped == 2
    assert all(ctx.repo.get_job(j.id).status == JobStatus.DOCS_READY for j in jobs)


@pytest.mark.parametrize("detail", [
    "Erreur LinkedIn : Page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.linkedin.com/jobs/view/1",
    "Erreur inattendue : Page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.hellowork.com/fr-fr/emplois/1",
    "Erreur inattendue : [Errno -3] Temporary failure in name resolution",
])
def test_network_outage_blocks_channel_without_failing_jobs(ctx, make_job, detail):
    method = ApplyMethod.LINKEDIN_EASY_APPLY
    jobs = [store_job(ctx, _job(make_job, n, source="linkedin", apply_method=method), score=90 - n) for n in (1, 2)]
    fake = FakeApplier("linkedin", [result(JobStatus.FAILED, detail)])
    report = _run(ctx, {"linkedin": fake})
    assert fake.calls == [jobs[0].id]
    assert (report.failed, report.skipped) == (0, 2)
    assert all(ctx.repo.get_job(j.id).status == JobStatus.DOCS_READY for j in jobs)  # tried again next run
    assert "sera retentée" in report.details[0]


def test_network_outage_that_never_ends_fails_the_offer(ctx, make_job):
    method = ApplyMethod.LINKEDIN_EASY_APPLY
    job = store_job(ctx, _job(make_job, 1, source="linkedin", apply_method=method))
    outage = "Erreur LinkedIn : Page.goto: net::ERR_CONNECTION_RESET at https://www.linkedin.com/jobs/view/1"
    for _ in range(service.MAX_NETWORK_RETRIES - 1):
        _run(ctx, {"linkedin": FakeApplier("linkedin", [result(JobStatus.FAILED, outage)])})
        assert ctx.repo.get_job(job.id).status == JobStatus.DOCS_READY
    last = _run(ctx, {"linkedin": FakeApplier("linkedin", [result(JobStatus.FAILED, outage)])})
    assert last.failed == 1
    assert ctx.repo.get_job(job.id).status == JobStatus.FAILED


@pytest.mark.parametrize("detail", ["Page.goto: net::ERR_ABORTED at https://www.linkedin.com/jobs/view/1",
                                    "Page.goto: net::ERR_CERT_DATE_INVALID", "Timeout 30000ms exceeded."])
def test_other_errors_still_fail_the_offer(ctx, make_job, detail):
    method = ApplyMethod.LINKEDIN_EASY_APPLY
    job = store_job(ctx, _job(make_job, 1, source="linkedin", apply_method=method))
    report = _run(ctx, {"linkedin": FakeApplier("linkedin", [result(JobStatus.FAILED, detail)])})
    assert report.failed == 1
    assert ctx.repo.get_job(job.id).status == JobStatus.FAILED


def test_fatal_channel_and_failures(ctx, make_job):
    jobs = [store_job(ctx, _job(make_job, n), score=90 - n) for n in (1, 2)]
    fake = FakeApplier("email", [result(JobStatus.FAILED, "SMTP refusé", method=ApplyMethod.EMAIL)],
                       fatal_after="identifiants refusés")
    report = _run(ctx, {"email": fake})
    assert fake.calls == [jobs[0].id] and report.skipped == 2 and report.failed == 0
    assert ctx.repo.get_job(jobs[0].id).status == JobStatus.DOCS_READY


def test_results_map_to_job_statuses(ctx, make_job):
    method = ApplyMethod.LINKEDIN_EASY_APPLY
    jobs = [store_job(ctx, _job(make_job, n, source="linkedin", apply_method=method), score=90 - n) for n in range(5)]
    fake = FakeApplier("linkedin", [
        result(JobStatus.SKIPPED, "[dry-run] rempli", ok=True), result(JobStatus.EXPIRED, "fermée"),
        result(JobStatus.SKIPPED, "Déjà postulé"), result(JobStatus.FAILED, "erreur"),
        result(JobStatus.NEEDS_MANUAL, "question inconnue"),
    ])
    report = _run(ctx, {"linkedin": fake})
    statuses = [ctx.repo.get_job(j.id).status for j in jobs]
    assert statuses == [JobStatus.DOCS_READY, JobStatus.EXPIRED, JobStatus.SKIPPED, JobStatus.FAILED,
                        JobStatus.NEEDS_MANUAL]
    assert (report.failed, report.needs_manual, report.skipped) == (1, 1, 3)


def test_applier_exception_and_cannot_apply(ctx, make_job):
    boom = store_job(ctx, _job(make_job, 1), score=90)
    other = store_job(ctx, _job(make_job, 2, source="lba", apply_method=ApplyMethod.LBA), score=80)
    report = _run(ctx, {"email": FakeApplier("email", raises=RuntimeError("boom")),
                        "lba": FakeApplier("lba", can=False)})
    assert ctx.repo.get_job(boom.id).status == JobStatus.FAILED
    assert ctx.repo.get_job(other.id).status == JobStatus.NEEDS_MANUAL
    assert report.failed == 1 and report.needs_manual == 1


def test_email_without_smtp_is_manual(ctx, make_job):
    job = store_job(ctx, _job(make_job, 1), score=90)
    report = service.apply_to_jobs(live(ctx))  # real make_applier: no SMTP credentials → no mailer
    assert report.needs_manual == 1
    assert "SMTP" in ctx.repo.get_job(job.id).raw["apply_detail"]


def test_explicit_ids_channels_limit(ctx, make_job):
    pending = store_job(ctx, _job(make_job, 1), status=JobStatus.PENDING_REVIEW, score=10)
    done = store_job(ctx, _job(make_job, 2), status=JobStatus.APPLIED)
    report = _run(ctx, {"email": FakeApplier("email")}, job_ids=[pending.id, done.id, "nope"])
    assert report.applied == 1 and ctx.repo.get_job(pending.id).status == JobStatus.APPLIED
    assert any("introuvable" in d for d in report.details) and any("statut" in d for d in report.details)
    store_job(ctx, _job(make_job, 3), score=90)
    store_job(ctx, _job(make_job, 4, source="lba", apply_method=ApplyMethod.LBA), score=95)
    lba = FakeApplier("lba")
    assert _run(ctx, {"lba": lba, "email": FakeApplier("email")}, channels=["lba"]).applied == 1
    assert len(lba.calls) == 1
    assert _run(ctx, {"email": FakeApplier("email")}, limit=0).applied == 0


def test_missing_documents_are_prepared(ctx, make_job, monkeypatch):
    job = store_job(ctx, _job(make_job, 1), score=90, docs=False)
    import jobbot.documents.service as docs_service

    monkeypatch.setattr(docs_service, "prepare_documents", lambda c, job=None, company=None: write_docs(c, job.id))
    assert _run(ctx, {"email": FakeApplier("email")}).applied == 1
    assert ctx.repo.application_for_job(job.id).cv_path.endswith("CV_Jean_DUPONT.pdf")


def test_browser_opened_once_and_pause_between(ctx, make_job, monkeypatch):
    import jobbot.apply.browser as browser
    import jobbot.apply.hellowork as hw
    import jobbot.apply.linkedin as li

    fake_browser, sleeps = FakeBrowser(), []
    monkeypatch.setattr(browser, "open_browser", fake_browser.open)
    made: list[FakeApplier] = []

    def factory(channel):
        def build(ctx_, context, pacer, answerer_factory):
            assert context == "browser-context" and callable(answerer_factory)
            made.append(FakeApplier(channel))
            return made[-1]
        return build

    monkeypatch.setattr(li, "LinkedInApplier", factory("linkedin"))
    monkeypatch.setattr(hw, "HelloWorkApplier", factory("hellowork"))
    monkeypatch.setattr(service, "build_pacer", lambda c: _pacer(c, sleeps))
    store_job(ctx, _job(make_job, 1, source="linkedin", apply_method=ApplyMethod.LINKEDIN_EASY_APPLY), score=90)
    store_job(ctx, _job(make_job, 2, source="hellowork", apply_method=ApplyMethod.HELLOWORK), score=80)
    report = service.apply_to_jobs(live(ctx))
    assert report.applied == 2 and (fake_browser.opened, fake_browser.closed) == (1, 1)
    assert len(sleeps) == 1 and ctx.repo.count_actions_today("linkedin", "apply") == 1


def _pacer(ctx, sleeps):
    from jobbot.pacing import Pacer

    return Pacer(ctx.repo, ctx.settings.limits, sleep=sleeps.append)


def test_manual_record_approve_reject(ctx, make_job, make_company):
    from jobbot import review

    job = store_job(ctx, _job(make_job, 1, apply_method=ApplyMethod.MANUAL), status=JobStatus.NEEDS_MANUAL)
    app = review.record_manual_application(ctx, job, detail="Envoyée sur le site")
    assert app.method == ApplyMethod.EXTERNAL and app.next_followup_at is not None
    assert ctx.repo.get_job(job.id).status == JobStatus.APPLIED
    assert "déjà" in review.approve_job(ctx, job.id) and "impossible" in review.reject_job(ctx, job.id)
    other = store_job(ctx, _job(make_job, 2), status=JobStatus.PENDING_REVIEW)
    assert "validée" in review.approve_job(ctx, other.id)
    assert ctx.repo.get_job(other.id).status == JobStatus.APPROVED
    assert "écartée" in review.reject_job(ctx, other.id)
    assert "introuvable" in review.approve_job(ctx, "x") and "introuvable" in review.reject_job(ctx, "x")
    company = make_company(status="pending_review")
    ctx.repo.upsert_company(company)
    assert "validée" in review.review_company(ctx, company.id, True)
    assert ctx.repo.get_company(company.id).status == "approved"
    ctx.repo.upsert_company(company.model_copy(update={"status": "contacted"}))
    assert "déjà" in review.review_company(ctx, company.id, False)
    assert "introuvable" in review.review_company(ctx, "nope", True)


# --------------------------------------------------------------------------- EmailApplier


def _secrets():
    return Secrets(_env_file=None, smtp_user="jean.dupont@example.com", smtp_password="abcd efgh ijkl mnop")


def test_email_applier_sends_with_attachments(ctx, make_job):
    job = make_job()
    docs = write_docs(ctx, job.id)
    FakeSMTP.instances.clear()
    mailer = Mailer(_secrets(), ctx.repo, ctx.profile, smtp_factory=FakeSMTP)
    applier = EmailApplier(live(ctx), mailer)
    assert applier.can_apply(job) and not applier.can_apply(job.model_copy(update={"contact_email": "x@"}))
    res = applier.apply(job, docs)
    assert res.status == JobStatus.APPLIED and res.email_to == "rh@acme-logiciels.fr" and res.email_message_id
    msg = FakeSMTP.instances[-1].messages[0]
    assert msg["Subject"] == docs.email_subject
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert names == ["CV_Jean_DUPONT.pdf", "Lettre_de_motivation_Jean_DUPONT.pdf"]
    ctx.repo.blacklist_add("acme-logiciels.fr")
    assert not applier.can_apply(job)
    assert applier.apply(job, docs).status == JobStatus.SKIPPED


def test_email_applier_dry_run_and_errors(ctx, make_job, tmp_path):
    job = make_job()
    docs = write_docs(ctx, job.id)
    dry = Mailer(_secrets(), ctx.repo, ctx.profile, dry_run=True, outbox_dir=tmp_path / "outbox")
    res = EmailApplier(ctx, dry).apply(job, docs)
    assert res.ok and res.status == JobStatus.SKIPPED and list((tmp_path / "outbox").glob("*.eml"))
    assert EmailApplier(ctx, FakeMailer()).apply(job, docs).status == JobStatus.SKIPPED  # dry ctx, live mailer
    failing = EmailApplier(live(ctx), FakeMailer(error=MailError("refusé")))
    assert failing.apply(job, docs).status == JobStatus.FAILED and failing.fatal is None
    fatal = EmailApplier(live(ctx), FakeMailer(error=MailFatalError("mot de passe")))
    assert fatal.apply(job, docs).status == JobStatus.FAILED and fatal.fatal
    assert "suspendu" in fatal.apply(job, docs).detail
    no_cv = docs.model_copy(update={"cv_pdf": str(tmp_path / "absent.pdf")})
    assert EmailApplier(live(ctx), FakeMailer()).apply(job, no_cv).status == JobStatus.FAILED
    fallback = EmailApplier(ctx, FakeMailer()).build_email(job, docs.model_copy(update={"email_subject": ""}))
    assert fallback.subject.startswith("Candidature – ")


# --------------------------------------------------------------------------- LbaApplier


def _lba_job(make_job, **raw):
    base = {"source": "offres_emploi_lba", "id": OFFER_ID}
    return make_job(source="lba", external_id=OFFER_ID, apply_method=ApplyMethod.LBA, raw={**base, **raw})


def _detail(name="lba_detail_offer_ok.json", **updates):
    data = json.loads(fixture_path(name).read_text(encoding="utf-8"))
    data.update(updates)
    return data


def _detail_url(sub="offres_emploi_lba", item=OFFER_ID):
    return f"{LBA_SITE}/api/_private/jobs/{sub}/{item}"


@respx.mock
def test_lba_apply_posts_payload_once(ctx, make_job):
    job = _lba_job(make_job)
    docs = write_docs(ctx, job.id)
    respx.get(_detail_url()).mock(return_value=httpx.Response(200, json=_detail()))
    post = respx.post(APPLY_URL).mock(return_value=httpx.Response(200, json={}))
    applier = LbaApplier(live(ctx))
    assert applier.can_apply(job)
    res = applier.apply(job, docs)
    assert res.status == JobStatus.APPLIED, res.detail
    request = post.calls.last.request
    assert request.headers["Authorization"] == "Bearer TEST.JWT.TOKEN"
    body = json.loads(request.content)
    assert body["recipient_id"] == "recruiters_6aaa5a821124e472788457ab"
    assert body["applicant_phone"] == "+33612345678" and body["applicant_last_name"] == "DUPONT"
    assert body["applicant_attachment_content"].startswith("data:application/pdf;base64,")
    assert body["applicant_inscription_formation"] is True and body["applicant_contract_duration"] == "24 mois"
    assert "caller" not in body and body["application_url"].startswith(f"{LBA_SITE}/emploi/offres_emploi_lba/")
    assert ctx.repo.kv_get(KV_PREFIX + "siret:12345678900011")
    again = applier.apply(job, docs)
    assert again.status == JobStatus.SKIPPED and post.call_count == 1


@respx.mock
def test_lba_dry_run_never_posts(ctx, make_job):
    job = _lba_job(make_job)
    respx.get(_detail_url()).mock(return_value=httpx.Response(200, json=_detail()))
    post = respx.post(APPLY_URL).mock(return_value=httpx.Response(200, json={}))
    res = LbaApplier(ctx).apply(job, write_docs(ctx, job.id))
    assert res.ok and res.status == JobStatus.SKIPPED and "[dry-run]" in res.detail and not post.called


@respx.mock
@pytest.mark.parametrize(("detail", "status"), [
    ({"contact": {"hasEmail": False}}, JobStatus.NEEDS_MANUAL),
    ({"job": {"status": "Active", "to_applicant_questions": ["Pourquoi nous ?"]}}, JobStatus.NEEDS_MANUAL),
    ({"job": {"status": "Pourvue"}}, JobStatus.EXPIRED),
])
def test_lba_detail_checks(ctx, make_job, detail, status):
    job = _lba_job(make_job)
    respx.get(_detail_url()).mock(return_value=httpx.Response(200, json=_detail(**detail)))
    assert LbaApplier(live(ctx)).apply(job, write_docs(ctx, job.id)).status == status


@respx.mock
def test_lba_detail_errors(ctx, make_job):
    job = _lba_job(make_job)
    docs = write_docs(ctx, job.id)
    route = respx.get(_detail_url())
    route.mock(return_value=httpx.Response(404))
    assert LbaApplier(live(ctx)).apply(job, docs).status == JobStatus.EXPIRED
    route.mock(return_value=httpx.Response(403))
    assert LbaApplier(live(ctx)).apply(job, docs).status == JobStatus.FAILED
    route.mock(return_value=httpx.Response(200, text="<html>"))
    assert LbaApplier(live(ctx)).apply(job, docs).status == JobStatus.FAILED
    route.mock(side_effect=httpx.ConnectError("down"))
    down = LbaApplier(live(ctx))
    assert down.apply(job, docs).status == JobStatus.FAILED and down.fatal
    assert down.apply(job, docs).status == JobStatus.SKIPPED
    assert LbaApplier(ctx).apply(make_job(), docs).status == JobStatus.NEEDS_MANUAL


@respx.mock
@pytest.mark.parametrize(("code", "message", "status", "fatal"), [
    (429, "TOO_MANY_APPLICATIONS_PER_DAY", JobStatus.FAILED, True),
    (429, "TOO_MANY_APPLICATIONS_PER_OFFER", JobStatus.SKIPPED, False),
    (400, "EXPIRED", JobStatus.EXPIRED, False),
    (400, "INTERNAL_EMAIL", JobStatus.NEEDS_MANUAL, False),
    (400, "BURNER", JobStatus.FAILED, False),
    (503, "down", JobStatus.FAILED, True),
])
def test_lba_post_refusals(ctx, make_job, code, message, status, fatal):
    job = _lba_job(make_job)
    respx.get(_detail_url()).mock(return_value=httpx.Response(200, json=_detail()))
    respx.post(APPLY_URL).mock(return_value=httpx.Response(code, json={"statusCode": code, "message": message}))
    applier = LbaApplier(live(ctx))
    res = applier.apply(job, write_docs(ctx, job.id))
    assert res.status == status and bool(applier.fatal) is fatal


@respx.mock
def test_lba_recruiter_application(ctx, make_company):
    siret = "85190096900011"
    company = make_company(source="lba_recruteur", raw={"siret": siret, "hasEmail": True})
    docs = write_docs(ctx, company_id=company.id)
    respx.get(_detail_url("recruteurs_lba", siret)).mock(
        return_value=httpx.Response(200, json=_detail("../sources_api/lba_detail_recruiter.json")))
    post = respx.post(APPLY_URL).mock(return_value=httpx.Response(202, json={"id": "x"}))
    applier = LbaApplier(live(ctx))
    assert applier.apply_to_recruiter(company, docs).status == JobStatus.APPLIED
    assert json.loads(post.calls.last.request.content)["recipient_id"] == "partners_6a003c199a6be614e489356f"
    assert applier.apply_to_recruiter(company, docs).status == JobStatus.SKIPPED
    assert applier.apply_to_recruiter(make_company(), docs).status == JobStatus.SKIPPED


def test_lba_payload_helpers(tmp_path):
    assert normalize_phone("+33 6 12 34 56 78") == "+33612345678"
    assert contract_start("2026-10", date(2026, 9, 24)) == ["Octobre 2026"]
    assert contract_start("2025-10", date(2026, 9, 24)) == ["Dès que possible"]
    assert contract_start("", date(2026, 9, 24)) == ["Dès que possible"]
    long = "Phrase courte. " * 400
    assert len(fit_message(long)) <= 3000 and fit_message(long).endswith(".")
    assert fit_message("court") == "court"
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.7 test")
    name, content = build_attachment(pdf)
    assert name == "cv.pdf" and base64.b64decode(content.split(",", 1)[1]).startswith(b"%PDF")
    docx = tmp_path / "cv.docx"
    docx.write_bytes(b"PK\x03\x04")
    assert build_attachment(docx)[1].startswith("data:application/vnd.openxmlformats")
    for bad, data in (("cv.doc", b"xx"), ("fake.pdf", b"nope")):
        (tmp_path / bad).write_bytes(data)
        with pytest.raises(ValueError):
            build_attachment(tmp_path / bad)
    (tmp_path / "big.pdf").write_bytes(b"%PDF" + b"0" * 3_000_001)
    with pytest.raises(ValueError, match="trop lourd"):
        build_attachment(tmp_path / "big.pdf")
    with pytest.raises(ValueError, match="introuvable"):
        build_attachment(tmp_path / "absent.pdf")
