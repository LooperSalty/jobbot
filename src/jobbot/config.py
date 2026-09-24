"""Configuration: candidate profile (config/profile.yaml), tunables (config/settings.yaml), secrets (.env)."""

from __future__ import annotations

import re
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobbot.models import ContractType


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- profile


class Identity(Frozen):
    first_name: str
    last_name: str
    email: str
    phone: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = "France"
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    driving_license: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Experience(Frozen):
    title: str
    company: str
    location: str = ""
    start: str = ""  # "2026-02" or "2025"
    end: str = ""  # "" means ongoing
    kind: str = "job"  # job | internship | freelance | seasonal
    bullets: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class Education(Frozen):
    degree: str
    school: str
    location: str = ""
    start: str = ""
    end: str = ""
    details: str = ""


class Project(Frozen):
    name: str
    description: str
    skills: list[str] = Field(default_factory=list)
    url: str | None = None


class SkillGroup(Frozen):
    category: str
    items: list[str]


class Language(Frozen):
    name: str
    level: str


class AlternanceInfo(Frozen):
    school: str = ""
    degree: str = ""
    rncp_level: str = ""
    duration_months: int = 24
    start_date: str = ""  # "2026-10"
    rhythm: str = ""  # "3 semaines en entreprise / 1 semaine à l'école"
    school_url: str | None = None


class LocationPref(Frozen):
    city: str
    postal_code: str = ""
    radius_km: int = 30
    latitude: float | None = None
    longitude: float | None = None
    country: str = "FR"  # ISO code: FR, LU, BE, DE...
    contract_types: list[ContractType] | None = None  # None = all target contract types (e.g. LU → [cdi])


class TargetPrefs(Frozen):
    contract_types: list[ContractType] = Field(default_factory=lambda: [ContractType.ALTERNANCE])
    job_titles: list[str] = Field(default_factory=list)  # search queries + title matching
    keywords: list[str] = Field(default_factory=list)  # extra search keywords
    locations: list[LocationPref] = Field(default_factory=list)
    remote_ok: bool = True
    exclude_title_keywords: list[str] = Field(default_factory=list)
    exclude_description_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    max_experience_years: int = 2
    min_salary_eur: int | None = None
    willing_to_relocate: bool = False
    relocation_regions: list[str] = Field(default_factory=list)  # region names, e.g. Grand Est
    # spontaneous applications
    naf_codes: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    min_headcount_code: str | None = None  # INSEE tranche_effectif_salarie code, e.g. "03" (6-9)


class Profile(Frozen):
    identity: Identity
    headline: str
    summary: str
    target: TargetPrefs
    alternance: AlternanceInfo | None = None
    experiences: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    qualities: list[str] = Field(default_factory=list)
    # Answers reused by form fillers (LinkedIn, HelloWork...). Keys are free-form questions or
    # canonical keys like "years_experience", "salary_expectation", "notice_period", "work_authorization".
    answers: dict[str, str] = Field(default_factory=dict)
    years_experience: dict[str, int] = Field(default_factory=dict)  # skill -> years ("default" key)

    def all_skills(self) -> list[str]:
        seen: dict[str, None] = {}
        for group in self.skills:
            for item in group.items:
                seen.setdefault(item, None)
        for exp in self.experiences:
            for s in exp.skills:
                seen.setdefault(s, None)
        for p in self.projects:
            for s in p.skills:
                seen.setdefault(s, None)
        return list(seen)

    def full_text(self) -> str:
        """Everything the candidate can truthfully claim — used as the truth base for tailoring."""
        parts: list[str] = [self.headline, self.summary]
        for e in self.experiences:
            parts.append(f"{e.title} — {e.company} ({e.start}–{e.end or 'présent'})")
            parts.extend(e.bullets)
            parts.append(", ".join(e.skills))
        for ed in self.education:
            parts.append(f"{ed.degree} — {ed.school} {ed.details}")
        for p in self.projects:
            parts.append(f"{p.name}: {p.description} ({', '.join(p.skills)})")
        for g in self.skills:
            parts.append(f"{g.category}: {', '.join(g.items)}")
        parts.extend(f"{lang.name} {lang.level}" for lang in self.languages)
        parts.extend(self.qualities)
        return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- settings


