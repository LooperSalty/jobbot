"""Markdown rendering of an interview prep sheet (French)."""

from __future__ import annotations

from datetime import datetime

from jobbot import clock
from jobbot.config import Profile
from jobbot.reports._logistics import logistics_lines
from jobbot.reports._prep_facts import PrepFacts
from jobbot.reports._prep_llm import PrepContent
from jobbot.reports._prep_questions import checklist
from jobbot.textutil import truncate

QUOTE_CHARS = 900
QUESTION_GROUPS = (("Techniques", ("technique",)), ("Alternance", ("alternance",)),
                   ("Comportement et motivation", ("comportement", "motivation")))


def _labels() -> tuple[dict, dict]:
    from jobbot.cli_support import APP_STATUS_FR, METHOD_FR

    return METHOD_FR, APP_STATUS_FR


def _header(facts: PrepFacts) -> str:
    methods, statuses = _labels()
    app = facts.application
    title = facts.job_title or "Candidature spontanée"
    lines = [f"# Entretien — {facts.company_name} · {title}", "",
             f"Candidature du {clock.french_date(app.applied_at)} · canal : "
             f"{methods.get(app.method, app.method.value)} · statut : {statuses.get(app.status, app.status.value)}"]
    if facts.job is not None and facts.job.url:
        lines.append(f"Offre : {facts.job.url}")
    return "\n".join(lines)


def _quote(text: str) -> str:
    body = truncate(text.strip(), QUOTE_CHARS)
    return "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())


def _logistics(facts: PrepFacts) -> str:
    lines = ["## Logistique", ""]
    found = logistics_lines(facts.logistics)
    if facts.logistics.found():
        lines += found
    else:
        lines += found + ["Aucune information logistique trouvée dans l'e-mail : vérifie le message original "
                          "(date, heure, lieu ou lien de visio) et réponds pour confirmer."]
    if facts.email_text.strip():
        lines += ["", "Message reçu :", "", _quote(facts.email_text)]
    return "\n".join(lines)


def _company(facts: PrepFacts, content: PrepContent) -> str:
    lines = ["## L'entreprise", "", content.company_summary, ""]
    company = facts.company
    if facts.website:
        lines.append(f"- **Site** : {facts.website}")
    if company is not None:
        details = [("Secteur", company.naf_label or company.naf_code), ("Effectif", company.headcount_range),
                   ("Ville", company.city)]
        lines += [f"- **{name}** : {value}" for name, value in details if value]
    if facts.job is not None and facts.job.location:
        lines.append(f"- **Lieu du poste** : {facts.job.location}")
    return "\n".join(lines).rstrip()


def _contract(facts: PrepFacts, profile: Profile) -> str | None:
    job = facts.job
    if facts.alternance and profile.alternance:
        return f"alternance ({profile.alternance.duration_months} mois, {profile.alternance.rhythm})"
    if job is not None and job.contract_type.value != "other":
        return job.contract_type.value.upper() if len(job.contract_type.value) <= 3 else job.contract_type.value
    return None


def _role(facts: PrepFacts, content: PrepContent, profile: Profile) -> str:
    lines = ["## Ce que le poste demande", ""]
    lines += [f"- {need}" for need in content.role_needs]
    contract = _contract(facts, profile)
    if contract:
        lines.append(f"- **Contrat** : {contract}")
    if facts.have:
        lines.append(f"- **Compétences demandées que tu maîtrises** : {', '.join(facts.have)}")
    if facts.missing:
        lines.append("- **À réviser — absentes de ton profil (ne prétends jamais les maîtriser)** : "
                     + ", ".join(facts.missing))
    if facts.soft:
        lines.append(f"- **Qualités recherchées** : {', '.join(facts.soft)}")
    if not facts.job_skills:
        lines.append("- Pas d'annonce détaillée : révise les bases de ta stack principale et lis leur site.")
    return "\n".join(lines)


def _stars(content: PrepContent) -> str:
    lines = ["## Tes expériences à mettre en avant (méthode STAR)", "",
             "Uniquement des faits de ton profil : entraîne-toi à les raconter à voix haute, avec tes mots."]
    for n, star in enumerate(content.stars, start=1):
        lines += ["", f"### {n}. {star.source}", "",
                  f"- **Situation** : {star.situation}", f"- **Tâche** : {star.task}",
                  f"- **Action** : {star.action}", f"- **Résultat** : {star.result}"]
        if star.skills:
            lines.append(f"- **Compétences à citer** : {', '.join(star.skills)}")
    return "\n".join(lines)


def _questions(content: PrepContent) -> str:
    lines = ["## Questions probables et pistes de réponse"]
    for title, categories in QUESTION_GROUPS:
        group = [qa for qa in content.questions if qa.category in categories]
        if not group:
            continue
        lines += ["", f"### {title}"]
        for qa in group:
            lines += ["", f"**{qa.question}**", f"→ {qa.answer}"]
    return "\n".join(lines)


def _bullets(title: str, items: list[str] | tuple[str, ...], box: bool = False) -> str:
    mark = "- [ ] " if box else "- "
    return "\n".join([f"## {title}", "", *(mark + item for item in items)])


def _footer(content: PrepContent, generated_at: datetime) -> str:
    local = clock.to_paris(generated_at)
    how = "IA vérifiée sur ton profil" if content.llm_used else "modèle de secours, sans IA"
    return (f"---\n_Fiche générée le {clock.french_date(local)} à {local:%H:%M} par jobbot ({how}). Rien n'est "
            "inventé : relis, vérifie et reformule avec tes mots._")


def render_prep(profile: Profile, facts: PrepFacts, content: PrepContent, *, generated_at: datetime) -> str:
    sections = [
        _header(facts), _logistics(facts), _company(facts, content), _role(facts, content, profile),
        _stars(content), _questions(content), _bullets("Questions à poser au recruteur", content.to_ask),
        _bullets("Check-list", checklist(profile, facts), box=True), _footer(content, generated_at),
    ]
    return "\n\n".join(s for s in sections if s).rstrip() + "\n"
