"""More deterministic form rules: salary, availability, work conditions, languages, degrees, alternance…

Same contract as ``jobbot.apply.answer_rules``: ``rule(RuleInput) -> Candidate | None``.
"""

from __future__ import annotations

import re

from jobbot.apply.answer_rules import Candidate, RuleInput, is_yes_no_question
from jobbot.apply.coerce import is_binary, level_option, prefer_not_option
from jobbot.config import AlternanceInfo, Experience, Profile
from jobbot.models import ContractType, JobOffer
from jobbot.textutil import normalize


def yes_no_expected(r: RuleInput) -> bool:
    """True when the field expects a yes/no answer (binary options, bare radio/checkbox, yes/no question)."""
    if r.options:
        return is_binary(r.options)
    return r.kind in ("radio", "checkbox") or is_yes_no_question(r.text)


def is_alternance(profile: Profile, job: JobOffer | None) -> bool:
    if job is not None and job.contract_type != ContractType.OTHER:
        return job.contract_type in (ContractType.ALTERNANCE, ContractType.STAGE)
    return bool(profile.target.contract_types) and profile.target.contract_types[0] == ContractType.ALTERNANCE


def _answer(profile: Profile, key: str) -> str:
    return (profile.answers.get(key) or "").strip()


# --------------------------------------------------------------------------- salary & availability

_SALARY = re.compile(r"salaire|salary|remuneration|pretentions?|compensation|pay expectation|expected pay|package")
_NOTICE = re.compile(r"preavis|notice|delai de prevenance")
_AVAILABILITY = re.compile(
    r"disponib|date de (debut|demarrage|prise de poste)|start date|when can you start|earliest start|"
    r"available to start|a partir de quand|quand (pouvez|pourriez) vous (commencer|demarrer)|date d entree"
)


def salary(r: RuleInput) -> Candidate | None:
    if not _SALARY.search(r.text):
        return None
    key = "salary_expectation_alternance" if is_alternance(r.profile, r.job) else "salary_expectation_cdi"
    value = _answer(r.profile, key)
    return Candidate((value,), 0.85, "profile") if value else None


def notice(r: RuleInput) -> Candidate | None:
    if not _NOTICE.search(r.text):
        return None
    value = _answer(r.profile, "notice_period")
    zero = "0" if re.search(r"immediat|aucun|none|^0", normalize(value)) else ""
    return Candidate(tuple(v for v in (value, zero) if v), 0.85, "profile") if value else None


def availability(r: RuleInput) -> Candidate | None:
    if not _AVAILABILITY.search(r.text):
        return None
    if yes_no_expected(r):
        return Candidate(("Oui",), 0.7)
    alt = r.profile.alternance
    value = _answer(r.profile, "availability_date") or (alt.start_date if alt else "")
    return Candidate((value,), 0.85, "profile") if value else None


# --------------------------------------------------------------------------- work conditions

_SIMPLE_ANSWERS: tuple[tuple[re.Pattern[str], str, float], ...] = tuple(
    (re.compile(pattern), key, conf)
    for pattern, key, conf in (
        (r"autoris\w*(?: e)? a travailler|autorisation de travail|work authori[sz]ation|authori[sz]ed to work|"
         r"legally (authori[sz]ed|allowed|eligible|entitled)|eligible to work|droit de travailler|permis de travail|"
         r"titre de sejour|right to work", "work_authorization", 0.9),
        (r"sponsor|\bvisa\b", "requires_sponsorship", 0.9),
        (r"relocat|demenag|mobilite geographique|willing to move|prepared to move", "willing_to_relocate", 0.85),
    )
)
_ONSITE = re.compile(
    r"sur site|on ?site|presentiel|au bureau|in the office|office based|commut|trajet|se rendre|hybrid|hybride|"
    r"travailler (a|en|dans|sur) |work(ing)? (in|at|from) "
)
_REMOTE = re.compile(r"teletravail|remote|a distance|work from home")


def simple_answers(r: RuleInput) -> Candidate | None:
    for pattern, key, conf in _SIMPLE_ANSWERS:
        if pattern.search(r.text):
            value = _answer(r.profile, key)
            return Candidate((value,), conf, "profile") if value else None
    return None


def workplace(r: RuleInput) -> Candidate | None:
    remote, onsite = _REMOTE.search(r.text), _ONSITE.search(r.text)
    if not (remote or onsite):
        return None
    if yes_no_expected(r):
        ok = r.profile.target.remote_ok if remote and not onsite else True
        return Candidate(("Oui" if ok else "Non",), 0.75)
    pref = _answer(r.profile, "remote_preference")
    return Candidate(tuple(v for v in (pref, "Hybride") if v), 0.7, "profile")


# --------------------------------------------------------------------------- languages

