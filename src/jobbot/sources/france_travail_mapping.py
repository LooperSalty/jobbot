"""Pure helpers of the France Travail source: query parameters and ``Offre`` → JobOffer mapping.

Split out of ``france_travail.py`` (400-line rule); import from ``jobbot.sources.france_travail``.
Generic helpers (``detect_remote``, ``department_from_postal``, ``domain_of``, ``method_for_url``,
``usable_email``) are shared with the La Bonne Alternance source.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from jobbot import clock
from jobbot.models import ApplyMethod, ContractType, JobOffer, JobStatus
from jobbot.textutil import extract_emails, normalize, stable_id, strip_accents

SOURCE = "france_travail"
OFFER_PAGE_URL = "https://candidat.francetravail.fr/offres/recherche/detail/{id}"
NATURE_ALTERNANCE = "E2,FS"  # E2 contrat d'apprentissage, FS contrat de professionnalisation
PUBLIEE_DEPUIS_VALUES = (1, 3, 7, 14, 31)  # only documented values
MAX_KEYWORDS = 7
TYPE_CONTRAT_CODES = {
    ContractType.CDI: "CDI", ContractType.CDD: "CDD",
    ContractType.INTERIM: "MIS", ContractType.FREELANCE: "LIB",
}
CONTRACT_FROM_CODE = {
    "CDI": ContractType.CDI, "CDD": ContractType.CDD, "SAI": ContractType.CDD,
    "MIS": ContractType.INTERIM, "LIB": ContractType.FREELANCE,
}
CONTRACT_WORDS = frozenset({
    "alternance", "alternant", "alternante", "apprenti", "apprentie", "apprentissage",
    "professionnalisation", "cdi", "cdd", "stage", "stagiaire", "h/f", "f/h", "hf",
})
STOP_WORDS = frozenset({"de", "du", "des", "en", "et", "le", "la", "les", "un", "une", "au", "aux", "pour", "sur"})
_DROPPED_WORDS = CONTRACT_WORDS | STOP_WORDS
_IGNORED_EMAIL_DOMAINS = ("francetravail.fr", "francetravail.io", "pole-emploi.fr", "example.com")

_KEYWORD_CHARS = re.compile(r"[^A-Za-z0-9@#$%^&+./\- ]+")
_CONTENT_RANGE = re.compile(r"(\d+)\s*-\s*(\d+)\s*/\s*(\d+)")
_TT = r"t[ée]l[ée]\s?-?travail"
_REMOTE_NO = re.compile(
    rf"(?:pas de|sans|aucun)\s+{_TT}|{_TT}\s*:?\s*(?:non|impossible|exclu)"
    r"|100\s?%\s?(?:sur site|en pr[ée]sentiel|pr[ée]sentiel)",
    re.IGNORECASE,
)
_REMOTE_YES = re.compile(rf"{_TT}|full[\s-]?remote|\bremote\b|travail\s+[àa]\s+distance|\bhybride\b", re.IGNORECASE)


# --------------------------------------------------------------------------- generic helpers


def detect_remote(text: str) -> bool | None:
    """True if the text offers remote/hybrid work, False if it explicitly excludes it, None if silent."""
    if not text:
        return None
    if _REMOTE_NO.search(text):
        return False
    return True if _REMOTE_YES.search(text) else None


def department_from_postal(postal_code: str | None) -> str | None:
    pc = (postal_code or "").strip()
    if len(pc) != 5 or not pc.isdigit():
        return None
    if pc.startswith("97"):
        return pc[:3]
    if pc.startswith("20"):
        return "2A" if int(pc) < 20200 else "2B"
    return pc[:2]


def domain_of(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    candidate = url.strip() if "://" in url else f"http://{url.strip()}"
    host = (urlparse(candidate).hostname or "").lower().removeprefix("www.")
    if not host or "." not in host or host.endswith(_IGNORED_EMAIL_DOMAINS):
        return None
    return host


def method_for_url(url: str) -> ApplyMethod:
    """Partner pages on HelloWork go to the HelloWork applier, everything else is external."""
    host = (urlparse(url).hostname or "").lower()
    return ApplyMethod.HELLOWORK if host.endswith("hellowork.com") else ApplyMethod.EXTERNAL


def usable_email(text: str | None) -> str | None:
    """First real e-mail address in ``text`` (France Travail relay/placeholder addresses ignored)."""
    for email in extract_emails(text or ""):
        domain = email.rsplit("@", 1)[-1]
        if not domain.endswith(_IGNORED_EMAIL_DOMAINS) and not email.startswith(("noreply", "no-reply")):
            return email
    return None


# --------------------------------------------------------------------------- query parameters


def keywords_param(keywords: str) -> str:
    """``motsCles``: ASCII words (2..99 chars, AND logic), contract/stop words removed, at most 7."""
    text = strip_accents(keywords or "").replace("'", " ").replace("’", " ")
    words: list[str] = []
    for word in re.split(r"[\s,]+", _KEYWORD_CHARS.sub(" ", text).lower()):
        if 2 <= len(word) <= 99 and word not in _DROPPED_WORDS and word not in words:
            words.append(word)
    return ",".join(words[:MAX_KEYWORDS])


def published_since(max_age_days: int) -> int:
    """Smallest documented ``publieeDepuis`` value covering ``max_age_days`` (capped at 31)."""
    return next((v for v in PUBLIEE_DEPUIS_VALUES if max_age_days <= v), PUBLIEE_DEPUIS_VALUES[-1])


def parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    match = _CONTENT_RANGE.search(value or "")
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def contract_filters(types: list[ContractType]) -> list[tuple[dict[str, str], bool]]:
    """One param set per API semantic: alternance (natureContrat) and plain contracts (typeContrat).

    The bool asks to drop alternance offers locally (a ``typeContrat=CDI`` query also returns them).
    """
    if not types:
        return [({}, False)]
    filters: list[tuple[dict[str, str], bool]] = []
    if ContractType.ALTERNANCE in types:
        filters.append(({"natureContrat": NATURE_ALTERNANCE}, False))
    codes = list(dict.fromkeys(TYPE_CONTRAT_CODES[t] for t in types if t in TYPE_CONTRAT_CODES))
    if codes:
        filters.append(({"typeContrat": ",".join(codes)}, True))
    return filters


def pick_commune(communes: list[dict[str, Any]], city: str) -> str | None:
    """INSEE code of ``city`` among geo.api.gouv.fr results (exact name first, else most populated)."""
    wanted = normalize(city)
    for commune in communes:
        if normalize(str(commune.get("nom") or "")) == wanted and commune.get("code"):
            return str(commune["code"])
    ranked = sorted(communes, key=lambda c: c.get("population") or 0, reverse=True)
    return str(ranked[0]["code"]) if ranked and ranked[0].get("code") else None


# --------------------------------------------------------------------------- offer mapping


def is_alternance(offre: dict[str, Any]) -> bool:
    nature = normalize(str(offre.get("natureContrat") or ""))  # a label in responses, not the code
    return offre.get("alternance") is True or "apprentissage" in nature or "professionnalisation" in nature


def _labels(items: Any, key: str = "libelle") -> list[str]:
    return [str(i[key]).strip() for i in items or [] if isinstance(i, dict) and i.get(key)]


def build_description(offre: dict[str, Any]) -> str:
    """Offer text + structured requirements, so keyword/ATS scoring sees everything."""
    parts = [str(offre.get("description") or "")]
    sections = (
        ("Compétences", _labels(offre.get("competences"))),
        ("Qualités professionnelles", _labels(offre.get("qualitesProfessionnelles"))),
        ("Langues", _labels(offre.get("langues"))),
        ("Formation", [" ".join(_labels([f], "niveauLibelle") + _labels([f], "domaineLibelle"))
                       for f in offre.get("formations") or [] if isinstance(f, dict)]),
    )
    parts.extend(f"{title} : {', '.join(v for v in values if v)}" for title, values in sections if any(values))
    if offre.get("experienceLibelle"):
        parts.append(f"Expérience : {offre['experienceLibelle']}")
    contract = " / ".join(str(v) for v in (offre.get("typeContratLibelle"), offre.get("natureContrat")) if v)
    if contract:
        parts.append(f"Contrat : {contract}")
    company_desc = (offre.get("entreprise") or {}).get("description")
    if company_desc:
        parts.append(f"Entreprise : {company_desc}")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def apply_route(offre: dict[str, Any]) -> tuple[ApplyMethod, str | None, str | None]:
    """(method, apply_url, contact_email). ``contact.courriel`` is usually a sentence: never invent an e-mail."""
    contact = offre.get("contact") or {}
    origin = offre.get("origineOffre") or {}
    offer_page = OFFER_PAGE_URL.format(id=offre.get("id"))
    email = usable_email(contact.get("courriel"))
    desc_email = usable_email(offre.get("description"))  # recruiters sometimes write it in the text
    if email:
        return ApplyMethod.EMAIL, contact.get("urlPostulation") or None, email
    if contact.get("urlPostulation"):
        url = str(contact["urlPostulation"])
        return method_for_url(url), url, desc_email
    partners = [p.get("url") for p in origin.get("partenaires") or [] if isinstance(p, dict) and p.get("url")]
    if partners:
        return method_for_url(partners[0]), partners[0], desc_email
    if desc_email:
        return ApplyMethod.EMAIL, origin.get("urlOrigine") or offer_page, desc_email
    return ApplyMethod.EXTERNAL, origin.get("urlOrigine") or offer_page, None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def offer_to_job(offre: dict[str, Any]) -> JobOffer:
    """Map one ``Offre`` object to a JobOffer (status NEW, found_at now). Reads both field casings."""
    ext_id = str(offre["id"])
    place = offre.get("lieuTravail") or {}
    company = offre.get("entreprise") or {}
    title = str(offre.get("intitule") or "").strip() or "(offre sans titre)"
    method, apply_url, email = apply_route(offre)
    salary = " — ".join(str(v) for v in ((offre.get("salaire") or {}).get(k) for k in ("libelle", "commentaire")) if v)
    code = str(offre.get("typeContrat") or "").upper()
    return JobOffer(
        id=stable_id(SOURCE, ext_id), source=SOURCE, external_id=ext_id, title=title,
        company=str(company.get("nom") or "").strip(), location=str(place.get("libelle") or ""),
        postal_code=place.get("codePostal") or place.get("codepostal") or None,
        latitude=_float(place.get("latitude")), longitude=_float(place.get("longitude")),
        description=build_description(offre), url=OFFER_PAGE_URL.format(id=ext_id),
        apply_url=apply_url, apply_method=method,
        contract_type=ContractType.ALTERNANCE if is_alternance(offre) else CONTRACT_FROM_CODE.get(code, ContractType.OTHER),
        remote=detect_remote(f"{title}\n{offre.get('description') or ''}"), salary=salary or None,
        contact_email=email, contact_name=((offre.get("contact") or {}).get("nom") or None),
        company_domain=domain_of(company.get("url")), published_at=clock.parse_dt(offre.get("dateCreation")),
        found_at=clock.now(), raw=offre,
    )


def expire_job(job: JobOffer) -> JobOffer:
    """Offer deleted at France Travail: licence art. 7 → drop raw data and contact details."""
    status = job.status if job.status == JobStatus.APPLIED else JobStatus.EXPIRED
    return job.model_copy(update={"status": status, "raw": {"expired": True}, "contact_name": None, "contact_email": None})
