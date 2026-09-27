"""Company career boards (source ``company_ats``): the public job APIs of the ATS used by employers jobbot already
knows — companies of the database (careers page / website) and employers of stored offers whose apply link points
to Lever, Greenhouse, Recruitee, SmartRecruiters, Workable, Teamtailor or a WTTJ company page; a careers page
without a known ATS is read for schema.org ``JobPosting`` JSON-LD (and the ATS links it contains).

Boards are read once per run (first ``search`` call, ``ats_reader.BoardReader``), at most
``settings.search.max_ats_boards_per_run``, each one at most every 20 h (longer after failures, repository kv),
≤ 1 request per second per provider, within a time budget. Only developer jobs (sales / senior roles dropped) of
the searched countries are kept; each query then gets the offers near its place (or full remote for the remote
query). Apply is always EXTERNAL on the posting's own page (``apply_url``), never automated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from jobbot.http import make_client
from jobbot.matching.geo import geocode, is_place, lookup_known_place
from jobbot.models import ContractType, JobOffer, JobStatus
from jobbot.sources import ats_mapping as mapping
from jobbot.sources.ats_fetch import FETCH_ERRORS, AtsFetcher, BoardNotFound
from jobbot.sources.ats_reader import BoardReader
from jobbot.sources.ats_targets import discover_targets, is_public_page, select_targets
from jobbot.sources.base import SearchQuery
from jobbot.sources.countries import FRANCE, is_foreign, location_country
from jobbot.sources.jsonld import job_postings, posting_fields
from jobbot.sources.offer_utils import detect_contract, detect_remote, fold, is_dev_title, is_senior_title
from jobbot.sources.zones import in_query_zone, is_french_place, offer_country

log = logging.getLogger(__name__)

DEFAULT_MAX_BOARDS = 80
REFRESH_HOURS = 20.0
MAX_GEOCODES = 40  # French places looked up per run (Géoplateforme, cached in-process)

Geocoder = Callable[[str], tuple[float, float] | None]


class CompanyATSSource:
    """Read-only source over the public job APIs of company ATS boards."""

    name = "company_ats"

    def __init__(self, repo: Any = None, *, profile: Any = None, max_boards: int = DEFAULT_MAX_BOARDS,
                 client: httpx.Client | None = None, geocoder: Geocoder | None = None, wttj_source: Any = None,
                 refresh_hours: float = REFRESH_HOURS):
        self._repo = repo
        self._max_boards = max_boards
        self._client = client
        self._geocoder = geocoder
        self._wttj = wttj_source
        self._refresh_hours = refresh_hours
        self._countries = _profile_countries(profile)
        self._fetcher: AtsFetcher | None = None
        self._offers: list[JobOffer] | None = None
        self._yielded: set[str] = set()
        self._places: dict[str, tuple[float, float] | None] = {}

    @classmethod
    def from_context(cls, ctx: Any) -> CompanyATSSource:
        return cls(ctx.repo, profile=ctx.profile, max_boards=ctx.settings.search.max_ats_boards_per_run)

    def available(self) -> tuple[bool, str]:
        return True, ""

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        """Offers of the query's zone not yet given to an earlier query of this run (the registry stores each
        once), at most ``max_results``."""
        query = self._placed(query)
        yielded = 0
        for job in self._load(query.max_age_days):
            if job.id in self._yielded or not _contract_ok(job, query):
                continue
            if in_query_zone(job, query, self._locate):
                self._yielded.add(job.id)
                yield job
                yielded += 1
                if yielded >= query.max_results:
                    return

    def fetch_details(self, job: JobOffer) -> JobOffer:
        """Text of SmartRecruiters / Workable postings (listed without it), JSON-LD offer pages; 404 → EXPIRED."""
        provider, slug, posting_id = (job.raw.get(k) for k in ("ats", "ats_slug", "posting_id"))
        if not (slug and posting_id) or provider not in ("smartrecruiters", "workable", "jsonld"):
            return job
        try:
            update = self._detail_update(job, provider, slug, posting_id)
        except BoardNotFound:
            return job.model_copy(update={"status": JobStatus.EXPIRED})
        except FETCH_ERRORS as exc:
            log.info("Page carrières : détail indisponible pour %s (%s)", job.url, exc)
            return job
        if update is None:
            return job.model_copy(update={"status": JobStatus.EXPIRED})
        return _merge_detail(job, update)

    # ------------------------------------------------------------------ boards

    def _http(self) -> AtsFetcher:
        if self._fetcher is None:
            client = self._client or make_client(timeout=20.0)
            self._client = client
            if self._wttj is None:
                from jobbot.sources.wttj import WelcomeToTheJungleSource

                self._wttj = WelcomeToTheJungleSource(client=client)
            self._fetcher = AtsFetcher(client, wttj_source=self._wttj, countries=self._countries)
        return self._fetcher

    def _load(self, max_age_days: int) -> list[JobOffer]:
        """Reads the due boards once per run; what was read is kept even if a later step fails."""
        if self._offers is not None:
            return self._offers
        self._offers = []
        if self._repo is None:
            return self._offers
        try:
            targets = select_targets(self._repo, discover_targets(self._repo), limit=self._max_boards,
                                     refresh_hours=self._refresh_hours)
        except Exception as exc:  # the database must never break the collect step
            log.warning("Pages carrières : liste des entreprises illisible (%s)", exc)
            return self._offers
        kept: dict[str, JobOffer] = {}
        reader = BoardReader(self._repo, self._http, refresh_hours=self._refresh_hours)
        try:
            for _target, jobs in reader.read_all(targets, max_age_days):
                for job in filter(self._keep, jobs):
                    kept.setdefault(job.id, job)
        finally:
            self._offers = list(kept.values())
        log.info("Pages carrières : %d tableau(x) à lire, %d offre(s) de développement gardée(s)",
                 len(targets), len(self._offers))
        return self._offers

    def _keep(self, job: JobOffer) -> bool:
        """Developer job, not a senior / sales role, in a searched country (or country unknown)."""
        code = offer_country(job)
        in_country = code is None or code in self._countries
        return in_country and is_dev_title(job.title) and not is_senior_title(job.title)

    def _locate(self, job: JobOffer) -> tuple[float, float] | None:
        """Coordinates of a French offer place not known offline (Géoplateforme, capped per run)."""
        if not is_french_place(job) or not is_place(job.location):
            return None
        return self._geocode(" ".join(p for p in (job.postal_code, job.location) if p))

    def _geocode(self, place: str) -> tuple[float, float] | None:
        if place not in self._places:
            if len(self._places) >= MAX_GEOCODES:
                return None
            self._places[place] = (self._geocoder or _geocode)(place)
        return self._places[place]

    def _placed(self, query: SearchQuery) -> SearchQuery:
        """A French search area without coordinates nor a known city gets geocoded coordinates (else nothing in
        it could be matched by distance)."""
        loc = query.location
        if query.remote or loc is None or is_foreign(loc) or (loc.latitude is not None and loc.longitude is not None):
            return query
        if lookup_known_place(loc.city) or not loc.city:
            return query
        coords = self._geocode(" ".join(p for p in (loc.postal_code, loc.city) if p))
        if coords is None:
            return query
        located = loc.model_copy(update={"latitude": coords[0], "longitude": coords[1]})
        return query.model_copy(update={"location": located})

    def _detail_update(self, job: JobOffer, provider: str, slug: str, posting_id: str) -> dict[str, Any] | None:
        fetcher = self._http()
        if provider == "smartrecruiters":
            detail = fetcher.smartrecruiters_detail(slug, posting_id)
            return mapping.smartrecruiters_detail(detail) if detail is not None else None
        if provider == "workable":
            detail = fetcher.workable_detail(slug, posting_id)
            return mapping.workable_detail(detail) if detail is not None else None
        if not is_public_page(job.url):  # an offer URL published by a third-party page: public hosts only
            return {}
        return _jsonld_detail(fetcher.page_html(job.url), job.title)


# --------------------------------------------------------------------------- helpers


def _profile_countries(profile: Any) -> frozenset[str]:
    locations = getattr(getattr(profile, "target", None), "locations", None) or []
    return frozenset({FRANCE, *(location_country(loc) for loc in locations)})


def _contract_ok(job: JobOffer, query: SearchQuery) -> bool:
    return not query.contract_types or job.contract_type in (ContractType.OTHER, *query.contract_types)


def _geocode(place: str) -> tuple[float, float] | None:
    return geocode(place)


def _jsonld_detail(page: str, title: str) -> dict[str, Any]:
    wanted = fold(title)
    for raw in job_postings(page):
        fields = posting_fields(raw)
        if fields is not None and fold(fields.title) == wanted:
            return {"description": fields.description}
    return {}


def _merge_detail(job: JobOffer, update: dict[str, Any]) -> JobOffer:
    description = update.get("description") or ""
    text = description if len(description) > len(job.description or "") else job.description
    fields: dict[str, Any] = {
        "description": text, "apply_url": update.get("apply_url") or job.apply_url,
        "url": update.get("url") or job.url,
        "raw": {**job.raw, "needs_details": False, "detail_fetched": True,
                **({"language": update["language"]} if update.get("language") else {})},
    }
    if job.contract_type == ContractType.OTHER:
        fields["contract_type"] = detect_contract(job.title, text, job.raw.get("contract_label"))
    if job.remote is None:
        fields["remote"] = detect_remote(job.title, text)
    return job.model_copy(update=fields)
