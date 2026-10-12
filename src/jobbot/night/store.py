"""Harvested candidates → database: one company per SIREN (or per normalised name + country without SIREN),
merged with what is stored, through the relevance gate (irrelevant ones are never stored; stored ones still
waiting are set aside), never touching status / contacts / domain found by enrichment.

Hiring signals (``raw["hiring_signal"] = [{title, source, date}]``) are merged, not replaced. A company located
at the establishment near its offers (``raw["local_establishment"]``, :mod:`jobbot.night.hiring`) keeps that place
(address, city, postal code, coordinates, SIRET) over a head office or another establishment; a real local
establishment wins over the offer's place given to a company without local agency (``raw["agency"] =
"absente"``, its notes ``raw["agency_notes"]``).
"""

from __future__ import annotations

import logging

from jobbot.context import AppContext
from jobbot.models import Company
from jobbot.night.budget import Deadline
from jobbot.night.models import HarvestResult
from jobbot.outreach.companies import merge_company as _merge_registry
from jobbot.outreach.priority import AGENCY_NOTES_KEY
from jobbot.outreach.relevance import IRRELEVANT_STATUS, REASON_KEY, is_relevant_employer, set_aside_copy
from jobbot.textutil import normalize, normalize_company

log = logging.getLogger(__name__)

SIGNAL_KEY = "hiring_signal"
LOCAL_KEY = "local_establishment"  # True: the place is the establishment near the employer's offers
AGENCY_KEY, AGENCY_ABSENT = "agency", "absente"  # no local agency: head office, working at the offer's place
MAX_SIGNALS = 10
PLACE_FIELDS = ("address", "city", "postal_code", "latitude", "longitude")
PLACE_RAW = ("siret", "distance_km", AGENCY_KEY, AGENCY_NOTES_KEY)


def _valid_signals(*lists: object) -> list[dict]:
    return [s for signals in lists if isinstance(signals, list) for s in signals
            if isinstance(s, dict) and s.get("title")]


def merge_signals(*lists: object) -> list[dict]:
    """Union of hiring signals (same title + source = one), most recent first, at most ``MAX_SIGNALS``."""
    merged: dict[tuple[str, str], dict] = {}
    for signal in _valid_signals(*lists):
        key = (normalize(str(signal["title"])), str(signal.get("source") or ""))
        if key not in merged or str(signal.get("date") or "") > str(merged[key].get("date") or ""):
            merged[key] = dict(signal)
    return sorted(merged.values(), key=lambda s: str(s.get("date") or ""), reverse=True)[:MAX_SIGNALS]


def _local_source(existing: Company, fresh: Company) -> Company | None:
    """The version located at the establishment near the offers (the fresh one first; a real establishment before
    the offer's place of a company without local agency), None when neither is."""
    located = [c for c in (fresh, existing) if c.raw.get(LOCAL_KEY)]
    real = [c for c in located if c.raw.get(AGENCY_KEY) != AGENCY_ABSENT]
    return (real or located or [None])[0]


def _with_local_place(merged: Company, source: Company | None) -> Company:
    if source is None:
        return merged
    raw = {k: v for k, v in merged.raw.items() if k not in PLACE_RAW}
    raw.update({k: source.raw[k] for k in PLACE_RAW if k in source.raw})
    update = {f: getattr(source, f) for f in PLACE_FIELDS}
    return merged.model_copy(update={**update, "raw": {**raw, LOCAL_KEY: True}})


SITE_KEY = "site_tranche"  # set by jobbot.night.large: the candidate carries a big local site of its own


def _big_site_place(merged: Company, existing: Company, fresh: Company) -> Company:
    """A « grandes entreprises » candidate brings its local site: it becomes the place only when the stored one is
    not as good (no place known, or farther from the search areas) and is neither an LBA recruiter's establishment
    nor the one near the offers; otherwise the stored place keeps its own siret / distance (never mixed)."""
    if SITE_KEY not in fresh.raw:
        return merged
    def distance(company: Company) -> float:
        value = company.raw.get("distance_km")
        return float(value) if isinstance(value, (int, float)) else float("inf")

    keep = (existing.source == "lba_recruteur" or existing.raw.get(LOCAL_KEY)
            or (existing.latitude is not None and distance(existing) <= distance(fresh)))
    raw = dict(merged.raw)
    if keep:
        for key in (*PLACE_RAW, SITE_KEY):
            raw.pop(key, None)
            if key in existing.raw:
                raw[key] = existing.raw[key]
        return merged.model_copy(update={"raw": raw})
    raw.update({k: fresh.raw[k] for k in (*PLACE_RAW, SITE_KEY) if k in fresh.raw})
    return merged.model_copy(update={**{f: getattr(fresh, f) for f in PLACE_FIELDS}, "raw": raw})