class LLMSettings(Frozen):
    provider: Literal["claude_cli", "anthropic", "none"] = "claude_cli"
    model: str = "claude-sonnet-5"  # quality tasks: CV, letters
    fast_model: str = "claude-haiku-4-5-20251001"  # classification, extraction, form answers
    cli_path: str = "claude"
    timeout_s: int = 240


class Limits(Frozen):
    linkedin_per_day: int = 15
    hellowork_per_day: int = 10
    lba_per_day: int = 20
    email_applications_per_day: int = 30
    spontaneous_per_day: int = 20
    followups_per_day: int = 10
    min_delay_s: float = 40.0  # between two applications / emails
    max_delay_s: float = 140.0
    action_min_ms: int = 400  # between two browser actions
    action_max_ms: int = 1600


class Thresholds(Frozen):
    min_match_score: float = 45.0  # below → filtered_out
    auto_apply_min_score: float = 60.0  # mode=auto: ≥ → apply automatically, else → pending_review
    target_ats_score: float = 80.0
    max_tailor_iterations: int = 3


class FollowupSettings(Frozen):
    first_after_days: int = 7
    second_after_days: int = 8  # after the first follow-up
    max_followups: int = 2
    give_up_after_days: int = 30  # then status → no_response


class SearchSettings(Frozen):
    sources: list[str] = Field(
        default_factory=lambda: ["lba", "france_travail", "linkedin", "hellowork", "indeed", "wttj", "free_work",
                                 "apec"]
    )
    max_results_per_query: int = 100
    max_age_days: int = 30


class OutreachSettings(Frozen):
    """Spontaneous applications: who may be contacted and how aggressively."""

    allow_nominative: bool = True  # named people (leaders, employees) and not only rh@/contact@
    allow_employee_search: bool = True  # look for HR / tech managers (team pages, Hunter, search engines)
    allow_unverified_guesses: bool = True  # send to a best-guess pattern (prenom.nom@) when it can't be verified
    max_unverified_per_day: int = Field(8, ge=0)
    max_bounce_rate: float = Field(0.08, ge=0, le=1)  # pause guessed sends when recent bounce rate exceeds this
    max_attempts_per_company: int = Field(2, ge=1)  # after a bounce, try the next best address
    preferred_roles: list[str] = Field(
        default_factory=lambda: ["rh", "recrutement", "talent", "cto", "directeur technique", "lead", "gérant",
                                 "président"]
    )


class ReportSettings(Frozen):
    weekly_report: bool = True
    weekly_report_day: int = Field(0, ge=0, le=6)  # 0 = Monday … 6 = Sunday
    interview_prep: bool = True  # generate a prep sheet when an interview invitation arrives
    weekly_email: bool = False  # also e-mail the weekly report to the candidate himself (SMTP required)


_HOURS_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def parse_active_hours(spec: str) -> tuple[time, time]:
    """'07:30-21:00' → (07:30, 21:00). ValueError (French) when malformed."""
    match = _HOURS_RE.match(spec or "")
    if not match:
        raise ValueError(f"Plage horaire invalide « {spec} » (format HH:MM-HH:MM, ex. 07:30-21:00)")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    return time(h1, m1), time(h2, m2)


