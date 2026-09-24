"""Likely interview questions with answer leads, questions to ask and the check-list (template, no LLM).

Answers are *leads* built from the master profile only: facts are quoted from it, everything else is phrased as
advice to the candidate (« dis-le seulement si c'est vrai »), never as an invented achievement.
"""

from __future__ import annotations

import re

from jobbot import clock
from jobbot.config import Education, Experience, Profile
from jobbot.reports._prep_facts import QA, PrepFacts, StarPoint, fold, item_label, month_label, of, shorten

MAX_TECH_SKILLS = 4
MAX_MISSING = 2
QUOTE_CHARS = 220
_GIT_WORDS = re.compile(r"\bgit\b|code review|pull request|revue de code")
_TEAM_WORDS = re.compile(r"\ben equipe\b|travail d'equipe")


def _skill_evidence(profile: Profile, skill: str) -> tuple[list[str], list[str]]:
    """(labels of the experiences/projects using ``skill``, bullets that show it)."""
    from jobbot.matching import lexicon

    labels: list[str] = []
    quotes: list[str] = []
    for item in [*profile.experiences, *profile.projects]:
        texts = item.bullets if isinstance(item, Experience) else [item.description]
        named = {lexicon.canonicalize(s) for s in item.skills}
        hits = [t for t in texts if skill in lexicon.expand_with_ancestors(lexicon.find_skills(t))]
        if skill in lexicon.expand_with_ancestors(named) or hits:
            labels.append(item_label(item))
            quotes += hits
    return labels[:2], quotes


def _close_skills(skill: str, profile_skills: frozenset[str]) -> list[str]:
    """Skills of the profile from the same family (Angular → React, TypeScript…)."""
    from jobbot.matching import lexicon

    family = lexicon.ancestors(skill) | {skill}
    return sorted(s for s in profile_skills if s != skill and lexicon.ancestors(s) & family)[:3]


def _known_skill(profile: Profile, skill: str, used: set[str]) -> QA:
    labels, quotes = _skill_evidence(profile, skill)
    quote = next((q for q in quotes if q not in used), None)
    if quote:
        used.add(quote)
    proof = f" Exemple concret : « {shorten(quote, QUOTE_CHARS)} »" if quote else ""
    return QA("technique", f"Quelle expérience avez-vous de {skill} ?",
              f"Utilisé : {' et '.join(labels) or 'dans mes projets'}.{proof}")


def _missing_skill(skill: str, facts: PrepFacts) -> QA:
    close = _close_skills(skill, facts.profile_skills)
    bridge = f"je m'appuierais sur ce que je maîtrise déjà ({', '.join(close)}) et " if close else ""
    return QA("technique", f"Que connaissez-vous de « {skill} » ?",
              f"Réponse honnête : pas encore pratiqué en projet ; {bridge}je m'y formerais rapidement "
              "(documentation officielle, petit projet d'essai, aide de l'équipe).")


def _technical(profile: Profile, facts: PrepFacts, stars: list[StarPoint]) -> list[QA]:
    used: set[str] = set()
    out = [_known_skill(profile, skill, used) for skill in facts.have[:MAX_TECH_SKILLS]]
    out += [_missing_skill(skill, facts) for skill in facts.missing[:MAX_MISSING]]
    if stars:
        out.append(QA("technique", "Présentez un projet technique dont vous êtes fier.",
                      f"{stars[0].situation} {shorten(stars[0].task, QUOTE_CHARS)} Raconte-le avec la méthode STAR "
                      "(voir plus haut)."))
    teamwork = _bullet_with(profile, _GIT_WORDS)
    if teamwork:
        out.append(QA("technique", "Comment travaillez-vous avec Git et en équipe de développement ?",
                      f"« {shorten(teamwork, QUOTE_CHARS)} »"))
    return out


def _bullet_with(profile: Profile, pattern: re.Pattern[str]) -> str | None:
    for exp in profile.experiences:
        for bullet in exp.bullets:
            if pattern.search(fold(bullet)):
                return bullet
    return None


def _alternance(profile: Profile, facts: PrepFacts, stars: list[StarPoint]) -> list[QA]:
    alt = profile.alternance
    if not (facts.alternance and alt):
        return []
    parts = [f"{alt.school} : {alt.degree}" if alt.school and alt.degree else alt.school or alt.degree,
             f"rythme {alt.rhythm}" if alt.rhythm else "", f"sur {alt.duration_months} mois",
             f"à partir {of(month_label(alt.start_date))}" if alt.start_date else ""]
    base = stars[0].source if stars else "mes stages"
    rhythm = alt.rhythm or "calendrier de l'école"
    return [
        QA("alternance", "Quelle est votre école et votre rythme d'alternance ?",
           " · ".join(p for p in parts if p) + "."),
        QA("alternance", "Pourquoi l'alternance ?",
           f"Pistes : le diplôme visé ({alt.degree or 'mon diplôme'}) ; appliquer tout de suite en entreprise ce "
           f"que j'apprends ; continuer sur la lancée de mon expérience {base}."),
        QA("alternance", "Comment allez-vous concilier l'école et l'entreprise ?",
           f"Le rythme est connu à l'avance ({rhythm}) : je planifie avec mon tuteur "
           "ce qui avance pendant mes semaines d'école et je documente mon travail pour la reprise."),
        QA("alternance", "Qu'attendez-vous de votre tuteur ?",
           "Un point régulier sur les priorités, des revues de code et des retours francs ; en échange, de "
           "l'autonomie sur des tâches bien cadrées."),
        QA("alternance", "Et après l'alternance ?",
           "Si la collaboration se passe bien, rester dans l'entreprise est une vraie option : dis-le seulement si "
           "c'est vrai pour toi."),
    ]


