"""Detectors used by ``match_job``: school/CFA fake offers, sales jobs, contract type, experience, remote, level.

Pure functions on accent-free lowercase text. Research: docs/research/france-travail.md §5 (school spam),
linkedin.md §6.4 (classification regexes), ats-and-documents.md §6.3 (years / rhythm / duration).
"""

from __future__ import annotations

import re

from jobbot.matching._text import GENDER_RX, fold, lower_same_len, norm, prep
from jobbot.models import ContractType, JobOffer

# --------------------------------------------------------------------------- school / CFA "fake" offers

SCHOOL_BRANDS = (
    "iscod", "studi", "openclassrooms", "open classrooms", "ipac", "ipac bachelor factory", "mydigitalschool",
    "my digital school", "digital campus", "ifocop", "sup de vinci", "epsi", "3w academy", "wild code school",
    "simplon", "o'clock", "oclock", "esgi", "ynov", "epitech", "ifag", "iseg", "efrei", "hetic", "iscom", "pigier",
    "cesi", "nextformation", "ipssi", "doranco", "m2i formation", "aftec", "estiam", "ia school", "web school factory",
    "efficom", "ieft", "human booster", "diginamic", "institut f2i", "eni ecole", "the hacking project", "le wagon",
    "ironhack", "jedha", "nexa digital school", "ecole multimedia", "ingetis", "isitech", "afpa", "greta", "cnam",
    "ecole 89", "ecole it", "ecole o'clock", "3il", "cefii",
)
_SCHOOL_BRAND_RX = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(b) for b in SCHOOL_BRANDS) + r")(?![a-z0-9])")
_SCHOOL_COMPANY_RX = re.compile(
    r"\b(?:ecole|ecoles|school|cfa|academy|academie|centre de formation|organisme de formation|"
    r"institut de formation|business school|formation|formations)\b"
)
_SCHOOL_STRONG = re.compile(
    r"trouve[rz]? (?:ton|votre|une|ta) (?:future? )?entreprise|"
    r"(?:rejoins|rejoignez|integre|integrez|integrer|rejoindre) (?:notre|nos) (?:ecole|formation|campus|"
    r"bachelor|mastere|cursus|promo)|formation (?:100 ?%|entierement|totalement|integralement) "
    r"(?:financee|prise en charge|gratuite)|(?:sans|aucun|0 ?(?:€|euros?)? de) frais de (?:scolarite|formation)|"
    r"(?:nos|parmi nos|reseau d'|reseau de) entreprises partenaires|"
    r"(?:entretien|test|tests|dossier|jury|procedure) d'admission|"
    r"candidate[rz]? (?:a|pour) (?:notre|la) formation|\bnotre ecole\b|\bnos ecoles\b|\bnotre campus\b|"
    r"(?:obtiens|obtenez|decroche[rz]?) (?:ton|votre) (?:diplome|titre|bachelor|mastere)"
)
_SCHOOL_WEAK = re.compile(
    r"formation financee|prise en charge (?:par|de) l'opco|\btitre rncp\b|\bbachelor\b|\bmastere\b|\bnotre cfa\b|"
    r"places limitees|rentree (?:de |en )?(?:septembre|octobre|novembre|janvier|fevrier|mars)|\bpromotion\b|"
    r"\binscriptions?\b|frais de scolarite"
)
_SCHOOL_TITLE = re.compile(r"\b(?:bachelor|mastere|msc|mba|titre rncp|formation diplomante|formation certifiante)\b")


def _flat(text: str) -> str:
    return norm(text).replace("-", " ")


def school_offer_reason(job: JobOffer) -> str | None:
    """French reason when the offer is a school/CFA recruiting students rather than an employer, else None."""
    company, title = _flat(job.company), _flat(job.title)
    desc = _flat(job.description)
    brand = _SCHOOL_BRAND_RX.search(company) or _SCHOOL_BRAND_RX.search(title)
    if brand:
        return f"Écartée : offre publiée par une école / un organisme de formation ({brand.group(0).upper()})"
    if company and _SCHOOL_COMPANY_RX.search(company):
        return f"Écartée : l'annonceur « {job.company} » est une école / un organisme de formation"
    if job.raw.get("is_delegated") is True:
        return "Écartée : offre déléguée à un CFA (formation imposée par l'école)"
    strong = _SCHOOL_STRONG.search(desc) or _SCHOOL_TITLE.search(title)
    weak = {m.group(0) for m in _SCHOOL_WEAK.finditer(desc)}
    if strong or len(weak) >= 3:
        cue = strong.group(0) if strong else ", ".join(sorted(weak)[:3])
        return f"Écartée : offre d'école / de CFA déguisée en alternance (« {cue} »)"
    return None


# --------------------------------------------------------------------------- sales jobs

