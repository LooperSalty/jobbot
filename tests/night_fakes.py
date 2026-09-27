"""Fictitious data for the night scraper tests: registry API items, stored offers. No real company."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from jobbot import clock
from jobbot.models import ApplyMethod, ContractType, JobOffer
from jobbot.textutil import stable_id

NEAR_URL = "https://recherche-entreprises.api.gouv.fr/near_point"
SEARCH_URL = "https://recherche-entreprises.api.gouv.fr/search"


def registry_item(siren: str, name: str, *, naf: str = "62.01Z", cp: str = "57000", city: str = "METZ",
                  lat: float = 49.12, lon: float = 6.18, tranche: str = "11", categorie: str = "PME",
                  sole_trader: bool = False, active: bool = True) -> dict[str, Any]:
    """One « unité légale » as recherche-entreprises returns it (trimmed)."""
    place = {"siret": f"{siren}00011", "code_postal": cp, "libelle_commune": city, "latitude": str(lat),
             "longitude": str(lon), "adresse": f"1 RUE DE L'EXEMPLE {cp} {city}", "etat_administratif": "A",
             "departement": cp[:2]}
    return {
        "siren": siren, "nom_complet": name, "nom_raison_sociale": name, "activite_principale": naf,
        "tranche_effectif_salarie": tranche, "categorie_entreprise": categorie,
        "etat_administratif": "A" if active else "C", "siege": dict(place), "matching_etablissements": [place],
        "dirigeants": [], "complements": {"est_entrepreneur_individuel": sole_trader},
    }


def page(items: list[dict], *, total: int | None = None, per_page: int = 25) -> dict[str, Any]:
    count = len(items) if total is None else total
    return {"results": items, "total_results": count, "total_pages": max(1, -(-count // per_page)),
            "page": 1, "per_page": per_page}


def offer(external_id: str, company: str, title: str = "Développeur web en alternance (H/F)", *,
          location: str = "Metz (57)", postal_code: str | None = "57000", days_ago: int = 5,
          lat: float | None = 49.12, lon: float | None = 6.18, **extra: Any) -> JobOffer:
    """A stored offer (source « lba ») of a fictitious employer."""
    return JobOffer(
        id=stable_id("lba", external_id), source="lba", external_id=external_id, title=title, company=company,
        location=location, postal_code=postal_code, latitude=lat, longitude=lon,
        description="Développement d'applications web en équipe.", url=f"https://exemple.test/{external_id}",
        apply_method=ApplyMethod.EXTERNAL, contract_type=ContractType.ALTERNANCE,
        found_at=clock.now() - timedelta(days=days_ago), published_at=clock.now() - timedelta(days=days_ago),
        **extra,
    )
