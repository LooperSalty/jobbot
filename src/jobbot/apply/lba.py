"""La Bonne Alternance « Candidature simplifiée » (docs/research/la-bonne-alternance.md §2.B, §4, §5, §6.5).

Flow (what the LBA website itself does): fetch the item detail right before applying (per-item JWT, valid
24 h, never stored nor logged) → JSON POST ``/api/v2/_private/application`` with the CV as a base64 data URL
and the cover letter as the message. LBA relays the application to the recruiter by e-mail.

Safety: never twice the same offer / SIRET / recipient (server cap = 3 all-time; jobbot = 1), never a recruiter
outside the target (``outreach.relevance``: checked on the stored data, then on the detail's NAF / size), dry-run
builds the payload without POSTing, recruiter questions or missing relay → NEEDS_MANUAL (the human applies on the
LBA page). Quotas and pauses are handled by ``apply.service`` (channel ``lba``).
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from jobbot import clock
from jobbot.apply.base import ApplyResult
from jobbot.config import Profile
from jobbot.context import AppContext
from jobbot.http import HostThrottle, make_client, request_with_retry
from jobbot.models import ApplyMethod, Company, JobOffer, JobStatus, TailoredDocuments
from jobbot.outreach.relevance import OFF_TARGET, is_relevant_employer
from jobbot.sources.lba_mapping import (
    LBA_SITE,
    RECRUITER_SOURCE,
    SOURCE,
    SUB_LBA,
    SUB_PARTNER,
    SUB_RECRUITER,
    page_url,
    recruiter_detail_facts,
)
from jobbot.textutil import truncate

log = logging.getLogger(__name__)

LBA_API = f"{LBA_SITE}/api"
DETAIL_URL = LBA_API + "/_private/jobs/{sub_type}/{item_id}"
APPLY_URL = LBA_API + "/v2/_private/application"
PDF_PREFIX = "data:application/pdf;base64,"
DOCX_PREFIX = "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,"
MAX_ATTACHMENT_BYTES = 3_000_000  # UI cap 3 145 728 bytes, API cap 4 215 276 base64 chars
MAX_MESSAGE_CHARS = 3000
MAX_NAME_CHARS = 50
MAX_DESCRIPTION_CHARS = 200
MAX_SEARCH_CHARS = 100
CONTRACT_DURATIONS = (6, 12, 18, 24, 36)  # options of the LBA form ("24 mois")
APPLY_TIMEOUT_S = 60.0
APPLY_INTERVAL_S = 1.2  # route limit: 5 requests / 5 s
KV_PREFIX = "lba:applied:"
OFFER_SUB_TYPES = (SUB_LBA, SUB_PARTNER)
MONTHS = ("Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août", "Septembre", "Octobre",
          "Novembre", "Décembre")
_ERROR_CODES = re.compile(r"TOO_MANY_APPLICATIONS_PER_(?:DAY|OFFER)|EXPIRED|NOTFOUND|INTERNAL_EMAIL|BURNER|"
                          r"FILE_TYPE_NOT_SUPPORTED")


@dataclass(frozen=True)
class _Target:
    sub_type: str
    item_id: str
    name: str  # company name (messages)
    title: str  # offer title / searched job (payload + page URL slug)
    method: ApplyMethod
    company: Company | None = None  # recruiter targets: judged again on the detail's NAF / size before applying


def _result(ok: bool, status: JobStatus, detail: str) -> ApplyResult:
    return ApplyResult(ok=ok, status=status, method=ApplyMethod.LBA, detail=detail)


# --------------------------------------------------------------------------- payload (pure)


def build_attachment(path: str | Path) -> tuple[str, str]:
    """(file name, data URL) for LBA; ValueError (French) when the file cannot be sent."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"CV introuvable : {p}")
    data = p.read_bytes()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"CV trop lourd pour La Bonne Alternance ({len(data) // 1024} Ko > 3 Mo)")
    suffix = p.suffix.lower()
    if suffix == ".pdf" and data[:4] == b"%PDF":
        prefix = PDF_PREFIX
    elif suffix == ".docx" and data[:2] == b"PK":
        prefix = DOCX_PREFIX
    else:
        raise ValueError(f"Format refusé par La Bonne Alternance (PDF ou DOCX valide uniquement) : {p.name}")
    return p.name, prefix + base64.b64encode(data).decode("ascii")


