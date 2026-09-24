"""Mapping of ``recherche-entreprises.api.gouv.fr`` results (unités légales) to :class:`Company`.

Pure functions, no I/O. Data minimisation (GDPR): only natural-person leaders are kept (never statutory
auditors), and their birth dates / nationality are dropped here, at ingestion.
"""

from __future__ import annotations

import math
import re

from jobbot import clock
from jobbot.models import Company
from jobbot.textutil import stable_id

DEFAULT_NAF_CODES = (
    "62.01Z", "62.02A", "62.02B", "62.03Z", "62.09Z", "58.21Z",
    "58.29A", "58.29B", "58.29C", "63.11Z", "63.12Z",
)
CORE_NAF = frozenset(DEFAULT_NAF_CODES)
NAF_LABELS = {
    "62.01Z": "Programmation informatique",
    "62.02A": "Conseil en systèmes et logiciels informatiques",
    "62.02B": "Tierce maintenance de systèmes et d'applications informatiques",
    "62.03Z": "Gestion d'installations informatiques",
    "62.09Z": "Autres activités informatiques",
    "63.11Z": "Traitement de données, hébergement et activités connexes",
    "63.12Z": "Portails Internet",
    "58.21Z": "Édition de jeux électroniques",
    "58.29A": "Édition de logiciels système et de réseau",
    "58.29B": "Édition de logiciels outils de développement et de langages",
    "58.29C": "Édition de logiciels applicatifs",
    "73.11Z": "Activités des agences de publicité",
    "72.19Z": "Recherche-développement en autres sciences physiques et naturelles",
    "70.22Z": "Conseil pour les affaires et autres conseils de gestion",
    "71.12B": "Ingénierie, études techniques",
    "74.10Z": "Activités spécialisées de design",
}
# INSEE "tranche d'effectif salarié" codes, in increasing order ("NN" = non-employer / unknown).
TRANCHE_CODES = ("NN", "00", "01", "02", "03", "11", "12", "21", "22", "31", "32", "41", "42", "51", "52", "53")
TRANCHE_LABELS = {
    "NN": "effectif non renseigné", "00": "0 salarié", "01": "1 ou 2 salariés", "02": "3 à 5 salariés",
    "03": "6 à 9 salariés", "11": "10 à 19 salariés", "12": "20 à 49 salariés", "21": "50 à 99 salariés",
    "22": "100 à 199 salariés", "31": "200 à 249 salariés", "32": "250 à 499 salariés",
    "41": "500 à 999 salariés", "42": "1 000 à 1 999 salariés", "51": "2 000 à 4 999 salariés",
    "52": "5 000 à 9 999 salariés", "53": "10 000 salariés et plus",
}
SYNTEC_IDCC = "1486"
SCORE_KEY = "outreach_score"  # raw key holding the prioritisation score
_SMALL_WORDS = {"le", "la", "les", "lès", "sur", "sous", "en", "de", "du", "des", "aux", "et"}


def tranche_codes_from(min_code: str) -> list[str]:
    """Every employer tranche code ≥ ``min_code`` (``NN`` never included)."""
    if min_code not in TRANCHE_CODES:
        raise ValueError(f"Code de tranche d'effectif inconnu : {min_code!r}")
    return [c for c in TRANCHE_CODES[TRANCHE_CODES.index(min_code):] if c != "NN"]


def company_from_result(item: dict, departments: set[str] | None = None) -> Company | None:
    """Map one API result (unité légale) to a :class:`Company`; None when unusable."""
    siren = str(item.get("siren") or "").strip()
    if not re.fullmatch(r"\d{9}", siren) or item.get("etat_administratif", "A") != "A":
        return None
    if (item.get("complements") or {}).get("est_entrepreneur_individuel"):
        return None
    place = _local_establishment(item, departments or set())
    if place is None:
        return None
    naf = item.get("activite_principale")
    tranche = item.get("tranche_effectif_salarie") or "NN"
    leaders_detail = person_leaders(item)
    return Company(
        id=stable_id("company", siren),
        name=_display_name(item, place),
        siren=siren,
        naf_code=naf,
        naf_label=NAF_LABELS.get(naf or ""),
        headcount_range=TRANCHE_LABELS.get(tranche),
        city=_title_city(place.get("libelle_commune") or ""),
        postal_code=place.get("code_postal") or None,
        address=place.get("adresse") or None,
        latitude=_float(place.get("latitude")),
        longitude=_float(place.get("longitude")),
        leaders=[f"{p['first']} {p['last']}".strip() for p in leaders_detail],
        source="recherche_entreprises",
        found_at=clock.now(),
        raw=_minimal_raw(item, place, leaders_detail),
    )


