"""Search preferences applied by ``match_job`` on top of the plain location fit (v2).

- **Country / area contracts** — ``LocationPref.country`` + ``LocationPref.contract_types`` (Luxembourg = CDI
  only): an offer abroad needs a search area in that country accepting its contract; an area restricted to
  some contracts ignores the others (clear French reason instead of a generic "trop loin").
- **Relocation** — ``willing_to_relocate`` + ``relocation_regions``: no distance filter for French offers in
  the accepted regions (anywhere in France when the list is empty). Never abroad, never over an area's
  contract restriction, never without evidence the offer is in France.
- **Salary floor** — ``min_salary_eur`` (gross, per year): never for an alternance (legal pay grid) nor an
  unknown contract (may be one); an unparsable or non-euro salary never filters; a range is kept when its
  top reaches the floor.
"""

from __future__ import annotations

import re

from jobbot.config import LocationPref, Profile
from jobbot.matching import filters
from jobbot.matching._text import norm
from jobbot.matching.geo import department_from_location, department_from_postal, haversine_km, lookup_known_place
from jobbot.matching.location import (
    LocationFit,
    assess_location,
    job_coordinates,
    job_department,
    job_departments,
    job_region,
)
from jobbot.matching.regions import DEPARTMENT_REGION, REGION_KEYS, REGIONS, key, region_of  # noqa: F401
from jobbot.models import ContractType, JobOffer
from jobbot.sources.countries import COUNTRY_NAMES, FRANCE, location_country

CONTRACT_LABELS = {
    ContractType.ALTERNANCE: "alternance", ContractType.CDI: "CDI", ContractType.CDD: "CDD",
    ContractType.STAGE: "stage", ContractType.FREELANCE: "freelance", ContractType.INTERIM: "intérim",
    ContractType.OTHER: "non précisé",
}
FOREIGN_UNKNOWN_PLACE_SCORE = 0.6  # offer in an accepted country, exact place unknown
RELOCATION_SCORE = 0.6
UNKNOWN_FOREIGN = "ZZ"  # abroad, country not recognised (France Travail "99 - <pays>")
HOURS_PER_YEAR = 1607.0  # legal yearly working time (35 h)
DAYS_PER_YEAR = 218.0  # "forfait jours"
PLAUSIBLE_ANNUAL = (5_000.0, 400_000.0)
SALARY_EXEMPT = frozenset({ContractType.ALTERNANCE, ContractType.OTHER})

_IN_COUNTRY = {
    "FR": "en France", "LU": "au Luxembourg", "BE": "en Belgique", "DE": "en Allemagne", "CH": "en Suisse",
    "ES": "en Espagne", "IT": "en Italie", "NL": "aux Pays-Bas", "GB": "au Royaume-Uni", "MC": "à Monaco",
}
_COUNTRY_WORDS = tuple((code, re.compile(r"(?<![a-z])(?:" + words + r")(?![a-z])")) for code, words in (
    ("LU", "luxembourg|luxemburg|letzebuerg"),
    ("BE", "belgique|belgium|belgie|wallonie|wallonia|bruxelles|brussels|vlaanderen"),
    ("DE", ("allemagne|germany|deutschland|saarland|rheinland pfalz|rhenanie palatinat|baden wurttemberg|"
            "bade wurtemberg|bayern|baviere|nordrhein westfalen")),
    ("CH", "suisse|switzerland|schweiz|romandie"), ("ES", "espagne|spain|espana"),
    ("IT", "italie|italy|italia"), ("NL", "pays bas|netherlands|nederland"),
    ("GB", "royaume uni|united kingdom|angleterre|england"), ("MC", "monaco"), ("FR", "france"),
))
_CODE_SUFFIX = re.compile(r",\s*([a-z]{2})\s*$")
_ABROAD = re.compile(r"^\s*99\s*-")

# --------------------------------------------------------------------------- regions (relocation)
# REGIONS, region_of… live in ``matching.regions`` (re-exported here).


def accepted_departments(entries: list[str]) -> dict[str, str]:
    """Departments covered by ``relocation_regions`` entries (regions, old regions, codes, names) → entry."""
    accepted: dict[str, str] = {}
    for entry in entries:
        for department in _entry_departments(entry):
            accepted.setdefault(department, entry.strip())
    return accepted


