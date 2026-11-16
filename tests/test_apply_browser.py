"""Browser appliers (LinkedIn, HelloWork, assisted ATS) + browser helpers.

Real headless bundled Chromium against local HTML fixtures served on realistic URLs with ``context.route``
(a catch-all route aborts everything else: no network). Timings are shrunk so the whole file runs fast.
"""

from __future__ import annotations

import dataclasses
import json
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from jobbot import clock
from jobbot.apply import assisted, browser, formfill, hellowork, linkedin
from jobbot.apply.external import ExternalApplier, supported_url
from jobbot.apply.answers import FormAnswerer
from jobbot.config import Limits
from jobbot.llm import FakeLLM
from jobbot.models import ApplyMethod, ContractType, JobStatus, TailoredDocuments
from jobbot.pacing import Pacer

FIX = Path(__file__).parent / "fixtures" / "apply"
LI_JOB = "https://www.linkedin.com/jobs/view/4410135372/"
HW_JOB = "https://www.hellowork.com/fr-fr/emplois/79006307.html"
ATS_JOB = "https://careers.acme-logiciels.fr/offres/42"


# --------------------------------------------------------------------------- infrastructure


@pytest.fixture(scope="module")
def pw():
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="module")
def chromium(pw):
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()


class Server:
    """Serves fixtures on real-looking URLs and records POSTs; everything else is aborted."""

    def __init__(self, context):
        self.context = context
        self.posts: list[tuple[str, str]] = []
        context.route("**/*", lambda route: route.abort())

    def page(self, pattern: str, file: str | None = None, *, body: str = "", status: int = 200) -> None:
        html = (FIX / file).read_text(encoding="utf-8") if file else body
        self.context.route(re.compile(pattern), lambda route: route.fulfill(status=status, body=html,
                                                                          content_type="text/html; charset=utf-8"))

    def capture(self, pattern: str, response: str = '{"ok": true}') -> None:
        def handler(route):
            self.posts.append((route.request.url, route.request.post_data or ""))
            route.fulfill(status=200, body=response, content_type="application/json")

        self.context.route(re.compile(pattern), handler)

    def json(self, fragment: str) -> list[dict]:
        return [json.loads(body or "{}") for url, body in self.posts if fragment in url]


@pytest.fixture
def bctx(chromium):
    context = chromium.new_context(locale="fr-FR", viewport={"width": 1280, "height": 900})
    context.set_default_timeout(8000)
    yield context
    context.close()


@pytest.fixture
def server(bctx):
    s = Server(bctx)
    s.capture(r"/__test/")
    return s


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    for name, value in (("PAGE_SETTLE_MS", 50), ("OPEN_WAIT_MS", 150), ("STEP_WAIT_MS", 120), ("SENT_POLL_MS", 100),
                        ("DISCARD_WAIT_MS", 150)):
        monkeypatch.setattr(linkedin, name, value)
    monkeypatch.setattr(hellowork, "STEP_WAIT_MS", 250)
    monkeypatch.setattr(assisted, "SETTLE_MS", 50)
    monkeypatch.setattr(assisted, "AFTER_CLICK_MS", 400)
    monkeypatch.setattr(formfill, "TYPEAHEAD_TIMEOUT_MS", 1500)


@pytest.fixture
def docs(tmp_path) -> TailoredDocuments:
    cv = tmp_path / "CV_Jean_DUPONT.pdf"
    cv.write_bytes(b"%PDF-1.4\n% cv de test\n")
    cv_docx = tmp_path / "CV_Jean_DUPONT.docx"
    cv_docx.write_bytes(b"PK docx de test")
    letter_pdf = tmp_path / "Lettre_de_motivation_Jean_DUPONT.pdf"
    letter_pdf.write_bytes(b"%PDF-1.4\n% lettre\n")
    letter = "Madame, Monsieur,\n\n" + " ".join(["Je souhaite rejoindre votre équipe de développement."] * 60)
    return TailoredDocuments(cv_pdf=str(cv), cv_docx=str(cv_docx), letter_pdf=str(letter_pdf), letter_text=letter,
                             created_at=clock.now())


def llm_responder(prompt: str, _system, _fast) -> str:
    if "Pourquoi souhaitez-vous rejoindre" in prompt:
        return json.dumps({"answer": "Votre produit en React et Node.js correspond à mon projet.", "confidence": 0.9})
    return json.dumps({"answer": None, "confidence": 0})


def make_pacer(ctx, **limits) -> Pacer:
    return Pacer(ctx.repo, Limits(**limits), sleep=lambda _s: None)


def factory(ctx):
    llm = FakeLLM(llm_responder)
    return lambda job: FormAnswerer(ctx.profile, llm, ctx.repo, job)


def live(ctx):
    return dataclasses.replace(ctx, dry_run=False)