_LEVEL_WORDS: tuple[tuple[int, re.Pattern[str]], ...] = tuple(
    (level, re.compile(pattern))
    for level, pattern in (
        (5, r"native|bilingu|maternelle|\bc2\b|natale"),
        (4, r"fluent|full professional|\bc1\b|avance|advanced|excellent|tres bon"),
        (3, r"professional working|professionnel|professional|courant|\bb2\b|upper intermediate|\bgood\b|\bbon\b"),
        (2, r"intermediate|intermediaire|conversational|conversation|limited working|\bb1\b|moyen|scolaire"),
        (1, r"debutant|beginner|elementary|elementaire|basic|notions|\ba1\b|\ba2\b|limited|faible"),
        (0, r"^none$|^aucun|^nul|no proficiency|pas du tout"),
    )
)
_LANGS = {
    "anglais": ("anglais", "english"), "francais": ("francais", "french"), "allemand": ("allemand", "german"),
    "espagnol": ("espagnol", "spanish"), "italien": ("italien", "italian"), "luxembourgeois": ("luxembourgeois",),
}
_LANG_QUESTION = re.compile(r"niveau|level|proficien|parlez|speak|maitris|fluent|courant|langue|language")


def lang_level(text: str) -> int | None:
    t = normalize(text)
    return next((level for level, pattern in _LEVEL_WORDS if pattern.search(t)), None)


def _profile_lang(profile: Profile, lang: str) -> tuple[int, str]:
    """(level 0-5, level text) of a language in the profile; (0, "") when absent."""
    if lang == "anglais" and _answer(profile, "english_level"):
        text = _answer(profile, "english_level")
        return lang_level(text) or 0, text
    for language in profile.languages:
        if normalize(language.name) in _LANGS.get(lang, (lang,)):
            return lang_level(language.level) or 0, language.level
    return 0, ""


def language(r: RuleInput) -> Candidate | None:
    lang = next((k for k, names in _LANGS.items() if any(re.search(rf"\b{n}\b", r.text) for n in names)), None)
    if lang is None or not (_LANG_QUESTION.search(r.text) or r.options):
        return None
    have, text = _profile_lang(r.profile, lang)
    if yes_no_expected(r):
        asked = lang_level(r.text) or 2
        return Candidate(("Oui" if have >= asked else "Non",), 0.85)
    if r.options:
        option = level_option(r.options, have, lang_level)
        return Candidate((option,), 0.85) if option else None
    return Candidate((text,), 0.8, "profile") if text else None


# --------------------------------------------------------------------------- degrees

_DEGREE_LEVELS: tuple[tuple[int, re.Pattern[str]], ...] = tuple(
    (level, re.compile(pattern))
    for level, pattern in (
        (8, r"doctor|phd|bac ?\+ ?8"),
        (5, r"master|bac ?\+ ?5|\bmba\b|ingenieur|engineer|niveau 7|level 7"),
        (4, r"bac ?\+ ?4|maitrise|\bm1\b"),
        (3, r"bachelor|licence|bac ?\+ ?3|\bbut\b|niveau 6|level 6"),
        (2, r"bac ?\+ ?2|\bbts\b|\bdut\b|deug|associate"),
        (0, r"\bbac\b|baccalaureat|high school|lycee|secondary"),
    )
)
_DEGREE = re.compile(r"diplome|degree|niveau d etudes?|education|highest level|bac ?\+|bachelor|master|licence|doctorat|phd")


def degree_level(text: str) -> int | None:
    """Level of the first degree mentioned in ``text`` (leftmost match wins)."""
    t = normalize(text)
    hits = [(m.start(), -level) for level, pattern in _DEGREE_LEVELS for m in [pattern.search(t)] if m]
    return -min(hits)[1] if hits else None  # same position ("bac+3" vs "bac"): the higher level wins


def highest_degree(profile: Profile) -> int | None:
    level = degree_level(_answer(profile, "highest_degree"))
    if level is not None:
        return level
    done = [degree_level(e.degree) for e in profile.education if e.end and not re.search(r"en cours|recherch", e.degree)]
    return max((lvl for lvl in done if lvl is not None), default=None)


def degree(r: RuleInput) -> Candidate | None:
    have = highest_degree(r.profile)
    if not _DEGREE.search(r.text) or have is None:
        return None
    if yes_no_expected(r):
        asked = degree_level(r.text)
        if asked is None:
            return None
        return Candidate(("Oui" if asked <= have else "Non",), 0.85)
    if r.options:
        option = level_option(r.options, have, degree_level)
        return Candidate((option,), 0.85) if option else None
    value = _answer(r.profile, "highest_degree")
    return Candidate((value,), 0.85, "profile") if value else None


# --------------------------------------------------------------------------- alternance

_ENROLLED = re.compile(r"(inscrit|admis|enrolled|accepted|trouve).*(ecole|school|formation|cfa|programme|program)")
_SCHOOL = re.compile(r"ecole|school|organisme de formation|\bcfa\b|etablissement de formation|campus")
_PROGRAM = re.compile(
    r"(diplome|formation|titre|programme|program|degree|cursus).*(prepar|vise|en cours|suiv|pursu|enrolled)|"
    r"(prepar|suivez|pursu).*(diplome|formation|titre|programme|program|degree)"
)
_DURATION = re.compile(r"duree|duration|length|combien de mois|how many months|nombre de mois")