def _entry_departments(entry: str) -> tuple[str, ...]:
    entry_key = key(entry)
    if entry_key in REGION_KEYS:
        return REGION_KEYS[entry_key]
    code = entry.strip().upper()
    department = code if code in DEPARTMENT_REGION else department_from_location(entry)
    return (department,) if department in DEPARTMENT_REGION else ()


# --------------------------------------------------------------------------- country of an offer


def offer_country(job: JobOffer) -> str | None:
    """ISO code of the offer's country ("FR", "LU"…, ``UNKNOWN_FOREIGN``), None when it cannot be told."""
    hint = str(job.raw.get("office_country") or "").strip().upper()
    if hint in COUNTRY_NAMES:
        return hint
    flat = norm(job.location)
    text = flat.replace("-", " ")
    found = [(m.start(), code) for code, rx in _COUNTRY_WORDS for m in rx.finditer(text)]
    suffix = _CODE_SUFFIX.search(text)
    if suffix and suffix.group(1).upper() in COUNTRY_NAMES:
        found.append((len(text), suffix.group(1).upper()))
    if found:
        return max(found)[1]  # the country is usually the last component ("Arlon, Luxembourg, Belgique")
    if _ABROAD.match(flat):
        return UNKNOWN_FOREIGN
    return FRANCE if region_of(job_department(job)) or job_region(job) is not None else None


def in_country(code: str, place: str = "") -> str:
    """'au Luxembourg', 'en Belgique'… ('à l'étranger (<place>)' for an unknown country)."""
    return _IN_COUNTRY.get(code) or f"à l'étranger ({place or code})"


# --------------------------------------------------------------------------- contracts per country / area


def _label(contract: ContractType) -> str:
    return CONTRACT_LABELS[contract]


def _labels(contracts: list[ContractType]) -> str:
    return ", ".join(CONTRACT_LABELS[c] for c in dict.fromkeys(contracts)) or "tous"


def area_contracts(loc: LocationPref, profile: Profile) -> list[ContractType]:
    """Contracts accepted in a search area: its own list, else the profile's (empty = any)."""
    return list(loc.contract_types or profile.target.contract_types)


def _allows(loc: LocationPref, profile: Profile, contract: ContractType) -> bool:
    accepted = area_contracts(loc, profile)
    return contract == ContractType.OTHER or not accepted or contract in accepted


def _wanted_contracts(profile: Profile) -> set[ContractType]:
    target = profile.target
    if not target.contract_types:
        return set()  # no restriction at all
    return set(target.contract_types) | {c for loc in target.locations for c in loc.contract_types or []}


def alternance_only(profile: Profile) -> bool:
    """Alternance is the only contract searched, in every area."""
    return _wanted_contracts(profile) == {ContractType.ALTERNANCE}


def unknown_contract_reason(job: JobOffer, profile: Profile, contract: ContractType, *,
                            fully_read: bool) -> str | None:
    """Alternance only: an offer read in full that never names its contract nor hints at a work-study one is a
    regular job (an alternance offer says so) — unlike a search card or a snippet whose full text is not known."""
    if contract != ContractType.OTHER or not fully_read or not alternance_only(profile):
        return None
    if filters.mentions_alternance(job):
        return None
    return "Écartée : contrat non précisé (seule l'alternance est recherchée)"


def contract_reason(job: JobOffer, profile: Profile, contract: ContractType) -> str | None:
    """Hard-filter reason from the contract type, the offer's country and the areas' contract lists."""
    wanted = _wanted_contracts(profile)
    if contract != ContractType.OTHER and wanted and contract not in wanted:
        return f"Écartée : type de contrat non recherché ({_label(contract)})"
    country = offer_country(job)
    if country not in (None, FRANCE) and profile.target.locations:
        return _abroad_reason(job, profile, contract, country)
    target = profile.target.contract_types
    if country == FRANCE and contract != ContractType.OTHER and target and contract not in target:
        return _only_elsewhere_reason(profile, contract)
    return None