def has_shot(detail: str) -> bool:
    m = re.search(r"Capture : (\S+\.png)", detail)
    return bool(m) and Path(m.group(1)).is_file()


@pytest.mark.browser
def test_external_application_submits_and_requires_confirmation(ctx, bctx, server, make_job, docs):
    url = "https://jobs.lever.co/acme/123/apply"
    html = """<form id="application-form">
      <label>Full name<input name="name" required></label>
      <label>Email<input type="email" name="email" required></label>
      <label>CV<input type="file" name="resume" required></label>
      <button type="submit">Submit application</button>
    </form><script>document.querySelector('form').onsubmit = e => { e.preventDefault();
      document.body.innerHTML = '<p>Thank you for your application</p>'; };</script>"""
    server.page(r"^https://jobs\.lever\.co/acme/123/apply$", body=html)
    job = make_job(url=url, apply_url=url, apply_method=ApplyMethod.EXTERNAL)
    result = ExternalApplier(live(ctx), bctx, factory(ctx)).apply(job, docs)
    assert result.status == JobStatus.APPLIED


@pytest.mark.browser
def test_greenhouse_application_submits(ctx, bctx, server, make_job, docs):
    url = "https://boards.greenhouse.io/acme/jobs/123"
    server.page(r"^https://boards\.greenhouse\.io/acme/jobs/123$", body="""<form>
      <label>First name<input id="first_name" required></label>
      <label>Last name<input id="last_name" required></label>
      <label>Email<input id="email" type="email" required></label>
      <label>Resume<input type="file" name="resume" required></label>
      <input id="submit_app" type="submit" value="Submit application"></form>
      <script>document.querySelector('form').onsubmit = e => { e.preventDefault();
      document.body.innerHTML = '<p>Your application has been received</p>'; };</script>""")
    job = make_job(url=url, apply_url=url, apply_method=ApplyMethod.EXTERNAL)
    result = ExternalApplier(live(ctx), bctx, factory(ctx)).apply(job, docs)
    assert result.status == JobStatus.APPLIED


@pytest.mark.browser
def test_external_dry_run_does_not_submit(ctx, bctx, server, make_job, docs):
    url = "https://jobs.lever.co/acme/123/apply"
    server.page(r"^https://jobs\.lever\.co/acme/123/apply$", body="""<form>
      <label>Full name<input name="name" required></label>
      <label>Email<input type="email" name="email" required></label>
      <label>CV<input type="file" name="resume" required></label>
      <button type="submit">Submit application</button></form>""")
    job = make_job(url=url, apply_url=url, apply_method=ApplyMethod.EXTERNAL)
    result = ExternalApplier(ctx, bctx, factory(ctx)).apply(job, docs)
    assert result.ok and result.status == JobStatus.SKIPPED
    assert not supported_url("https://jobs.lever.co.example.com/acme")


@pytest.mark.browser
def test_external_required_checkbox_stays_for_user(ctx, bctx, server, make_job, docs):
    url = "https://jobs.lever.co/acme/123/apply"
    server.page(r"^https://jobs\.lever\.co/acme/123/apply$", body="""<form>
      <label>Email<input type="email" name="email" required></label>
      <label>CV<input type="file" name="resume" required></label>
      <label>Consent<input type="checkbox" name="consent" required></label>
      <button type="submit">Submit application</button></form>""")
    job = make_job(url=url, apply_url=url, apply_method=ApplyMethod.EXTERNAL)
    result = ExternalApplier(live(ctx), bctx, factory(ctx)).apply(job, docs)
    assert result.status == JobStatus.NEEDS_MANUAL and "case obligatoire" in result.detail


@pytest.mark.browser
def test_external_unconfirmed_submission_is_not_reported_as_sent(ctx, bctx, server, make_job, docs):
    url = "https://jobs.lever.co/acme/123/apply"
    server.page(r"^https://jobs\.lever\.co/acme/123/apply$", body="""<form>
      <label>Full name<input name="name" required></label>
      <label>Email<input type="email" name="email" required></label>
      <label>CV<input type="file" name="resume" required></label>
      <button type="submit">Submit application</button></form>
      <script>document.querySelector('form').onsubmit=e=>e.preventDefault()</script>""")
    job = make_job(url=url, apply_url=url, apply_method=ApplyMethod.EXTERNAL)
    result = ExternalApplier(live(ctx), bctx, factory(ctx)).apply(job, docs)
    assert result.status == JobStatus.NEEDS_MANUAL and "confirmation absente" in result.detail


# --------------------------------------------------------------------------- LinkedIn


@pytest.fixture
def li_site(bctx, server):
    bctx.add_cookies([{"name": "li_at", "value": "test", "domain": ".linkedin.com", "path": "/"}])
    server.page(r"^https://www\.linkedin\.com/jobs/view/4410135372/(\?.*)?$", "linkedin_job.html")
    server.page(r"^https://www\.linkedin\.com/jobs/view/4410135372/apply/", "linkedin_sdui_apply.html")
    return server


