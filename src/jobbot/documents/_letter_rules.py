"""Cover letter: shared context, fixed parts (subject, greeting, closing, e-mail) and the deterministic template.

Structure vous / moi / nous (research §6.6). For an alternance, the first paragraph states the programme, the
school, the RNCP level, the rhythm, the 24-month duration and the start date (WTTJ / France Travail). The
apprenticeship-aid sentence (decree 2026-168) is only added before 1 January 2027, and with its amount only when
the company is known to have fewer than 250 employees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from jobbot import clock
from jobbot.config import Profile
from jobbot.documents._cv_rules import (
    HEADLINE_SEPARATOR,
    is_alternance_context,
    join_fr,
    key_skills,
    make_input,
    title_in_sentence,
)
from jobbot.documents._format import display_url, has_started, month_label
from jobbot.documents._truth import TruthBase
from jobbot.matching import lexicon
from jobbot.matching._text import display_title
from jobbot.matching.relayed import is_relayed
from jobbot.models import Company, JobOffer, KeywordHit

GREETING = "Madame, Monsieur,"
CLOSING = "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées."
OPT_OUT = ("Si vous ne souhaitez plus recevoir de message de ma part, répondez simplement « STOP » : "
           "je ne vous recontacterai pas.")
AID_DEADLINE = datetime(2027, 1, 1, tzinfo=clock.PARIS)
AID_LEVELS = {"6", "7"}
SME_LIMIT = 250
MAX_EMAIL_WORDS = 150
MAX_LETTER_SKILLS = 7
_VOWEL_START = re.compile(r"^[aeiouyâàéèêîïôûh]", re.I)
_SPACE_CLASS = "[ \u00a0\u202f]"
_NUMBER_RX = re.compile(rf"\d(?:\d|{_SPACE_CLASS})*\d|\d")
_HEADCOUNT_RX = re.compile(  # "45 collaborateurs", "1 200 salariés" (users / agents are not employees)
    rf"(?<![\w.,])(\d{{1,3}}(?:{_SPACE_CLASS}\d{{3}})+|\d+)\s*(?:collaborat|salari|employ)", re.I
)


@dataclass(frozen=True)
class LetterInput:
    profile: Profile
    job: JobOffer | None
    company: Company | None
    keywords: list[KeywordHit]
    truth: TruthBase
    alternance: bool
    company_name: str
    role: str  # offer title (display form) or the candidate's target title
    skills: list[str]  # offer skills the candidate has (catalogue spelling), most important first
    qualities: list[str]  # profile qualities, those the offer asks for first
    now: datetime


def _role(profile: Profile, job: JobOffer | None) -> str:
    if job is not None:
        shown = display_title(job.title) or job.title
        return shown[:1].upper() + (shown[1:].lower() if shown.isupper() else shown[1:])
    return profile.headline.split(HEADLINE_SEPARATOR)[0].strip()


def make_letter_input(profile: Profile, job: JobOffer | None, company: Company | None,
                      keywords: list[KeywordHit] | None) -> LetterInput:
    kws = list(keywords or [])
    tailor_input = make_input(profile, kws, job, company)
    skills = key_skills(tailor_input, "", limit=MAX_LETTER_SKILLS)
    if len(skills) < 3:
        extra = [i for g in profile.skills for i in g.items if i not in skills]
        skills = (skills + extra)[:3]
    name = (job.company if job is not None and job.company else "") or (company.name if company else "")
    return LetterInput(
        profile=profile, job=job, company=company, keywords=kws, truth=tailor_input.truth,
        alternance=is_alternance_context(profile, job), company_name=name.strip(), role=_role(profile, job),
        skills=skills, qualities=_ranked_qualities(profile, tailor_input.truthful), now=clock.to_paris(clock.now()),
    )


def _ranked_qualities(profile: Profile, truthful: list[KeywordHit]) -> list[str]:
    """Profile qualities, the ones the offer asks for first ("autonome" ↔ keyword "Autonomie")."""
    wanted = {lexicon.canonicalize(k.keyword) for k in truthful if k.category == "soft"}

    def asked(quality: str) -> bool:
        return bool(lexicon.find_soft_skills(quality) & wanted)

    return sorted(profile.qualities, key=lambda q: not asked(q))


# --------------------------------------------------------------------------- fixed parts


def _level(inp: LetterInput) -> str:
    alt = inp.profile.alternance
    match = re.search(r"bac\s*\+\s*\d", alt.degree if alt else "", re.I)
    return match.group(0).replace(" ", "") if match else ""


def lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


def _job_subject(inp: LetterInput, job: JobOffer) -> str:
    if inp.alternance:
        return f"Candidature – {inp.role} en alternance"
    contract = job.contract_type.value.upper() if job.contract_type.value in ("cdi", "cdd") else ""
    return f"Candidature – {inp.role}" + (f" ({contract})" if contract else "")


def _spontaneous_subject(inp: LetterInput) -> str:
    alt = inp.profile.alternance
    if not inp.alternance or alt is None:
        return f"Candidature spontanée – {inp.role}"
    months = f"{alt.duration_months} mois" if alt.duration_months else ""
    details = ", ".join(x for x in (_level(inp), months) if x)
    when = f" dès {month_label(alt.start_date)}" if alt.start_date and not _started(inp) else ""
    return f"Candidature spontanée – Alternance {inp.role}" + (f" ({details})" if details else "") + when


def build_subject(inp: LetterInput) -> str:
    return _job_subject(inp, inp.job) if inp.job is not None else _spontaneous_subject(inp)


def start_phrase(inp: LetterInput) -> str:
    """'à partir d'octobre 2026' before the start date, 'depuis octobre 2026' after it, '' when unknown."""
    alt = inp.profile.alternance
    start = month_label(alt.start_date) if alt is not None else ""
    if not start:
        return ""
    return f"depuis {start}" if _started(inp) else f"à partir {_de(start)}"


