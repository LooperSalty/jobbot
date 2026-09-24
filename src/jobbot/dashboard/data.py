"""Dashboard data layer: queries → plain rows (lists of dicts) and user actions. No Streamlit import here."""

from __future__ import annotations

import os
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from jobbot import clock
from jobbot.cli_support import (
    APP_STATUS_FR,
    COMPANY_STATUS_FR,
    CONTACT_KIND_FR,
    EVENT_FR,
    JOB_STATUS_FR,
    METHOD_FR,
    label,
)
from jobbot.context import AppContext, find_root
from jobbot.models import ApplicationStatus, Company, ContactEmail, JobOffer, JobStatus

OPEN_APPLICATIONS = (ApplicationStatus.APPLIED, ApplicationStatus.FOLLOWED_UP)
ADVANCED = frozenset({JobStatus.SHORTLISTED, JobStatus.DOCS_READY, JobStatus.PENDING_REVIEW, JobStatus.APPROVED,
                      JobStatus.APPLIED, JobStatus.NEEDS_MANUAL})
MANUAL_FOLLOWUP_DAYS = 21
MAX_ROWS = 1000


def dashboard_root() -> Path:
    env = os.environ.get("JOBBOT_ROOT", "").strip()
    return Path(env).resolve() if env else find_root()


def load_context(root: Path | None = None) -> AppContext:
    """Context without LLM (the dashboard never generates text)."""
    from jobbot.llm import DisabledLLM

    return AppContext.build(root or dashboard_root(), llm=DisabledLLM())


def _date(value: Any) -> str:
    return clock.to_paris(value).strftime("%d/%m/%Y") if value else ""


# --------------------------------------------------------------------------- overview


def kpis(ctx: AppContext) -> dict[str, int]:
    stats = ctx.repo.stats()
    jobs, apps = stats["jobs_by_status"], stats["applications_by_status"]
    week_ago = clock.now() - timedelta(days=7)
    recent = [a for a in ctx.repo.list_applications(limit=MAX_ROWS) if a.applied_at >= week_ago]
    answered = sum(apps.get(s.value, 0) for s in (ApplicationStatus.RESPONSE, ApplicationStatus.INTERVIEW,
                                                   ApplicationStatus.OFFER, ApplicationStatus.REJECTED))
    return {
        "offres": sum(jobs.values()),
        "retenues": sum(jobs.get(s.value, 0) for s in ADVANCED),
        "a_valider": (jobs.get(JobStatus.PENDING_REVIEW.value, 0)
                      + stats["companies_by_status"].get("pending_review", 0)),
        "a_finir": jobs.get(JobStatus.NEEDS_MANUAL.value, 0),
        "candidatures": sum(apps.values()),
        "cette_semaine": len(recent),
        "reponses": answered,
        "entretiens": apps.get(ApplicationStatus.INTERVIEW.value, 0),
        "relances_dues": len(ctx.repo.due_followups()),
        "emails_envoyes": stats["emails_sent"],
    }


def pipeline_counts(ctx: AppContext) -> list[dict[str, Any]]:
    counts = ctx.repo.stats()["jobs_by_status"]
    return [{"statut": JOB_STATUS_FR[s], "offres": counts.get(s.value, 0)} for s in JobStatus if counts.get(s.value)]


# --------------------------------------------------------------------------- jobs


def job_row(ctx: AppContext, job: JobOffer) -> dict[str, Any]:
    docs = ctx.repo.latest_documents(job_id=job.id)
    return {
        "id": job.id, "score": job.match_score,
        "ats_cv": docs.cv_ats.score if docs and docs.cv_ats else None,
        "ats_lettre": docs.letter_ats.score if docs and docs.letter_ats else None,
        "offre": job.title, "entreprise": job.company, "lieu": job.location, "source": job.source,
        "canal": label(METHOD_FR, job.apply_method), "statut": label(JOB_STATUS_FR, job.status),
        "contrat": job.contract_type.value, "lien": job.url or None, "postuler": job.apply_url or job.url or None,
        "trouvee_le": _date(job.found_at), "detail": str(job.raw.get("apply_detail", "")),
        "cv_pdf": docs.cv_pdf if docs else None, "lettre_pdf": docs.letter_pdf if docs else None,
    }


