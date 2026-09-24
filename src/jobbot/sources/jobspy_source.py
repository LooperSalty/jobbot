"""Indeed (read-only) through the optional ``python-jobspy`` library, always run in a child process.

Why a child process:
- python-jobspy pins numpy/pandas versions (numpy 1.26.3, pandas < 3) that conflict with jobbot's venv, so it
  can live in its own interpreter: ``JOBSPY_PYTHON`` (env var) or ``<project>/.venv-jobspy``;
- the Indeed scraper can hang on page 2 (JobSpy issue #385): a hard timeout kills the child.

Indeed's ToS forbid bots in the apply process: offers are always ``ApplyMethod.EXTERNAL``.
A foreign search location (Luxembourg track) is searched on that country's Indeed only
(``country_indeed="luxembourg"``, around the place); French/national queries use ``countries``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from jobbot import clock
from jobbot.config import LocationPref
from jobbot.context import find_root
from jobbot.http import HostThrottle
from jobbot.models import ApplyMethod, ContractType, JobOffer
from jobbot.sources.base import SearchQuery, SourceUnavailable
from jobbot.sources.countries import INDEED_COUNTRIES, is_foreign, location_country
from jobbot.textutil import extract_emails, stable_id

log = logging.getLogger(__name__)

ENV_PYTHON = "JOBSPY_PYTHON"
LOCAL_VENV = ".venv-jobspy"
MIN_INTERVAL_S = 6.0  # between two Indeed scrapes
TIMEOUT_S = 120
MAX_RESULTS = 100
KM_PER_MILE = 1.609344  # JobSpy sends Indeed a radius in MILES
_THROTTLE_KEY = "https://fr.indeed.com/"
_INSTALL_HINT = (
    "python-jobspy n'est pas installé (optionnel, il impose numpy 1.26 / pandas 2 incompatibles avec le venv "
    "principal). Pour activer Indeed : uv venv .venv-jobspy --python 3.12 puis "
    "uv pip install --python .venv-jobspy\\Scripts\\python.exe python-jobspy (ou définis JOBSPY_PYTHON)."
)
# Runs in the JobSpy interpreter: kwargs as JSON on stdin, rows as JSON into the file given as argv[1].
_WORKER = r"""
import json, sys
kwargs = json.loads(sys.stdin.read())
from jobspy import scrape_jobs
frame = scrape_jobs(**kwargs)
payload = frame.to_json(orient="records", date_format="iso", force_ascii=False) if len(frame) else "[]"
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(payload)
"""
_PERIODS = {"yearly": "an", "monthly": "mois", "weekly": "semaine", "daily": "jour", "hourly": "heure"}
_ALT_WORD = re.compile(r"\b(alternance|alternant(e|s|es)?|apprenti(e|s|es)?|apprentissage)\b", re.I)
_TITLE_CONTRACTS = (
    (ContractType.ALTERNANCE, re.compile(r"\b(alternance|alternant(e|s|es)?|apprenti(e|s|es)?|en apprentissage)\b",
                                         re.I)),
    (ContractType.ALTERNANCE, re.compile(r"\bcontrat (pro|de professionnalisation)\b", re.I)),
    (ContractType.STAGE, re.compile(r"\b(stage|stagiaire|internship|intern)\b", re.I)),
    (ContractType.FREELANCE, re.compile(r"\b(freelance|free-lance|ind[ée]pendant)\b", re.I)),
    (ContractType.INTERIM, re.compile(r"\bint[ée]rim(aire)?\b", re.I)),
    (ContractType.CDD, re.compile(r"\bcdd\b", re.I)),
    (ContractType.CDI, re.compile(r"\bcdi\b", re.I)),
)
_DESCRIPTION_CONTRACTS = (  # stricter: "apprentissage automatique" is machine learning, not a contract
    (ContractType.ALTERNANCE, re.compile(
        r"contrat d.apprentissage|contrat de professionnalisation|\ben alternance\b|\balternant(e|s|es)?\b"
        r"|\bapprenti(e|s|es)?\b", re.I)),
    (ContractType.CDD, re.compile(r"\bcdd\b", re.I)),
    (ContractType.CDI, re.compile(r"\bcdi\b", re.I)),
    (ContractType.STAGE, re.compile(r"\b(stage|stagiaire)\b", re.I)),
    (ContractType.INTERIM, re.compile(r"\bint[ée]rim(aire)?\b", re.I)),
)
_NOT_DEV_TITLE = re.compile(
    r"business dev|d[ée]velopp\w* (commercial|foncier|d'affaires|immobilier)|charg[ée]e? de d[ée]veloppement", re.I
)
_PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError)


class JobSpyError(RuntimeError):
    """The JobSpy child process failed (timeout, crash, unreadable output)."""


class JobSpySource:
    """Indeed search via python-jobspy (optional dependency, separate interpreter allowed)."""

    name = "indeed"

    def __init__(self, countries: tuple[str, ...] = ("france",)):
        self.countries = tuple(c.strip().lower() for c in countries if c and c.strip()) or ("france",)
        self._python = _resolve_python()
        self._status: tuple[bool, str] | None = None
        self._throttle = HostThrottle(MIN_INTERVAL_S)
        self._done: set[str] = set()  # identical scrapes are not repeated within a run

    def available(self) -> tuple[bool, str]:
        if self._status is None:
            self._status = _check_python(self._python)
        return self._status

    def search(self, query: SearchQuery) -> Iterator[JobOffer]:
        ok, reason = self.available()
        if not ok:
            raise SourceUnavailable(reason)
        found_at, seen, errors, attempts = clock.now(), set(), [], 0
        for country in self._countries_for(query):
            kwargs = self._next_kwargs(query, country)
            if kwargs is None:
                continue
            attempts += 1
            for job in _jobs_from_rows(self._safe_scrape(kwargs, country, errors), query, found_at):
                if job.external_id not in seen:
                    seen.add(job.external_id)
                    yield job
                if len(seen) >= query.max_results:
                    return
        if attempts and len(errors) == attempts:
            raise SourceUnavailable("Indeed (JobSpy) indisponible — " + " ; ".join(errors))

    def fetch_details(self, job: JobOffer) -> JobOffer:
        return job  # JobSpy already returns the full description (markdown)

    def _countries_for(self, query: SearchQuery) -> tuple[str, ...]:
        """A foreign place is searched on its own country's Indeed only (never on Indeed France)."""
        if query.remote or not is_foreign(query.location):
            return self.countries
        code = location_country(query.location)
        if code not in INDEED_COUNTRIES:
            log.info("Indeed : pays %s non pris en charge par JobSpy, requête « %s » ignorée", code, query.keywords)
            return ()
        return (INDEED_COUNTRIES[code],)

    def _safe_scrape(self, kwargs: dict, country: str, errors: list[str]) -> list[dict]:
        try:
            return self._scrape(kwargs)
        except JobSpyError as exc:
            log.warning("Indeed (%s) : échec de la recherche « %s » : %s", country, kwargs["search_term"], exc)
            errors.append(f"{country} : {exc}")
            return []

    def _next_kwargs(self, query: SearchQuery, country: str) -> dict | None:
        kwargs = _build_scrape_kwargs(query, country)
        if kwargs is None:
            return None
        signature = json.dumps(kwargs, sort_keys=True, ensure_ascii=False)
        if signature in self._done:
            log.debug("Indeed : recherche déjà faite pendant cette exécution (%s)", signature)
            return None
        self._done.add(signature)
        return kwargs

    def _scrape(self, kwargs: dict) -> list[dict]:
        self._throttle.wait(_THROTTLE_KEY)
        return _run_worker(self._python, kwargs, TIMEOUT_S)