def li_job(make_job, scenario: str = "full"):
    return make_job(source="linkedin", external_id="4410135372", url=f"{LI_JOB}?scenario={scenario}", company="Acme",
                    apply_method=ApplyMethod.LINKEDIN_EASY_APPLY, contract_type=ContractType.ALTERNANCE,
                    contact_email=None)


def li_applier(ctx, bctx, **limits):
    return linkedin.LinkedInApplier(ctx, bctx, make_pacer(ctx, **limits), factory(ctx))


@pytest.mark.browser
def test_linkedin_full_flow_submits(ctx, bctx, li_site, make_job, docs):
    c = live(ctx)
    result = li_applier(c, bctx).apply(li_job(make_job), docs)
    assert (result.ok, result.status, result.method) == (True, JobStatus.APPLIED, ApplyMethod.LINKEDIN_EASY_APPLY)
    sent = li_site.json("submit")[0]
    assert sent["email-sel"] == "jean.dupont@example.com"
    assert sent["cc-sel"] == "France (+33)"
    assert sent["phone"] == "+33 6 12 34 56 78"
    assert sent["jobs-document-upload-file-input-upload-resume"] == ["CV_Jean_DUPONT.pdf"]
    assert (sent["q-react"], sent["q-kotlin"]) == ("1", "0")
    assert sent["q-en"] == "Professionnel"
    assert (sent["auth"], sent["master"]) == ("Oui", "Non")
    assert sent["q-city"] == "Metz, Grand Est, France"
    assert "React" in sent["q-why"]
    assert sent["q-salary"].startswith("Selon la grille")
    assert sent["q-news"] is False and sent["follow-company-checkbox"] is False
    assert not li_site.json("discard")


@pytest.mark.browser
def test_linkedin_dry_run_fills_then_discards(ctx, bctx, li_site, make_job, docs):
    result = li_applier(ctx, bctx).apply(li_job(make_job), docs)
    assert (result.ok, result.status) == (True, JobStatus.SKIPPED)
    assert "dry-run" in result.detail and has_shot(result.detail)
    assert not li_site.json("submit")
    discarded = li_site.json("discard")[0]
    assert discarded["phone"] and discarded["q-react"] == "1"


@pytest.mark.browser
def test_linkedin_unknown_required_question_needs_manual(ctx, bctx, li_site, make_job, docs):
    result = li_applier(live(ctx), bctx).apply(li_job(make_job, "unknown"), docs)
    assert result.status == JobStatus.NEEDS_MANUAL and not result.ok
    assert "Numéro de sécurité sociale" in result.detail and has_shot(result.detail)
    assert not li_site.json("submit") and li_site.json("discard")


@pytest.mark.browser
def test_linkedin_stuck_validation(ctx, bctx, li_site, make_job, docs):
    result = li_applier(live(ctx), bctx).apply(li_job(make_job, "stuck"), docs)
    assert result.status == JobStatus.NEEDS_MANUAL and "bloqué" in result.detail
    assert not li_site.json("submit")


@pytest.mark.browser
def test_linkedin_sdui_variant_in_shadow_dom(ctx, bctx, li_site, make_job, docs):
    result = li_applier(live(ctx), bctx).apply(li_job(make_job, "sdui"), docs)
    assert result.status == JobStatus.APPLIED, result.detail
    sent = li_site.json("submit")[0]
    assert sent == {"phone": "+33 6 12 34 56 78", "years": "2", "files": ["CV_Jean_DUPONT.pdf"]}


@pytest.mark.browser
@pytest.mark.parametrize(
    ("scenario", "status", "fragment"),
    [
        ("external", JobStatus.NEEDS_MANUAL, "site de l'employeur"),
        ("applied", JobStatus.SKIPPED, "Déjà postulé"),
        ("closed", JobStatus.EXPIRED, "n'accepte plus"),
        ("captcha", JobStatus.NEEDS_MANUAL, "captcha"),
        ("login", JobStatus.NEEDS_MANUAL, "connexion requise"),
    ],
)
def test_linkedin_page_states(ctx, bctx, li_site, make_job, docs, scenario, status, fragment):
    result = li_applier(live(ctx), bctx).apply(li_job(make_job, scenario), docs)
    assert result.status == status and fragment in result.detail
    assert not result.ok and not li_site.json("submit")
    if scenario == "external":
        assert result.method == ApplyMethod.EXTERNAL


@pytest.mark.browser
def test_linkedin_without_session(ctx, bctx, server, make_job, docs):
    server.page(r"^https://www\.linkedin\.com/jobs/view/", "linkedin_job.html")
    result = li_applier(live(ctx), bctx).apply(li_job(make_job), docs)
    assert result.status == JobStatus.NEEDS_MANUAL and "jobbot login linkedin" in result.detail


