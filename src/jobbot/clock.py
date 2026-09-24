"""Time helpers. Everything is stored as timezone-aware UTC; displayed in Europe/Paris."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")


def now() -> datetime:
    return datetime.now(UTC)


def to_paris(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(PARIS)


def parse_dt(value: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 string (accepts trailing 'Z'); naive values are assumed UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


FRENCH_MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


def french_date(dt: datetime | None = None) -> str:
    """'23 septembre 2026' — for letters ("Metz, le 23 septembre 2026")."""
    d = to_paris(dt or now())
    return f"{d.day} {FRENCH_MONTHS[d.month - 1]} {d.year}"