_SALES_TITLE = re.compile(
    r"\b(?:business\s+develop(?:er|per|peur|pement)s?|biz\s*dev|bizdev|business\s+development|"
    r"developpeur\s+(?:commercial|d'affaires|business(?!\s+intelligence)|"
    r"de\s+(?:reseau|franchise|clientele|marche|portefeuille|partenariats?|affaires|ventes?))|"
    r"developpement\s+commercial|charge\s+(?:de\s+developpement|d'affaires)|"
    r"sales|commercial(?:e)?\s+(?:sedentaire|terrain|b2b|itinerant)|key\s+account|account\s+(?:manager|executive)|"
    r"ingenieur\s+(?:commercial|d'affaires)|technico\s*commercial|conseiller\s+commercial|"
    r"developpeur\s+de\s+l'activite)\b"
)


def sales_title_reason(job: JobOffer) -> str | None:
    title = GENDER_RX.sub(" ", _flat(job.title))
    if _SALES_TITLE.search(" ".join(title.split())):
        return ("Écartée : poste commercial (business developer / développeur commercial), "
                "pas du développement logiciel")
    return None


# --------------------------------------------------------------------------- contract type

_ALT_TITLE = re.compile(
    r"\b(?:alternance|alternante?s?|apprentie?s?|apprentissage|contrat\s+(?:d'\s*)?apprentissage|"
    r"contrat\s+de\s+professionnalisation|contrat\s+pro|work\s*study|apprenticeship)\b"
)
# Any hint of a work-study contract, even loose (« Contrat : Apprentissage », « #alternance »): under an
# alternance-only search an unknown contract is ruled out only when none appears (a false hint such as
# « apprentissage automatique » merely leaves the offer to the normal scoring).
_ALT_CUE = re.compile(r"alternan|apprenti|professionnalisation|\bcontrat\s+pro\b|work\s*study|apprenticeship|"
                      r"ausbildung|\bdual(?:es)?\s+studium")
_ALT_DESC = re.compile(
    r"\ben\s+alternance\b|\balternante?s?\b|\bapprentie?s?\b|contrat\s+(?:d'\s*)?apprentissage|"
    r"contrat\s+de\s+professionnalisation|\bcontrat\s+pro\b|rythme\s+(?:d'|de\s+l')?alternance|"
    r"offre\s+d'alternance|\bwork\s*study\b|\bapprenticeship\b"
)
_STAGE = re.compile(r"\b(?:stage|stages|stagiaire|internship|intern|pfe)\b")
_CONTRACT_RULES = (
    (re.compile(r"\b(?:freelance|free lance|portage salarial|tjm)\b"), ContractType.FREELANCE),
    (re.compile(r"\b(?:interim|interimaire|travail temporaire)\b"), ContractType.INTERIM),
    (re.compile(r"\bcdi\b|duree indeterminee|permanent contract"), ContractType.CDI),
    (re.compile(r"\bcdd\b|duree determinee|fixed term"), ContractType.CDD),
)


def detect_contract(job: JobOffer) -> ContractType:
    """Contract type, recognising alternance from the title/description even when the source said CDD/OTHER."""
    if job.contract_type == ContractType.ALTERNANCE:
        return ContractType.ALTERNANCE
    title, desc = _flat(job.title), _flat(job.description)
    if _ALT_TITLE.search(title):
        return ContractType.ALTERNANCE
    if job.contract_type in (ContractType.OTHER, ContractType.CDD) and _ALT_DESC.search(desc):
        return ContractType.ALTERNANCE
    if job.contract_type != ContractType.OTHER:
        return job.contract_type
    if _STAGE.search(title):
        return ContractType.STAGE
    for text in (title, desc):
        for rx, contract in _CONTRACT_RULES:
            if rx.search(text):
                return contract
    return ContractType.OTHER


def mentions_alternance(job: JobOffer) -> bool:
    """The title or the description hints at a work-study contract, however loosely (see ``_ALT_CUE``)."""
    return bool(_ALT_CUE.search(_flat(f"{job.title}\n{job.description or ''}")))


# --------------------------------------------------------------------------- experience

_NUM_WORDS = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9,
    "dix": 10, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "seven": 7, "eight": 8, "ten": 10,
}
_NUM_WORD_RX = re.compile(r"\b(" + "|".join(_NUM_WORDS) + r")\b(?=\s*(?:\(\d+\)\s*)?\+?\s*(?:ans?|annees?|years?)\b)")
_YEARS_PATTERNS = (
    # "3 ans d'expérience", "3 à 5 ans d'expérience", "5+ ans d'expérience", "5 ans minimum d'expérience"
    re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:a|-|/|ou)\s*\d{1,2}\s*)?(?:ans?|annees?)\s*\+?\s*"
               r"(?:minimum\s+|min\s+|au moins\s+|environ\s+)?(?:d'|de\s+)?(?:experiences?|exp)\b"),
    # "expérience significative de 5 ans", "Expérience : 3 ans", "expérience exigée de 2 an(s)"
    re.compile(r"\bexperiences?\s+(?:(?:professionnelle|significative|confirmee|reussie|minimum|minimale|requise|"
               r"exigee|souhaitee|similaire|equivalente)\s+){0,3}(?:de\s+|d'au moins\s+|d'environ\s+|:\s*|minimum\s+|"
               r"superieure a\s+|>\s*)?(?:minimum\s+|au moins\s+|plus de\s+)?(\d{1,2})\s*\+?\s*(?:ans?|annees?)\b"),
    # "3+ years of experience", "5 years experience", "2-4 years of professional experience"
    re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|to)\s*\d{1,2}\s*)?(?:years?|yrs?)\s+(?:of\s+)?(?:[a-z]+\s+){0,2}?"
               r"(?:experience|exp)\b"),
    re.compile(r"\b(?:at least|minimum of)\s+(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b"),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)\b"),
)
_LOOSE_YEARS = re.compile(r"\b(?:minimum|au moins)\s+(\d{1,2})\s*\+?\s*(?:ans?|annees?)\b|"
                          r"\b(\d{1,2})\s*(?:ans?|annees?)\s+minimum\b")