def normalize_phone(phone: str) -> str:
    """'+33 6 12 34 56 78' → '+33612345678' (LBA accepts +33… or 06…)."""
    return re.sub(r"[^\d+]", "", phone or "")


def fit_message(text: str, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Cut the letter at a paragraph/sentence end below ``max_chars``."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind("\n\n"), head.rfind(". "), head.rfind(".\n"))
    return head[: cut + 1].rstrip() if cut >= max_chars * 0.6 else truncate(text, max_chars)


def contract_start(start: str, today: date) -> list[str]:
    """'2026-10' → ['Octobre 2026'] when within the next 12 months, else ['Dès que possible']."""
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", (start or "").strip())
    if match:
        year, month = int(match[1]), int(match[2])
        ahead = (year - today.year) * 12 + (month - today.month)
        if 1 <= month <= 12 and 0 <= ahead <= 11:
            return [f"{MONTHS[month - 1]} {year}"]
    return ["Dès que possible"]


def _formation_fields(profile: Profile, today: date) -> dict[str, Any]:
    alt = profile.alternance
    if alt is None or not alt.school:
        return {"applicant_inscription_formation": False}
    fields: dict[str, Any] = {
        "applicant_inscription_formation": True,
        "applicant_contract_start": contract_start(alt.start_date, today),
        "applicant_formation_description": truncate(" — ".join(x for x in (alt.school, alt.degree) if x),
                                                    MAX_DESCRIPTION_CHARS),
        "applicant_rythm_description": truncate(alt.rhythm or "", MAX_DESCRIPTION_CHARS),
    }
    if alt.duration_months in CONTRACT_DURATIONS:
        fields["applicant_contract_duration"] = f"{alt.duration_months} mois"
    return fields


def build_payload(
    profile: Profile, docs: TailoredDocuments, *, recipient_id: str, application_url: str, searched: str,
    today: date | None = None,
) -> dict[str, Any]:
    """JSON body of ``POST /api/v2/_private/application`` (ValueError when the CV cannot be attached)."""
    identity = profile.identity
    name, content = build_attachment(docs.cv_pdf)
    payload: dict[str, Any] = {
        "applicant_first_name": identity.first_name[:MAX_NAME_CHARS],
        "applicant_last_name": identity.last_name[:MAX_NAME_CHARS],
        "applicant_email": identity.email,
        "applicant_phone": normalize_phone(identity.phone),
        "applicant_message": fit_message(docs.letter_text),
        "applicant_attachment_name": name,
        "applicant_attachment_content": content,
        "recipient_id": recipient_id,
        "job_searched_by_user": truncate(searched, MAX_SEARCH_CHARS),
        "application_url": application_url,
    }
    payload.update(_formation_fields(profile, today or clock.to_paris(clock.now()).date()))
    return payload


def _server_code(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    message = str(body.get("message") or body.get("error") or "") if isinstance(body, dict) else ""
    found = _ERROR_CODES.search(message or resp.text[:500])
    return found.group(0) if found else truncate(message or resp.text[:200], 200)


# --------------------------------------------------------------------------- applier


class LbaApplier:
    """Channel ``lba``: « Candidature simplifiée » relayed by La Bonne Alternance."""

    channel = "lba"

    def __init__(self, ctx: AppContext, client: httpx.Client | None = None):
        self.ctx = ctx
        self._client = client
        self._throttle = HostThrottle(APPLY_INTERVAL_S)
        self.fatal: str | None = None  # set when the channel must stop for this run (LBA down, daily cap)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(timeout=APPLY_TIMEOUT_S, headers={"Accept": "application/json"})
        return self._client

    def can_apply(self, job: JobOffer) -> bool:
        raw = job.raw or {}
        return (job.source == SOURCE and job.apply_method == ApplyMethod.LBA
                and raw.get("source") in OFFER_SUB_TYPES and bool(raw.get("id")))

    def apply(self, job: JobOffer, docs: TailoredDocuments) -> ApplyResult:
        if not self.can_apply(job):
            return _result(False, JobStatus.NEEDS_MANUAL, "Offre sans « Candidature simplifiée » La Bonne Alternance.")
        target = _Target(str(job.raw["source"]), str(job.raw["id"]), job.company, job.title, ApplyMethod.LBA)
        return self._guarded(target, docs)

    def apply_to_recruiter(self, company: Company, docs: TailoredDocuments) -> ApplyResult:
        """Spontaneous application to an LBA recruiter (``hasEmail``) relayed by La Bonne Alternance."""
        siret = str(company.raw.get("siret") or company.raw.get("id") or "")
        if company.source != RECRUITER_SOURCE or not siret:
            return _result(False, JobStatus.SKIPPED, f"{company.name} : entreprise non référencée par LBA.")
        off_target = self._off_target(company)
        if off_target is not None:
            return off_target
        titles = self.ctx.profile.target.job_titles
        target = _Target(SUB_RECRUITER, siret, company.name, titles[0] if titles else "Développeur web",
                         ApplyMethod.LBA, company)
        return self._guarded(target, docs)

    def _off_target(self, company: Company) -> ApplyResult | None:
        """Never spend one of the 3 applications LBA allows per company on an employer outside the target."""
        relevant, reason = is_relevant_employer(company, self.ctx.profile)
        if relevant:
            return None
        return _result(False, JobStatus.SKIPPED, f"Entreprise {OFF_TARGET} ({reason}) : rien envoyé.")

    # ------------------------------------------------------------------ flow

    def _guarded(self, target: _Target, docs: TailoredDocuments) -> ApplyResult:
        try:
            return self._apply(target, docs)
        except Exception as exc:  # never let one application break the batch
            log.exception("Candidature LBA %s/%s en échec", target.sub_type, target.item_id)
            return _result(False, JobStatus.FAILED, truncate(f"Erreur La Bonne Alternance : {exc}", 240))

    def _apply(self, target: _Target, docs: TailoredDocuments) -> ApplyResult:
        if self.fatal:
            return _result(False, JobStatus.SKIPPED, f"La Bonne Alternance suspendue pour aujourd'hui : {self.fatal}")
        if self._already(f"item:{target.sub_type}/{target.item_id}"):
            return _result(False, JobStatus.SKIPPED, "Déjà postulé à cette offre via La Bonne Alternance.")
        detail, failure = self._detail(target)
        if failure is not None:
            return failure
        blocked = self._check_detail(target, detail)
        if blocked is not None:
            return blocked
        try:
            payload = build_payload(
                self.ctx.profile, docs, recipient_id=str(detail["recipient_id"]),
                application_url=page_url(target.sub_type, target.item_id, target.title), searched=target.title,
            )
        except ValueError as exc:
            return _result(False, JobStatus.FAILED, str(exc))
        if self.ctx.dry_run:
            return _result(True, JobStatus.SKIPPED, f"[dry-run] Candidature LBA pour {target.name} préparée "
                                                    f"(CV {payload['applicant_attachment_name']}), non envoyée.")
        return self._post(target, detail, payload)

    def _detail(self, target: _Target) -> tuple[dict[str, Any], ApplyResult | None]:
        url = DETAIL_URL.format(sub_type=target.sub_type, item_id=target.item_id)
        try:
            resp = request_with_retry(self.client, "GET", url, retries=2, throttle=self._throttle)
        except httpx.HTTPError as exc:
            self.fatal = f"site injoignable ({exc})"
            return {}, _result(False, JobStatus.FAILED, f"La Bonne Alternance injoignable : {exc}")
        if resp.status_code == 404:
            return {}, _result(False, JobStatus.EXPIRED, "Offre La Bonne Alternance introuvable (retirée).")
        if resp.status_code != 200:
            if resp.status_code >= 500 or resp.status_code in (419, 429):
                self.fatal = f"HTTP {resp.status_code} sur le détail"
            return {}, _result(False, JobStatus.FAILED, f"Détail LBA indisponible (HTTP {resp.status_code}).")
        try:
            data = resp.json()
        except ValueError:
            return {}, _result(False, JobStatus.FAILED, "Détail LBA illisible (réponse non JSON).")
        return (data, None) if isinstance(data, dict) else ({}, _result(False, JobStatus.FAILED, "Détail LBA vide."))

    def _detail_off_target(self, target: _Target, detail: dict[str, Any]) -> ApplyResult | None:
        """Recruiters are judged again on the detail's real NAF / size (a search hit lacks them)."""
        if target.company is None:
            return None
        return self._off_target(recruiter_detail_facts(target.company, detail))

    def _check_detail(self, target: _Target, detail: dict[str, Any]) -> ApplyResult | None:
        page = page_url(target.sub_type, target.item_id, target.title)
        job = detail.get("job") or {}
        status = str(job.get("status") or detail.get("status") or "Active")
        if status != "Active":
            return _result(False, JobStatus.EXPIRED, f"Offre La Bonne Alternance clôturée ({status}).")
        siret = str((detail.get("company") or {}).get("siret") or "")
        recipient = str(detail.get("recipient_id") or "")
        if (siret and self._already(f"siret:{siret}")) or (recipient and self._already(f"recipient:{recipient}")):
            return _result(False, JobStatus.SKIPPED, f"Déjà postulé chez {target.name} via La Bonne Alternance.")
        off_target = self._detail_off_target(target, detail)  # before suggesting any manual application
        if off_target is not None:
            return off_target
        if not (detail.get("contact") or {}).get("hasEmail") or not recipient or not detail.get("token"):
            return _result(False, JobStatus.NEEDS_MANUAL, f"Pas de candidature simplifiée LBA : postule sur {page}")
        if job.get("to_applicant_questions"):
            return _result(False, JobStatus.NEEDS_MANUAL,
                           f"Le recruteur pose des questions : réponds-y toi-même sur {page}")
        return None

    def _post(self, target: _Target, detail: dict[str, Any], payload: dict[str, Any]) -> ApplyResult:
        headers = {"Authorization": f"Bearer {detail['token']}", "Content-Type": "application/json"}
        self._throttle.wait(APPLY_URL)
        try:
            resp = self.client.post(APPLY_URL, json=payload, headers=headers, timeout=APPLY_TIMEOUT_S)
        except httpx.HTTPError as exc:
            self.fatal = f"envoi impossible ({exc})"
            return _result(False, JobStatus.FAILED, f"Envoi LBA impossible : {exc}")
        if resp.status_code in (200, 201, 202):
            self._remember(target, detail)
            return ApplyResult(ok=True, status=JobStatus.APPLIED, method=target.method,
                               detail=f"Candidature simplifiée envoyée via La Bonne Alternance à {target.name}.")
        return self._refused(target, detail, resp)

    def _refused(self, target: _Target, detail: dict[str, Any], resp: httpx.Response) -> ApplyResult:
        code = _server_code(resp)
        log.warning("LBA a refusé la candidature %s/%s : HTTP %s %s", target.sub_type, target.item_id,
                    resp.status_code, code)
        if code == "TOO_MANY_APPLICATIONS_PER_OFFER":
            self._remember(target, detail)
            return _result(False, JobStatus.SKIPPED, "LBA : nombre maximal de candidatures déjà atteint pour cette offre.")
        if code == "TOO_MANY_APPLICATIONS_PER_DAY" or resp.status_code in (419, 429) or resp.status_code >= 500:
            self.fatal = f"HTTP {resp.status_code} {code}".strip()
            return _result(False, JobStatus.FAILED, f"LBA indisponible ou limite atteinte (HTTP {resp.status_code}).")
        if code in ("EXPIRED", "NOTFOUND"):
            return _result(False, JobStatus.EXPIRED, "Offre La Bonne Alternance expirée ou supprimée.")
        if code == "INTERNAL_EMAIL":
            page = page_url(target.sub_type, target.item_id, target.title)
            return _result(False, JobStatus.NEEDS_MANUAL, f"Candidature LBA impossible ici : postule sur {page}")
        return _result(False, JobStatus.FAILED, f"Candidature LBA refusée (HTTP {resp.status_code} : {code}).")

    # ------------------------------------------------------------------ dedup registry (kv)

    def _already(self, key: str) -> bool:
        return self.ctx.repo.kv_get(KV_PREFIX + key) is not None

    def _remember(self, target: _Target, detail: dict[str, Any]) -> None:
        stamp = clock.iso(clock.now()) or ""
        keys = [f"item:{target.sub_type}/{target.item_id}"]
        siret = str((detail.get("company") or {}).get("siret") or "")
        if siret:
            keys.append(f"siret:{siret}")
        if detail.get("recipient_id"):
            keys.append(f"recipient:{detail['recipient_id']}")
        for key in keys:
            try:
                self.ctx.repo.kv_set(KV_PREFIX + key, stamp)
            except Exception:  # the application left: a DB hiccup must not turn it into a failure
                log.exception("Mémorisation de la candidature LBA %s impossible", key)
