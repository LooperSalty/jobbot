"""French regions and big-city areas named instead of a precise place (offline, deterministic).

Job boards — LinkedIn above all — often place an offer at region level (« Île-de-France, France », « Région
parisienne », « Grand Est ») or around a big city (« Lyon et périphérie », « Grand Lyon », « Toulouse Métropole »).
``vague_place`` recognises those forms so that the location fit decides by region membership or by distance to
the city instead of « localisation à vérifier ». Foreign countries are told by ``target_rules.offer_country``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from jobbot.matching._text import norm
from jobbot.matching.geo import KNOWN_PLACES

REGIONS: dict[str, tuple[str, ...]] = {
    "Auvergne-Rhône-Alpes": ("01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"),
    "Bourgogne-Franche-Comté": ("21", "25", "39", "58", "70", "71", "89", "90"),
    "Bretagne": ("22", "29", "35", "56"),
    "Centre-Val de Loire": ("18", "28", "36", "37", "41", "45"),
    "Corse": ("2A", "2B"),
    "Grand Est": ("08", "10", "51", "52", "54", "55", "57", "67", "68", "88"),
    "Hauts-de-France": ("02", "59", "60", "62", "80"),
    "Île-de-France": ("75", "77", "78", "91", "92", "93", "94", "95"),
    "Normandie": ("14", "27", "50", "61", "76"),
    "Nouvelle-Aquitaine": ("16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"),
    "Occitanie": ("09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"),
    "Pays de la Loire": ("44", "49", "53", "72", "85"),
    "Provence-Alpes-Côte d'Azur": ("04", "05", "06", "13", "83", "84"),
    "Guadeloupe": ("971",), "Martinique": ("972",), "Guyane": ("973",), "La Réunion": ("974",),
    "Mayotte": ("976",),
}
PARIS_REGION = "Île-de-France"
OLD_REGIONS: dict[str, tuple[str, ...]] = {  # pre-2016 regions and usual short names
    "alsace": ("67", "68"), "lorraine": ("54", "55", "57", "88"), "champagne ardenne": ("08", "10", "51", "52"),
    "aquitaine": ("24", "33", "40", "47", "64"), "limousin": ("19", "23", "87"),
    "poitou charentes": ("16", "17", "79", "86"), "auvergne": ("03", "15", "43", "63"),
    "rhone alpes": ("01", "07", "26", "38", "42", "69", "73", "74"), "bourgogne": ("21", "58", "71", "89"),
    "franche comte": ("25", "39", "70", "90"), "languedoc roussillon": ("11", "30", "34", "48", "66"),
    "midi pyrenees": ("09", "12", "31", "32", "46", "65", "81", "82"), "nord pas de calais": ("59", "62"),
    "picardie": ("02", "60", "80"), "basse normandie": ("14", "50", "61"), "haute normandie": ("27", "76"),
    "paca": REGIONS["Provence-Alpes-Côte d'Azur"], "sud": REGIONS["Provence-Alpes-Côte d'Azur"],
    "idf": REGIONS[PARIS_REGION], "centre": REGIONS["Centre-Val de Loire"],
    "ara": REGIONS["Auvergne-Rhône-Alpes"], "bfc": REGIONS["Bourgogne-Franche-Comté"], "reunion": ("974",),
}
DEPARTMENT_REGION = {dept: name for name, depts in REGIONS.items() for dept in depts}
# French big cities whose name is used as an area ("Lyon et périphérie") → department (coordinates: geo.KNOWN_PLACES).
METRO_DEPARTMENTS: dict[str, str] = {
    "metz": "57", "nancy": "54", "thionville": "57", "strasbourg": "67", "mulhouse": "68", "colmar": "68",
    "reims": "51", "troyes": "10", "dijon": "21", "besancon": "25", "lyon": "69", "grenoble": "38",
    "saint etienne": "42", "clermont ferrand": "63", "marseille": "13", "aix en provence": "13", "nice": "06",
    "toulon": "83", "avignon": "84", "montpellier": "34", "nimes": "30", "perpignan": "66", "toulouse": "31",
    "bordeaux": "33", "pau": "64", "la rochelle": "17", "poitiers": "86", "limoges": "87", "nantes": "44",
    "angers": "49", "le mans": "72", "rennes": "35", "brest": "29", "tours": "37", "orleans": "45", "rouen": "76",
    "le havre": "76", "caen": "14", "lille": "59", "amiens": "80",
}
_METRO_ALIASES = {"aix marseille": "marseille", "aix marseille provence": "marseille", "clermont auvergne":
                  "clermont ferrand", "lyonnaise": "lyon", "lilloise": "lille", "toulousaine": "toulouse",
                  "bordelaise": "bordeaux", "nantaise": "nantes", "marseillaise": "marseille",
                  "strasbourgeoise": "strasbourg", "grenobloise": "grenoble", "rennaise": "rennes",
                  "montpellieraine": "montpellier", "nicoise": "nice", "messine": "metz", "nanceienne": "nancy",
                  "rouennaise": "rouen", "remoise": "reims", "dijonnaise": "dijon", "parisienne": "paris"}
_AREA_WORDS = r"peripherie|agglomeration|environs|alentours|banlieue|region"
_METRO_FORMS = (
    re.compile(rf"^(?P<city>.+?) et (?:sa |ses |son )?(?:{_AREA_WORDS})$"),
    re.compile(r"^(?:region|agglomeration|metropole europeenne|eurometropole|metropole|aire urbaine|bassin|grand|"
               r"greater|communaute urbaine|banlieue) (?:de |d |du |des )?(?P<city>.+?)"
               r"(?: metropolitan)?(?: area| region| metropole)?$"),
    re.compile(r"^(?P<city>.+?) (?:metropole|metropolitan area|metropolitan region|area|agglomeration)$"),
)
_AMBIGUOUS_SHORT_NAMES = frozenset({"sud", "centre", "ara", "bfc"})  # accepted in relocation_regions only
_COUNTRY_PARTS = frozenset({"france", "fr", "france metropolitaine"})
_COUNTRY_ONLY = frozenset({"", "france", "fr", "france entiere", "toute la france", "france metropolitaine"})


def key(text: str | None) -> str:
    """Accent-free lowercase words: 'Île-de-France' → 'ile de france', "Côte d'Azur" → 'cote d azur'."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", norm(text or "")).split())