def test_linkedin_guards_without_browser(ctx, make_job, docs, tmp_path, monkeypatch):
    applier = li_applier(ctx, None)
    job = li_job(make_job)
    assert applier.can_apply(job)
    assert not applier.can_apply(job.model_copy(update={"apply_method": ApplyMethod.EXTERNAL}))
    big = tmp_path / "gros.pdf"
    big.write_bytes(b"0" * (linkedin.MAX_CV_BYTES + 1))
    result = applier.apply(job, docs.model_copy(update={"cv_pdf": str(big)}))
    assert result.status == JobStatus.FAILED and "trop volumineux" in result.detail
    quota = li_applier(ctx, None, linkedin_per_day=0).apply(job, docs)
    assert quota.status == JobStatus.SKIPPED and "Quota" in quota.detail
    assert linkedin.job_url(job.model_copy(update={"url": "https://x.org"})) == LI_JOB


@pytest.mark.browser
def test_linkedin_unexpected_error_is_failed(ctx, bctx, make_job, docs, monkeypatch):
    applier = li_applier(ctx, bctx)

    def boom(*_a):
        raise RuntimeError("crash navigateur")

    monkeypatch.setattr(applier, "_apply_on", boom)
    result = applier.apply(li_job(make_job), docs)
    assert result.status == JobStatus.FAILED and "crash navigateur" in result.detail


# --------------------------------------------------------------------------- HelloWork


@pytest.fixture
def hw_site(server):
    server.page(r"^https://www\.hellowork\.com/fr-fr/emplois/79006307\.html", "hellowork_offer.html")
    server.capture(r"^https://www\.hellowork\.com/fr-fr/(offres|rebond)/")
    return server


def hw_job(make_job, scenario: str = "full"):
    return make_job(source="hellowork", external_id="79006307", url=f"{HW_JOB}?scenario={scenario}",
                    apply_method=ApplyMethod.HELLOWORK, company="Chausséa", contact_email=None)


def hw_applier(ctx, bctx):
    return hellowork.HelloWorkApplier(ctx, bctx, make_pacer(ctx), factory(ctx))


@pytest.mark.browser
def test_hellowork_full_flow(ctx, bctx, hw_site, make_job, docs):
    result = hw_applier(live(ctx), bctx).apply(hw_job(make_job), docs)
    assert (result.ok, result.status) == (True, JobStatus.APPLIED), result.detail
    sent = hw_site.json("postcandidateinformation")[0]
    assert sent["JweHashResume"] == "tok-CV_Jean_DUPONT.pdf"
    assert sent["Firstname"] == "Jean" and sent["Email"] == "jean.dupont@example.com"
    assert sent["MotivationLetter"].startswith("Madame") and len(sent["MotivationLetter"]) <= 2900
    assert not hw_site.json("rebond")  # never accept the multi-apply bounce


@pytest.mark.browser
def test_hellowork_dry_run_stops_before_submit(ctx, bctx, hw_site, make_job, docs):
    result = hw_applier(ctx, bctx).apply(hw_job(make_job), docs)
    assert (result.ok, result.status) == (True, JobStatus.SKIPPED)
    assert "dry-run" in result.detail and has_shot(result.detail)
    assert not hw_site.json("postcandidateinformation")


@pytest.mark.browser
def test_hellowork_extra_questions_answered(ctx, bctx, hw_site, make_job, docs):
    result = hw_applier(live(ctx), bctx).apply(hw_job(make_job, "extra"), docs)
    assert result.status == JobStatus.APPLIED, result.detail
    assert hw_site.json("offres/extra")[0] == {"Availability": "Oui", "Phone": "+33 6 12 34 56 78"}


@pytest.mark.browser
@pytest.mark.parametrize(
    ("scenario", "account", "status", "fragment"),
    [
        ("external", True, JobStatus.NEEDS_MANUAL, "site du partenaire"),
        ("already", True, JobStatus.SKIPPED, "Déjà postulé"),
        ("nosession", False, JobStatus.NEEDS_MANUAL, "session HelloWork absente"),
        ("anon", True, JobStatus.NEEDS_MANUAL, "formulaire anonyme"),
        ("otp", True, JobStatus.NEEDS_MANUAL, "OTP"),
        ("prequal", True, JobStatus.NEEDS_MANUAL, "Code d’accès"),
    ],
)
def test_hellowork_states(ctx, bctx, hw_site, make_job, docs, scenario, account, status, fragment):
    hw_site.capture(r"/fr-fr/compte/accountdata", json.dumps({"IsAuthenticated": account}))
    result = hw_applier(live(ctx), bctx).apply(hw_job(make_job, scenario), docs)
    assert result.status == status and fragment in result.detail, result.detail
    assert not result.ok