def merge_company(existing: Company, fresh: Company) -> Company:
    """Registry refresh (:func:`jobbot.outreach.companies.merge_company`) + hiring signals + known website + the
    establishment near the offers + the big local site of a « grandes entreprises » candidate."""
    merged = _big_site_place(_merge_registry(existing, fresh), existing, fresh)
    merged = _with_local_place(merged, _local_source(existing, fresh))
    raw = dict(merged.raw)
    signals = merge_signals(existing.raw.get(SIGNAL_KEY), fresh.raw.get(SIGNAL_KEY))
    if signals:
        raw[SIGNAL_KEY] = signals
    update: dict = {"raw": raw}
    if not merged.website and fresh.website:
        update["website"] = fresh.website
    if existing.source not in ("recherche_entreprises", "lba_recruteur") and fresh.source != existing.source:
        update["source"] = fresh.source
    return merged.model_copy(update=update)


def combine(candidates: list[Company]) -> list[Company]:
    """One candidate per id (the same company from several sources / locations), details merged."""
    merged: dict[str, Company] = {}
    for company in candidates:
        merged[company.id] = merge_company(merged[company.id], company) if company.id in merged else company
    return list(merged.values())


def _rejected(ctx: AppContext, company: Company) -> str | None:
    if ctx.repo.is_blacklisted(company.name, company.siren or "", company.domain or ""):
        return "liste noire"
    if normalize_company(company.name) in {normalize_company(x) for x in ctx.profile.target.exclude_companies}:
        return "entreprise exclue"
    return None


def _reopened(company: Company) -> Company:
    """A company set aside before, relevant now (e.g. it published a developer offer): back in the queue."""
    raw = {k: v for k, v in company.raw.items() if k != REASON_KEY}
    return company.model_copy(update={"status": "new", "raw": raw})


def _store_new(ctx: AppContext, company: Company) -> str:
    if not is_relevant_employer(company, ctx.profile)[0]:
        return "irrelevant"
    if _rejected(ctx, company):
        return "rejected"
    ctx.repo.upsert_company(company)
    return "new"


def _store_one(ctx: AppContext, company: Company) -> str:
    """'new' | 'updated' | 'same' | 'irrelevant' | 'rejected'. A stored company is merged atomically
    (``Repository.update_company``): a status the day sets meanwhile (contacted…) is never overwritten."""
    if ctx.repo.get_company(company.id) is None:
        return _store_new(ctx, company)
    outcome = ["same"]

    def change(current: Company) -> Company | None:
        merged = merge_company(current, company)
        relevant, reason = is_relevant_employer(merged, ctx.profile)
        if not relevant:
            outcome[0] = "irrelevant"
            return set_aside_copy(current, reason)
        if _rejected(ctx, merged):
            outcome[0] = "rejected"
            return None
        merged = _reopened(merged) if current.status == IRRELEVANT_STATUS else merged
        outcome[0] = "same" if merged == current else "updated"
        return merged

    ctx.repo.update_company(company.id, change)
    return outcome[0]


def store_candidates(ctx: AppContext, candidates: list[Company], deadline: Deadline) -> HarvestResult:
    """Store the combined candidates (best outreach score first); stops at the deadline."""
    companies = sorted(combine(candidates), key=lambda c: -int(c.raw.get("outreach_score", 0) or 0))
    counts = {"new": 0, "updated": 0, "same": 0, "irrelevant": 0, "rejected": 0}
    errors: list[str] = []
    stopped = False
    for company in companies:
        if deadline.expired():
            stopped = True
            break
        try:
            counts[_store_one(ctx, company)] += 1
        except Exception as exc:  # one company never stops the harvest
            log.exception("Enregistrement de %s impossible", company.name)
            errors.append(f"{company.name} : {exc}")
    relevant = counts["new"] + counts["updated"] + counts["same"] + counts["rejected"]
    return HarvestResult(found=len(companies), new=counts["new"], relevant=relevant,
                         irrelevant=counts["irrelevant"], stopped=stopped, errors=errors)
