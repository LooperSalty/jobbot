"""LinkedIn Easy Apply (« Candidature simplifiée ») with the logged-in persistent browser profile.

Selectors come from docs/research/linkedin.md §3.5 / §6.6: variant A (Ember modal, role=dialog) and
variant B (SDUI anchor ``openSDUIApplyFlow``, form in a shadow root). FR + EN labels. Never automates
login, 2FA, captcha or checkpoints: those stop the application with a screenshot.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from playwright.sync_api import BrowserContext, Locator, Page
from playwright.sync_api import Error as PlaywrightError

from jobbot.apply.answers import FormAnswerer
from jobbot.apply.base import Applier, ApplyResult
from jobbot.apply.browser import (
    body_text,
    check_upload,
    detect_blocker,
    first_visible,
    human_pause,
    is_logged_in,
    screenshot,
)
from jobbot.apply.formfill import error_messages, fill_fields, make_resolver, upload_documents
from jobbot.apply.formscan import scan_fields
from jobbot.context import AppContext
from jobbot.models import ApplyMethod, JobOffer, JobStatus, TailoredDocuments
from jobbot.pacing import Pacer, QuotaExceeded
from jobbot.textutil import normalize, truncate

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- selectors (FR + EN)
EASY_APPLY_LABELS = ("candidature simplifiee", "easy apply")
SDUI_OPENER = "a[href*='openSDUIApplyFlow=true']"
APPLY_BUTTONS = ("button#jobs-apply-button-id", "button.jobs-apply-button", ".jobs-apply-button--top-card button")
MODAL = "div.jobs-easy-apply-modal[role='dialog'], div[data-test-modal][role='dialog']"
FIELD_SEL = (
    "[data-test-form-element] input, [data-test-form-element] select, [data-test-form-element] textarea, "
    ".fb-dash-form-element input, .fb-dash-form-element select, .fb-dash-form-element textarea, "
    "fieldset[data-test-form-builder-radio-button-form-component] input, "
    "[data-test-single-line-text-form-component] input, [data-test-text-entity-list-form-component] select, "
    "input[id*='easyApplyFormElement'], select[id*='easyApplyFormElement'], textarea[id*='easyApplyFormElement']"
)
EXCLUDE_SEL = ".jobs-document-upload-redesign-card__container, .ui-attachment, #follow-company-checkbox, [type=file]"
NEXT_SEL = (
    "button[data-easy-apply-next-button]", "button[aria-label='Continue to next step']",
    "button[aria-label='Passer à l’étape suivante']", "button[aria-label=\"Passer à l'étape suivante\"]",
    "button:has-text('Suivant')", "button:has-text('Next')", "button:has-text('Continuer')",
)
REVIEW_SEL = (
    "button[aria-label='Review your application']", "button[aria-label='Vérifiez votre candidature']",
    "button:has-text('Vérifier')", "button:has-text('Review')",
)
SUBMIT_SEL = (
    "button[data-live-test-easy-apply-submit-button]", "button[aria-label='Submit application']",
    "button[aria-label='Envoyer la candidature']", "button:has-text('Envoyer la candidature')",
    "button:has-text('Submit application')",
)
DISMISS_SEL = ("button[data-test-modal-close-btn]", "button[aria-label='Dismiss']", "button[aria-label='Ignorer']")
DISCARD_SEL = (
    "button[data-control-name='discard_application_confirm_btn']", "button:has-text('Supprimer')",
    "button:has-text('Discard')",
)
DONE_SEL = ("button:has-text('Terminé')", "button:has-text('Done')", "button[aria-label='Dismiss']")
ERROR_SEL = ".artdeco-inline-feedback--error, .artdeco-inline-feedback__message"
FOLLOW_CB = "input#follow-company-checkbox"
FOLLOW_LABEL = "label[for='follow-company-checkbox']"
TOP_CARD_SEL = (
    ".job-details-jobs-unified-top-card__container--two-pane, .jobs-unified-top-card, .jobs-details-top-card, "
    ".jobs-s-apply"
)
ALREADY_APPLIED_SEL = ".jobs-s-apply__application-link"
SENT_RE = re.compile(r"candidature (a été )?envoyée|application (was )?sent|your application was sent", re.I)
ALREADY_APPLIED_RE = re.compile(
    r"candidature envoyée il y a|vous avez postulé|déjà postulé|applied \d+ \w+ ago|you applied|application submitted",
    re.I,
)
CLOSED_RE = re.compile(
    r"n.accepte plus (de )?candidatures|no longer accepting applications|candidatures (sont )?closes|"
    r"offre (a )?expiré|this job is no longer available|n.est plus disponible",
    re.I,
)
LIMIT_RE = re.compile(
    r"limite quotidienne|daily (application )?limit|exceeded the daily|limite de candidatures|reached the limit", re.I
)

# --------------------------------------------------------------------------- limits & timings (ms)
MAX_STEPS = 12
MAX_CV_BYTES = 5 * 1024 * 1024  # "Max file size is (5 MB)" in the upload button label
NAV_TIMEOUT_MS = 30000
PAGE_SETTLE_MS = 2000
OPEN_WAIT_MS = 1800
STEP_WAIT_MS = 1500
CLICK_TIMEOUT_MS = 5000
SENT_POLL_MS = 1000
SENT_POLLS = 12
DISCARD_WAIT_MS = 1500


def _result(ok: bool, status: JobStatus, detail: str, method: ApplyMethod = ApplyMethod.LINKEDIN_EASY_APPLY) -> ApplyResult:
    return ApplyResult(ok=ok, status=status, method=method, detail=detail)


def _with_shot(detail: str, shot: str | None) -> str:
    return f"{detail} Capture : {shot}" if shot else detail


def job_url(job: JobOffer) -> str:
    if "linkedin.com" in (job.url or ""):
        return job.url
    return f"https://www.linkedin.com/jobs/view/{job.external_id}/"


class LinkedInApplier(Applier):
    channel = "linkedin"

    def __init__(
        self, ctx: AppContext, context: BrowserContext, pacer: Pacer,
        answerer_factory: Callable[[JobOffer], FormAnswerer],
    ):
        self.ctx = ctx
        self.context = context
        self.pacer = pacer
        self.answerer_factory = answerer_factory

    def can_apply(self, job: JobOffer) -> bool:
        return job.source == "linkedin" and job.apply_method == ApplyMethod.LINKEDIN_EASY_APPLY

    def apply(self, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        problem = check_upload(docs.cv_pdf, MAX_CV_BYTES)
        if problem:
            return _result(False, JobStatus.FAILED, f"CV inutilisable pour LinkedIn : {problem}")
        try:
            self.pacer.ensure_available(self.channel)
        except QuotaExceeded as exc:
            return _result(False, JobStatus.SKIPPED, str(exc))
        page: Page | None = None
        try:
            page = self.context.new_page()
            return self._apply_on(page, job, docs)
        except Exception as exc:  # browser crash, timeout...: this job fails, the batch goes on
            log.exception("Candidature LinkedIn %s en échec", job.id)
            shot = screenshot(self.ctx, page, f"linkedin_{job.external_id}_erreur") if page else None
            return _result(False, JobStatus.FAILED, _with_shot(f"Erreur LinkedIn : {truncate(str(exc), 200)}.", shot))
        finally:
            _close(page)

    # ------------------------------------------------------------------ flow

    def _apply_on(self, page: Page, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        page.goto(job_url(job), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(PAGE_SETTLE_MS)
        stop = self._precheck(page, job)
        if stop is not None:
            return stop
        variant = self._open(page)
        if variant == "external":
            return _result(False, JobStatus.NEEDS_MANUAL, "Offre LinkedIn sans Candidature simplifiée : candidature "
                           "sur le site de l'employeur (bouton « Postuler »).", ApplyMethod.EXTERNAL)
        if variant == "none":
            return self._manual(page, job, "bouton « Candidature simplifiée » introuvable")
        return self._walk(page, variant, job, docs)

    def _precheck(self, page: Page, job: JobOffer) -> ApplyResult | None:
        blocker = detect_blocker(page)
        if blocker:
            return self._manual(page, job, f"{blocker[1]} — connecte-toi avec « jobbot login linkedin » (jamais automatisé)")
        if not is_logged_in(page, "linkedin"):
            return self._manual(page, job, "session LinkedIn absente — lance « jobbot login linkedin »")
        card = self._top_card_text(page)
        if CLOSED_RE.search(card):
            return _result(False, JobStatus.EXPIRED, "Offre LinkedIn fermée : elle n'accepte plus de candidatures.")
        if page.locator(ALREADY_APPLIED_SEL).count() or ALREADY_APPLIED_RE.search(card):
            return _result(False, JobStatus.SKIPPED, "Déjà postulé à cette offre sur LinkedIn.")
        if LIMIT_RE.search(card):
            return self._manual(page, job, "limite quotidienne de candidatures LinkedIn atteinte, réessaie demain")
        return None

    def _top_card_text(self, page: Page) -> str:
        card = page.locator(TOP_CARD_SEL)
        return body_text(card) if card.count() else body_text(page, limit=3000)

    def _open(self, page: Page) -> str:
        """'A' (modal), 'B' (SDUI), 'external' or 'none'. Never clicks an external Apply button."""
        sdui = first_visible(page, (SDUI_OPENER,))
        if sdui is not None:
            sdui.click(timeout=CLICK_TIMEOUT_MS)
            page.wait_for_timeout(OPEN_WAIT_MS)
            return "B"
        button = first_visible(page, APPLY_BUTTONS)
        if button is None:
            return "none"
        label = normalize(f"{button.get_attribute('aria-label') or ''} {button.inner_text()}")
        if not any(marker in label for marker in EASY_APPLY_LABELS):
            return "external"
        button.click(timeout=CLICK_TIMEOUT_MS)
        page.wait_for_timeout(OPEN_WAIT_MS)
        if first_visible(page, (MODAL,)) is not None:
            return "A"
        return "B" if page.locator("[id*='easyApplyFormElement']").count() else "none"

    def _scope(self, page: Page, variant: str) -> Any:
        return page.locator(MODAL).first if variant == "A" else page

    def _walk(self, page: Page, variant: str, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        resolve = make_resolver(self.ctx.profile, docs, self.answerer_factory(job))
        uploaded, filled, previous_errors = False, 0, None
        for _ in range(MAX_STEPS):
            scope = self._scope(page, variant)
            stop, uploaded, count = self._fill_step(page, scope, job, docs, resolve, uploaded)
            if stop is not None:
                return stop
            filled += count
            submit = first_visible(scope, SUBMIT_SEL)
            if submit is not None:
                return self._submit(page, scope, submit, job, filled)
            errors = self._next_step(page, scope)
            stop = self._stalled(page, job, errors, previous_errors)
            if stop is not None:
                return stop
            previous_errors = errors
        return self._abort(page, job, f"plus de {MAX_STEPS} étapes")

    def _fill_step(
        self, page: Page, scope: Any, job: JobOffer, docs: TailoredDocuments, resolve: Callable, uploaded: bool
    ) -> tuple[ApplyResult | None, bool, int]:
        """Upload the CV (once) and answer the current step: (stop result or None, uploaded, fields filled)."""
        blocker = detect_blocker(page)
        if blocker:
            return self._abort(page, job, blocker[1]), uploaded, 0
        if not uploaded:
            upload = upload_documents(scope, docs, max_bytes=MAX_CV_BYTES)
            if upload.problems:
                return self._abort(page, job, "dépôt du CV impossible : " + "; ".join(upload.problems)), False, 0
            uploaded = upload.uploaded > 0
        report = fill_fields(scope, scan_fields(scope, selector=FIELD_SEL, exclude=EXCLUDE_SEL), resolve,
                             pause=lambda: human_pause(self.pacer))
        if report.missing:
            reason = "question(s) obligatoire(s) sans réponse fiable : " + " ; ".join(report.missing[:6])
            return self._abort(page, job, reason), uploaded, report.filled
        return None, uploaded, report.filled

    def _stalled(
        self, page: Page, job: JobOffer, errors: tuple[str, ...] | None, previous: tuple[str, ...] | None
    ) -> ApplyResult | None:
        if errors is None:
            return self._abort(page, job, "aucun bouton Suivant / Vérifier / Envoyer trouvé")
        if errors and errors == previous:
            return self._abort(page, job, "formulaire bloqué par une erreur : " + " | ".join(errors[:3]))
        return None

    def _next_step(self, page: Page, scope: Any) -> tuple[str, ...] | None:
        button = first_visible(scope, REVIEW_SEL) or first_visible(scope, NEXT_SEL)
        if button is None:
            return None
        button.click(timeout=CLICK_TIMEOUT_MS)
        page.wait_for_timeout(STEP_WAIT_MS)
        return error_messages(scope, ERROR_SEL)

    def _submit(self, page: Page, scope: Any, submit: Locator, job: JobOffer, filled: int) -> ApplyResult:
        self._set_follow(scope, want=False)
        shot = screenshot(self.ctx, page, f"linkedin_{job.external_id}_avant_envoi")
        if self.ctx.dry_run:
            self._discard(page)
            return _result(True, JobStatus.SKIPPED, _with_shot(
                f"[dry-run] Formulaire LinkedIn rempli ({filled} champ(s)), candidature NON envoyée.", shot))
        submit.click(timeout=CLICK_TIMEOUT_MS)
        if self._wait_sent(page):
            close = first_visible(page, DONE_SEL)
            if close is not None:
                close.click(timeout=CLICK_TIMEOUT_MS)
            return _result(True, JobStatus.APPLIED, "Candidature simplifiée LinkedIn envoyée.")
        if LIMIT_RE.search(body_text(page)):
            return self._manual(page, job, "limite quotidienne de candidatures LinkedIn atteinte")
        return self._manual(page, job, "envoi non confirmé — vérifie dans « Mes offres d'emploi » sur LinkedIn")

    def _wait_sent(self, page: Page) -> bool:
        for _ in range(SENT_POLLS):
            page.wait_for_timeout(SENT_POLL_MS)
            if first_visible(page, SUBMIT_SEL) is not None:
                continue
            sent = page.get_by_text(SENT_RE)
            if sent.count() and sent.first.is_visible():
                return True
        return False

    # ------------------------------------------------------------------ helpers

    def _set_follow(self, scope: Any, *, want: bool) -> None:
        box = scope.locator(FOLLOW_CB)
        try:
            if not box.count() or box.first.is_checked() == want:
                return
            label = first_visible(scope, (FOLLOW_LABEL,))
            if label is not None:
                label.click(timeout=CLICK_TIMEOUT_MS)
            else:
                box.first.set_checked(want, force=True)
        except PlaywrightError as exc:
            log.warning("Case « Suivre l'entreprise » non modifiée : %s", exc)

    def _discard(self, page: Page) -> None:
        """Close the modal and confirm « Supprimer » so nothing is left half-filled (best effort)."""
        try:
            dismiss = first_visible(page, DISMISS_SEL)
            if dismiss is None:
                return
            dismiss.click(timeout=CLICK_TIMEOUT_MS)
            page.wait_for_timeout(DISCARD_WAIT_MS)
            confirm = first_visible(page, DISCARD_SEL)
            if confirm is not None:
                confirm.click(timeout=CLICK_TIMEOUT_MS)
            page.locator(MODAL).first.wait_for(state="hidden", timeout=5000)
        except PlaywrightError as exc:
            log.warning("Abandon de la candidature LinkedIn incomplet : %s", exc)

    def _manual(self, page: Page, job: JobOffer, reason: str) -> ApplyResult:
        shot = screenshot(self.ctx, page, f"linkedin_{job.external_id}_manuel")
        return _result(False, JobStatus.NEEDS_MANUAL, _with_shot(f"LinkedIn : {reason}.", shot))

    def _abort(self, page: Page, job: JobOffer, reason: str) -> ApplyResult:
        shot = screenshot(self.ctx, page, f"linkedin_{job.external_id}_abandon")
        self._discard(page)
        return _result(False, JobStatus.NEEDS_MANUAL,
                       _with_shot(f"LinkedIn : {reason}. Candidature abandonnée, rien n'a été envoyé.", shot))


def _close(page: Page | None) -> None:
    if page is None:
        return
    try:
        page.close()
    except PlaywrightError as exc:
        log.debug("Fermeture de l'onglet : %s", exc)