def email_subject(inp: LetterInput, subject: str) -> str:
    return f"{subject} – {inp.profile.identity.full_name}"


def _started(inp: LetterInput) -> bool:
    alt = inp.profile.alternance
    return alt is not None and has_started(alt.start_date, inp.now)


def _as_int(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def company_size(inp: LetterInput) -> int | None:
    """Head count: largest bound of the company's size label ("20 à 49 salariés" → 49), else the size stated in
    the offer ("ESN de 45 collaborateurs" → 45; users or agents are not employees)."""
    label = (inp.company.headcount_range if inp.company else None) or ""
    numbers = [_as_int(n) for n in _NUMBER_RX.findall(label)]
    if not numbers and inp.job is not None:
        numbers = [_as_int(m.group(1)) for m in _HEADCOUNT_RX.finditer(inp.job.description or "")]
    return max(numbers) if numbers else None


def aid_sentence(inp: LetterInput, *, with_amount: bool) -> str:
    alt = inp.profile.alternance
    if not inp.alternance or alt is None or inp.now >= AID_DEADLINE:
        return ""
    base = ("un contrat débutant avant le 1er janvier 2027 ouvre droit à l'aide de l'État à l'embauche d'un "
            "apprenti")
    size = company_size(inp)
    if with_amount and size is not None and size < SME_LIMIT and alt.rncp_level in AID_LEVELS:
        level = _level(inp) or "Bac+3 à Bac+5"
        return (f"Pour information, {base} (2 000 € pour un diplôme {level} dans une entreprise de moins de "
                "250 salariés).")
    return "" if with_amount else f"{base[:1].upper()}{base[1:]}."


# --------------------------------------------------------------------------- deterministic paragraphs


def _de(label: str) -> str:
    return f"d'{label}" if _VOWEL_START.match(label) else f"de {label}"


def _programme(inp: LetterInput) -> str:
    alt = inp.profile.alternance
    assert alt is not None
    name, _, rest = alt.degree.partition("(")
    details = rest.rstrip(") ").strip()
    title = f"le titre « {name.strip()} »" + (f" ({details})" if details else "")
    months = f" sur {alt.duration_months} mois" if alt.duration_months else ""
    rhythm = f" au rythme de {alt.rhythm}" if alt.rhythm else ""
    when = start_phrase(inp)
    lead = f"{when[:1].upper()}{when[1:]}, je prépare" if when else "Je prépare"
    return f"{lead} {title} à {alt.school}, en alternance{months}{rhythm}."


def relay_sentence(inp: LetterInput) -> str:
    """« J'ai pris connaissance de votre offre par l'intermédiaire de <école>. » for an offer relayed by the
    candidate's school (Google Classroom); the school name comes from the profile only."""
    if inp.job is None or not is_relayed(inp.job):
        return ""
    school = (inp.profile.alternance.school if inp.profile.alternance else "").strip()
    return f"J'ai pris connaissance de votre offre par l'intermédiaire de {school or 'mon école'}."


def with_relay(paragraphs: list[str], inp: LetterInput) -> list[str]:
    """Paragraphs with the school-relay sentence opening the first one (once)."""
    sentence = relay_sentence(inp)
    if not sentence or not paragraphs or "par l'intermédiaire de" in paragraphs[0]:
        return paragraphs
    return [f"{sentence} {paragraphs[0]}", *paragraphs[1:]]


def _where(inp: LetterInput) -> str:
    return f"au sein de {inp.company_name}" if inp.company_name else "au sein de votre entreprise"


def _intro(inp: LetterInput) -> str:
    role = title_in_sentence(inp.role)
    if inp.alternance and inp.profile.alternance is not None:
        target = f" en tant que {role}" if inp.job is not None else f" comme {role}"
        return (f"{_programme(inp)} Je souhaite effectuer cette alternance {_where(inp)}{target}. "
                "Disponible immédiatement, je peux démarrer le contrat dès sa signature.")
    degree = _last_degree(inp)
    cities = join_fr(_cities(inp)) or inp.profile.identity.city
    return (f"Titulaire d'un {degree} et disponible immédiatement, je vous adresse ma candidature au poste de "
            f"{role} {_where(inp)}. Je recherche un premier poste où mettre à profit mes expériences de "
            f"développement web et logiciel en entreprise. Basé à {inp.profile.identity.city}, je peux travailler "
            f"sur {cities}.")


def _cities(inp: LetterInput) -> list[str]:
    """Target cities valid for this contract type (a location may be restricted, e.g. Luxembourg → CDI only)."""
    contract = inp.job.contract_type if inp.job is not None else None
    cities = []
    for loc in inp.profile.target.locations:
        allowed = getattr(loc, "contract_types", None)
        if contract is None or not allowed or contract in allowed:
            cities.append(loc.city)
    return cities


def _last_degree(inp: LetterInput) -> str:
    alt = inp.profile.alternance
    for ed in inp.profile.education:
        if alt is None or not alt.school or alt.school.lower() not in ed.school.lower():
            school = ed.school.split(",")[0].strip()
            return f"{ed.degree.split(HEADLINE_SEPARATOR)[0].strip()} ({school})"
    return "diplôme en informatique"


def _you_skills(inp: LetterInput) -> list[str]:
    return inp.skills[:3]


def _me_skills(inp: LetterInput) -> list[str]:
    return inp.skills[3:] if len(inp.skills) >= 5 else inp.skills


def _you(inp: LetterInput) -> str:
    skills = join_fr(_you_skills(inp))
    if inp.job is not None:
        return (f"Votre offre de {title_in_sentence(inp.role)} a retenu toute mon attention : les missions décrites, "
                f"autour de {skills}, correspondent précisément aux technologies que j'utilise dans mes projets. "
                "Contribuer à des applications utilisées au quotidien, au sein d'une équipe qui livre régulièrement "
                "en production, est exactement le cadre dans lequel je veux progresser.")
    company = inp.company
    city = f" à {company.city}" if company and company.city else ""
    if company is not None and company.raw.get("large_target"):  # « grande entreprise » outside the digital sector
        return (f"Je m'adresse à votre service informatique{city} : les applications et les outils métier qu'une "
                f"structure comme la vôtre fait vivre au quotidien sont le cadre dans lequel je veux progresser. Mes "
                f"compétences en {skills} peuvent y être utiles, qu'il s'agisse de faire évoluer des applications "
                "existantes ou d'en concevoir de nouvelles.")
    activity = f"votre activité ({company.naf_label.lower()})" if company and company.naf_label else "votre activité"
    return (f"Je m'adresse à vous car {activity}{city} correspond au domaine dans lequel je souhaite construire "
            f"mon parcours de développeur. Mes compétences en {skills} peuvent être utiles à vos projets, "
            "qu'il s'agisse de faire évoluer vos applications existantes ou d'en concevoir de nouvelles.")


def _at(kind: str, company: str) -> str:
    return "en freelance" if kind == "freelance" else f"chez {company}"


def _me(inp: LetterInput) -> str:
    exps = [e for e in inp.profile.experiences if e.kind in ("internship", "job", "freelance") and e.bullets]
    facts = [f"{_at(e.kind, e.company)}, {lower_first(e.bullets[0].split(' : ')[0].rstrip('.'))}" for e in exps[:2]]
    body = " ; ".join(facts) or "plusieurs applications web complètes"
    return (f"Titulaire d'un {_last_degree(inp)}, j'ai déjà mené des projets complets en entreprise, de l'analyse "
            f"des besoins jusqu'à la mise en production. J'ai notamment réalisé les travaux suivants : {body}. "
            f"Ces expériences m'ont appris à travailler avec {join_fr(_me_skills(inp))} dans des conditions réelles.")


def _us(inp: LetterInput) -> str:
    qualities = join_fr(inp.qualities[:3]) if inp.qualities else "motivé"
    aid = aid_sentence(inp, with_amount=True)
    parts = [
        f"{qualities[:1].upper()}{qualities[1:]}, je vous apporterai une réelle capacité à livrer des fonctionnalités "
        "utiles et documentées ; j'attends de mon côté de progresser aux côtés de développeurs expérimentés, "
        "notamment sur l'architecture et la qualité logicielle.",
        aid,
        "Je me tiens à votre disposition pour un entretien afin de vous présenter mon parcours plus en détail.",
    ]
    return " ".join(p for p in parts if p)


def rule_paragraphs(inp: LetterInput) -> list[str]:
    return with_relay([_intro(inp), _you(inp), _me(inp), _us(inp)], inp)


# --------------------------------------------------------------------------- e-mail


def rule_pitch(inp: LetterInput) -> str:
    alt = inp.profile.alternance
    if inp.alternance and alt is not None:
        when = f"{start_phrase(inp)} " if alt.start_date else ""
        months = f" sur {alt.duration_months} mois" if alt.duration_months else ""
        rhythm = f" ({alt.rhythm})" if alt.rhythm else ""
        lead = (f"Titulaire d'un {_last_degree(inp)}, je prépare {when}un {_level(inp) or 'diplôme'} à {alt.school}, "
                f"en alternance{months}{rhythm}.")
    else:
        lead = f"Titulaire d'un {_last_degree(inp)}, je suis disponible immédiatement."
    return f"{lead} J'ai notamment travaillé avec {join_fr(inp.skills[:4])} en entreprise et en projet."


def build_email_body(inp: LetterInput, pitch: str) -> str:
    identity = inp.profile.identity
    role = title_in_sentence(inp.role)
    if inp.job is not None:
        opening = (f"Je vous adresse ma candidature au poste de {role}"
                   f"{' en alternance' if inp.alternance else ''} {_where(inp)}.")
    else:
        kind = "une alternance" if inp.alternance else "un poste"
        opening = f"Je vous adresse ma candidature spontanée pour {kind} de {role} {_where(inp)}."
    aid = aid_sentence(inp, with_amount=False) if inp.job is None else ""
    contact = " · ".join(x for x in (identity.phone, identity.email) if x)
    blocks = [
        GREETING,
        " ".join(x for x in (opening, relay_sentence(inp), pitch, aid) if x),
        "Vous trouverez ci-joint mon CV et ma lettre de motivation. Je me tiens à votre disposition pour un échange.",
        "\n".join(x for x in ("Bien cordialement,", identity.full_name, contact, display_url(identity.linkedin)) if x),
    ]
    if inp.job is None:
        blocks.append(OPT_OUT)
    return "\n\n".join(blocks)
