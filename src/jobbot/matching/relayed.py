"""Offers relayed by the candidate's school (Google Classroom posts, ``raw["relayed_by_school"]``).

- The school/CFA "fake offer" filter does not apply: the post names the school, the offer is a company's.
- ``raw["school_bonus"]`` (``classroom.school_bonus``) is added to the score (« offre relayée par l'école »).
- The level the offer targets (``raw["target_level"]``) is compared with the candidate's programme (RNCP level
  of ``profile.alternance``, Bac+5 = 7 by default): an offer only for clearly lower levels (BTS / Bac+2, CAP, Bac)
  is filtered with the reason; Bac+3 to Bac+5, Master, Mastère, ingénieur, RNCP 6-7 are fine.
- A deadline already passed, or a linked board offer that expired, filters the offer too.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from jobbot import clock
from jobbot.config import Profile
from jobbot.matching._text import norm
from jobbot.models import JobOffer

DEFAULT_BONUS = 5.0
DEFAULT_CANDIDATE_LEVEL = 7  # RNCP 7 = Bac+5
BONUS_REASON = "Offre relayée par l'école (+{bonus:g} points)"
_LEVELS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"bac\s*\+\s*[89]|doctorat|\bphd\b|niveau 8|rncp\s*8"), 8),
    (re.compile(r"bac\s*\+\s*5|\bmaster\b|\bmastere\b|\bmsc\b|\bm2\b|\bmba\b|ingenieur|niveau 7|rncp\s*(?:niveau\s*)?7"
                r"|\bexpert\b"), 7),
    (re.compile(r"bac\s*\+\s*[34]|\blicence\b|\bbachelor\b|\bbut\b|\bm1\b|niveau 6|rncp\s*(?:niveau\s*)?6"), 6),
    (re.compile(r"bac\s*\+\s*2|\bbts\b|\bdut\b|\bdeust\b|niveau 5|rncp\s*(?:niveau\s*)?5"), 5),
    (re.compile(r"\bbac\b(?!\s*\+)|baccalaureat|niveau 4|rncp\s*(?:niveau\s*)?4|\bbac pro\b"), 4),
    (re.compile(r"\bcap\b|\bbep\b|niveau 3|rncp\s*(?:niveau\s*)?3"), 3),
)
_LEVEL_LABELS = {3: "CAP / BEP", 4: "Bac", 5: "Bac+2 (BTS / BUT 2e année)", 6: "Bac+3", 7: "Bac+5", 8: "Bac+8"}
_DMY = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")
_YMD = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_FR_MONTHS = ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre",
              "novembre", "decembre")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:er)?\s+(" + "|".join(_FR_MONTHS) + r")(?:\s+(\d{4}))?")


def is_relayed(job: JobOffer) -> bool:
    return job.raw.get("relayed_by_school") is True


def school_bonus(job: JobOffer) -> float:
    if not is_relayed(job):
        return 0.0
    try:
        return max(0.0, float(job.raw.get("school_bonus", DEFAULT_BONUS)))
    except (TypeError, ValueError):
        return DEFAULT_BONUS


# --------------------------------------------------------------------------- level


def target_levels(text: str) -> set[int]:
    """RNCP levels (3-8) named in a free-text target level ('Bac+3 à Bac+5' → {6, 7})."""
    folded = norm(text or "")
    return {level for rx, level in _LEVELS if rx.search(folded)}


def candidate_level(profile: Profile) -> int:
    raw = (profile.alternance.rncp_level if profile.alternance else "") or ""
    digits = re.findall(r"[3-8]", raw)
    return int(digits[0]) if digits else DEFAULT_CANDIDATE_LEVEL


def level_reason(job: JobOffer, profile: Profile) -> str | None:
    """Filter reason when the offer only targets levels well below the candidate's programme."""
    levels = target_levels(str(job.raw.get("target_level") or ""))
    wanted = candidate_level(profile)
    if not levels or max(levels) >= wanted - 1:
        return None
    shown = str(job.raw.get("target_level") or _LEVEL_LABELS[max(levels)])
    return (f"Écartée : l'offre vise le niveau « {shown} », incompatible avec la formation préparée "
            f"({_LEVEL_LABELS.get(wanted, f'RNCP {wanted}')})")


# --------------------------------------------------------------------------- deadline & board


def parse_deadline(text: str, today: date) -> date | None:
    folded = norm(text or "")
    try:
        if m := _YMD.search(folded):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := _DMY.search(folded):
            year = int(m.group(3))
            return date(year + 2000 if year < 100 else year, int(m.group(2)), int(m.group(1)))
        if m := _DAY_MONTH.search(folded):
            return date(int(m.group(3) or today.year), _FR_MONTHS.index(m.group(2)) + 1, int(m.group(1)))
    except ValueError:
        return None
    return None


def deadline_reason(job: JobOffer, now: datetime | None = None) -> str | None:
    text = str(job.raw.get("deadline") or "")
    today = clock.to_paris(now or clock.now()).date()
    when = parse_deadline(text, today)
    if when is None or when >= today:
        return None
    return f"Écartée : date limite de candidature dépassée ({text})"


def blocking_reason(job: JobOffer, profile: Profile) -> str | None:
    """Hard filters specific to school-relayed offers (None when the offer is fine)."""
    if not is_relayed(job):
        return None
    if job.raw.get("board_expired"):
        board = job.raw.get("board_source") or "le site d'emploi"
        return f"Écartée : l'offre liée sur {board} est expirée"
    return level_reason(job, profile) or deadline_reason(job)