def job_rows(ctx: AppContext, statuses: list[JobStatus] | None = None, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    return [job_row(ctx, j) for j in ctx.repo.list_jobs(statuses or None, limit=limit)]


def jobs_in(ctx: AppContext, status: JobStatus, limit: int = 200) -> list[JobOffer]:
    return ctx.repo.list_jobs(status, limit=limit)


def ats_summary(ctx: AppContext, job_id: str) -> dict[str, Any] | None:
    docs = ctx.repo.latest_documents(job_id=job_id)
    if docs is None or docs.cv_ats is None:
        return None
    report = docs.cv_ats
    return {
        "score": report.score, "mots_cles": report.keyword_score, "intitule": report.title_score,
        "sections": report.section_score, "format": report.format_score,
        "presents": [k.keyword for k in report.matched], "manquants": [k.keyword for k in report.missing],
        "alertes": [*report.warnings, *docs.truth_warnings],
    }


def file_bytes(path: str | None) -> bytes | None:
    if not path:
        return None
    p = Path(path)
    try:
        return p.read_bytes() if p.is_file() else None
    except OSError:
        return None


# --------------------------------------------------------------------------- queues & actions


def pending_companies(ctx: AppContext) -> list[Company]:
    return ctx.repo.list_companies("pending_review")


def approve(ctx: AppContext, job_id: str) -> str:
    from jobbot.review import approve_job

    return approve_job(ctx, job_id)


def reject(ctx: AppContext, job_id: str) -> str:
    from jobbot.review import reject_job

    return reject_job(ctx, job_id, "Refusée dans le tableau de bord.")


def review_company(ctx: AppContext, company_id: str, approved: bool) -> str:
    from jobbot.review import review_company as review

    return review(ctx, company_id, approved)


def mark_applied(ctx: AppContext, job_id: str) -> str:
    from jobbot.review import record_manual_application

    job = ctx.repo.get_job(job_id)
    if job is None:
        return f"Offre {job_id} introuvable."
    app = record_manual_application(ctx, job, detail="Envoyée à la main (tableau de bord).")
    return f"Candidature enregistrée, relance prévue le {_date(app.next_followup_at) or '—'}."


def manual_followups(ctx: AppContext, days: int = MANUAL_FOLLOWUP_DAYS) -> list[dict[str, Any]]:
    """Relances without e-mail (``manual_followup`` events of open applications, last ``days`` days)."""
    since = clock.now() - timedelta(days=days)
    rows = []
    for app in ctx.repo.list_applications(OPEN_APPLICATIONS, limit=MAX_ROWS):
        for event in ctx.repo.list_events(app.id or 0):
            if event.kind == "manual_followup" and event.at >= since:
                rows.append({"date": _date(event.at), "entreprise": app.company_name, "poste": app.job_title,
                             "canal": label(METHOD_FR, app.method), "a_faire": event.detail})
    return rows


# --------------------------------------------------------------------------- applications & companies


def application_rows(ctx: AppContext) -> list[dict[str, Any]]:
    return [{
        "id": a.id, "entreprise": a.company_name, "poste": a.job_title, "canal": label(METHOD_FR, a.method),
        "statut": label(APP_STATUS_FR, a.status), "envoyee_le": _date(a.applied_at), "email": a.email_to or "",
        "relances": a.followup_count, "prochaine_relance": _date(a.next_followup_at),
        "derniere_reponse": _date(a.last_response_at),
    } for a in ctx.repo.list_applications(limit=MAX_ROWS)]


def timeline(ctx: AppContext, limit: int = 200) -> list[dict[str, Any]]:
    events = []
    for app in ctx.repo.list_applications(limit=MAX_ROWS):
        for event in ctx.repo.list_events(app.id or 0):
            events.append((event.at, {"date": clock.to_paris(event.at).strftime("%d/%m/%Y %H:%M"),
                                      "entreprise": app.company_name, "evenement": label(EVENT_FR, event.kind),
                                      "detail": event.detail}))
    events.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in events[:limit]]


def best_contact(company: Company) -> ContactEmail | None:
    """The address the spontaneous campaign would try first (selection order, quotas ignored)."""
    from jobbot.outreach.people import is_small_company
    from jobbot.outreach.selection import sort_contacts

    ranked = sort_contacts(list(company.contacts), small_company=is_small_company(company))
    return ranked[0] if ranked else None


