"""Assisted application on external sites (company ATS, WTTJ, Indeed, APEC…): pre-fill, never submit.

Opens the offer, clicks an obvious « Postuler / Apply » link, fills identity fields, uploads the CV and
answers questions it is confident about, then leaves the tab open: the human checks and clicks « Envoyer ».
ATS detection patterns: docs/research/ats-and-documents.md §2.2.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

from jobbot.apply.answer_rules import identity_values
from jobbot.apply.answers import FormAnswerer
from jobbot.apply.base import ApplyResult
from jobbot.apply.browser import body_text, detect_blocker, dismiss_cookie_banner, screenshot
from jobbot.apply.formfill import fill_fields, make_resolver, upload_documents
from jobbot.apply.formscan import scan_fields
from jobbot.config import Profile
from jobbot.context import AppContext
from jobbot.models import ApplyMethod, JobOffer, JobStatus, TailoredDocuments
from jobbot.textutil import truncate

log = logging.getLogger(__name__)

APPLY_TEXT_RE = re.compile(
    r"^\s*(postuler( maintenant| à cette offre| à l.offre| en ligne)?|je postule|candidater|déposer ma candidature|"
    r"apply( now| for this (job|position))?|i.m interested|je suis intéressée?(\(e\))?)\s*$",
    re.I,
)
APPLY_CANDIDATES = "a, button, [role=button]"
NOT_A_SUBMIT_JS = """(el) => { if (el.type === 'submit') return false; const f = el.closest('form');
  return !f || !f.querySelector('input[type=file], input[type=email], textarea'); }"""
CLOSED_RE = re.compile(
    r"n.est plus ouvert|n.est plus disponible|offre (a )?expiré|offre (est )?(clôturée|pourvue)|"
    r"no longer (accepting|available|open)|position (has been )?filled|job (has )?expired",
    re.I,
)
ATS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.I))
    for name, pattern in (
        ("workday", r"\.myworkdayjobs\.com|/wday/cxs/"), ("taleo", r"\.taleo\.net"),
        ("successfactors", r"successfactors\.(eu|com)|career\d+\.sapsf\.|jobs\.sap\.com|rmk-map"),
        ("smartrecruiters", r"smartrecruiters\.com|smartr\.me/oneclick-ui"),
        ("greenhouse", r"greenhouse\.io|gh_jid="), ("lever", r"jobs\.lever\.co"), ("teamtailor", r"\.teamtailor\.com"),
        ("flatchr", r"careers\.flatchr\.io"), ("wttj", r"welcometothejungle\.com/.+/jobs/|welcomekit\.co"),
        ("jobaffinity", r"jobaffinity\.fr/apply/"), ("talentsoft", r"talent-soft\.com"),
        ("digitalrecruiters", r"\.digitalrecruiters\.com"), ("taleez", r"taleez\.com"),
        ("workable", r"apply\.workable\.com"), ("recruitee", r"\.recruitee\.com"), ("indeed", r"indeed\.(com|fr)"),
    )
)
ACCOUNT_REQUIRED = frozenset({"workday", "taleo", "successfactors", "talentsoft", "wttj"})
DOCX_PREFERRED = frozenset({"taleo", "successfactors"})  # weak PDF parsing (research §2.2)
# Known ATS field selectors (Lever/Greenhouse from the research; others DOC-SOURCED/UNCERTAIN).
ATS_FIELDS: dict[str, dict[str, str]] = {
    "greenhouse": {"first_name": "#first_name", "last_name": "#last_name", "email": "#email", "phone": "#phone"},
    "lever": {"full_name": "input[name='name']", "email": "input[name='email']", "phone": "input[name='phone']",
              "linkedin": "input[name='urls[LinkedIn]']", "github": "input[name='urls[GitHub]']",
              "portfolio": "input[name='urls[Portfolio]']"},
    "teamtailor": {"first_name": "input[name='candidate[first_name]']", "last_name": "input[name='candidate[last_name]']",
                   "email": "input[name='candidate[email]']", "phone": "input[name='candidate[phone]']"},
    "workable": {"first_name": "input[name='firstname']", "last_name": "input[name='lastname']",
                 "email": "input[name='email']", "phone": "input[name='phone']"},
    "recruitee": {"full_name": "input[name='candidate.name']", "email": "input[name='candidate.email']",
                  "phone": "input[name='candidate.phone']"},
}
NAV_TIMEOUT_MS = 30000
SETTLE_MS = 1500
AFTER_CLICK_MS = 2500
CLICK_TIMEOUT_MS = 5000


def detect_ats(url: str) -> str | None:
    return next((name for name, pattern in ATS_PATTERNS if pattern.search(url or "")), None)


def _manual(detail: str) -> ApplyResult:
    return ApplyResult(ok=False, status=JobStatus.NEEDS_MANUAL, method=ApplyMethod.EXTERNAL, detail=detail)


def assisted_apply(
    ctx: AppContext, context: BrowserContext, job: JobOffer, docs: TailoredDocuments, answerer: FormAnswerer
) -> ApplyResult:
    """Pre-fill the external application form and leave it open for the human (never submits)."""
    url = job.apply_url or job.url
    if not url:
        return ApplyResult(ok=False, status=JobStatus.FAILED, method=ApplyMethod.EXTERNAL,
                           detail="Aucune URL de candidature pour cette offre.")
    try:
        page = context.new_page()
    except PlaywrightError as exc:
        return ApplyResult(ok=False, status=JobStatus.FAILED, method=ApplyMethod.EXTERNAL,
                           detail=f"Navigateur indisponible ({truncate(str(exc), 160)}) : postule sur {url}.")
    try:
        return _assist(ctx, context, page, url, job, docs, answerer)
    except Exception as exc:  # the tab stays open: the human can still apply by hand
        log.exception("Pré-remplissage de %s en échec", url)
        shot = screenshot(ctx, page, f"assist_{job.external_id}_erreur")
        detail = f"Pré-remplissage impossible ({truncate(str(exc), 160)}) : postule à la main sur {url}."
        return _manual(f"{detail} Capture : {shot}" if shot else detail)


def _assist(
    ctx: AppContext, context: BrowserContext, page: Page, url: str, job: JobOffer, docs: TailoredDocuments,
    answerer: FormAnswerer,
) -> ApplyResult:
    response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(SETTLE_MS)
    if (response is not None and response.status in (404, 410)) or CLOSED_RE.search(body_text(page, limit=6000)):
        _close(page)
        return ApplyResult(ok=False, status=JobStatus.EXPIRED, method=ApplyMethod.EXTERNAL,
                           detail=f"Offre fermée ou introuvable sur {_host(url)}.")
    dismiss_cookie_banner(page)
    page = _open_apply_form(context, page)
    dismiss_cookie_banner(page)
    ats = detect_ats(page.url) or detect_ats(url)
    where = f"{_host(page.url)}{f' ({ats})' if ats else ''}"
    blocker = detect_blocker(page, captcha=False)
    if blocker and blocker[0] != "captcha":
        shot = screenshot(ctx, page, f"assist_{job.external_id}_connexion")
        return _manual(f"{where} : {blocker[1]} — connecte-toi / crée ton compte dans la fenêtre puis postule "
                       f"(CV prêt : {docs.cv_pdf}).{f' Capture : {shot}' if shot else ''}")
    filled = _fill_known_ats(page, ats, ctx.profile)
    count, missing, problems = _autofill(page, ctx.profile, docs, answerer, prefer_docx=ats in DOCX_PREFERRED)
    shot = screenshot(ctx, page, f"assist_{job.external_id}_prerempli")
    return _manual(_detail(where, filled + count, missing, problems, ats, shot))


def _detail(where: str, filled: int, missing: list[str], problems: list[str], ats: str | None, shot: str | None) -> str:
    parts = [f"pré-rempli : {filled} champ(s) sur {where}."]
    if ats in ACCOUNT_REQUIRED:
        parts.append("Compte candidat requis sur cet ATS.")
    if missing:
        parts.append("À compléter : " + " ; ".join(missing[:8]) + ".")
    if problems:
        parts.append("Documents : " + " ; ".join(problems[:3]) + ".")
    parts.append("Vérifie puis clique toi-même sur « Envoyer » (jobbot ne soumet jamais).")
    if shot:
        parts.append(f"Capture : {shot}")
    return " ".join(parts)


def autofill_page(page: Page, profile: Profile, docs: TailoredDocuments, answerer: FormAnswerer) -> int:
    """Fill every recognisable field of the page and its iframes; returns the number of fields filled."""
    return _autofill(page, profile, docs, answerer)[0]


def _autofill(
    page: Page, profile: Profile, docs: TailoredDocuments, answerer: FormAnswerer, *, prefer_docx: bool = False
) -> tuple[int, list[str], list[str]]:
    resolve = make_resolver(profile, docs, answerer)
    filled, missing, problems = 0, [], []
    for frame in page.frames:
        try:
            upload = upload_documents(frame, docs, prefer_docx=prefer_docx)
            report = fill_fields(frame, scan_fields(frame), resolve)
        except PlaywrightError as exc:
            log.warning("Cadre %s ignoré : %s", frame.url, exc)
            continue
        filled += upload.uploaded + report.filled
        missing.extend(report.missing)
        problems.extend(upload.problems)
    return filled, missing, problems


def _open_apply_form(context: BrowserContext, page: Page) -> Page:
    """Click the « Postuler » link/button (not a form submit) when the form is not shown yet."""
    if page.locator("input[type=file]:visible, input[type=email]:visible, textarea:visible").count():
        return page
    candidates = page.locator(APPLY_CANDIDATES).filter(has_text=APPLY_TEXT_RE)
    visible = (candidates.nth(i) for i in range(min(candidates.count(), 10)))
    button = next((b for b in visible if b.is_visible() and b.evaluate(NOT_A_SUBMIT_JS)), None)
    if button is None:
        return page
    before = set(context.pages)
    try:
        button.click(timeout=CLICK_TIMEOUT_MS)
        page.wait_for_timeout(AFTER_CLICK_MS)
    except PlaywrightError as exc:
        log.info("Bouton « Postuler » non cliquable : %s", exc)
        return page
    opened = [p for p in context.pages if p not in before]
    target = opened[-1] if opened else page
    try:
        target.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PlaywrightError as exc:
        log.debug("Chargement du formulaire : %s", exc)
    return target


def _fill_known_ats(page: Page, ats: str | None, profile: Profile) -> int:
    filled = 0
    for key, selector in ATS_FIELDS.get(ats or "", {}).items():
        values = identity_values(key, profile)
        field = page.locator(selector).first
        try:
            if not values or not field.count() or not field.is_editable() or field.input_value().strip():
                continue
            field.fill(values[0], timeout=CLICK_TIMEOUT_MS)
            filled += 1
        except PlaywrightError as exc:
            log.debug("Champ %s (%s) non rempli : %s", key, ats, exc)
    return filled


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _close(page: Page) -> None:
    try:
        page.close()
    except PlaywrightError as exc:
        log.debug("Fermeture de l'onglet : %s", exc)
