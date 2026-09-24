"""Source registry: build the enabled sources, the search queries, collect jobs, complete descriptions.

Sources are imported lazily (several are optional or written separately); a missing module or an
unavailable source is reported, never fatal. One failing source/query never stops the others.
"""

from __future__ import annotations

import importlib
import logging
import re
import weakref
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jobbot.config import LocationPref, Profile, Settings
from jobbot.context import AppContext
from jobbot.models import ContractType, JobOffer, JobStatus
from jobbot.sources.base import JobSource, SearchQuery, SourceUnavailable
from jobbot.sources.countries import is_foreign, location_country
from jobbot.textutil import normalize, normalize_company

log = logging.getLogger(__name__)

# source name (settings.search.sources / JobOffer.source) -> (module, class)
SOURCE_SPECS: dict[str, tuple[str, str]] = {
    "lba": ("jobbot.sources.lba", "LaBonneAlternanceSource"),
    "france_travail": ("jobbot.sources.france_travail", "FranceTravailSource"),
    "linkedin": ("jobbot.sources.linkedin", "LinkedInSource"),
    "hellowork": ("jobbot.sources.hellowork", "HelloWorkSource"),
    "indeed": ("jobbot.sources.jobspy_source", "JobSpySource"),
    "wttj": ("jobbot.sources.wttj", "WelcomeToTheJungleSource"),
    "free_work": ("jobbot.sources.free_work", "FreeWorkSource"),
    "apec": ("jobbot.sources.apec", "ApecSource"),
}
SECRET_SOURCES = frozenset({"lba", "france_travail"})  # constructors take ctx.secrets
ALIASES = {
    "jobspy": "indeed", "la_bonne_alternance": "lba", "labonnealternance": "lba", "francetravail": "france_travail",
    "pole_emploi": "france_travail", "welcome_to_the_jungle": "wttj", "freework": "free_work",
}
DETAILS_MIN_CHARS = 400
# Sources whose ``fetch_details`` completes a search card (Indeed via JobSpy already returns the full text).
DETAIL_SOURCES = frozenset({"linkedin", "hellowork", "lba", "wttj", "apec", "free_work", "france_travail"})
MAX_ERROR_CHARS = 300
# Politeness caps (queries per run) for scraped boards — docs/research: LinkedIn guest ≈ 9 keywords × 3 geos
# a day, HelloWork "a handful of queries, page 1 only", Indeed via JobSpy a few searches. APIs are uncapped.
# build_queries() is title-major, so the first queries cover every contract × location of the top titles.
SOURCE_QUERY_BUDGET = {"linkedin": 27, "hellowork": 6, "indeed": 10, "apec": 18, "wttj": 27}
# Sources able to search around a foreign location (Luxembourg track): LinkedIn/HelloWork geo ids, WTTJ
# coordinates, Free-Work country keys, France Travail ``paysContinent``, Indeed (JobSpy) ``country_indeed``.
FOREIGN_LOCATION_SOURCES = frozenset({"linkedin", "hellowork", "wttj", "free_work", "france_travail", "indeed"})
_CONTRACT_WORDS = frozenset({
    "alternance", "alternant", "alternante", "apprenti", "apprentie", "apprentissage", "cdi", "cdd",
    "stage", "h/f", "f/h",
})


class CollectReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    new: int = 0
    updated: int = 0
    per_source: dict[str, int] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- sources


def canonical_name(name: str) -> str:
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    return ALIASES.get(key, key)


def _instantiate(ctx: AppContext, name: str) -> JobSource:
    """Import the source module lazily and build the source (ImportError/KeyError propagate)."""
    module_name, class_name = SOURCE_SPECS[name]
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls(ctx.secrets) if name in SECRET_SOURCES else cls()


def _build(ctx: AppContext, names: list[str] | None) -> tuple[list[JobSource], dict[str, str]]:
    sources: list[JobSource] = []
    problems: dict[str, str] = {}
    requested = names if names is not None else ctx.settings.search.sources
    for name in dict.fromkeys(canonical_name(n) for n in requested):
        if name not in SOURCE_SPECS:
            problems[name] = "source inconnue"
            log.warning("Source inconnue ignorée : %s", name)
            continue
        try:
            source = _instantiate(ctx, name)
            ok, reason = source.available()
        except ImportError as exc:
            problems[name] = f"module indisponible ({exc})"
            log.warning("Source %s indisponible : module absent (%s)", name, exc)
            continue
        except Exception as exc:  # a broken third-party source must not stop the run
            problems[name] = f"initialisation impossible : {exc}"
            log.warning("Source %s : initialisation impossible (%s)", name, exc)
            continue
        if not ok:
            problems[name] = reason or "indisponible"
            log.info("Source %s désactivée : %s", name, problems[name])
            continue
        sources.append(source)
    return sources, problems


def build_sources(ctx: AppContext, names: list[str] | None = None) -> list[JobSource]:
    """Available sources among ``names`` (default: ``settings.search.sources``), in that order."""
    return _build(ctx, names)[0]


# --------------------------------------------------------------------------- queries


