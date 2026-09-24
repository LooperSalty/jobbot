"""Pure helpers of the La Bonne Alternance source: path A / path B JSON → JobOffer / Company.

Split out of ``lba.py`` (400-line rule); import from ``jobbot.sources.lba``. ``raw`` keeps what
``apply.lba.LbaApplier`` needs: ``source`` (detail sub-type), ``id``, ``recipient_id``, ``hasEmail``/``smart_apply``,
``application_count``. The per-offer apply token (valid 24 h) is never stored.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from jobbot import clock
from jobbot.models import ApplyMethod, Company, ContractType, JobOffer, JobStatus
from jobbot.sources.france_travail_mapping import CONTRACT_WORDS, detect_remote, domain_of, method_for_url
from jobbot.textutil import normalize_company, slugify, stable_id

log = logging.getLogger(__name__)

SOURCE = "lba"
RECRUITER_SOURCE = "lba_recruteur"
LBA_SITE = "https://labonnealternance.apprentissage.beta.gouv.fr"
SUB_RECRUITER, SUB_LBA, SUB_PARTNER = "recruteurs_lba", "offres_emploi_lba", "offres_emploi_partenaires"
REMOTE_MODES = {"remote": True, "hybrid": True, "onsite": False}
_ADDRESS = re.compile(r"(\d{5})\s+([^\d].*)$")
_HELLOWORK_ID = re.compile(r"/emplois/(\d+)\.html")


def search_text(keywords: str) -> str:
    words = [w for w in (keywords or "").split() if w.lower().strip("()") not in CONTRACT_WORDS]
    return " ".join(words)[:200]


def geo_point(point: Any) -> tuple[float | None, float | None]:
    """GeoJSON ``{"coordinates": [lon, lat]}`` → (lat, lon)."""
    coords = (point or {}).get("coordinates") if isinstance(point, dict) else None
    if isinstance(coords, list) and len(coords) == 2:
        return float(coords[1]), float(coords[0])
    return None, None


def split_address(address: str | None) -> tuple[str | None, str]:
    match = _ADDRESS.search((address or "").strip())
    return (match[1], match[2].strip()) if match else (None, "")


def page_url(sub_type: str, url_id: str, title: str) -> str:
    return f"{LBA_SITE}/emploi/{sub_type}/{url_id}/{slugify(title)}"


def _lines(title: str, items: Any) -> str:
    values = [str(v).replace("\t", " : ").strip() for v in items or [] if v]
    return f"{title} : " + " ; ".join(values) if values else ""


def detail_description(detail: dict[str, Any]) -> str:
    job = detail.get("job") or {}
    parts = [
        str(job.get("description") or ""),
        _lines("Profil recherché", job.get("offer_desired_skills")),
        _lines("Compétences à acquérir", (job.get("offer_to_be_acquired_skills") or [])[:15]),
        _lines("Conditions d'accès", job.get("offer_access_conditions")),
        f"Durée du contrat : {job['dureeContrat']}" if job.get("dureeContrat") else "",
        f"Rythme : {job['contract_rythm']}" if job.get("contract_rythm") else "",
        f"Entreprise : {job['employeurDescription']}" if job.get("employeurDescription") else "",
    ]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def hit_to_job(hit: dict[str, Any]) -> JobOffer:
    """Path B search hit (offer) → JobOffer. Partner offers get their apply target from the detail."""
    sub_type = str(hit.get("sub_type") or "")
    url_id = str(hit.get("url_id") or hit["_id"])
    title = str(hit.get("title") or "").strip() or "(offre sans titre)"
    smart = bool(hit.get("smart_apply"))
    lat, lon = geo_point(hit.get("location"))
    description = str(hit.get("description") or "").strip()
    raw = {
        "source": sub_type, "id": url_id, "lba_id": hit.get("_id"), "origin": "site", "recipient_id": None,
        "smart_apply": smart, "hasEmail": smart, "application_count": hit.get("application_count"),
        "level": hit.get("level") or None, "rome_labels": hit.get("rome_labels") or [],
        "activity_sector": hit.get("activity_sector"), "is_formation_included": hit.get("is_formation_included"),
        "search_hit": hit,
    }
    return JobOffer(
        id=stable_id(SOURCE, url_id), source=SOURCE, external_id=url_id, title=title,
        company=str(hit.get("organization_name") or "").strip(), location=str(hit.get("address") or ""),
        postal_code=split_address(hit.get("address"))[0], latitude=lat, longitude=lon,
        description=description, url=page_url(sub_type, url_id, title),
        apply_method=ApplyMethod.LBA if smart else ApplyMethod.EXTERNAL,
        contract_type=ContractType.ALTERNANCE, remote=detect_remote(f"{title}\n{description}"),
        published_at=clock.parse_dt(hit.get("publication_date")), found_at=clock.now(), raw=raw,
    )


def detail_route(job: JobOffer, detail: dict[str, Any]) -> tuple[ApplyMethod, str | None, str | None]:
    """(method, apply_url, partner_url): LBA relay when ``hasEmail`` + ``recipient_id``, else the partner page."""
    contact = detail.get("contact") or {}
    url = str(contact.get("url") or "")
    partner_url = url if url.startswith("http") and not url.startswith(LBA_SITE) else None
    if contact.get("hasEmail") and detail.get("recipient_id"):
        return ApplyMethod.LBA, None, partner_url
    if partner_url:
        return method_for_url(partner_url), partner_url, partner_url
    if job.apply_method == ApplyMethod.LBA:  # no relay after all: the human applies from the LBA page
        return ApplyMethod.EXTERNAL, job.url or None, None
    return job.apply_method, job.apply_url, None


def apply_detail(job: JobOffer, detail: dict[str, Any]) -> JobOffer:
    """Merge a path B detail: full text, apply target, relay info (the 24 h token is never stored)."""
    contact, jobd, company = detail.get("contact") or {}, detail.get("job") or {}, detail.get("company") or {}
    has_email, recipient = bool(contact.get("hasEmail")), detail.get("recipient_id")
    method, apply_url, partner_url = detail_route(job, detail)
    hellowork = _HELLOWORK_ID.search(partner_url or "")
    description = detail_description(detail)
    active = str(jobd.get("status") or detail.get("status") or "Active") == "Active"
    raw = {
        **job.raw, "recipient_id": recipient, "hasEmail": has_email, "smart_apply": has_email,
        "application_count": detail.get("applicationCount", job.raw.get("application_count")),
        "partner_label": jobd.get("partner_label"), "siret": company.get("siret"),
        "company_size": company.get("size"), "applicant_questions": jobd.get("to_applicant_questions"),
        "hellowork_id": hellowork[1] if hellowork else None, "detail_fetched": True,
    }
    expired = not active and job.status != JobStatus.APPLIED
    return job.model_copy(update={
        "apply_method": method, "apply_url": apply_url, "raw": raw,
        "description": description if len(description) > len(job.description) else job.description,
        "remote": True if (detail.get("place") or {}).get("remoteOnly") else job.remote,
        "status": JobStatus.EXPIRED if expired else job.status,
    })


def api_job_to_job(item: dict[str, Any]) -> JobOffer | None:
    """Path A ``JobOfferRead`` → JobOffer (recruiters are handled by ``search_recruiters``)."""
    ident = item.get("identifier") or {}
    label = str(ident.get("partner_label") or "")
    if label == SUB_RECRUITER:
        return None
    lba_id = ident.get("id")
    sub_type = (SUB_LBA if label == SUB_LBA else SUB_PARTNER) if lba_id else None
    ext_id = str(lba_id) if lba_id else f"{slugify(label)}-{ident.get('partner_job_id')}"
    work, apply = item.get("workplace") or {}, item.get("apply") or {}
    offer, contract = item.get("offer") or {}, item.get("contract") or {}
    title = str(offer.get("title") or "").strip() or "(offre sans titre)"
    recipient, target = apply.get("recipient_id"), str(apply.get("url") or "") or None
    if recipient:
        method, apply_url = ApplyMethod.LBA, None
    elif target and not target.startswith(LBA_SITE):
        method, apply_url = method_for_url(target), target
    else:
        method, apply_url = ApplyMethod.EXTERNAL, target
    description = "\n\n".join(p for p in (
        str(offer.get("description") or "").strip(), _lines("Profil recherché", offer.get("desired_skills")),
        _lines("Compétences à acquérir", offer.get("to_be_acquired_skills")),
        _lines("Conditions d'accès", offer.get("access_conditions")),
        f"Entreprise : {work['description']}" if work.get("description") else "",
    ) if p)
    remote = REMOTE_MODES.get(str(contract.get("remote")), detect_remote(f"{title}\n{description}"))
    location = work.get("location") or {}
    lat, lon = geo_point(location.get("geopoint"))
    raw = {
        "source": sub_type, "id": str(lba_id) if lba_id else None, "origin": "api", "recipient_id": recipient,
        "smart_apply": bool(recipient), "hasEmail": bool(recipient), "application_count": None,
        "partner_label": label, "partner_job_id": ident.get("partner_job_id"), "siret": work.get("siret"),
        "is_delegated": item.get("is_delegated"), "target_diploma": offer.get("target_diploma"), "api_item": item,
    }
    return JobOffer(
        id=stable_id(SOURCE, ext_id), source=SOURCE, external_id=ext_id, title=title,
        company=str(work.get("name") or work.get("brand") or work.get("legal_name") or "").strip(),
        location=str(location.get("address") or ""), postal_code=split_address(location.get("address"))[0],
        latitude=lat, longitude=lon, description=description,
        url=page_url(sub_type, ext_id, title) if sub_type else (target or ""), apply_url=apply_url,
        apply_method=method, contract_type=ContractType.ALTERNANCE, remote=remote,
        company_domain=domain_of(work.get("website")),
        published_at=clock.parse_dt((offer.get("publication") or {}).get("creation")), found_at=clock.now(), raw=raw,
    )


def hit_to_company(hit: dict[str, Any]) -> Company:
    """Path B ``recruteurs_lba`` hit → Company (spontaneous application target)."""
    siret = str(hit.get("url_id") or "")
    name = str(hit.get("organization_name") or hit.get("title") or "").strip()
    postal_code, city = split_address(hit.get("address"))
    lat, lon = geo_point(hit.get("location"))
    smart = bool(hit.get("smart_apply"))
    raw = {
        "source": SUB_RECRUITER, "id": siret, "siret": siret, "lba_id": hit.get("_id"), "origin": "site",
        # verified live: the relay recipient of a recruteurs_lba company is "partners_<search _id>"
        "recipient_id": f"partners_{hit['_id']}" if hit.get("_id") else None,
        "smart_apply": smart, "hasEmail": smart, "application_count": hit.get("application_count"),
        "rome_labels": hit.get("rome_labels") or [], "keywords": hit.get("keywords") or [],
        "distance_km": hit.get("distance"), "detail_url": page_url(SUB_RECRUITER, siret, name),
    }
    return Company(
        id=stable_id("company", siret[:9] if siret else normalize_company(name)), name=name,
        siren=siret[:9] if len(siret) == 14 else None, naf_label=hit.get("activity_sector") or None,
        city=city, postal_code=postal_code, address=hit.get("address") or None, latitude=lat, longitude=lon,
        source=RECRUITER_SOURCE, found_at=clock.now(), raw=raw,
    )


def api_recruiter_to_company(item: dict[str, Any]) -> Company | None:
    work, apply = item.get("workplace") or {}, item.get("apply") or {}
    siret = str(work.get("siret") or "")
    name = str(work.get("name") or work.get("brand") or work.get("legal_name") or "").strip()
    if not name:
        return None
    naf = (work.get("domain") or {}).get("naf") or {}
    location = work.get("location") or {}
    postal_code, city = split_address(location.get("address"))
    lat, lon = geo_point(location.get("geopoint"))
    recipient = apply.get("recipient_id")
    raw = {
        "source": SUB_RECRUITER, "id": siret, "siret": siret, "lba_id": (item.get("identifier") or {}).get("id"),
        "origin": "api", "recipient_id": recipient, "smart_apply": bool(recipient), "hasEmail": bool(recipient),
        "phone": apply.get("phone"), "apply_url": apply.get("url"),
    }
    return Company(
        id=stable_id("company", siret[:9] if siret else normalize_company(name)), name=name,
        siren=siret[:9] if len(siret) == 14 else None, naf_code=naf.get("code"), naf_label=naf.get("label"),
        headcount_range=work.get("size"), city=city, postal_code=postal_code, address=location.get("address"),
        latitude=lat, longitude=lon, domain=domain_of(work.get("website")), website=work.get("website"),
        description=work.get("description"), source=RECRUITER_SOURCE, found_at=clock.now(), raw=raw,
    )


def merge_company(site: Company, api: Company) -> Company:
    """Keep path B data (application_count, smart_apply) and fill the gaps from path A."""
    fields = ("naf_code", "naf_label", "headcount_range", "domain", "website", "description", "siren")
    update: dict[str, Any] = {f: getattr(api, f) for f in fields if getattr(site, f) is None and getattr(api, f)}
    update["raw"] = {**api.raw, **site.raw, "recipient_id": api.raw.get("recipient_id") or site.raw.get("recipient_id"),
                     "phone": api.raw.get("phone"), "hasEmail": bool(site.raw.get("hasEmail") or api.raw.get("hasEmail"))}
    return site.model_copy(update=update)


def safe_map(mapper: Callable[[dict], Any], item: Any) -> Any:
    try:
        return mapper(item)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        log.warning("Élément La Bonne Alternance ignoré (format inattendu) : %s", exc)
        return None
