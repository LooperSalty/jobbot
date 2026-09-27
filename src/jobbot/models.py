"""Domain models shared by every module.

All models are immutable (frozen). To "change" one, use ``model.model_copy(update={...})``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContractType(StrEnum):
    CDI = "cdi"
    CDD = "cdd"
    ALTERNANCE = "alternance"
    STAGE = "stage"
    FREELANCE = "freelance"
    INTERIM = "interim"
    OTHER = "other"


class ApplyMethod(StrEnum):
    LINKEDIN_EASY_APPLY = "linkedin_easy_apply"
    HELLOWORK = "hellowork"
    LBA = "lba"  # La Bonne Alternance in-site application
    EMAIL = "email"  # send CV + letter to a contact email
    EXTERNAL = "external"  # company ATS / website: prepared docs, human clicks "submit"
    MANUAL = "manual"


class JobStatus(StrEnum):
    NEW = "new"  # just collected
    FILTERED_OUT = "filtered_out"  # failed pre-filter (contract, location, excluded words...)
    SHORTLISTED = "shortlisted"  # passed match threshold
    DOCS_READY = "docs_ready"  # tailored CV + letter generated
    PENDING_REVIEW = "pending_review"  # waiting for user approval (review mode)
    APPROVED = "approved"  # user approved, ready to apply
    APPLIED = "applied"
    NEEDS_MANUAL = "needs_manual"  # automation could not finish (unknown question, external ATS...)
    FAILED = "failed"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class ApplicationStatus(StrEnum):
    APPLIED = "applied"
    FOLLOWED_UP = "followed_up"
    RESPONSE = "response"  # any human answer not yet classified
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    NO_RESPONSE = "no_response"  # all follow-ups sent, nothing came back
    WITHDRAWN = "withdrawn"
    BOUNCED = "bounced"  # email address invalid


TERMINAL_APPLICATION_STATUSES = frozenset(
    {
        ApplicationStatus.OFFER,
        ApplicationStatus.REJECTED,
        ApplicationStatus.NO_RESPONSE,
        ApplicationStatus.WITHDRAWN,
        ApplicationStatus.BOUNCED,
    }
)


class JobOffer(Frozen):
    """A job/alternance offer collected from any source."""

    id: str  # stable id = textutil.stable_id(source, external_id)
    source: str  # france_travail | lba | hellowork | linkedin | wttj | indeed | ...
    external_id: str
    title: str
    company: str = ""
    location: str = ""
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    description: str = ""
    url: str = ""  # public offer page
    apply_url: str | None = None  # where to apply if different from url
    apply_method: ApplyMethod = ApplyMethod.EXTERNAL
    contract_type: ContractType = ContractType.OTHER
    remote: bool | None = None
    salary: str | None = None
    contact_email: str | None = None
    contact_name: str | None = None
    company_domain: str | None = None
    published_at: datetime | None = None
    found_at: datetime
    status: JobStatus = JobStatus.NEW
    match_score: float | None = None  # 0-100, pre-filter relevance
    match_reasons: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class ContactEmail(Frozen):
    email: str
    confidence: float = 0.5  # 0..1
    source: str = "scraped"  # scraped | pattern | hunter | api | manual
    kind: str = "generic"  # recruitment | hr | tech | leader | generic | named
    person_name: str | None = None
    verified: bool = False


class Company(Frozen):
    """A company targeted for a spontaneous application."""

    id: str  # stable_id("company", siren or normalized name)
    name: str
    siren: str | None = None
    naf_code: str | None = None
    naf_label: str | None = None
    headcount_range: str | None = None
    city: str = ""
    postal_code: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    domain: str | None = None
    website: str | None = None
    careers_url: str | None = None  # careers page or company ATS (teamtailor, jobtoolz...) when found
    description: str | None = None  # short summary of what they do (for personalization)
    leaders: list[str] = Field(default_factory=list)
    contacts: list[ContactEmail] = Field(default_factory=list)
    source: str = "recherche_entreprises"  # recherche_entreprises | lba_recruteur | manual
    found_at: datetime
    # new | enriched | no_email | pending_review | approved | rejected | contacted | blacklisted | irrelevant
    status: str = "new"
    raw: dict[str, Any] = Field(default_factory=dict)


class KeywordHit(Frozen):
    keyword: str
    weight: float
    category: str = "hard"  # hard | soft | title | tool | language | certification
    found: bool = False


class ATSReport(Frozen):
    """Explainable ATS-like score of a document against a job description."""

    score: float  # 0-100 overall
    keyword_score: float  # 0-100
    title_score: float  # 0-100
    section_score: float  # 0-100
    format_score: float  # 0-100
    matched: list[KeywordHit] = Field(default_factory=list)
    missing: list[KeywordHit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    word_count: int = 0


class TailoredDocuments(Frozen):
    job_id: str | None = None
    company_id: str | None = None
    cv_pdf: str  # absolute path
    cv_docx: str | None = None
    letter_pdf: str | None = None
    letter_text: str = ""
    email_subject: str = ""
    email_body: str = ""
    cv_ats: ATSReport | None = None
    letter_ats: ATSReport | None = None
    truth_warnings: list[str] = Field(default_factory=list)  # claims not backed by the master profile
    created_at: datetime


class Application(Frozen):
    id: int | None = None
    job_id: str | None = None
    company_id: str | None = None
    method: ApplyMethod
    status: ApplicationStatus = ApplicationStatus.APPLIED
    applied_at: datetime
    company_name: str = ""
    job_title: str = ""
    email_to: str | None = None
    email_message_id: str | None = None  # Message-ID of the first email (threading)
    email_thread_ids: list[str] = Field(default_factory=list)  # all Message-IDs sent in this thread
    cv_path: str | None = None
    letter_path: str | None = None
    followup_count: int = 0
    last_contact_at: datetime | None = None
    next_followup_at: datetime | None = None
    last_response_at: datetime | None = None
    notes: str = ""


class ApplicationEvent(Frozen):
    id: int | None = None
    application_id: int
    at: datetime
    # applied | followup_sent | manual_followup | reply | interview | rejected | bounce | note | status_change
    # | retry_after_bounce
    kind: str
    detail: str = ""


class SentEmail(Frozen):
    id: int | None = None
    to: str
    subject: str
    message_id: str
    in_reply_to: str | None = None
    kind: str  # application | spontaneous | followup | report | notification
    application_id: int | None = None
    sent_at: datetime
    dry_run: bool = False
