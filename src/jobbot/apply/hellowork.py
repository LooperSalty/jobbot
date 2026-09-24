"""HelloWork in-site application (modes ATS / Smartapply) with the logged-in persistent browser profile.

Selectors from docs/research/hellowork.md §3.2-3.3 / §6.4. Only the logged-in flow is automated: login,
Friendly Captcha, the anonymous "create an account" checkbox and the e-mail OTP are left to the human.
External modes (« Postuler sur le site du partenaire ») are handed over (NEEDS_MANUAL).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

from jobbot.apply.answers import FormAnswerer
from jobbot.apply.base import Applier, ApplyResult
from jobbot.apply.browser import (
    body_text,
    check_upload,
    detect_blocker,
    dismiss_cookie_banner,
    first_visible,
    fit_text,
    human_pause,
    is_logged_in,
    screenshot,
)
from jobbot.apply.formfill import fill_fields, make_resolver
from jobbot.apply.formscan import scan_fields
from jobbot.context import AppContext
from jobbot.models import ApplyMethod, JobOffer, JobStatus, TailoredDocuments
from jobbot.pacing import Pacer, QuotaExceeded
from jobbot.textutil import truncate

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- selectors
BASE = "https://www.hellowork.com"
OFFER_URL = BASE + "/fr-fr/emplois/{id}.html"
APPLY_HEADER = "a[data-cy=applyButtonHeader]"
APPLY_SECTION = "#postuler"
STEP_FRAME = "turbo-frame#offer-detail-step-frame"
FORM = "#offer-detail-main-step-form"
IDENTITY_FIELDS = {"Firstname": "first_name", "LastName": "last_name", "Email": "email"}
CV_INPUT = "input[data-cy=cv-uploader-input]"
RESUME_TOKEN = "input[name=JweHashResume]"
MESSAGE = "textarea[name=MotivationLetter]"
MESSAGE_TOGGLE = "label[data-cy=motivationFieldButton]"
CGU_BOX = "input[data-cy=cgu-box]"
SUBMIT = "button[data-cy=submitButton]"
NEXT_SEL = ("button[type=submit]", "button:has-text('Continuer')", "button:has-text('Postuler')",
            "button:has-text('Envoyer')")
EXTERNAL_RE = re.compile(r"site du (partenaire|recruteur)|site de l.employeur|partenaire", re.I)
SUCCESS_RE = re.compile(r"candidature (a )?(bien )?(été )?envoy|bien été transmise|merci pour votre candidature", re.I)
ALREADY_RE = re.compile(r"déjà postulé|vous avez (déjà )?postulé|candidature (déjà )?envoyée le", re.I)
EXPIRED_RE = re.compile(r"la page n.existe plus|offre n.est plus (disponible|en ligne)|cette offre a expiré", re.I)
TOKEN_CHANGED_JS = """(before) => { const e = document.querySelector("input[name='JweHashResume']");
  return !!(e && e.value && e.value !== before); }"""

# --------------------------------------------------------------------------- limits & timings
MAX_CV_BYTES = 2 * 1024 * 1024  # "Poids max. 2Mo"
MAX_MESSAGE_CHARS = 2900
MAX_EXTRA_STEPS = 4
NAV_TIMEOUT_MS = 30000
FORM_TIMEOUT_MS = 20000
UPLOAD_TIMEOUT_MS = 30000
STEP_WAIT_MS = 1500
CLICK_TIMEOUT_MS = 5000


def _result(ok: bool, status: JobStatus, detail: str, method: ApplyMethod = ApplyMethod.HELLOWORK) -> ApplyResult:
    return ApplyResult(ok=ok, status=status, method=method, detail=detail)


def _with_shot(detail: str, shot: str | None) -> str:
    return f"{detail} Capture : {shot}" if shot else detail


def offer_url(job: JobOffer) -> str:
    for url in (job.apply_url, job.url):
        if url and "hellowork.com" in url:
            return url
    return OFFER_URL.format(id=job.external_id)


class HelloWorkApplier(Applier):
    channel = "hellowork"

    def __init__(
        self, ctx: AppContext, context: BrowserContext, pacer: Pacer,
        answerer_factory: Callable[[JobOffer], FormAnswerer],
    ):
        self.ctx = ctx
        self.context = context
        self.pacer = pacer
        self.answerer_factory = answerer_factory

    def can_apply(self, job: JobOffer) -> bool:
        on_hellowork = job.source == "hellowork" or "hellowork.com" in f"{job.url} {job.apply_url or ''}"
        return job.apply_method == ApplyMethod.HELLOWORK and on_hellowork

    def apply(self, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        problem = check_upload(docs.cv_pdf, MAX_CV_BYTES)
        if problem:
            return _result(False, JobStatus.FAILED, f"CV inutilisable pour HelloWork : {problem}")
        try:
            self.pacer.ensure_available(self.channel)
        except QuotaExceeded as exc:
            return _result(False, JobStatus.SKIPPED, str(exc))
        page: Page | None = None
        try:
            page = self.context.new_page()
            return self._apply_on(page, job, docs)
        except Exception as exc:  # this job fails, the batch goes on
            log.exception("Candidature HelloWork %s en échec", job.id)
            shot = screenshot(self.ctx, page, f"hellowork_{job.external_id}_erreur") if page else None
            return _result(False, JobStatus.FAILED, _with_shot(f"Erreur HelloWork : {truncate(str(exc), 200)}.", shot))
        finally:
            if page is not None:
                try:
                    page.close()
                except PlaywrightError as exc:
                    log.debug("Fermeture de l'onglet : %s", exc)

    # ------------------------------------------------------------------ flow

    def _apply_on(self, page: Page, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        response = page.goto(offer_url(job), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        if (response is not None and response.status in (404, 410)) or EXPIRED_RE.search(body_text(page, limit=3000)):
            return _result(False, JobStatus.EXPIRED, "Offre HelloWork expirée (page supprimée).")
        dismiss_cookie_banner(page)
        stop = self._precheck(page, job)
        if stop is not None:
            return stop
        header = page.locator(APPLY_HEADER).first
        header.click(timeout=CLICK_TIMEOUT_MS)
        page.locator(FORM).wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
        stop = self._fill_step_one(page, job, docs)
        if stop is not None:
            return stop
        shot = screenshot(self.ctx, page, f"hellowork_{job.external_id}_avant_envoi")
        if self.ctx.dry_run:
            return _result(True, JobStatus.SKIPPED, _with_shot(
                "[dry-run] Formulaire HelloWork rempli (CV + message), candidature NON envoyée.", shot))
        return self._submit(page, job, docs)

    def _precheck(self, page: Page, job: JobOffer) -> ApplyResult | None:
        blocker = detect_blocker(page)
        if blocker:
            return self._manual(page, job, f"{blocker[1]} — connecte-toi avec « jobbot login hellowork »")
        if not is_logged_in(page, "hellowork"):
            return self._manual(page, job, "session HelloWork absente — lance « jobbot login hellowork » "
                                "(connexion, captcha et code e-mail restent manuels)")
        if ALREADY_RE.search(body_text(page, limit=5000)):
            return _result(False, JobStatus.SKIPPED, "Déjà postulé à cette offre sur HelloWork.")
        header = first_visible(page, (APPLY_HEADER,))
        if header is None:
            return self._manual(page, job, "bouton « Postuler » introuvable")
        label = header.inner_text().strip()
        if EXTERNAL_RE.search(label):
            return _result(False, JobStatus.NEEDS_MANUAL, f"Candidature HelloWork externe (« {label} ») : "
                           "à finaliser sur le site du recruteur.", ApplyMethod.EXTERNAL)
        return None

    def _fill_step_one(self, page: Page, job: JobOffer, docs: TailoredDocuments) -> ApplyResult | None:
        cgu = page.locator(CGU_BOX)
        if cgu.count() and cgu.first.is_visible() and not cgu.first.is_checked():
            return self._manual(page, job, "formulaire anonyme (création de compte + code e-mail) — connecte-toi "
                                "avec « jobbot login hellowork »")
        self._fill_identity(page)
        if not self._upload_cv(page, docs.cv_pdf):
            return self._manual(page, job, "le dépôt du CV n'a pas été confirmé par HelloWork")
        self._fill_message(page, docs.letter_text)
        return None

    def _fill_identity(self, page: Page) -> None:
        """Logged-in forms are pre-filled: only complete empty fields, never overwrite."""
        identity = self.ctx.profile.identity
        values = {"first_name": identity.first_name, "last_name": identity.last_name, "email": identity.email}
        for name, key in IDENTITY_FIELDS.items():
            field = page.locator(f"{APPLY_SECTION} [name='{name}']").first
            if field.count() and not field.input_value().strip():
                field.fill(values[key], timeout=CLICK_TIMEOUT_MS)
                human_pause(self.pacer)

    def _upload_cv(self, page: Page, cv_path: str) -> bool:
        token = page.locator(RESUME_TOKEN).first
        before = token.input_value() if token.count() else ""
        page.locator(CV_INPUT).first.set_input_files(cv_path, timeout=CLICK_TIMEOUT_MS)
        try:
            page.wait_for_function(TOKEN_CHANGED_JS, arg=before, timeout=UPLOAD_TIMEOUT_MS)
        except PlaywrightError as exc:
            log.warning("Dépôt du CV HelloWork non confirmé : %s", exc)
            return False
        human_pause(self.pacer)
        return True

    def _fill_message(self, page: Page, letter: str) -> None:
        area = page.locator(MESSAGE).first
        if not letter.strip() or not area.count():
            return
        if not area.is_visible():
            page.locator(MESSAGE_TOGGLE).first.click(timeout=CLICK_TIMEOUT_MS)
            area.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
        area.fill(fit_text(letter, MAX_MESSAGE_CHARS), timeout=CLICK_TIMEOUT_MS)
        human_pause(self.pacer)

    def _submit(self, page: Page, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        page.locator(SUBMIT).first.click(timeout=CLICK_TIMEOUT_MS)
        resolve = make_resolver(self.ctx.profile, docs, self.answerer_factory(job))
        for _ in range(MAX_EXTRA_STEPS):
            page.wait_for_timeout(STEP_WAIT_MS)
            frame = page.locator(STEP_FRAME).first if page.locator(STEP_FRAME).count() else page.locator(APPLY_SECTION).first
            blocker = detect_blocker(page)
            if blocker:
                return self._manual(page, job, f"{blocker[1]} après l'envoi du formulaire — à terminer à la main")
            if SUCCESS_RE.search(body_text(frame)):
                return _result(True, JobStatus.APPLIED, "Candidature HelloWork envoyée.")
            report = fill_fields(frame, scan_fields(frame), resolve, pause=lambda: human_pause(self.pacer))
            if report.missing:
                return self._manual(page, job, "questions complémentaires sans réponse fiable : "
                                    + " ; ".join(report.missing[:6]) + " — candidature à finaliser")
            button = first_visible(frame, NEXT_SEL)
            if button is None:
                break
            button.click(timeout=CLICK_TIMEOUT_MS)
        return self._manual(page, job, "état inconnu après l'envoi — vérifie « Mes candidatures » sur HelloWork")

    def _manual(self, page: Page, job: JobOffer, reason: str) -> ApplyResult:
        shot = screenshot(self.ctx, page, f"hellowork_{job.external_id}_manuel")
        return _result(False, JobStatus.NEEDS_MANUAL, _with_shot(f"HelloWork : {reason}.", shot))
