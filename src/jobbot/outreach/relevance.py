"""Is a company a plausible employer for the candidate's (software) apprenticeship? One gate for spontaneous targets.

Every source goes through it — ``recherche-entreprises`` (NAF code, INSEE tranche, IDCC) and « recruteurs LBA »
(site: NAF *label* ``activity_sector`` + the jobs LBA expects them to hire for, ``rome_labels``; official API: NAF
code + ``size``) — at discovery (irrelevant ones are not stored), at enrichment and right before any application
(e-mail campaign, LBA relay: companies still waiting are set aside with status ``irrelevant``). La Bonne Alternance
allows 3 applications per company, ever: a wasted one is lost for good.

Relevant when (first match wins):

1. its NAF code is in ``profile.target.naf_codes`` or is a digital one (62, 63.1, 58.2, 61, 72.19Z);
2. no NAF code but a NAF label naming a digital activity (« Programmation informatique », « Édition de logiciels »…);
3. large organisation (≥ 250 employees, or category ETI / GE): an IT department is likely (SNCF, banks,
   hospitals…) — except field-work sectors (farming, extraction, construction, catering, temp work, cleaning), whose
   local establishments never hire a developer;
4. consulting / engineering (70.22Z, 71.12B) under the Syntec agreement (IDCC 1486), or 70.22Z with ≥ 50 employees
   (IT consulting is often registered there);
5. La Bonne Alternance lists a developer job among those it expects the company to hire for;
6. its name says IT (« … Informatique », « … Software », « … IT »), or — sector unknown — its name or description does.

Otherwise irrelevant, with a French reason such as « secteur hors numérique (NAF 42.11Z, 10 à 19 salariés) ».
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from jobbot.config import Profile
from jobbot.db import Repository
from jobbot.models import Company
from jobbot.outreach._registry import SYNTEC_IDCC, TRANCHE_LABELS
from jobbot.textutil import strip_accents, truncate

__all__ = [
    "IRRELEVANT_STATUS", "OFF_TARGET", "OPEN_STATUSES", "REASON_KEY", "headcount_min", "is_relevant_employer",
    "report_lines", "set_aside", "split_relevant", "sweep_review_queue",
]

log = logging.getLogger(__name__)

IRRELEVANT_STATUS = "irrelevant"
REASON_KEY = "irrelevant_reason"
OFF_TARGET = "hors cible"  # marker of every refusal message (apply results, report lines)
OPEN_STATUSES = frozenset({"new", "enriched", "no_email", "pending_review", "approved"})  # never contacted yet
LARGE_HEADCOUNT = 250
CONSULTING_HEADCOUNT = 50
MAX_REPORTED = 10  # report lines naming set-aside companies (the rest is counted)
DIGITAL_NAF_PREFIXES = ("62", "631", "582", "61", "7219")  # without dots: 62.xx, 63.1x, 58.2x, 61.xx, 72.19Z
CONSULTING_NAF = frozenset({"7022Z", "7112B"})
FIELD_DIVISIONS = frozenset({"01", "02", "03", "05", "06", "07", "08", "09", "41", "42", "43", "56", "78", "81"})
LARGE_CATEGORIES = frozenset({"ETI", "GE"})
TRANCHE_MIN = {
    "00": 0, "01": 1, "02": 3, "03": 6, "11": 10, "12": 20, "21": 50, "22": 100, "31": 200, "32": 250, "41": 500,
    "42": 1000, "51": 2000, "52": 5000, "53": 10000,
}

_NUMBER = re.compile(r"\d{1,3}(?:[   ]\d{3})+|\d+")  # "1 000 à 1 999 salariés", "250-499"
_DIGITAL_LABEL = re.compile(
    r"informatique|edition de (?:logiciels|jeux electroniques)|traitement de donnees|portails internet"
    r"|telecommunication|recherche-developpement en autres sciences physiques"
)
_DEV_JOB = re.compile(
    r"developpeu\w* (?:web|informatique|logiciel|full|front|back|mobile|d'applications?|java|python|php)"
    r"|devops|programmeu|informaticien|data engineer"
    r"|(?:ingenieur|concepteur|analyste|chef de projet)\w*[^,;]{0,50}(?:informatique|logiciel|systemes d'information)"
)
_IT_WORDS = re.compile(
    r"informati|logiciel|software|\bdigital|(?<!commande )numerique|\bweb\b|\bdata\b|\besn\b|\bssii\b|cyber"
    r"|\bcloud\b|\bdevops\b|systemes d'information|conseil en systemes"
    r"|developpement (?:web|logiciel|informatique|d'applications?)"
)
_IT_LOOSE = re.compile(r"tech\b|dev\b|\binternet\b|telecom|applications? (?:web|mobiles?)|intelligence artificielle")
_IT_ACRONYM = re.compile(r"\bIT\b")  # case-sensitive, on the raw name only


# --------------------------------------------------------------------------- facts


def _fold(text: str | None) -> str:
    return strip_accents(text or "").lower().replace("’", "'")


def naf_key(code: str | None) -> str:
    """'62.01Z' → '6201Z' (dots, spaces and case ignored)."""
    return re.sub(r"[^0-9A-Z]", "", (code or "").upper())


def headcount_min(company: Company) -> int | None:
    """Lower bound of the headcount: INSEE tranche code first, else the first number of ``headcount_range``."""
    code = str(company.raw.get("tranche_effectif_salarie") or "")
    if code in TRANCHE_MIN:
        return TRANCHE_MIN[code]
    match = _NUMBER.search(company.headcount_range or "")
    return int(re.sub(r"\D", "", match.group(0))) if match else None


def _size_text(company: Company, size: int | None) -> str:
    text = (company.headcount_range or "").strip()
    if not text:
        text = TRANCHE_LABELS.get(str(company.raw.get("tranche_effectif_salarie") or ""), "")
    if not text or size is None:
        return "effectif inconnu"
    return text if "salari" in text else f"{text} salariés"


def _listed(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value or [] if v] if isinstance(value, (list, tuple)) else []


# --------------------------------------------------------------------------- rules


def _targeted(key: str, profile: Profile) -> bool:
    """NAF chosen in the profile (an entry shorter than a full code, e.g. '62', is a prefix)."""
    wanted = [naf_key(c) for c in profile.target.naf_codes]
    return any(key == w or (len(w) < 5 and key.startswith(w)) for w in wanted if w)


def _digital(key: str, label: str) -> bool:
    if key:
        return key.startswith(DIGITAL_NAF_PREFIXES)
    return bool(_DIGITAL_LABEL.search(_fold(label)))


def _large(company: Company, size: int | None) -> bool:
    return (size is not None and size >= LARGE_HEADCOUNT) or company.raw.get("categorie_entreprise") in LARGE_CATEGORIES


def _consulting(company: Company, key: str, size: int | None) -> bool:
    if key not in CONSULTING_NAF:
        return False
    syntec = SYNTEC_IDCC in _listed(company.raw.get("idcc"))
    return syntec or (key == "7022Z" and size is not None and size >= CONSULTING_HEADCOUNT)


def _hires_developers(company: Company) -> bool:
    """LBA lists a developer job among those it expects the company to hire for (site ``rome_labels``/``keywords``)."""
    labels = _listed(company.raw.get("rome_labels")) + _listed(company.raw.get("keywords"))
    return any(_DEV_JOB.search(_fold(label)) for label in labels)


def _says_it(company: Company, *, sector_known: bool) -> bool:
    """IT name (always); IT name or description with looser words when the sector is unknown."""
    name = company.name or ""
    if _IT_ACRONYM.search(name) or _IT_WORDS.search(_fold(name)):
        return True
    if sector_known:
        return False
    text = _fold(f"{name}\n{company.description or ''}")
    return bool(_IT_WORDS.search(text) or _IT_LOOSE.search(text))


def is_relevant_employer(company: Company, profile: Profile) -> tuple[bool, str]:
    """(relevant?, French reason) — see the module docstring for the rules."""
    code, label = (company.naf_code or "").strip(), (company.naf_label or "").strip()
    key = naf_key(code)
    sector = f"NAF {code}" if code else truncate(label, 60)
    if key and _targeted(key, profile):
        return True, f"NAF ciblé ({code})"
    if _digital(key, label):
        return True, f"secteur numérique ({sector})"
    size = headcount_min(company)
    field = key[:2] in FIELD_DIVISIONS
    if _large(company, size) and not field:
        return True, f"grande structure ({_size_text(company, size)}) : service informatique probable"
    if _consulting(company, key, size):
        return True, f"conseil / ingénierie ({sector}, {_size_text(company, size)})"
    if _hires_developers(company):
        return True, "La Bonne Alternance y prévoit des recrutements en développement"
    if not field and _says_it(company, sector_known=bool(key or label)):
        return True, "nom ou activité tournés vers le numérique"
    if key or label:
        return False, f"secteur hors numérique ({sector}, {_size_text(company, size)})"
    return False, "secteur inconnu et nom sans rapport avec le numérique"


# --------------------------------------------------------------------------- bookkeeping


def set_aside(repo: Repository, company: Company, reason: str) -> Company:
    """Status ``irrelevant`` (kept for the record, never contacted) for a company still waiting; others unchanged."""
    if company.status not in OPEN_STATUSES:
        return company
    updated = company.model_copy(update={
        "status": IRRELEVANT_STATUS, "raw": {**company.raw, REASON_KEY: truncate(reason, 300)},
    })
    try:
        repo.upsert_company(updated)
    except Exception:  # the company is skipped anyway; it will be set aside on a later run
        log.exception("Entreprise hors cible %s non marquée", company.name)
        return company
    return updated


def split_relevant(
    repo: Repository, profile: Profile, companies: Iterable[Company]
) -> tuple[list[Company], list[tuple[Company, str]]]:
    """(relevant companies, [(irrelevant company, reason)]); irrelevant ones still waiting are set aside."""
    relevant: list[Company] = []
    off: list[tuple[Company, str]] = []
    for company in companies:
        ok, reason = is_relevant_employer(company, profile)
        if ok:
            relevant.append(company)
            continue
        log.info("Entreprise %s %s : %s", company.name, OFF_TARGET, reason)
        off.append((set_aside(repo, company, reason), reason))
    return relevant, off


def sweep_review_queue(repo: Repository, profile: Profile) -> list[tuple[Company, str]]:
    """Set aside the companies waiting for validation (``pending_review``) that are outside the target: they would
    only waste a review, and fill the LBA validation queue."""
    return split_relevant(repo, profile, repo.list_companies("pending_review"))[1]


def report_lines(off: list[tuple[Company, str]], limit: int = MAX_REPORTED) -> list[str]:
    """One report line per set-aside company (the first ``limit``), then a count of the others."""
    lines = [f"{company.name} : {OFF_TARGET}, écartée ({reason})" for company, reason in off[:limit]]
    if len(off) > limit:
        lines.append(f"… et {len(off) - limit} autre(s) entreprise(s) {OFF_TARGET} écartée(s)")
    return lines