REGION_KEYS: dict[str, tuple[str, ...]] = {**{key(name): depts for name, depts in REGIONS.items()}, **OLD_REGIONS}
_VAGUE_REGIONS: dict[str, tuple[str, ...]] = {
    **{k: depts for k, depts in REGION_KEYS.items() if k not in _AMBIGUOUS_SHORT_NAMES},
    "cote d azur": ("06", "83"), "provence alpes cote azur": REGIONS["Provence-Alpes-Côte d'Azur"],
}


@dataclass(frozen=True)
class VaguePlace:
    label: str  # as written in the offer ("Île-de-France", "Lyon et périphérie")
    departments: tuple[str, ...]  # departments the offer may be in
    center: tuple[float, float] | None = None  # big-city area: the city centre; None for a whole region


def region_of(department: str | None) -> str | None:
    """Current region name of a French department code, None when unknown."""
    return DEPARTMENT_REGION.get((department or "").upper())


def place_label(location: str | None) -> str:
    """The location without its trailing country: 'Île-de-France, France' → 'Île-de-France'."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    while len(parts) > 1 and key(parts[-1]) in _COUNTRY_PARTS:
        parts.pop()
    return ", ".join(parts)


def is_country_only(location: str | None) -> bool:
    """'France', 'France entière', '' — the offer names no place at all inside the country."""
    return key(re.sub(r"\([^)]*\)", " ", location or "")) in _COUNTRY_ONLY


@lru_cache(maxsize=2048)
def vague_place(location: str | None) -> VaguePlace | None:
    """Region or big-city area named as the offer's place (first component, country dropped), else None."""
    label = place_label(location).split(",")[0].strip()
    k = key(re.sub(r"\([^)]*\)", " ", label))
    if not k:
        return None
    departments = _region_departments(k)
    if departments:
        return VaguePlace(label, departments)
    return _metro(label, k)


def _region_departments(k: str) -> tuple[str, ...]:
    variants = (k, re.sub(r"^region (?:de la |de l |du |de |d )?", "", k), re.sub(r" region$", "", k))
    return next((_VAGUE_REGIONS[v] for v in variants if v in _VAGUE_REGIONS), ())


def _metro(label: str, k: str) -> VaguePlace | None:
    for form in _METRO_FORMS:
        m = form.match(k)
        if m is None:
            continue
        city = _METRO_ALIASES.get(m.group("city"), m.group("city"))
        if city == "paris":
            return VaguePlace(label, REGIONS[PARIS_REGION])
        if city in METRO_DEPARTMENTS and city in KNOWN_PLACES:
            return VaguePlace(label, (METRO_DEPARTMENTS[city],), KNOWN_PLACES[city])
    return None