def _abroad_reason(job: JobOffer, profile: Profile, contract: ContractType, country: str) -> str | None:
    areas = [loc for loc in profile.target.locations if location_country(loc) == country]
    where = in_country(country, job.location)
    if not areas:
        return f"Écartée : offre {where} — aucune zone de recherche dans ce pays"
    if any(_allows(loc, profile, contract) for loc in areas):
        return None
    accepted = [c for loc in areas for c in area_contracts(loc, profile)]
    return f"Écartée : offre en {_label(contract)} {where} — contrats recherchés dans ce pays : {_labels(accepted)}"


def _only_elsewhere_reason(profile: Profile, contract: ContractType) -> str | None:
    """A contract accepted only by foreign areas (e.g. CDD in Luxembourg only) for an offer in France."""
    areas = [loc for loc in profile.target.locations if loc.contract_types and contract in loc.contract_types]
    if not areas or any(location_country(loc) == FRANCE for loc in areas):
        return None  # a French area accepts it: the location fit decides
    places = ", ".join(dict.fromkeys(in_country(location_country(loc)) for loc in areas))
    return f"Écartée : contrat {_label(contract)} recherché uniquement {places}"


# --------------------------------------------------------------------------- location fit (+ relocation)


def location_fit(job: JobOffer, profile: Profile, contract: ContractType) -> LocationFit:
    """``assess_location`` + foreign places without coordinates, area contract restrictions and relocation."""
    abroad = _abroad_without_coordinates(job, profile, contract)
    if abroad is not None:
        return abroad
    fit = assess_location(job, profile, contract)
    if fit.blocking is None:
        return fit
    zone = _zone_reason(job, profile, contract)
    if zone:
        return LocationFit(0.0, zone, blocking=zone, distance_km=fit.distance_km)
    return relocation_fit(job, profile) or fit


def _abroad_without_coordinates(job: JobOffer, profile: Profile, contract: ContractType) -> LocationFit | None:
    country = offer_country(job)
    if country in (None, FRANCE) or job_coordinates(job) is not None:
        return None
    if not any(location_country(loc) == country and _allows(loc, profile, contract)
               for loc in profile.target.locations):
        return None
    where = in_country(country, job.location)
    return LocationFit(FOREIGN_UNKNOWN_PLACE_SCORE,
                       f"{where[:1].upper()}{where[1:]} — lieu précis à vérifier ({job.location})")


def _zone_reason(job: JobOffer, profile: Profile, contract: ContractType) -> str | None:
    for loc in profile.target.locations:
        if not _allows(loc, profile, contract) and _in_area(job, loc):
            return (f"Écartée : offre en {_label(contract)} dans la zone de {loc.city} — "
                    f"contrats recherchés dans cette zone : {_labels(area_contracts(loc, profile))}")
    return None


def _in_area(job: JobOffer, loc: LocationPref) -> bool:
    coords = job_coordinates(job)
    has_center = loc.latitude is not None and loc.longitude is not None
    center = (loc.latitude, loc.longitude) if has_center else lookup_known_place(loc.city)
    if coords and center:
        return haversine_km(*coords, *center) <= loc.radius_km
    department = job_department(job)
    area_department = department_from_postal(loc.postal_code) or department_from_location(loc.city)
    return department is not None and department == area_department


def relocation_fit(job: JobOffer, profile: Profile) -> LocationFit | None:
    """Fit of a French offer outside the search areas when the candidate accepts to move there, else None."""
    target = profile.target
    if not target.willing_to_relocate:
        return None
    country, departments = offer_country(job), job_departments(job)
    if country not in (None, FRANCE) or (country != FRANCE and not any(region_of(d) for d in departments)):
        return None  # abroad, or no evidence that the offer is in France
    label = "toute la France" if not target.relocation_regions else _accepted_region(job, departments,
                                                                                      target.relocation_regions)
    if label is None:
        return None
    return LocationFit(RELOCATION_SCORE, f"Hors des zones de recherche, mais déménagement accepté ({label})")


def _accepted_region(job: JobOffer, departments: tuple[str, ...], entries: list[str]) -> str | None:
    """The ``relocation_regions`` entry covering the offer: its department, or every department of the whole
    region it names (« Île-de-France » needs the whole region accepted)."""
    accepted = accepted_departments(entries)
    if departments and all(d.upper() in accepted for d in departments):
        return accepted[departments[0].upper()]
    text = f" {key(job.location)} "
    return next((entry.strip() for entry in entries if key(entry) and f" {key(entry)} " in text), None)


