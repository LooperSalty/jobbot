"""Small helpers shared by the API / ATS sources (Adzuna, Jooble, company career boards).

Contract and remote detection reuse the LinkedIn rules (title first, then an explicit contract hint from the
API, then the description); title filters keep developer jobs and drop sales / senior roles.
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from jobbot import clock
from jobbot.models import ContractType
from jobbot.sources.countries import COUNTRY_NAMES
from jobbot.sources.linkedin import detect_remote as _linkedin_remote
from jobbot.sources.linkedin import guess_contract_type
from jobbot.textutil import strip_accents

_PERIODS = {"year": "an", "yearly": "an", "annual": "an", "month": "mois", "monthly": "mois", "week": "semaine",
            "weekly": "semaine", "day": "jour", "daily": "jour", "hour": "heure", "hourly": "heure"}
# Explicit contract labels of the APIs (Recruitee employment_type_code, SmartRecruiters typeOfEmployment /
# « Contract » custom field, JSON-LD employmentType, Jooble « type »…). Order matters: fixed_term before full-time.
_HINTS: tuple[tuple[re.Pattern[str], ContractType], ...] = (
    (re.compile(r"alternan|apprenti|apprenticeship|professionnalisation|work[\s_-]?study"), ContractType.ALTERNANCE),
    (re.compile(r"intern|stage|stagiaire|trainee"), ContractType.STAGE),
    (re.compile(r"\bcdd\b|fixed[\s_-]?term|temporary|befristet|duree determinee"), ContractType.CDD),
    (re.compile(r"\bcdi\b|permanent|unbefristet|duree indeterminee"), ContractType.CDI),
    (re.compile(r"freelance|contractor|independ|portage"), ContractType.FREELANCE),
    (re.compile(r"interim"), ContractType.INTERIM),
)
_DEV_TITLE = re.compile(
    r"(developpe|developer|\bdevs?\b|devops|programmeu|programmer|software|logiciel|full[\s-]?stack|"
    r"front[\s-]?end|back[\s-]?end|\bsre\b|site reliability|engineer|ingenieure? (d.)?(etudes|logiciel|"
    r"developpement|informatique|devops|cloud|web|test|qa|data|systemes? embarques?)|integrat\w+ web|webmaster|"
    r"\bweb\b|mobile|\bios\b|android|\bqa\b|test automation|data engineer|cloud|python|\bjava\b|javascript|"
    r"typescript|\bphp\b|\.net\b|react|angular|symfony|laravel|node)"
)
_NOT_DEV_TITLE = re.compile(
    r"(business dev|developpe\w* (commercial|foncier|d'affaires|immobilier|rh)|charge\w* de developpement|"
    r"commercial|\bsales\b|account (executive|manager)|marketing|customer success|solutions? engineer|"
    r"pre-?sales|avant-vente|recruit|talent acquisition|juriste|\blegal\b|comptab|finance|acheteu)"
)
_SENIOR_TITLE = re.compile(
    r"\b(senior|sr|lead|principal|staff|head|director|directeur|directrice|manager|vp|chief|cto|architecte?|"
    r"expert|experte)\b"
)
# Country names (French / English / local) → ISO code, for the ATS APIs that give a name instead of a code.
_COUNTRY_WORDS = {
    **{strip_accents(name).lower(): code for code, name in COUNTRY_NAMES.items()},
    "france": "FR", "luxembourg": "LU", "belgium": "BE", "belgie": "BE", "germany": "DE", "deutschland": "DE",
    "switzerland": "CH", "schweiz": "CH", "netherlands": "NL", "nederland": "NL", "spain": "ES", "espana": "ES",
    "italy": "IT", "italia": "IT", "united kingdom": "GB", "uk": "GB", "england": "GB", "united states": "US",
    "usa": "US", "canada": "CA", "portugal": "PT", "poland": "PL", "polska": "PL", "ireland": "IE",
    "austria": "AT", "sweden": "SE", "denmark": "DK", "norway": "NO", "finland": "FI", "india": "IN",
    "singapore": "SG", "australia": "AU", "brazil": "BR", "mexico": "MX", "japan": "JP", "china": "CN",
}


class QueryFailed(RuntimeError):
    """One query failed (network, HTTP 5xx / 4xx, unreadable answer): the source's next queries still run.
    ``SourceUnavailable`` stays for what stops the source for the run (refused key, quota, anti-bot block)."""


def dict_items(value: object) -> list[dict]:
    """The dict elements of a JSON list (anything else → [])."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def as_int(value: object) -> int:
    try:
        return int(value) if value not in (None, "") and not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        return 0