# ---------------------------------------------------------------------- interpreter & worker


def _resolve_python() -> str:
    configured = os.environ.get(ENV_PYTHON, "").strip()
    if configured:
        if Path(configured).exists():
            return configured
        log.warning("%s pointe vers %s, introuvable : Python courant utilisé", ENV_PYTHON, configured)
    return _default_python()


def _default_python() -> str:
    venv = find_root() / LOCAL_VENV
    for candidate in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _check_python(python: str) -> tuple[bool, str]:
    if os.path.normcase(os.path.abspath(python)) == os.path.normcase(os.path.abspath(sys.executable)):
        try:
            found = importlib.util.find_spec("jobspy") is not None
        except (ImportError, ValueError):
            found = False
        return (True, "") if found else (False, _INSTALL_HINT)
    try:
        proc = subprocess.run([python, "-c", "import jobspy"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Python dédié à JobSpy inutilisable ({python}) : {exc}"
    if proc.returncode != 0:
        return False, f"python-jobspy absent de {python}. {_INSTALL_HINT}"
    return True, ""


def _run_worker(python: str, kwargs: dict, timeout_s: int) -> list[dict]:
    """Run ``jobspy.scrape_jobs(**kwargs)`` in ``python`` and return the rows (hard timeout)."""
    fd, out_name = tempfile.mkstemp(prefix="jobspy-", suffix=".json")
    os.close(fd)
    out_path = Path(out_name)
    try:
        proc = subprocess.run(
            [python, "-X", "utf8", "-c", _WORKER, str(out_path)], input=json.dumps(kwargs, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
        )
        if proc.returncode != 0:
            last = next((line for line in reversed(proc.stderr.splitlines()) if line.strip()), "")
            raise JobSpyError(last or f"code retour {proc.returncode}")
        return _read_rows(out_path)
    except subprocess.TimeoutExpired as exc:
        raise JobSpyError(f"délai dépassé ({timeout_s} s), Indeed ne répond pas") from exc
    except OSError as exc:
        raise JobSpyError(f"impossible de lancer {python} : {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            out_path.unlink(missing_ok=True)


def _read_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    try:
        rows = json.loads(text) if text else []
    except ValueError as exc:
        raise JobSpyError(f"sortie JobSpy illisible : {exc}") from exc
    if not isinstance(rows, list):
        raise JobSpyError("sortie JobSpy inattendue (liste attendue)")
    return [row for row in rows if isinstance(row, dict)]


# ---------------------------------------------------------------------- query building


def _build_scrape_kwargs(query: SearchQuery, country: str) -> dict | None:
    kwargs: dict[str, Any] = {
        "site_name": ["indeed"], "search_term": _search_term(query), "country_indeed": country,
        "results_wanted": max(1, min(query.max_results, MAX_RESULTS)), "description_format": "markdown", "verbose": 0,
    }
    if query.remote:  # JobSpy: hours_old and is_remote are mutually exclusive → age filtered afterwards
        return {**kwargs, "location": "France", "is_remote": True} if country == "france" else None
    return {**kwargs, **_place(query.location, country), "hours_old": query.max_age_days * 24}


def _place(loc: LocationPref | None, country: str) -> dict[str, Any]:
    """``location`` (+ ``distance`` in miles) for one Indeed country."""
    in_that_country = INDEED_COUNTRIES.get(location_country(loc)) == country
    if loc is None or not in_that_country:
        return {"location": country.title()}  # national / country-wide pass (e.g. France, Luxembourg)
    return {"location": loc.city or country.title(), "distance": max(1, round(loc.radius_km / KM_PER_MILE))}


def _search_term(query: SearchQuery) -> str:
    """JobSpy has no apprenticeship job_type: the contract goes into the search words."""
    term = " ".join(query.keywords.split())
    wanted = set(query.contract_types)
    if wanted == {ContractType.ALTERNANCE} and not _ALT_WORD.search(term):
        term = f"{term} alternance"
    elif wanted == {ContractType.STAGE} and not re.search(r"\bstage\b", term, re.I):
        term = f"{term} stage"
    return term


# ---------------------------------------------------------------------- mapping


def _jobs_from_rows(rows: list[dict], query: SearchQuery, found_at: datetime) -> Iterator[JobOffer]:
    cutoff = found_at - timedelta(days=query.max_age_days)
    for row in rows:
        try:
            job = _row_to_job(row, found_at=found_at)
        except _PARSE_ERRORS as exc:
            log.warning("Indeed : offre ignorée (format inattendu : %s)", exc)
            continue
        if job.published_at and job.published_at < cutoff:
            continue  # Indeed resurfaces very old postings
        if _NOT_DEV_TITLE.search(job.title):
            continue
        yield job


def _row_to_job(row: dict, *, found_at: datetime | None = None) -> JobOffer:
    raw_id = str(row.get("id") or "")
    job_key = raw_id.removeprefix("in-") if raw_id else _jk_from_url(row.get("job_url"))
    title = " ".join(str(row.get("title") or "").split())
    if not job_key or not title:
        raise ValueError("identifiant ou titre Indeed manquant")
    description = str(row.get("description") or "")
    emails = _emails(row.get("emails"))
    return JobOffer(
        id=stable_id("indeed", job_key), source="indeed", external_id=job_key, title=title,
        company=str(row.get("company") or ""), location=_location_label(row.get("location")),
        description=description, url=row.get("job_url") or f"https://fr.indeed.com/viewjob?jk={job_key}",
        apply_url=row.get("job_url_direct") or None, apply_method=ApplyMethod.EXTERNAL,
        contract_type=_detect_contract(title, description, row.get("job_type")),
        remote=row.get("is_remote") if isinstance(row.get("is_remote"), bool) else None,
        salary=_format_salary(row.get("min_amount"), row.get("max_amount"), row.get("currency"), row.get("interval")),
        contact_email=emails[0] if emails else None, company_domain=_domain(row.get("company_url_direct")),
        published_at=_parse_date(row.get("date_posted")), found_at=found_at or clock.now(),
        raw={
            "jobspy_id": raw_id, "site": row.get("site") or "indeed", "job_type": row.get("job_type"),
            "location_raw": row.get("location"), "company_url": row.get("company_url"), "emails": emails,
            "job_level": row.get("job_level"), "company_industry": row.get("company_industry"),
            "company_num_employees": row.get("company_num_employees"),
        },
    )


def _detect_contract(title: str, description: str = "", job_type: str | None = None) -> ContractType:
    for contract, pattern in _TITLE_CONTRACTS:
        if pattern.search(title or ""):
            return contract
    if str(job_type or "").lower() == "internship":
        return ContractType.STAGE
    for contract, pattern in _DESCRIPTION_CONTRACTS:
        if pattern.search(description or ""):
            return contract
    return ContractType.OTHER


def _jk_from_url(url: object) -> str:
    values = parse_qs(urlparse(str(url or "")).query).get("jk") or [""]
    return values[0]


def _location_label(value: object) -> str:
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    if not parts:
        return ""
    country = parts[-1] if len(parts) > 1 else "FR"
    return parts[0] if country.upper() in ("FR", "FRANCE") else f"{parts[0]}, {country}"


def _emails(value: object) -> list[str]:
    if isinstance(value, list):
        return extract_emails(", ".join(map(str, value)))
    return extract_emails(str(value or ""))


def _format_salary(low: Any, high: Any, currency: str | None, period: str | None) -> str | None:
    values = [float(v) for v in (low, high) if isinstance(v, int | float) and v > 0]
    if not values:
        return None
    amounts = " – ".join(f"{round(v):,}".replace(",", " ") for v in dict.fromkeys(values))
    symbol = "€" if (currency or "EUR").upper() == "EUR" else currency
    unit = _PERIODS.get(period or "")
    return f"{amounts} {symbol}" + (f" / {unit}" if unit else "")


def _domain(url: object) -> str | None:
    host = urlparse(str(url or "")).netloc.lower().split(":")[0]
    return host.removeprefix("www.") or None


def _parse_date(value: object) -> datetime | None:
    try:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000, UTC)  # pandas epoch milliseconds
        return clock.parse_dt(value) if isinstance(value, str) else None
    except (ValueError, OverflowError, OSError):
        return None
