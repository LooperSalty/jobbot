"""Reports of the night run and of its steps (immutable)."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HarvestResult(_Frozen):
    found: int = 0  # distinct candidates (registry near_point + LBA recruiters + offers' employers)
    new: int = 0
    relevant: int = 0
    irrelevant: int = 0
    stopped: bool = False  # time budget reached
    errors: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)


class EnrichResult(_Frozen):
    processed: int = 0
    with_email: int = 0
    generic_emails: int = 0  # new role addresses (recrutement@, rh@, contact@…)
    nominative_emails: int = 0  # new addresses of named people (published, Hunter or guessed)
    not_started: int = 0  # companies left for the next night (time budget)
    irrelevant: int = 0


class PrepareResult(_Frozen):
    prepared: int = 0
    already_ready: int = 0
    planned: int = 0  # dry-run: would have been prepared
    failed: int = 0
    stopped: bool = False
    details: list[str] = Field(default_factory=list)


class NightReport(_Frozen):
    """What one night brought; nothing is ever sent at night."""

    companies_found: int = 0
    companies_new: int = 0
    relevant: int = 0
    irrelevant: int = 0
    enriched: int = 0
    with_email: int = 0
    generic_emails: int = 0
    nominative_emails: int = 0
    docs_prepared: int = 0
    queue_ready: int = 0
    daily_cap: int = 0  # spontaneous e-mails allowed per day (sending ramp)
    purged: int = 0  # companies whose named contacts were deleted (retention)
    duration_min: float = 0.0
    stopped_early: bool = False  # the time budget cut a step short
    dry_run: bool = False
    errors: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)

    @property
    def queue_days(self) -> int | None:
        """Days of sending the ready queue represents (None when empty or without a daily cap)."""
        if self.daily_cap <= 0 or self.queue_ready <= 0:
            return None
        return math.ceil(self.queue_ready / self.daily_cap)

    def summary(self) -> str:
        """« Nuit : +X entreprises (Y pertinentes, N nouvelles), +Z adresses (dont N nominatives), D dossiers
        prêts, file d'attente : Q entreprises ≈ J jours d'envoi »."""
        days = self.queue_days
        horizon = ""
        if days is not None:
            horizon = f" ≈ {days} jours d'envoi" if days > 1 else " ≈ 1 jour d'envoi"
        text = (f"Nuit : +{self.companies_found} entreprises ({self.relevant} pertinentes, {self.companies_new} "
                f"nouvelles), +{self.generic_emails + self.nominative_emails} adresses (dont "
                f"{self.nominative_emails} nominatives), {self.docs_prepared} dossiers prêts, file d'attente : "
                f"{self.queue_ready} entreprises{horizon}")
        if self.stopped_early:
            text += " — arrêt à l'heure prévue, la suite la nuit prochaine"
        if self.errors:
            text += f" — {len(self.errors)} étape(s) en erreur"
        return text
