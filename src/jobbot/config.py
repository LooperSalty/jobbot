"""Configuration: candidate profile (config/profile.yaml), tunables (config/settings.yaml), secrets (.env)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobbot.config_sections import (  # noqa: F401
    ClassroomSettings,
    Frozen,
    LargeCompanies,
    NightSettings,
    OutreachSettings,
    ReportSettings,
    SpontaneousRamp,
    parse_active_hours,
    parse_clock,
)
from jobbot.models import ContractType


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
    external_per_day: int = 5  # offres sur les plateformes externes prises en charge
    spontaneous_per_day: int = 20  # used when the ramp below is disabled
    spontaneous_ramp: SpontaneousRamp = Field(default_factory=SpontaneousRamp)
    followups_per_day: int = 10
    prepare_per_run: int = Field(default=25, ge=1)  # offers given a tailored CV + letter per run (LLM time)
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
                                 "apec", "adzuna", "jooble", "company_ats"]
    )
    max_results_per_query: int = 100
    max_age_days: int = 30
    # « score » step: short offers (search cards) completed through their source before scoring, per run
    max_detail_fetches_per_run: int = Field(default=60, ge=0)
    # « company_ats » source: company career boards (ATS job APIs / careers pages) read per run
    max_ats_boards_per_run: int = Field(default=80, ge=0)


_NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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
    night: NightSettings = Field(default_factory=NightSettings)
    paths: Paths = Field(default_factory=Paths)
    notify_jarvis: bool = False  # speak notifications through the local Jarvis TTS
    jarvis_script: str | None = None  # path to jarvis_notify.py (called as: python <script> --type tell "<msg>")
    notify_email: bool = False  # important / urgent notifications also e-mailed to yourself (SMTP, server)
    notify_email_max_per_day: int = Field(10, ge=0)
    notify_ntfy_topic: str | None = None  # ntfy.sh push topic (phone app « ntfy »), None = off
    notify_telegram: bool = False  # Telegram bot (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env)
    notify_telegram_all: bool = False  # also « info » notifications (summaries); default: important / urgent only
    search_engine_url: str | None = None  # self-hosted SearXNG (JSON enabled), used before Brave / DuckDuckGo
    search_engine_engines: list[str] = Field(default_factory=lambda: ["bing"])  # SearXNG « engines= » of every query

    @field_validator("notify_ntfy_topic", mode="before")
    @classmethod
    def _valid_topic(cls, value: object) -> object:
        """Blank → None; otherwise a ntfy topic name (letters, digits, - and _, 64 max: part of the URL)."""
        if value is None or not str(value).strip():
            return None
        topic = str(value).strip()
        if not _NTFY_TOPIC_RE.match(topic):
            raise ValueError(f"Sujet ntfy invalide « {topic} » : lettres, chiffres, - et _ uniquement (64 max)")
        return topic

    @field_validator("search_engine_url", mode="before")
    @classmethod
    def _valid_engine(cls, value: object) -> object:
        if value is None or not str(value).strip():
            return None
        url = str(value).strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", url):
            raise ValueError(f"Adresse SearXNG invalide « {url} » (ex. http://127.0.0.1:8888)")
        return url

    @field_validator("search_engine_engines", mode="before")
    @classmethod
    def _engine_names(cls, value: object) -> object:
        """'bing, Brave' or a list → ['bing', 'brave'] (empty = every engine enabled on the instance)."""
        if value is None:
            return []
        items = value.split(",") if isinstance(value, str) else value
        return [str(v).strip().lower() for v in items if str(v).strip()] if isinstance(items, list) else value


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
    claude_code_oauth_token: str | None = None  # « claude setup-token » (server): passed to « claude -p »
    telegram_bot_token: str | None = None  # notifications through a Telegram bot (never logged)
    telegram_chat_id: str | None = None
    adzuna_app_id: str | None = None  # Adzuna job API (developer.adzuna.com), with adzuna_app_key
    adzuna_app_key: str | None = None
    jooble_api_key: str | None = None  # Jooble REST API, French key (fr.jooble.org/api/about)

    @field_validator(
        "france_travail_client_id", "france_travail_client_secret", "lba_api_token", "hunter_api_key",
        "brave_api_key", "anthropic_api_key", "smtp_user", "smtp_password", "classroom_course_urls",
        "claude_code_oauth_token", "telegram_bot_token", "telegram_chat_id", mode="before",
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


SETTINGS_FILE = "config/settings.yaml"
LOCAL_SETTINGS_FILE = "config/settings.local.yaml"  # gitignored, wins over settings.yaml


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


def deep_merge(base: dict, override: dict) -> dict:
    """New dict where ``override`` wins; nested mappings are merged key by key (lists and scalars replaced)."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        merged[key] = deep_merge(current, value) if isinstance(current, dict) and isinstance(value, dict) else value
    return merged


def settings_data(root: Path, *, include_local: bool = True) -> dict:
    """``config/settings.yaml`` with the optional, gitignored ``config/settings.local.yaml`` merged over it
    (machine-specific overrides, e.g. ``headless: true`` on a server)."""
    path = root / SETTINGS_FILE
    data = _read_yaml(path) if path.exists() else {}
    local = root / LOCAL_SETTINGS_FILE
    return deep_merge(data, _read_yaml(local)) if include_local and local.exists() else data


def load_settings(root: Path, *, include_local: bool = True) -> Settings:
    return Settings.model_validate(settings_data(root, include_local=include_local))


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
