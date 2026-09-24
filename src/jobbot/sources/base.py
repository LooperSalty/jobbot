"""Contract for job sources (boards / APIs)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from jobbot.config import LocationPref
from jobbot.models import ContractType, JobOffer


class SearchQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    keywords: str
    location: LocationPref | None = None
    contract_types: list[ContractType] = Field(default_factory=list)  # empty = any
    remote: bool = False
    max_results: int = 100
    max_age_days: int = 30


class SourceUnavailable(RuntimeError):
    """Raised when a source cannot run (missing credentials, blocked, site changed)."""


class JobSource(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(True, "") if usable, else (False, human-readable reason in French)."""
        ...

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        """Yield offers (status NEW, found_at=now). Must respect query.max_results and be polite."""
        ...

    def fetch_details(self, job: JobOffer) -> JobOffer:
        """Return a copy with the full description / apply info when the search listing was partial."""
        ...
