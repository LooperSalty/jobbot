"""Generic form filling on a Playwright Page, Frame or Locator (fields read by ``formscan``).

Shared by LinkedIn Easy Apply, HelloWork extra steps and the assisted (external ATS) filler. Playwright
CSS locators pierce open shadow roots, so this also works for LinkedIn's SDUI variant.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator
from pydantic import BaseModel, ConfigDict

from jobbot.apply.answer_rules import identity_values
from jobbot.apply.answers import FormAnswer, FormAnswerer, is_job_specific
from jobbot.apply.browser import check_upload, fit_text, page_of
from jobbot.apply.coerce import coerce
from jobbot.apply.formscan import FieldInfo, locate, scan_fields
from jobbot.config import Profile
from jobbot.models import TailoredDocuments
from jobbot.textutil import normalize

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.6
DEFAULT_MAX_UPLOAD = 2 * 1024 * 1024
TYPE_DELAY_MS = 60
TYPEAHEAD_TIMEOUT_MS = 3000
ACTION_TIMEOUT_MS = 3000
LETTER_FIELD_RE = re.compile(r"lettre|cover|motivation|message|commentaire|comments|additional information|complementaire")
LETTER_FILE_RE = re.compile(r"lettre|cover|motivation")
CV_FILE_RE = re.compile(r"\bcv\b|resume|curriculum|candidature|upload|fichier|file|document|piece jointe")
SKIP_FILE_RE = re.compile(r"photo|picture|image|avatar|diplome|transcript|releve|portfolio|justificatif")
AUTOCOMPLETE_KEYS = {
    "given-name": "first_name", "family-name": "last_name", "name": "full_name", "email": "email", "tel": "phone",
    "tel-national": "phone", "postal-code": "postal_code", "address-level2": "city", "country-name": "country",
}
HINT_KEYS = (
    ("first_name", r"first_?name|prenom|given_?name|fname"), ("last_name", r"last_?name|family_?name|surname|lname"),
    ("email", r"e_?mail|courriel"), ("phone", r"phone|telephone|mobile"), ("postal_code", r"zip|postal"),
    ("city", r"city|ville"), ("linkedin", r"linkedin"), ("github", r"github"), ("portfolio", r"portfolio|website"),
)


class FillReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    filled: int = 0
    missing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class UploadReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    uploaded: int = 0
    problems: tuple[str, ...] = ()


# --------------------------------------------------------------------------- answering


def _identity_answer(key: str | None, field: FieldInfo, profile: Profile) -> FormAnswer | None:
    if key is None:
        return None
    value = coerce(field.kind, field.question().options, identity_values(key, profile))
    return FormAnswer(value=value, confidence=0.9, source="profile") if value else None


def _hint_key(field: FieldInfo) -> str | None:
    compact = normalize(field.hints).replace(" ", "_")
    return next((key for key, pattern in HINT_KEYS if re.search(pattern, compact)), None)


def make_resolver(profile: Profile, docs: TailoredDocuments, answerer: FormAnswerer) -> Callable[[FieldInfo], FormAnswer]:
    """field → answer: autocomplete tokens → cover letter → FormAnswerer → name/id hints."""

    def resolve(field: FieldInfo) -> FormAnswer:
        found = _identity_answer(AUTOCOMPLETE_KEYS.get(field.autocomplete.split(" ")[-1]), field, profile)
        if found:
            return found
        if field.kind == "textarea" and docs.letter_text and LETTER_FIELD_RE.search(normalize(field.label)):
            return FormAnswer(value=fit_text(docs.letter_text, field.maxlength), confidence=0.9, source="profile")
        allow_llm = field.required or (field.kind == "textarea" and is_job_specific(field.label))
        answer = answerer.answer(field.question(), allow_llm=allow_llm)
        if answer.value is None and field.kind in ("text", "typeahead"):
            return _identity_answer(_hint_key(field), field, profile) or answer
        return answer

    return resolve


# --------------------------------------------------------------------------- filling


def fill_fields(
    scope: Any, fields: list[FieldInfo], resolve: Callable[[FieldInfo], FormAnswer], *,
    pause: Callable[[], None] | None = None,
) -> FillReport:
    """Fill every empty, visible field we can answer confidently; list required ones we cannot."""
    filled, missing, errors = 0, [], []
    for field in (f for f in fields if _needs_filling(f)):
        name = field.label or field.hints.strip() or "champ sans libellé"
        answer = _usable_answer(field, resolve)
        if answer is not None and _fill_one(scope, field, answer.value):
            filled += 1
            if pause:
                pause()
            continue
        if answer is not None:
            errors.append(name)
        if field.required:
            missing.append(name)
    return FillReport(filled=filled, missing=tuple(missing), errors=tuple(errors))


def _needs_filling(field: FieldInfo) -> bool:
    return not (field.kind == "file" or field.disabled or not field.visible or field.is_filled)


def _usable_answer(field: FieldInfo, resolve: Callable[[FieldInfo], FormAnswer]) -> FormAnswer | None:
    if not field.supported:
        return None
    try:
        answer = resolve(field)
    except Exception as exc:  # one bad question must not abort the whole form
        log.warning("Réponse impossible pour « %s » : %s", field.label, exc)
        return None
    if answer.value in (None, "", []) or answer.confidence < MIN_CONFIDENCE:
        return None
    if field.kind == "checkbox" and not field.options and answer.value == "Non":
        return None  # leave unticked; a required box then stays "missing"
    return answer


def _fill_one(scope: Any, field: FieldInfo, value: str | list[str]) -> bool:
    try:
        if field.kind in ("radio", "checkbox") and field.options:
            return _check_options(scope, field, value)
        if field.kind == "checkbox":
            return _check(locate(scope, field.keys[0]))
        loc = locate(scope, field.keys[0])
        if field.kind == "select":
            loc.select_option(label=str(value), timeout=ACTION_TIMEOUT_MS)
            return True
        if field.kind == "typeahead":
            return _typeahead(scope, loc, str(value))
        loc.fill(fit_text(str(value), field.maxlength), timeout=ACTION_TIMEOUT_MS)
        return True
    except PlaywrightError as exc:
        log.warning("Champ « %s » non rempli : %s", field.label, exc)
        return False


def _check_options(scope: Any, field: FieldInfo, value: str | list[str]) -> bool:
    wanted = [value] if isinstance(value, str) else list(value)
    ok = True
    for option in wanted:
        if option not in field.options:
            return False
        ok = _check(locate(scope, field.keys[field.options.index(option)])) and ok
    return ok


_CLICK_LABEL_JS = """(el) => { const r = el.getRootNode();
  const l = el.id ? r.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null; (l || el).click(); }"""


def _check(loc: Locator) -> bool:
    try:
        loc.check(timeout=ACTION_TIMEOUT_MS)
    except PlaywrightError:
        loc.evaluate(_CLICK_LABEL_JS)  # visually hidden inputs styled through their <label>
    return loc.is_checked()


def _typeahead(scope: Any, loc: Locator, value: str) -> bool:
    loc.click(timeout=ACTION_TIMEOUT_MS)
    loc.fill("")
    loc.press_sequentially(value, delay=TYPE_DELAY_MS)
    options = page_of(scope).locator("[role='option']:visible")
    try:
        options.first.wait_for(state="visible", timeout=TYPEAHEAD_TIMEOUT_MS)
    except PlaywrightError:
        loc.press("ArrowDown")
        loc.press("Enter")
        return bool(loc.input_value())
    options.nth(best_option_index(options.all_inner_texts()[:10], value)).click(timeout=ACTION_TIMEOUT_MS)
    return True


def best_option_index(texts: list[str], value: str) -> int:
    """'Metz' → prefer 'Metz, Grand Est, France' over 'Metzervisse, …' (exact head, then prefix, else first)."""
    target = normalize(value)
    heads = [normalize(t.split(",")[0]) for t in texts]
    exact = next((i for i, head in enumerate(heads) if head == target), None)
    if exact is not None:
        return exact
    return next((i for i, t in enumerate(texts) if normalize(t).startswith(target)), 0)


# --------------------------------------------------------------------------- documents


def _pick_document(field: FieldInfo, docs: TailoredDocuments, first_cv: bool, prefer_docx: bool) -> str | None:
    text = normalize(f"{field.label} {field.hints}")
    if SKIP_FILE_RE.search(text) and not CV_FILE_RE.search(text):
        return None
    if LETTER_FILE_RE.search(text):
        return docs.letter_pdf
    if not (CV_FILE_RE.search(text) or first_cv):
        return None
    accept = field.accept.lower()
    wants_docx = prefer_docx or (accept and ".pdf" not in accept and "pdf" not in accept and "doc" in accept)
    return docs.cv_docx if wants_docx and docs.cv_docx else docs.cv_pdf


def upload_documents(
    scope: Any, docs: TailoredDocuments, *, max_bytes: int = DEFAULT_MAX_UPLOAD, prefer_docx: bool = False,
    selector: str = "input[type='file']", exclude: str = "",
) -> UploadReport:
    """Put the CV (and letter when asked) into empty file inputs, respecting the size limit."""
    uploaded, problems, cv_done = 0, [], False
    for field in scan_fields(scope, selector=selector, exclude=exclude):
        if field.files or field.disabled:
            continue
        path = _pick_document(field, docs, not cv_done, prefer_docx)
        if path is None:
            continue
        problem = check_upload(path, max_bytes)
        if problem:
            problems.append(problem)
            continue
        try:
            locate(scope, field.keys[0]).set_input_files(path, timeout=10000)
            uploaded += 1
            cv_done = cv_done or path in (docs.cv_pdf, docs.cv_docx)
        except PlaywrightError as exc:
            problems.append(f"dépôt de {path} impossible : {exc}")
    return UploadReport(uploaded=uploaded, problems=tuple(problems))


def error_messages(scope: Any, selector: str) -> tuple[str, ...]:
    try:
        return tuple(t.strip() for t in scope.locator(selector).all_inner_texts() if t.strip())
    except PlaywrightError:
        return ()