def _contact_columns(best: ContactEmail | None) -> dict[str, Any]:
    from jobbot.outreach.selection import is_guess

    if best is None:
        return {"contact": "", "type_contact": "", "personne": "", "adresse_devinee": False}
    return {"contact": best.email, "type_contact": label(CONTACT_KIND_FR, best.kind),
            "personne": best.person_name or "", "adresse_devinee": is_guess(best)}


def _shown_contact(company: Company) -> ContactEmail | None:
    """The address chosen when the documents were prepared (review queue), else the best one."""
    chosen = str(company.raw.get("review_contact") or "").lower()
    match = next((c for c in company.contacts if chosen and c.email.lower() == chosen), None)
    return match or best_contact(company)


def contact_label(row: dict[str, Any]) -> str:
    """'rh@x.fr (Recrutement)' / 'julie.martin@x.fr (RH (nommé), Julie Martin, adresse devinée non vérifiée)'."""
    if not row.get("contact"):
        return "—"
    extras = [x for x in (row.get("type_contact"), row.get("personne")) if x]
    if row.get("adresse_devinee"):
        extras.append("adresse devinée non vérifiée")
    return f"{row['contact']} ({', '.join(extras)})" if extras else row["contact"]


def company_row(company: Company) -> dict[str, Any]:
    c = company
    return {
        "id": c.id, "entreprise": c.name, "ville": c.city, "activite": c.naf_label or c.naf_code or "",
        "effectif": c.headcount_range or "", "statut": label(COMPANY_STATUS_FR, c.status),
        "site": c.website or (f"https://{c.domain}" if c.domain else None), **_contact_columns(_shown_contact(c)),
        "personnes_connues": len(c.raw.get("people") or []), "source": c.source, "carrieres": c.careers_url,
        "description": c.description or "",
    }


def company_rows(ctx: AppContext, statuses: list[str] | None = None) -> list[dict[str, Any]]:
    return [company_row(c) for c in ctx.repo.list_companies(statuses or None, limit=MAX_ROWS)]


def source_stats(ctx: AppContext) -> list[dict[str, Any]]:
    jobs = ctx.repo.list_jobs(limit=None)
    total, kept, applied, scores = Counter(), Counter(), Counter(), {}
    for job in jobs:
        total[job.source] += 1
        kept[job.source] += int(job.status in ADVANCED)
        applied[job.source] += int(job.status == JobStatus.APPLIED)
        if job.match_score is not None:
            scores.setdefault(job.source, []).append(job.match_score)
    return [{
        "source": name, "offres": total[name], "retenues": kept[name], "candidatures": applied[name],
        "score_moyen": round(sum(scores.get(name, [0])) / max(1, len(scores.get(name, []))), 1),
    } for name in sorted(total, key=lambda n: -total[n])]


# --------------------------------------------------------------------------- settings (Réglages)


def settings_sections() -> dict[str, list[Any]]:
    from jobbot.preferences import SETUP_QUESTIONS

    sections: dict[str, list[Any]] = {}
    for q in SETUP_QUESTIONS:
        sections.setdefault(q.section, []).append(q)
    return sections


def current_values(root: Path, questions: list[Any]) -> dict[str, Any]:
    from jobbot.preferences import current_answer

    values = {}
    for q in questions:
        try:
            values[q.key] = current_answer(root, q)
        except Exception:  # unreadable value: shown empty, never blocks the page
            values[q.key] = None
    return values


def save_answers(root: Path, questions: list[Any], answers: dict[str, Any]) -> tuple[int, list[str]]:
    """Apply the changed answers (secrets: empty = unchanged). Returns (saved count, French errors)."""
    from jobbot.cli_wizard import same_value
    from jobbot.config import ConfigError
    from jobbot.preferences import apply_answer

    current = current_values(root, questions)
    saved, errors = 0, []
    for q in questions:
        value = answers.get(q.key)
        if same_value(q, current.get(q.key), value):
            continue
        try:
            apply_answer(root, q, value)
            saved += 1
        except ConfigError as exc:
            errors.append(f"{q.label} : {exc}")
        except (TypeError, ValueError) as exc:
            errors.append(f"{q.label} : valeur invalide ({exc})")
    return saved, errors