def fold(text: str | None) -> str:
    return " ".join(strip_accents(text or "").lower().replace("’", "'").split())


def contract_from_hint(hint: str | None) -> ContractType:
    """Contract from an API label (« apprenticeship », « fulltime_permanent », « Stage », « CDD »…)."""
    folded = fold(hint)
    return next((contract for pattern, contract in _HINTS if pattern.search(folded)), ContractType.OTHER)


def detect_contract(title: str, description: str = "", hint: str | None = None) -> ContractType:
    """Title first (« … en alternance »), then the API's explicit label, then the description."""
    by_title = guess_contract_type(title or "")
    if by_title != ContractType.OTHER:
        return by_title
    by_hint = contract_from_hint(hint)
    if by_hint != ContractType.OTHER:
        return by_hint
    return guess_contract_type("", description or "")


def detect_remote(*texts: str | None) -> bool | None:
    """True for a full-remote offer, None when unknown or partial (hybrid)."""
    return _linkedin_remote(" ".join(t for t in texts if t))


def is_dev_title(title: str) -> bool:
    folded = fold(title)
    return bool(_DEV_TITLE.search(folded)) and not _NOT_DEV_TITLE.search(folded)


def is_not_dev_title(title: str) -> bool:
    """Sales / real-estate « développeur » and other non-IT jobs a keyword search brings back."""
    return bool(_NOT_DEV_TITLE.search(fold(title)))


def is_senior_title(title: str) -> bool:
    return bool(_SENIOR_TITLE.search(fold(title)))


def country_code(value: object) -> str | None:
    """ISO code from « FR », « fr », « France », « Nederland »… (None when unknown)."""
    text = fold(str(value or "")).strip(" .")
    if not text:
        return None
    if re.fullmatch(r"[a-z]{2}", text):
        return text.upper()
    return _COUNTRY_WORDS.get(text)


def format_salary(low: Any, high: Any, currency: str | None = "EUR", period: str | None = None) -> str | None:
    values = [float(v) for v in (low, high) if isinstance(v, int | float) and not isinstance(v, bool) and v > 0]
    if not values:
        return None
    amounts = " – ".join(f"{round(v):,}".replace(",", " ") for v in dict.fromkeys(values))
    symbol = "€" if (currency or "EUR").upper() == "EUR" else currency
    unit = _PERIODS.get((period or "").lower())
    return f"{amounts} {symbol}" + (f" / {unit}" if unit else "")


def parse_date(value: object) -> datetime | None:
    """ISO strings, « 2026-09-01 12:17:30 UTC », RFC 822 (RSS) dates and epoch milliseconds."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().removesuffix(" UTC")
    try:
        parsed = clock.parse_dt(text)
    except (TypeError, ValueError):
        parsed = _rfc822(text)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _rfc822(text: str) -> datetime | None:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def float_or_none(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def domain_of(url: str | None) -> str | None:
    host = urlparse(url or "").netloc.lower().split(":")[0]
    return host.removeprefix("www.") or None


def secret_value(value: object) -> str:
    """A key read from .env: blank or ``KEY=   # comment`` (read as the comment by python-dotenv) → ""."""
    text = str(value or "").strip()
    return "" if text.startswith("#") else text


def clean_title(title: object) -> str:
    return " ".join(str(title or "").split())


def strip_tags(text: object) -> str:
    """« <strong>Java</strong> Developer &amp; co » → « Java Developer & co » (API highlight markup)."""
    return clean_title(htmllib.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))).replace(" ,", ",")


def with_contract_word(keywords: str, contracts: list[ContractType]) -> str:
    """Keyword APIs have no apprenticeship / internship filter: the contract goes into the search words."""
    term = " ".join((keywords or "").split())
    wanted = set(contracts)
    folded = fold(term)
    if wanted == {ContractType.ALTERNANCE} and not re.search(r"alternan|apprenti", folded):
        return f"{term} alternance"
    if wanted == {ContractType.STAGE} and not re.search(r"\bstage", folded):
        return f"{term} stage"
    return term
