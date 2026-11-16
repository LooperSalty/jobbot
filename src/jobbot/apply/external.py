"""Automatic applications on a small set of directly hosted ATS forms."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError

from jobbot.apply.assisted import CLOSED_RE, _fill_known_ats, _open_apply_form
from jobbot.apply.base import ApplyResult
from jobbot.apply.browser import body_text, detect_blocker, dismiss_cookie_banner
from jobbot.apply.formscan import scan_fields
from jobbot.models import ApplyMethod, JobOffer, JobStatus, TailoredDocuments

log = logging.getLogger(__name__)
_HOSTS = {"jobs.lever.co": "lever", "boards.greenhouse.io": "greenhouse",
          "job-boards.greenhouse.io": "greenhouse"}
_SUCCESS = re.compile(r"application (?:has been |was |successfully )?(?:submitted|received)|"
                      r"thank you for (?:your application|applying)|"
                      r"candidature (?:a bien été |a été |bien )?(?:envoyée|reçue|transmise)", re.I)
_BUTTONS = {"lever": "form button[type=submit], form input[type=submit]",
            "greenhouse": "form button[type=submit], form input[type=submit], form #submit_app"}


def supported_url(url: str | None) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme == "https" and parsed.hostname in _HOSTS and not parsed.username and not parsed.password


def _manual(detail: str) -> ApplyResult:
    return ApplyResult(ok=False, status=JobStatus.NEEDS_MANUAL, method=ApplyMethod.EXTERNAL, detail=detail)


class ExternalApplier:
    channel = "external"

    def __init__(self, ctx, browser, answerer_factory):
        self.ctx, self.browser, self.answerer_factory = ctx, browser, answerer_factory

    def can_apply(self, job: JobOffer) -> bool:
        return supported_url(job.apply_url or job.url)

    def apply(self, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        page = None
        try:
            page = self.browser.new_page()
            return self._apply(page, job, docs)
        except Exception as exc:
            log.warning("Formulaire externe non terminé : %s", exc)
            return _manual("Formulaire externe interrompu : vérifie son état avant de réessayer.")
        finally:
            if page:
                try:
                    page.close()
                except PlaywrightError:
                    pass

    def _apply(self, page, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        url = job.apply_url or job.url
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        if not supported_url(page.url):
            return _manual("Redirection vers un site externe non pris en charge.")
        if (response and response.status in (404, 410)) or CLOSED_RE.search(body_text(page, limit=6000)):
            return ApplyResult(ok=False, status=JobStatus.EXPIRED, method=ApplyMethod.EXTERNAL,
                               detail="Offre fermée ou introuvable.")
        dismiss_cookie_banner(page)
        page = _open_apply_form(self.browser, page)
        if not supported_url(page.url):
            return _manual("Formulaire ouvert sur un site externe non pris en charge.")
        blocker = detect_blocker(page)
        if blocker:
            return _manual(f"Formulaire bloqué : {blocker[1]}")
        ats = _HOSTS[urlparse(page.url).hostname]
        fields = scan_fields(page)
        if not fields or not page.locator("form input[type=email]").count():
            return _manual("Formulaire de candidature non reconnu.")
        if any(f.kind == "checkbox" and f.required and not f.is_filled for f in fields):
            return _manual("Une case obligatoire nécessite ton intervention.")
        _fill_known_ats(page, ats, self.ctx.profile)
        # The assisted filler may answer checkboxes; automatic submission leaves their choice to the user.
        from jobbot.apply.formfill import fill_fields, make_resolver, upload_documents

        upload = upload_documents(page, docs)
        fields = scan_fields(page)
        report = fill_fields(page, [f for f in fields if f.kind != "checkbox"],
                             make_resolver(self.ctx.profile, docs, self.answerer_factory(job)))
        fields = scan_fields(page)
        missing = [f.label or f.hints or "champ obligatoire" for f in fields
                   if f.required and not f.disabled and not f.is_filled]
        if upload.problems or report.missing or missing:
            return _manual("Champs ou documents à vérifier : " + "; ".join([*upload.problems, *report.missing, *missing][:5]))
        cv_name = Path(docs.cv_pdf).name
        has_cv = page.locator("input[type=file]").evaluate_all(
            "(els, name) => els.some(e => Array.from(e.files || []).some(f => f.name === name))", cv_name)
        if not has_cv:
            return _manual("CV non joint au formulaire : candidature à terminer manuellement.")
        if not supported_url(page.url) or detect_blocker(page):
            return _manual("Formulaire bloqué ou redirigé : candidature à terminer manuellement.")
        buttons = page.locator(_BUTTONS[ats])
        visible = [buttons.nth(i) for i in range(buttons.count()) if buttons.nth(i).is_visible()]
        if len(visible) != 1 or not visible[0].is_enabled():
            return _manual("Bouton d'envoi impossible à identifier de façon unique.")
        if self.ctx.dry_run:
            return ApplyResult(ok=True, status=JobStatus.SKIPPED, method=ApplyMethod.EXTERNAL,
                               detail="Simulation : formulaire préparé sans envoi.")
        before = body_text(page, limit=12000)
        if _SUCCESS.search(before):
            return _manual("Confirmation déjà présente avant l'envoi : état à vérifier.")
        visible[0].click(timeout=5000)
        page.wait_for_timeout(3000)
        if _SUCCESS.search(body_text(page, limit=12000)):
            return ApplyResult(ok=True, status=JobStatus.APPLIED, method=ApplyMethod.EXTERNAL,
                               detail=f"Candidature confirmée sur {ats}.")
        return _manual("Envoi tenté mais confirmation absente : vérifie avant toute nouvelle tentative.")