def _rncp(profile: Profile) -> str:
    alt = profile.alternance
    m = re.search(r"RNCP\s*(\d{3,6})", alt.degree if alt else "")
    if m:
        return f"RNCP {m.group(1)}"
    return f"RNCP niveau {alt.rncp_level}" if alt and alt.rncp_level else ""


def _alt_rhythm_rncp(r: RuleInput, alt: AlternanceInfo) -> Candidate | None:
    parts = []
    if re.search(r"rythme|rhythm|calendrier", r.text):
        parts.append(alt.rhythm)
    if "rncp" in r.text:
        parts.append(_rncp(r.profile))
    value = " — ".join(p for p in parts if p)
    return Candidate((value,), 0.85, "profile") if value else None


def _alt_duration(r: RuleInput, alt: AlternanceInfo) -> Candidate | None:
    if re.search(r"alternance|apprentissage|apprenti|contrat|contract", r.text) and _DURATION.search(r.text):
        return Candidate((f"{alt.duration_months} mois", str(alt.duration_months)), 0.85, "profile")
    return None


def _alt_enrolled(r: RuleInput, alt: AlternanceInfo) -> Candidate | None:
    if _ENROLLED.search(r.text) and yes_no_expected(r):
        return Candidate(("Oui" if alt.school else "Non",), 0.8)
    return None


def _alt_program(r: RuleInput, alt: AlternanceInfo) -> Candidate | None:
    if _PROGRAM.search(r.text) and alt.degree and not yes_no_expected(r):
        return Candidate((alt.degree,), 0.8, "profile")
    return None


def _alt_school(r: RuleInput, alt: AlternanceInfo) -> Candidate | None:
    if _SCHOOL.search(r.text) and alt.school and not yes_no_expected(r):
        return Candidate((alt.school,), 0.8, "profile")
    return None


def alternance(r: RuleInput) -> Candidate | None:
    alt = r.profile.alternance
    if alt is None:
        return None
    for sub in (_alt_rhythm_rncp, _alt_duration, _alt_enrolled, _alt_program, _alt_school):
        cand = sub(r, alt)
        if cand is not None:
            return cand
    return None


# --------------------------------------------------------------------------- misc

_DRIVING = re.compile(r"permis|driving licen|driver s licen|drivers licen|vehicule|vehicle|voiture|\bcar\b")
_ADULT = re.compile(r"18 ans|18 years|majeur|at least 18|over 18|plus de 18|age legal")
_HOW_HEARD = re.compile(
    r"comment avez vous (connu|entendu|trouve|decouvert)|how did you (hear|find|learn|discover)|"
    r"ou avez vous (vu|trouve)|source de (l offre|candidature)|referral source"
)
_CURRENT = re.compile(
    r"(entreprise|employeur|societe|poste|titre|intitule) actuel|current (company|employer|title|position|job)"
)
_DIVERSITY = re.compile(r"genre|gender|\bsexe\b|\bsex\b|ethni|\brace\b|disabilit|handicap|rqth|veteran|orientation sexuelle")
SOURCE_LABELS = {
    "linkedin": "LinkedIn", "hellowork": "HelloWork", "indeed": "Indeed", "wttj": "Welcome to the Jungle",
    "france_travail": "France Travail", "lba": "La Bonne Alternance", "apec": "Apec", "free_work": "Free-Work",
}


def driving(r: RuleInput) -> Candidate | None:
    if not _DRIVING.search(r.text):
        return None
    if yes_no_expected(r):
        return Candidate((_answer(r.profile, "driving_license") or "Oui",), 0.85, "profile")
    value = r.profile.identity.driving_license or _answer(r.profile, "driving_license")
    return Candidate((value,), 0.8, "profile") if value else None


def adult(r: RuleInput) -> Candidate | None:
    return Candidate(("Oui",), 0.9) if _ADULT.search(r.text) else None


def how_heard(r: RuleInput) -> Candidate | None:
    if not _HOW_HEARD.search(r.text):
        return None
    source = SOURCE_LABELS.get(r.job.source, "") if r.job else ""
    values = tuple(v for v in (source, _answer(r.profile, "how_did_you_hear"), "Site d'offres d'emploi", "Autre", "Other") if v)
    return Candidate(values, 0.8, "profile")


def _current_experience(profile: Profile) -> Experience | None:
    ongoing = [e for e in profile.experiences if not e.end]
    return (ongoing or profile.experiences or [None])[0]


def current_job(r: RuleInput) -> Candidate | None:
    exp = _current_experience(r.profile) if _CURRENT.search(r.text) else None
    if exp is None:
        return None
    wants_company = re.search(r"entreprise|employeur|societe|company|employer", r.text)
    return Candidate((exp.company if wants_company else exp.title,), 0.75, "profile")


def diversity(r: RuleInput) -> Candidate | None:
    if not _DIVERSITY.search(r.text) or not r.options:
        return None
    option = prefer_not_option(r.options)
    return Candidate((option,), 0.8) if option else None