@pytest.mark.browser
def test_hellowork_upload_not_confirmed(ctx, bctx, hw_site, make_job, docs, monkeypatch):
    monkeypatch.setattr(hellowork, "UPLOAD_TIMEOUT_MS", 600)
    result = hw_applier(live(ctx), bctx).apply(hw_job(make_job, "noupload"), docs)
    assert result.status == JobStatus.NEEDS_MANUAL and "dépôt du CV" in result.detail
    assert not hw_site.json("postcandidateinformation")


@pytest.mark.browser
def test_hellowork_expired_offer(ctx, bctx, server, make_job, docs):
    server.page(r"^https://www\.hellowork\.com/", body="<h1>Erreur 410 - La page n'existe plus</h1>", status=410)
    result = hw_applier(live(ctx), bctx).apply(hw_job(make_job), docs)
    assert result.status == JobStatus.EXPIRED


def test_hellowork_guards_without_browser(ctx, make_job, docs, tmp_path, monkeypatch):
    applier = hw_applier(ctx, None)
    job = hw_job(make_job)
    assert applier.can_apply(job)
    assert applier.can_apply(job.model_copy(update={"source": "lba"}))  # partner offer routed to HelloWork
    assert not applier.can_apply(job.model_copy(update={"apply_method": ApplyMethod.EXTERNAL}))
    big = tmp_path / "gros.pdf"
    big.write_bytes(b"0" * (hellowork.MAX_CV_BYTES + 1))
    result = applier.apply(job, docs.model_copy(update={"cv_pdf": str(big)}))
    assert result.status == JobStatus.FAILED and "2 Mo" in result.detail
    blocked = hellowork.HelloWorkApplier(ctx, None, make_pacer(ctx, hellowork_per_day=0), factory(ctx))
    assert blocked.apply(job, docs).status == JobStatus.SKIPPED
    assert hellowork.offer_url(job.model_copy(update={"url": "", "apply_url": None})) == HW_JOB

    def boom(*_a):
        raise RuntimeError("crash")

    failing = hellowork.HelloWorkApplier(ctx, _FakeContext(), make_pacer(ctx), factory(ctx))
    monkeypatch.setattr(failing, "_apply_on", boom)
    assert failing.apply(job, docs).status == JobStatus.FAILED


class _FakePage:
    def screenshot(self, **_kw):
        raise OSError("pas d'écran")

    def close(self):
        return None


class _FakeContext:
    def new_page(self):
        return _FakePage()


# --------------------------------------------------------------------------- assisted (external ATS)


@pytest.fixture
def ats_site(server):
    server.page(r"^https://careers\.acme-logiciels\.fr/offres/42(\?.*)?$", "ats_offer.html")
    server.page(r"^https://careers\.acme-logiciels\.fr/offres/42/candidature", "ats_form.html")
    return server


def ats_job(make_job, scenario: str = "reveal", url: str | None = None):
    return make_job(source="wttj", external_id="42", url=url or f"{ATS_JOB}?scenario={scenario}", company="Acme",
                    apply_method=ApplyMethod.EXTERNAL, contact_email=None)


def assist(ctx, bctx, job, docs):
    return assisted.assisted_apply(ctx, bctx, job, docs, factory(ctx)(job))


@pytest.mark.browser
def test_assisted_prefills_but_never_submits(ctx, bctx, ats_site, make_job, docs):
    result = assist(ctx, bctx, ats_job(make_job), docs)
    assert (result.ok, result.status, result.method) == (False, JobStatus.NEEDS_MANUAL, ApplyMethod.EXTERNAL)
    assert "pré-rempli" in result.detail and has_shot(result.detail)
    assert "Numéro de sécurité sociale" in result.detail and "politique de confidentialité" in result.detail
    page = bctx.pages[-1]  # left open for the human
    values = page.evaluate("""() => ({fn: fn.value, last: document.querySelector('[name=last_name]').value,
        email: document.querySelector('[name=contact]').value, tel: tel.value, city: city.value, li: li.value,
        cv: Array.from(cv.files).map(f => f.name), lm: lm.value, level: level.value,
        dispo: (document.querySelector('[name=dispo]:checked') || {}).value, rgpd: document.querySelector('[name=rgpd]').checked})""")
    assert values["fn"] == "Jean" and values["last"] == "DUPONT" and values["email"] == "jean.dupont@example.com"
    assert values["tel"] == "+33 6 12 34 56 78" and values["city"] == "Metz" and "linkedin.com/in/" in values["li"]
    assert values["cv"] == ["CV_Jean_DUPONT.pdf"] and 0 < len(values["lm"]) <= 1500
    assert values["level"] == "Bac+3" and values["dispo"] == "oui" and values["rgpd"] is False
    assert not ats_site.json("submit")