def _title_key(title: str) -> str:
    words = [w for w in normalize(title).split() if w not in _CONTRACT_WORDS]
    return re.sub(r"[^a-z0-9#+]", "", "".join(words))


def _search_titles(profile: Profile) -> list[str]:
    titles = [t.strip() for t in profile.target.job_titles if t and t.strip()]
    if not titles and profile.target.keywords:
        titles = [" ".join(profile.target.keywords[:3])]
    unique: dict[str, str] = {}
    for title in titles:
        unique.setdefault(_title_key(title) or normalize(title), title)
    return list(unique.values())


def _location_key(loc: LocationPref | None) -> str:
    return f"{normalize(loc.city)}|{loc.postal_code}|{loc.radius_km}|{location_country(loc)}" if loc else "-"


def _contract_sets(profile: Profile) -> list[list[ContractType]]:
    """One set per target contract ([] = any contract), then the contracts only some locations accept."""
    target = list(dict.fromkeys(profile.target.contract_types))
    extra = [c for loc in profile.target.locations for c in loc.contract_types or [] if c not in target]
    return ([[c] for c in target] or [[]]) + [[c] for c in dict.fromkeys(extra)]


def _location_allows(loc: LocationPref | None, contracts: list[ContractType], target: list[ContractType]) -> bool:
    """``LocationPref.contract_types`` replaces the target contracts for that location (Luxembourg → CDI only)."""
    if loc is None:
        return True
    if loc.contract_types:
        return bool(contracts) and contracts[0] in loc.contract_types
    return not contracts or contracts[0] in target


def build_queries(profile: Profile, settings: Settings) -> list[SearchQuery]:
    """job titles × locations × contract types (+ a national remote query per title when remote_ok).

    ``LocationPref.contract_types`` gives the contracts searched at that location (Luxembourg → CDI only);
    a contract listed there but not in the target is searched at that location only (never remote).
    """
    target = list(profile.target.contract_types)
    locations: list[LocationPref | None] = list(profile.target.locations) or [None]
    common: dict[str, Any] = {
        "max_results": settings.search.max_results_per_query, "max_age_days": settings.search.max_age_days,
    }
    queries: dict[tuple, SearchQuery] = {}
    for title in _search_titles(profile):
        for contracts in _contract_sets(profile):
            variants = [(loc, False) for loc in locations if _location_allows(loc, contracts, target)]
            if profile.target.remote_ok and (not contracts or contracts[0] in target):
                variants.append((None, True))
            for loc, remote in variants:
                key = (_title_key(title), _location_key(loc), tuple(contracts), remote)
                queries.setdefault(key, SearchQuery(
                    keywords=title, location=loc, contract_types=contracts, remote=remote, **common,
                ))
    return list(queries.values())


# --------------------------------------------------------------------------- collection


def _excluded(ctx: AppContext, job: JobOffer) -> bool:
    company_key = normalize_company(job.company)
    if company_key:
        padded = f" {company_key} "
        for excluded in ctx.profile.target.exclude_companies:
            key = normalize_company(excluded)
            if key and f" {key} " in padded:
                return True
    email = job.contact_email or ""
    email_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return ctx.repo.is_blacklisted(job.company, company_key, email, email_domain, job.company_domain or "")


def _store(ctx: AppContext, job: JobOffer) -> bool | None:
    """True = new, False = updated, None = skipped/failed."""
    try:
        if _excluded(ctx, job):
            log.debug("Offre %s ignorée (entreprise exclue ou blacklistée)", job.id)
            return None
        return ctx.repo.upsert_job(job)
    except Exception as exc:  # one bad item never aborts the batch
        log.warning("Offre %s (%s) non enregistrée : %s", job.id, job.source, exc)
        return None


def _supported(name: str, queries: list[SearchQuery]) -> list[SearchQuery]:
    """France-only sources skip queries on a foreign location (e.g. Luxembourg): they would search France."""
    if name in FOREIGN_LOCATION_SOURCES:
        return queries
    return [q for q in queries if not is_foreign(q.location)]


def _budgeted(name: str, queries: list[SearchQuery]) -> list[SearchQuery]:
    queries = _supported(name, queries)
    budget = SOURCE_QUERY_BUDGET.get(name)
    if budget is None or len(queries) <= budget:
        return queries
    log.info("Source %s : %d requêtes sur %d (limite de politesse)", name, budget, len(queries))
    return queries[:budget]


def _collect_source(ctx: AppContext, source: JobSource, queries: list[SearchQuery]) -> tuple[int, int, str | None]:
    new = updated = 0
    seen: set[str] = set()
    failures: list[str] = []
    for query in _budgeted(getattr(source, "name", ""), queries):
        try:
            for job in source.search(query):
                if job.id in seen:
                    continue
                seen.add(job.id)
                stored = _store(ctx, job)
                if stored is not None:
                    new, updated = new + int(stored), updated + int(not stored)
        except SourceUnavailable as exc:
            failures.append(str(exc))
            log.warning("Source %s indisponible : %s", source.name, exc)
            break
        except Exception as exc:
            failures.append(f"« {query.keywords} » : {exc}")
            log.warning("Source %s, requête « %s » en échec : %s", source.name, query.keywords, exc)
    if not failures:
        return new, updated, None
    summary = failures[-1] if len(failures) == 1 else f"{len(failures)} requêtes en échec, dernière : {failures[-1]}"
    return new, updated, summary[:MAX_ERROR_CHARS]