_NOT_DURATION = re.compile(r"contrat|duree|alternance|formation|cursus|cycle|diplome|\bage\b|ecole|mois|\bcdd\b|stage")
_NOT_CANDIDATE = re.compile(r"(?:fort|forte|forts|fortes) de\s*$|(?:tuteur|tutrice|maitre d'apprentissage|mentor|"
                            r"encadre par|accompagne par|epaule par)[^.]{0,60}$|\bdepuis\s*$")
_LESS_THAN = re.compile(r"(?:moins de|less than|jusqu'a|up to|under)\s*$")


def _experience_text(text: str) -> str:
    t = lower_same_len(fold(prep(text or ""))).replace("\n", " ")
    return _NUM_WORD_RX.sub(lambda m: str(_NUM_WORDS[m.group(1)]), " ".join(t.split()))


def _accept_years(t: str, start: int) -> bool:
    before = t[max(0, start - 80) : start]
    return not (_NOT_CANDIDATE.search(before) or _LESS_THAN.search(before[-20:]))


def required_experience_years(text: str) -> int | None:
    """Minimum years of experience asked by an offer (FR + EN phrasings), None when not stated."""
    t = _experience_text(text)
    found: list[int] = []
    for rx in _YEARS_PATTERNS:
        for m in rx.finditer(t):
            if _accept_years(t, m.start()):
                found.append(int(m.group(1)))
    for m in _LOOSE_YEARS.finditer(t):
        window = t[max(0, m.start() - 50) : m.end() + 50]
        if _accept_years(t, m.start()) and not _NOT_DURATION.search(window):
            found.append(int(m.group(1) or m.group(2)))
    plausible = [y for y in found if y <= 25]
    return max(plausible) if plausible else None


JUNIOR_RX = re.compile(
    r"\bjunior\b|\bdebutante?s?\b|jeunes? diplomee?s?|premiere experience|sans experience|entry level|\bgraduate\b|"
    r"pas d'experience (?:requise|exigee)|aucune experience"
)


def junior_friendly(text: str) -> bool:
    return bool(JUNIOR_RX.search(_flat(text)))


# --------------------------------------------------------------------------- remote, level, duration

FULL_REMOTE_RX = re.compile(
    r"full\s*remote|100\s*%\s*(?:en\s+)?(?:teletravail|remote|a distance)|teletravail\s+(?:complet|total|integral)|"
    r"entierement\s+(?:en\s+)?(?:teletravail|a distance)|fully\s+remote|remote\s+first|remote\s+only"
)
REMOTE_RX = re.compile(r"\bteletravail\b|\bremote\b|\ba distance\b|\bhybride\b|\bhome office\b")
BAC5_RX = re.compile(r"bac\s*\+\s*5|\bbac5\b|\bmaster\b|\bmastere\b|niveau 7|\bm2\b|\bmsc\b|diplome d'ingenieur|"
                     r"ecole d'ingenieurs?|cycle ingenieur|titre d'ingenieur")
BAC3_RX = re.compile(r"bac\s*\+\s*3|\blicence\b|\bbachelor\b|niveau 6|\bb\.?u\.?t\.? informatique")
BAC2_RX = re.compile(r"bac\s*\+\s*2|\bbts\b|\bdut\b|\bdeust\b|niveau 5")
LONG_CONTRACT_RX = re.compile(r"\b(?:24|36)\s*mois\b|\b(?:2|3|deux|trois)\s*ans\b")
SHORT_CONTRACT_RX = re.compile(r"\b(?:6|12)\s*mois\b|\b(?:1|un)\s*an\b")


def is_full_remote(job: JobOffer) -> bool:
    return bool(FULL_REMOTE_RX.search(_flat(f"{job.title} {job.description}")))


def mentions_remote(job: JobOffer) -> bool:
    return bool(job.remote) or bool(REMOTE_RX.search(_flat(f"{job.title} {job.description}")))


def contains(rx: re.Pattern[str], text: str) -> bool:
    return bool(rx.search(_flat(text)))