@pytest.mark.browser
def test_assisted_follows_new_tab_and_prefers_docx(ctx, bctx, ats_site, make_job, docs):
    result = assist(ctx, bctx, ats_job(make_job, "newtab"), docs)
    assert result.status == JobStatus.NEEDS_MANUAL and "pré-rempli" in result.detail
    page = bctx.pages[-1]
    assert page.url.endswith("/offres/42/candidature")
    got = page.evaluate("""() => ({first: document.querySelector('[name=firstname]').value,
        last: document.querySelector('[name=lastname]').value, email: document.querySelector('[name=email_address]').value,
        cv: Array.from(document.querySelector('[name=resume_file]').files).map(f => f.name),
        photo: document.querySelector('[name=photo]').files.length})""")
    assert got == {"first": "Jean", "last": "DUPONT", "email": "jean.dupont@example.com", "cv": ["CV_Jean_DUPONT.docx"],
                   "photo": 0}
    assert not ats_site.json("submit")


@pytest.mark.browser
@pytest.mark.parametrize(
    ("scenario", "status", "fragment"),
    [("closed", JobStatus.EXPIRED, "fermée"), ("login", JobStatus.NEEDS_MANUAL, "connexion requise")],
)
def test_assisted_states(ctx, bctx, ats_site, make_job, docs, scenario, status, fragment):
    result = assist(ctx, bctx, ats_job(make_job, scenario), docs)
    assert result.status == status and fragment in result.detail


@pytest.mark.browser
def test_assisted_404_and_errors(ctx, bctx, server, make_job, docs, monkeypatch):
    server.page(r"^https://gone\.example\.fr/", body="Not found", status=404)
    assert assist(ctx, bctx, ats_job(make_job, url="https://gone.example.fr/job/1"), docs).status == JobStatus.EXPIRED
    no_url = ats_job(make_job).model_copy(update={"url": "", "apply_url": None})
    assert assist(ctx, bctx, no_url, docs).status == JobStatus.FAILED

    def boom(*_a):
        raise RuntimeError("DOM inattendu")

    monkeypatch.setattr(assisted, "_assist", boom)
    result = assist(ctx, bctx, ats_job(make_job), docs)
    assert result.status == JobStatus.NEEDS_MANUAL and "DOM inattendu" in result.detail


@pytest.mark.browser
def test_assisted_lever_known_selectors(ctx, bctx, server, make_job, docs):
    server.page(r"^https://jobs\.lever\.co/acme/", "lever_apply.html")
    job = ats_job(make_job, url="https://jobs.lever.co/acme/abc-123/apply")
    result = assist(ctx, bctx, job, docs)
    assert "(lever)" in result.detail
    page = bctx.pages[-1]
    got = page.evaluate("""() => Object.fromEntries(['name', 'email', 'phone', 'org', 'urls[LinkedIn]', 'urls[GitHub]',
        'comments'].map(n => [n, document.querySelector(`[name="${n}"]`).value]))""")
    assert got["name"] == "Jean DUPONT" and got["email"] == "jean.dupont@example.com"
    assert got["urls[GitHub]"] == "https://github.com/jean-dupont-exemple" and got["org"] == "Indépendant"
    assert got["comments"].startswith("Madame")
    assert page.evaluate("document.querySelector('[name=resume]').files[0].name") == "CV_Jean_DUPONT.pdf"
    assert not server.json("submit")


@pytest.mark.browser
def test_autofill_page_reaches_iframes(ctx, bctx, server, make_job, docs):
    server.page(r"^https://www\.acme\.fr/careers/", body='<h1>Carrières</h1><iframe width="900" height="700" '
                'src="https://job-boards.greenhouse.io/embed/job_app?for=acme&token=7194969"></iframe>')
    server.page(r"^https://job-boards\.greenhouse\.io/embed/", "greenhouse_embed.html")
    page = bctx.new_page()
    page.goto("https://www.acme.fr/careers/7194969")
    page.frame_locator("iframe").locator("#first_name").wait_for()
    job = ats_job(make_job)
    count = assisted.autofill_page(page, ctx.profile, docs, factory(ctx)(job))
    frame = page.frames[1]
    assert count >= 6
    assert frame.locator("#first_name").input_value() == "Jean"
    assert frame.locator("#question_59674544").input_value().startswith("https://www.linkedin.com/in/")
    assert assisted.detect_ats(frame.url) == "greenhouse"
    assert assisted.detect_ats("https://acme.wd3.myworkdayjobs.com/fr-FR/careers/job/x") == "workday"
    assert assisted.detect_ats("https://example.org") is None


# --------------------------------------------------------------------------- browser helpers


class _ProxyPlaywright:
    """Reuses the running Playwright (a second sync_playwright cannot start in the same thread)."""

    def __init__(self, real):
        self.chromium = real.chromium
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True