class ClassroomSettings(Frozen):
    """Offers relayed by the school in Google Classroom: watched often, applied to immediately."""

    enabled: bool = True
    backend: Literal["browser", "api"] = "browser"  # browser: logged-in profile (no setup); api: OAuth (optional)
    poll_minutes: int = Field(15, ge=1, le=1439)  # period of the scheduled « classroom watch --once » (≤ 1 day)
    active_hours: str = "07:30-21:00"  # outside this window the watcher exits at once
    auto_apply: bool = True  # apply right away, even during the warm-up review period
    min_score: float | None = Field(None, ge=0, le=100)  # None → thresholds.auto_apply_min_score
    headless: bool = True  # headful retry once when Google asks to log in
    browser_profile: str = "data/browser-profile-classroom"  # never shared with the LinkedIn/HelloWork profile
    max_posts: int = Field(30, ge=1, le=200)
    school_bonus: float = Field(5.0, ge=0, le=30)  # added to the match score of a school-relayed offer

    @field_validator("active_hours")
    @classmethod
    def _valid_hours(cls, value: str) -> str:
        parse_active_hours(value)
        return value.strip()


class Paths(Frozen):
    data_dir: str = "data"
    output_dir: str = "output"
    db: str = "data/jobbot.db"
    browser_profile: str = "data/browser-profile"
    profile: str = "config/profile.yaml"
    logs_dir: str = "data/logs"


class Settings(Frozen):
    setup_completed: bool = False  # set by `jobbot setup` (first-run wizard)
    mode: Literal["auto", "review"] = "auto"
    warmup_days: int = 3  # after setup, stay in review mode this many days before `auto` takes effect
    setup_date: str | None = None  # ISO date of the first completed setup (warm-up reference)
    headless: bool = False  # browser automation visible by default (safer + you can watch)
    browser_channel: str | None = "chrome"  # use installed Chrome; None → bundled Chromium
    language: str = "fr"
    llm: LLMSettings = Field(default_factory=LLMSettings)
    limits: Limits = Field(default_factory=Limits)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    followup: FollowupSettings = Field(default_factory=FollowupSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    outreach: OutreachSettings = Field(default_factory=OutreachSettings)
    reports: ReportSettings = Field(default_factory=ReportSettings)
    classroom: ClassroomSettings = Field(default_factory=ClassroomSettings)
    paths: Paths = Field(default_factory=Paths)
    notify_jarvis: bool = False  # speak notifications through the local Jarvis TTS
    jarvis_script: str | None = None  # path to jarvis_notify.py (called as: python <script> --type tell "<msg>")


class Secrets(BaseSettings):
    """Read from environment / .env. Never logged."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    france_travail_client_id: str | None = None
    france_travail_client_secret: str | None = None
    lba_api_token: str | None = None
    hunter_api_key: str | None = None
    brave_api_key: str | None = None
    anthropic_api_key: str | None = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None  # Gmail "mot de passe d'application"
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    classroom_course_urls: str | None = None  # school's Google Classroom course(s), comma-separated (private)

    @field_validator(
        "france_travail_client_id", "france_travail_client_secret", "lba_api_token", "hunter_api_key",
        "brave_api_key", "anthropic_api_key", "smtp_user", "smtp_password", "classroom_course_urls", mode="before",
    )
    @classmethod
    def _blank_or_comment_is_absent(cls, value: object) -> object:
        """``KEY=   # comment`` in .env is read as the comment text by python-dotenv: treat it (and "") as unset."""
        if isinstance(value, str) and (not value.strip() or value.strip().startswith("#")):
            return None
        return value


# --------------------------------------------------------------------------- loaders


class ConfigError(RuntimeError):
    pass


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Fichier de configuration introuvable : {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} doit contenir un objet YAML (clé: valeur)")
    return data


def load_settings(root: Path) -> Settings:
    path = root / "config" / "settings.yaml"
    return Settings.model_validate(_read_yaml(path)) if path.exists() else Settings()


def load_profile(path: Path) -> Profile:
    if not path.exists():
        raise ConfigError(f"Profil introuvable : {path} — lance « jobbot init » (copie du modèle fictif "
                          "config/profile.example.yaml) puis remplace-le par ton profil (ou « jobbot import-cv »).")
    try:
        return Profile.model_validate(_read_yaml(path))
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError → readable message
        raise ConfigError(f"Profil invalide ({path}):\n{exc}") from exc


def load_secrets(root: Path) -> Secrets:
    return Secrets(_env_file=root / ".env")  # type: ignore[call-arg]