def _dept_of(establishment: dict) -> str:
    dept = establishment.get("departement")
    if dept:
        return str(dept)
    cp = str(establishment.get("code_postal") or "")
    return cp[:3] if cp.startswith("97") else cp[:2]


def _local_establishment(item: dict, departments: set[str]) -> dict | None:
    """First *active* matching establishment in the wanted departments (closed ones are returned too)."""
    matching = item.get("matching_etablissements") or []
    active = [e for e in matching if e.get("etat_administratif", "A") == "A"]
    local = [e for e in active if not departments or _dept_of(e) in departments]
    if local:
        return local[0]
    if matching:  # only closed or out-of-area establishments matched
        return None
    return item.get("siege") or {}


def _display_name(item: dict, place: dict) -> str:
    siege = item.get("siege") or {}
    for candidate in (place.get("nom_commercial"), siege.get("nom_commercial"), item.get("nom_raison_sociale")):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return str(item.get("nom_complet") or item.get("siren"))


def name_candidates(item: dict) -> list[str]:
    """Every name under which the company may be known (for domain discovery)."""
    siege = item.get("siege") or {}
    names: list[str | None] = [item.get("nom_raison_sociale"), siege.get("nom_commercial"), item.get("sigle")]
    names.extend(siege.get("liste_enseignes") or [])
    names.extend(e.get("nom_commercial") for e in item.get("matching_etablissements") or [])
    paren = re.search(r"\(([^)]+)\)\s*$", str(item.get("nom_complet") or ""))
    if paren:
        names.append(paren.group(1))
    seen: dict[str, str] = {}
    for n in names:
        if n and str(n).strip():
            seen.setdefault(str(n).strip().upper(), str(n).strip())
    return list(seen.values())


def person_leaders(item: dict) -> list[dict[str, str]]:
    """Natural-person leaders, statutory auditors excluded, birth data never kept."""
    out: list[dict[str, str]] = []
    for d in item.get("dirigeants") or []:
        role = str(d.get("qualite") or "")
        if d.get("type_dirigeant") != "personne physique" or "commissaire aux comptes" in role.lower():
            continue
        first = str(d.get("prenoms") or "").split(" ")[0].strip().title()
        last = str(d.get("nom") or "").strip().upper()
        if first or last:
            out.append({"first": first, "last": last, "role": role})
    return out


def _idcc(item: dict) -> set[str]:
    siege = item.get("siege") or {}
    return set(siege.get("liste_idcc") or []) | set((item.get("complements") or {}).get("liste_idcc") or [])


def _minimal_raw(item: dict, place: dict, leaders: list[dict[str, str]]) -> dict:
    tva = item.get("tva") or []
    return {
        "siret": place.get("siret"),
        "names": name_candidates(item),
        "tranche_effectif_salarie": item.get("tranche_effectif_salarie") or "NN",
        "categorie_entreprise": item.get("categorie_entreprise"),
        "naf25": item.get("activite_principale_naf25"),
        "idcc": sorted(_idcc(item)),
        "tva": tva[0] if isinstance(tva, list) and tva else None,
        "date_creation": item.get("date_creation"),
        "caractere_employeur": place.get("caractere_employeur"),
        "leaders_detail": leaders,
        SCORE_KEY: base_score(item, place),
    }


def base_score(item: dict, place: dict) -> int:
    """Research scoring signals (distance points are added at discovery time)."""
    tranche = item.get("tranche_effectif_salarie") or "NN"
    score = 3 if item.get("activite_principale") in CORE_NAF else 0
    score += 2 if SYNTEC_IDCC in _idcc(item) else 0
    score += 2 if tranche in TRANCHE_CODES and TRANCHE_CODES.index(tranche) >= TRANCHE_CODES.index("03") else 0
    score += 1 if place.get("caractere_employeur") == "O" else 0
    score += 1 if item.get("categorie_entreprise") in ("ETI", "GE") else 0
    return score


def _float(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _title_city(name: str) -> str:
    words = re.split(r"([\s-])", name.strip().lower())
    return "".join(w if (i and w in _SMALL_WORDS) else w[:1].upper() + w[1:] for i, w in enumerate(words))