@pytest.mark.browser
def test_open_browser_falls_back_to_bundled_chromium(ctx, pw, monkeypatch):
    proxy = _ProxyPlaywright(pw)
    monkeypatch.setattr(browser, "sync_playwright", lambda: proxy)
    settings = ctx.settings.model_copy(update={"browser_channel": "canal-inexistant", "headless": True})
    with browser.open_browser(dataclasses.replace(ctx, settings=settings)) as context:
        page = context.new_page()
        page.set_content("<p>ok</p>")
        assert page.inner_text("p") == "ok"
        assert page.evaluate("navigator.language") == "fr-FR"
    assert proxy.stopped
    assert (ctx.root / "data" / "browser-profile").is_dir()


class _LoginPage:
    def __init__(self, fail_goto: bool = False, closed: bool = False):
        self.fail_goto, self.closed = fail_goto, closed

    def goto(self, url, **_kw):
        if self.fail_goto:
            raise browser.PlaywrightError("net::ERR")
        self.url = url

    def is_closed(self):
        return self.closed


class _LoginContext:
    def __init__(self, page):
        self.pages = [page]


def _fake_open(page):
    @contextmanager
    def _open(_ctx, *, headless=None):
        assert headless is False  # login always happens in a visible window
        yield _LoginContext(page)

    return _open


def test_login_waits_for_human(ctx, monkeypatch):
    states = iter([False, False, True])
    ticks = iter(range(0, 10000, 2))
    monkeypatch.setattr(browser, "open_browser", _fake_open(_LoginPage()))
    monkeypatch.setattr(browser, "is_logged_in", lambda _p, _s: next(states))
    monkeypatch.setattr(browser, "_sleep", lambda _s: None)
    monkeypatch.setattr(browser, "_monotonic", lambda: next(ticks))
    assert browser.login(ctx, "hellowork", timeout_s=60) is True


def test_login_timeout_closed_window_and_errors(ctx, monkeypatch):
    ticks = iter(range(0, 10000, 5))
    monkeypatch.setattr(browser, "open_browser", _fake_open(_LoginPage()))
    monkeypatch.setattr(browser, "is_logged_in", lambda _p, _s: False)
    monkeypatch.setattr(browser, "_sleep", lambda _s: None)
    monkeypatch.setattr(browser, "_monotonic", lambda: next(ticks))
    assert browser.login(ctx, "linkedin", timeout_s=20) is False
    monkeypatch.setattr(browser, "open_browser", _fake_open(_LoginPage(closed=True)))
    assert browser.login(ctx, "linkedin", timeout_s=20) is False
    monkeypatch.setattr(browser, "open_browser", _fake_open(_LoginPage(fail_goto=True)))
    assert browser.login(ctx, "wttj", timeout_s=20) is False
    with pytest.raises(ValueError):
        browser.login(ctx, "monster", timeout_s=1)  # type: ignore[arg-type]


@pytest.mark.browser
def test_is_logged_in_per_site(bctx, server):
    server.page(r"^https://www\.linkedin\.com/", body="<p>feed</p>")
    server.page(r"^https://www\.hellowork\.com/fr-fr/$", body="<body><p>accueil</p></body>")
    server.capture(r"/fr-fr/compte/accountdata", json.dumps({"IsAuthenticated": True}))
    server.page(r"^https://www\.welcometothejungle\.com/fr/me", body='<a href="/fr/me/">Mon espace</a>')
    server.page(r"^https://www\.welcometothejungle\.com/fr/signin", body="<p>Connexion</p>")
    page = bctx.new_page()
    page.goto("https://www.linkedin.com/feed/")
    assert browser.is_logged_in(page, "linkedin") is False
    bctx.add_cookies([{"name": "li_at", "value": "x", "domain": ".linkedin.com", "path": "/"}])
    assert browser.is_logged_in(page, "linkedin") is True
    page.goto("https://www.linkedin.com/checkpoint/challenge/1")
    assert browser.is_logged_in(page, "linkedin") is False
    page.goto("https://www.hellowork.com/fr-fr/")
    assert browser.is_logged_in(page, "hellowork") is True
    page.goto("about:blank")
    assert browser.is_logged_in(page, "hellowork") is False
    page.goto("https://www.welcometothejungle.com/fr/me")
    assert browser.is_logged_in(page, "wttj") is True
    page.goto("https://www.welcometothejungle.com/fr/signin")
    assert browser.is_logged_in(page, "wttj") is False
    with pytest.raises(ValueError):
        browser.is_logged_in(page, "monster")  # type: ignore[arg-type]
    page.close()
    assert browser.is_logged_in(page, "hellowork") is False  # closed page → Playwright error → False