# --------------------------------------------------------------------------- salary floor

_NUMBER = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ .,]\d{3})+|\d+)(?:[.,](\d{1,2}))?(?!\d)\s*(k)?(?![a-z])")
_FOREIGN_CURRENCY = re.compile(r"\$|£|\bchf\b|\busd\b|\bgbp\b|\bdollars?\b")
_EURO = re.compile(r"€|\beur\b|\beuros?\b")
_MONTHS = re.compile(r"\bsur\s+(\d{1,2})(?:[.,]\d+)?\s*mois\b")
_EXTRA_MONTH = re.compile(r"\b\d{1,2}\s*(?:e|eme|er)\s*mois\b")
_PERIODS = (
    ("hour", re.compile(r"horaire|\bheures?\b|/\s*h\b|\bhour(?:ly)?\b|\bhr\b")),
    ("day", re.compile(r"\bjours?\b|journalier|\bday\b|\bdaily\b|\btjm\b|/\s*j\b")),
    ("month", re.compile(r"\bmois\b|mensuel|\bmonth(?:ly)?\b")),
    ("year", re.compile(r"\bans?\b|annuel|\bannee\b|\byear(?:ly)?\b|\byr\b|\bper annum\b")),
)


def annual_salary(text: str | None) -> tuple[float, float] | None:
    """(min, max) gross yearly euros from a free salary text, None when it cannot be read reliably."""
    for segment in re.split(r"\s+—\s+|;|\n", text or ""):
        parsed = _parse_salary(norm(segment))
        if parsed is not None:
            return parsed
    return None


def _parse_salary(t: str) -> tuple[float, float] | None:
    if _FOREIGN_CURRENCY.search(t) and not _EURO.search(t):
        return None
    months = _MONTHS.search(t)
    months_count = float(months.group(1)) if months and 12 <= int(months.group(1)) <= 15 else 12.0
    t = _EXTRA_MONTH.sub(" ", _MONTHS.sub(" ", t))
    period = _period(t)
    yearly = [_yearly(value, period, months_count) for value in _numbers(t)]
    plausible = [v for v in yearly if v is not None and PLAUSIBLE_ANNUAL[0] <= v <= PLAUSIBLE_ANNUAL[1]]
    return (min(plausible), max(plausible)) if plausible else None


def _numbers(t: str) -> list[float]:
    found: list[tuple[float, bool]] = []
    for m in _NUMBER.finditer(t):
        value = float(re.sub(r"[ .,]", "", m.group(1))) + (float(f"0.{m.group(2)}") if m.group(2) else 0.0)
        found.append((value * 1000 if m.group(3) else value, bool(m.group(3))))
    thousands = any(k for _, k in found)  # "35 - 43 k€": the "k" applies to the whole range
    return [v * 1000 if thousands and not k and v < 1000 else v for v, k in found]


def _period(t: str) -> str | None:
    found = [(m.start(), name) for name, rx in _PERIODS if (m := rx.search(t))]
    return min(found)[1] if found else None


def _yearly(value: float, period: str | None, months: float) -> float | None:
    factors = {"year": 1.0, "month": months, "hour": HOURS_PER_YEAR, "day": DAYS_PER_YEAR}
    if period is not None:
        return value * factors[period]
    if value >= 10_000:
        return value  # no period: a big amount is yearly, a small one monthly
    return value * 12 if value >= 800 else None


def _eur(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def salary_reason(job: JobOffer, profile: Profile, contract: ContractType) -> str | None:
    """Hard-filter reason when the announced salary (top of the range) is below ``min_salary_eur``."""
    floor = profile.target.min_salary_eur
    if not floor or floor <= 0 or contract in SALARY_EXEMPT:
        return None
    parsed = annual_salary(job.salary)
    if parsed is None or parsed[1] >= floor:
        return None
    return f"Écartée : salaire annoncé inférieur au minimum souhaité ({_eur(parsed[1])} € par an < {_eur(floor)} €)"
