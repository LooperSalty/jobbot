"""Markdown rendering of the weekly report (French). Its last section lists the week's applications in a form
usable as proof of job search (« justificatif de recherche d'emploi ») for France Travail."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

from jobbot import clock
from jobbot.config import Profile
from jobbot.models import Application
from jobbot.reports._weekly_stats import Rate, WeekStats, channel_name
from jobbot.textutil import truncate

MAX_LIST = 15
DETAIL_CHARS = 160


def _cell(value: object) -> str:
    text = " ".join(str(value if value is not None else "").split()).replace("|", "\\|")
    return text or "—"


def _table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    return lines + ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]


def _day(value: datetime | None) -> str:
    return clock.to_paris(value).strftime("%d/%m/%Y") if value else ""


def _status(app: Application) -> str:
    from jobbot.cli_support import APP_STATUS_FR, label

    return label(APP_STATUS_FR, app.status)


def last_day(stats: WeekStats) -> date:
    return clock.to_paris(stats.end - timedelta(microseconds=1)).date()


def _header(profile: Profile, stats: WeekStats, generated_at: datetime) -> list[str]:
    ident = profile.identity
    first, last = clock.to_paris(stats.start).date(), last_day(stats)
    who = " · ".join(p for p in (ident.full_name, ident.email, ident.phone, ident.city) if p)
    local = clock.to_paris(generated_at)
    lines = [f"# Rapport hebdomadaire — semaine {first.isocalendar().week} (du {first:%d/%m/%Y} au {last:%d/%m/%Y})",
             "", f"Candidat : {who}", f"Généré le {clock.french_date(local)} à {local:%H:%M} par jobbot."]
    if stats.end >= generated_at:
        lines.append("Semaine en cours : chiffres arrêtés au moment de la génération.")
    return lines


def _overview(stats: WeekStats) -> list[str]:
    kept = sum(stats.kept_per_source.values())
    spontaneous = sum(1 for a in stats.applications if a.job_id is None)
    return [
        "## En bref", "",
        f"- Offres trouvées : {len(stats.jobs)} (dont {kept} retenues)",
        f"- Candidatures envoyées : {len(stats.applications)} ({len(stats.applications) - spontaneous} sur offre, "
        f"{spontaneous} spontanées)",
        f"- Réponses reçues : {len(stats.responses)} · entretiens : {stats.interviews}",
        f"- E-mails envoyés (candidatures, spontanées, relances) : {stats.emails_sent}",
        f"- Actions à faire à la main : {stats.pending_manual} · relances prévues dans les 7 jours : "
        f"{len(stats.upcoming)}",
    ]


def _sources(stats: WeekStats) -> list[str]:
    if not stats.per_source:
        return ["## Offres trouvées par source", "", "Aucune nouvelle offre cette semaine."]
    rows = [(name, count, stats.kept_per_source.get(name, 0))
            for name, count in sorted(stats.per_source.items(), key=lambda kv: (-kv[1], kv[0]))]
    return ["## Offres trouvées par source", "", *_table(("Source", "Offres", "Retenues"), rows)]


def _rates(title: str, first: str, rates: tuple[Rate, ...]) -> list[str]:
    if not rates:
        return [f"## {title}", "", "Aucune candidature pour l'instant."]
    rows = [(r.name, r.week, r.total, r.answered, r.percent) for r in rates]
    headers = (first, "Cette semaine", "Total", "Réponses", "Taux de réponse")
    return [f"## {title}", "", *_table(headers, rows), "",
            "_Taux calculés depuis le début de la recherche (adresses invalides exclues)._"]


def _responses(stats: WeekStats) -> list[str]:
    lines = ["## Réponses de la semaine", ""]
    for app, event in stats.responses[:MAX_LIST]:
        when = _day(event.at if event else app.last_response_at)
        what = truncate(event.detail, DETAIL_CHARS) if event and event.detail else _status(app)
        title = f" — {app.job_title}" if app.job_title else ""
        lines.append(f"- {when} · {app.company_name or 'Entreprise inconnue'}{title} : {what}")
    return lines if len(lines) > 2 else [*lines, "Aucune réponse cette semaine."]


def _pending(stats: WeekStats) -> list[str]:
    lines = ["## À faire à la main", ""]
    if stats.needs_manual:
        lines.append(f"- Candidatures à finir à la main ({len(stats.needs_manual)}) — « jobbot assist » ouvre "
                     "chaque formulaire pré-rempli :")
        lines += [f"  - {j.company or 'Entreprise inconnue'} — {j.title} ({j.apply_url or j.url or 'lien inconnu'})"
                  for j in stats.needs_manual[:MAX_LIST]]
    if stats.pending_jobs or stats.pending_companies:
        lines.append(f"- À valider : {stats.pending_jobs} offre(s), {stats.pending_companies} entreprise(s) — "
                     "« jobbot approve » ou le tableau de bord.")
    lines += [f"- Relance manuelle : {truncate(event.detail, DETAIL_CHARS)}"
              for _, event in stats.manual_followups[:MAX_LIST]]
    return lines if len(lines) > 2 else [*lines, "Rien en attente."]


def _upcoming(stats: WeekStats) -> list[str]:
    if not stats.upcoming:
        return ["## Relances prévues (7 prochains jours)", "", "Aucune relance prévue."]
    rows = [(_day(a.next_followup_at), a.company_name, a.job_title or "Candidature spontanée", channel_name(a),
             a.followup_count + 1) for a in stats.upcoming[:MAX_LIST]]
    return ["## Relances prévues (7 prochains jours)", "",
            *_table(("Date", "Entreprise", "Poste", "Canal", "Relance n°"), rows)]


def _proof_row(app: Application, stats: WeekStats) -> tuple[object, ...]:
    job = stats.jobs_by_id.get(app.job_id or "")
    company = stats.companies_by_id.get(app.company_id or "")
    place = (job.location if job else "") or (company.city if company else "")
    contract = job.contract_type.value if job and job.contract_type.value != "other" else "non précisé"
    step = "Réponse à une offre" if app.job_id else "Candidature spontanée"
    return (_day(app.applied_at), app.company_name, app.job_title or "—", contract, step, channel_name(app), place,
            _status(app))


def _proof(profile: Profile, stats: WeekStats) -> list[str]:
    first, last = clock.to_paris(stats.start).date(), last_day(stats)
    lines = ["## Justificatif de recherche d'emploi (France Travail)", "",
             f"Démarches de {profile.identity.full_name} du {first:%d/%m/%Y} au {last:%d/%m/%Y} — à reporter lors "
             "de l'actualisation ou à présenter en cas de contrôle.", ""]
    if not stats.applications:
        return [*lines, "Aucune candidature envoyée cette semaine."]
    headers = ("Date", "Entreprise", "Poste", "Contrat", "Démarche", "Canal", "Lieu", "Suite")
    return lines + _table(headers, (_proof_row(a, stats) for a in stats.applications))


def render_weekly(profile: Profile, stats: WeekStats, *, generated_at: datetime) -> str:
    blocks = [
        _header(profile, stats, generated_at), _overview(stats), _sources(stats),
        _rates("Candidatures par canal", "Canal", stats.channel_rates),
        _rates("Taux de réponse par source", "Source", stats.source_rates), _responses(stats), _pending(stats),
        _upcoming(stats), _proof(profile, stats),
    ]
    return "\n\n".join("\n".join(block) for block in blocks).rstrip() + "\n"