@pytest.mark.browser
@pytest.mark.parametrize(
    ("html", "kind"),
    [
        ("<p>Tout va bien</p>", None),
        ("<iframe title='reCAPTCHA captcha' src='about:blank'></iframe>", "captcha"),
        ("<input autocomplete='one-time-code'>", "otp"),
        ("<form><input type='password'></form>", "login"),
        ("<h1>Let's do a quick security check</h1>", "checkpoint"),
    ],
)
def test_detect_blocker(bctx, html, kind):
    page = bctx.new_page()
    page.set_content(html)
    found = browser.detect_blocker(page)
    assert (found[0] if found else None) == kind
    if kind == "captcha":
        assert browser.detect_blocker(page, captcha=False) is None


@pytest.mark.browser
def test_screenshot_cookie_banner_and_scan(ctx, bctx):
    page = bctx.new_page()
    page.set_content("""<div id="b"><button id="onetrust-reject-all-handler" onclick="b.remove()">Tout refuser</button></div>
      <div id="host"></div><input type="date" required aria-label="Date de naissance">
      <label for="x">Pays</label><input id="x" name="country">
      <script>host.attachShadow({mode:'open'}).innerHTML='<label for="s">Prénom</label><input id="s" required>';</script>""")
    assert browser.dismiss_cookie_banner(page) is True
    assert browser.dismiss_cookie_banner(page) is False
    shot = browser.screenshot(ctx, page, "test écran")
    assert shot and Path(shot).is_file() and "screens" in shot
    fields = formfill.scan_fields(page)
    labels = {f.label: f for f in fields}
    assert labels["Prénom"].required and labels["Date de naissance"].supported is False
    page.close()
    assert browser.screenshot(ctx, page, "fermée") is None
    assert formfill.scan_fields(page) == []


def test_small_helpers(tmp_path, ctx):
    calls = []
    browser.human_pause(Pacer(ctx.repo, Limits(), sleep=calls.append))
    assert calls and 0.4 <= calls[0] <= 1.6
    assert browser.check_upload(None, 10) == "aucun document à joindre"
    assert "introuvable" in browser.check_upload(tmp_path / "absent.pdf", 10)
    img = tmp_path / "cv.png"
    img.write_bytes(b"x")
    assert "format" in browser.check_upload(img, 10)
    text = "Première phrase. " * 20
    assert browser.fit_text(text, 100).endswith(".") and len(browser.fit_text(text, 100)) <= 100
    assert browser.fit_text("x" * 50, 10).endswith("…")
    assert browser.fit_text("court", 0) == "court"
    assert formfill.best_option_index(["Metzervisse, Grand Est", "Metz, Grand Est"], "Metz") == 1
    assert formfill.best_option_index(["Nancy", "Metzeresche"], "Metz") == 1
    assert formfill.best_option_index(["Nancy"], "Metz") == 0


# --------------------------------------------------------------------------- live checks (read-only)
# Run explicitly: pytest tests/test_apply_browser.py -m live
# Only public GETs (robots-allowed offer page, public Lever board): no login, no upload, no submit.

HW_LIVE_OFFER = "https://www.hellowork.com/fr-fr/emplois/79006307.html"


@pytest.mark.live
@pytest.mark.browser
def test_live_hellowork_offer_selectors(chromium):
    context = chromium.new_context(locale="fr-FR")
    try:
        page = context.new_page()
        response = page.goto(HW_LIVE_OFFER, wait_until="domcontentloaded", timeout=30000)
        if response is None or response.status in (404, 410):
            pytest.skip("offre HelloWork expirée")
        browser.dismiss_cookie_banner(page)
        header = page.locator(hellowork.APPLY_HEADER).first
        assert header.count() == 1
        assert "Postuler" in header.inner_text()
        assert page.locator(hellowork.APPLY_SECTION).count() == 1
        assert browser.is_logged_in(page, "hellowork") is False  # anonymous: accountdata IsAuthenticated=false
        assert browser.detect_blocker(page) is None
    finally:
        context.close()


@pytest.mark.live
@pytest.mark.browser
def test_live_lever_form_selectors(chromium):
    from jobbot.http import make_client

    with make_client() as client:
        postings = client.get("https://api.lever.co/v0/postings/qonto", params={"mode": "json", "limit": 1}).json()
    if not postings:
        pytest.skip("aucune offre Lever publique")
    context = chromium.new_context(locale="fr-FR")
    try:
        page = context.new_page()
        page.goto(postings[0]["applyUrl"], wait_until="domcontentloaded", timeout=30000)
        assert assisted.detect_ats(page.url) == "lever"
        for key in ("full_name", "email", "phone"):
            assert page.locator(assisted.ATS_FIELDS["lever"][key]).count() >= 1, key
        assert page.locator("input[type=file][name=resume]").count() == 1
        labels = [f.label for f in formfill.scan_fields(page)]
        assert labels, "aucun champ lu sur le formulaire Lever"
    finally:
        context.close()