def collect_jobs(
    ctx: AppContext, sources: list[JobSource] | None = None, queries: list[SearchQuery] | None = None
) -> CollectReport:
    """Run every query on every source and upsert the offers (excluded/blacklisted companies skipped)."""
    errors: dict[str, str] = {}
    if sources is None:
        sources, errors = _build(ctx, None)
    queries = queries if queries is not None else build_queries(ctx.profile, ctx.settings)
    total_new = total_updated = 0
    per_source: dict[str, int] = {}
    for source in sources:
        name = getattr(source, "name", type(source).__name__)
        new, updated, error = _collect_source(ctx, source, queries)
        total_new, total_updated = total_new + new, total_updated + updated
        per_source[name] = per_source.get(name, 0) + new + updated
        if error:
            errors[name] = error
        log.info("Source %s : %d nouvelle(s) offre(s), %d mise(s) à jour", name, new, updated)
    log.info("Collecte terminée : %d nouvelles offres, %d mises à jour, %d erreur(s)",
             total_new, total_updated, len(errors))
    return CollectReport(new=total_new, updated=total_updated, per_source=per_source, errors=errors)


# --------------------------------------------------------------------------- details

_detail_cache: dict[str, Any] = {"ctx": None, "sources": {}}  # one context at a time (weak reference)


def _detail_source(ctx: AppContext, name: str) -> JobSource | None:
    ref = _detail_cache["ctx"]
    if ref is None or ref() is not ctx:
        _detail_cache.update(ctx=weakref.ref(ctx), sources={})
    sources: dict[str, JobSource | None] = _detail_cache["sources"]
    key = canonical_name(name)
    if key not in sources:
        try:
            sources[key] = _instantiate(ctx, key) if key in SOURCE_SPECS else None
        except Exception as exc:
            log.warning("Source %s indisponible pour compléter l'offre : %s", key, exc)
            sources[key] = None
    return sources[key]


def fetch_details_only(ctx: AppContext, job: JobOffer) -> JobOffer | None:
    """The owning source's full version of ``job``, never persisted (None when unavailable or failing).
    Used to read a board offer linked from elsewhere (e.g. a post of the school in Google Classroom)."""
    source = _detail_source(ctx, job.source)
    if source is None:
        return None
    try:
        return source.fetch_details(job)
    except Exception as exc:  # a board down must never break the caller
        log.warning("Offre %s (%s) illisible : %s", job.external_id, job.source, exc)
        return None


def ensure_details(ctx: AppContext, job: JobOffer) -> JobOffer:
    """Complete a short description through the owning source, persist and return the new job."""
    if len(job.description or "") >= DETAILS_MIN_CHARS:
        return job
    source = _detail_source(ctx, job.source)
    if source is None:
        return job
    try:
        fetched = source.fetch_details(job)
    except Exception as exc:
        log.warning("Impossible de compléter l'offre %s (%s) : %s", job.id, job.source, exc)
        return job
    if fetched == job:
        return job
    expired = fetched.status == JobStatus.EXPIRED and job.status != JobStatus.APPLIED
    merged = fetched.model_copy(update={
        "id": job.id, "found_at": job.found_at, "match_score": job.match_score,
        "match_reasons": job.match_reasons, "status": JobStatus.EXPIRED if expired else job.status,
    })
    try:
        ctx.repo.save_job(merged)
    except Exception as exc:
        log.warning("Offre %s complétée mais non enregistrée : %s", job.id, exc)
    return merged


def needs_details(job: JobOffer) -> bool:
    """Short description (search card) that the owning source can complete."""
    return len(job.description or "") < DETAILS_MIN_CHARS and canonical_name(job.source) in DETAIL_SOURCES


def complete_new_offers(ctx: AppContext) -> int:
    """Before scoring: full description of the NEW offers only known by a short card, most promising first
    (provisional score), at most ``settings.search.max_detail_fetches_per_run`` per run — the sources keep their
    own politeness delays. The others keep a provisional score. Returns the number of offers completed."""
    from jobbot.matching.match import match_jobs

    cap = ctx.settings.search.max_detail_fetches_per_run
    pending = [job for job in ctx.repo.list_jobs(JobStatus.NEW) if needs_details(job)]
    if not pending or cap <= 0:
        return 0
    scores = [score for score, _ in match_jobs(pending, ctx.profile)]
    ranked = [job for _, job in sorted(zip(scores, pending, strict=True), key=lambda pair: -pair[0])]
    if len(ranked) > cap:
        log.info("Annonces courtes : %d complétées sur %d (limite par exécution)", cap, len(ranked))
    completed = 0
    for job in ranked[:cap]:
        done = ensure_details(ctx, job)
        completed += int((done.description or "") != (job.description or "") or done.status != job.status)
    log.info("Annonces courtes complétées avant notation : %d", completed)
    return completed