def _last_degree(profile: Profile) -> Education | None:
    """Most recent completed education (a degree still in progress comes first in the profile)."""
    year = clock.to_paris(clock.now()).year
    done = [ed for ed in profile.education if ed.end[:4].isdigit() and int(ed.end[:4]) <= year]
    return (done or profile.education or [None])[0]


def _pitch(profile: Profile, stars: list[StarPoint]) -> str:
    school = _last_degree(profile)
    steps = [f"parcours : {school.degree} ({school.school})" if school else "",
             f"expérience clé : {stars[0].source}" if stars else "",
             f"un projet : {profile.projects[0].name}" if profile.projects else "",
             f"ce que je cherche : {profile.headline}"]
    return "En 4 temps — " + " ; ".join(s for s in steps if s) + "."


def _salary(profile: Profile, facts: PrepFacts) -> str | None:
    answers = profile.answers
    if facts.alternance:
        return answers.get("salary_expectation_alternance")
    place = fold(facts.job.location if facts.job else "")
    value = answers.get("salary_expectation_cdi_luxembourg") if "luxembourg" in place else None
    value = value or answers.get("salary_expectation_cdi")
    return f"{value} € brut annuel (fourchette à ajuster selon le poste)" if value and value.isdigit() else value


def _behavioural(profile: Profile, facts: PrepFacts, stars: list[StarPoint]) -> list[QA]:
    first = stars[0].source if stars else "ton expérience principale"
    site = f" (site : {facts.website})" if facts.website else ""
    skills = ", ".join(facts.have[:3]) or "le poste"
    team = _bullet_with(profile, _TEAM_WORDS)
    out = [
        QA("motivation", "Présentez-vous.", _pitch(profile, stars)),
        QA("motivation", f"Pourquoi {facts.company_name} ?",
           f"Relie ce que fait l'entreprise à ce que tu sais faire ({skills}) et cite un élément précis lu sur leur "
           f"site{site} ou dans l'annonce."),
        QA("comportement", "Quelles sont vos qualités ?",
           f"{', '.join(profile.qualities[:3]) or 'tes qualités'} — avec une preuve pour chacune (ex. : {first})."),
        QA("comportement", "Quel est votre principal défaut ?",
           "Un vrai point d'amélioration et ce que tu fais concrètement pour progresser (pas de faux défaut)."),
        QA("comportement", "Parlez-moi d'une difficulté et de la façon dont vous l'avez surmontée.",
           f"Prends {first} et raconte-la en STAR : le problème, ce que tu as fait, le résultat."),
    ]
    if team:
        out.append(QA("comportement", "Aimez-vous travailler en équipe ?", f"« {shorten(team, QUOTE_CHARS)} »"))
    salary = _salary(profile, facts)
    if salary:
        out.append(QA("motivation", "Quelles sont vos prétentions salariales ?", salary))
    start = profile.answers.get("availability_date") or month_label(profile.alternance.start_date
                                                                     if profile.alternance else "")
    if start:
        out.append(QA("motivation", "Quand pouvez-vous commencer ?", start + "."))
    return out


def likely_questions(profile: Profile, facts: PrepFacts, stars: list[StarPoint]) -> list[QA]:
    return [*_technical(profile, facts, stars), *_alternance(profile, facts, stars),
            *_behavioural(profile, facts, stars)]


def questions_to_ask(facts: PrepFacts) -> list[str]:
    questions = [
        "Comment se compose l'équipe technique et avec qui travaillerais-je au quotidien ?",
        "Quelles seraient mes premières missions sur les trois premiers mois ?",
        "Comment sont organisés les déploiements, les revues de code et les tests ?",
    ]
    if facts.alternance:
        questions += [
            "Qui serait mon tuteur, et comment s'organise le suivi avec l'école ?",
            "Comment l'équipe gère-t-elle mes semaines d'école sur les projets en cours ?",
            "Quelles perspectives à l'issue de l'alternance ?",
        ]
    else:
        questions.append("Comment sera évaluée la réussite de la période d'essai ?")
    questions.append("Quelles sont les prochaines étapes du recrutement ?")
    return questions


def checklist(profile: Profile, facts: PrepFacts) -> list[str]:
    mode = facts.logistics.mode
    links = [f"{name} : {url}" for name, url in (("portfolio", profile.identity.portfolio),
                                                  ("GitHub", profile.identity.github),
                                                  ("LinkedIn", profile.identity.linkedin)) if url]
    items = ["Relire l'offre et cette fiche ; préparer 2 ou 3 exemples STAR à voix haute."]
    if mode in (None, "visio"):
        items.append("Visio : tester le lien, la caméra et le micro 15 min avant ; endroit calme et bien éclairé.")
    if mode in (None, "sur place"):
        items.append("Sur place : prévoir le trajet pour arriver 10 min en avance ; CV imprimé.")
    if mode == "téléphone":
        items.append("Téléphone : être au calme, CV et fiche sous les yeux, batterie chargée.")
    if links:
        items.append("Avoir sous la main : " + " · ".join(links) + ".")
    items.append("Après l'entretien : e-mail de remerciement court dans les 24 h.")
    return items
