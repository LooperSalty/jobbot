"""CLI commands (registered in ``jobbot.cli``): the daily steps, the review queue and the full run."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from jobbot.cli_support import dry_banner, fail, print_report, state_of

CHANNELS = ("linkedin", "hellowork", "lba", "email", "external")
STEP_TITLES = {
    "classroom": "Offres de l'école (Classroom)", "collect": "Collecte des offres", "score": "Tri des offres", "prepare": "CV et lettres",
    "apply": "Candidatures", "discover": "Entreprises découvertes", "enrich": "Recherche de contacts",
    "spontaneous": "Candidatures spontanées", "inbox": "Boîte mail", "followup": "Relances",
}


def search(
    ctx: typer.Context,
    source: Annotated[list[str] | None, typer.Option("--source", "-s", help="Source(s) (défaut : settings.yaml).")]
    = None,
) -> None:
    """Collecte les offres sur les sources configurées."""
    from jobbot.sources.registry import build_sources, collect_jobs

    state = state_of(ctx)
    sources = build_sources(state.app, source) if source else None
    print_report(state.console, STEP_TITLES["collect"], collect_jobs(state.app, sources=sources))


def score(ctx: typer.Context) -> None:
    """Note les nouvelles offres (0-100) et garde les pertinentes (annonces courtes lues en entier d'abord)."""
    from jobbot.cli_tools import jobs_table
    from jobbot.models import JobStatus
    from jobbot.pipeline import score_new_offers

    state = state_of(ctx)
    print_report(state.console, STEP_TITLES["score"], score_new_offers(state.app))
    best = state.app.repo.list_jobs(JobStatus.SHORTLISTED, limit=15)
    if best:
        state.console.print(jobs_table(best, "Meilleures offres retenues"))


def prepare(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Nombre maximum d'offres.")] = 10,
    job: Annotated[list[str] | None, typer.Option("--job", "-j", help="Id d'offre (répétable).")] = None,
) -> None:
    """Génère CV + lettre ciblés (score ATS) pour les meilleures offres retenues."""
    from jobbot.pipeline import prepare_shortlisted

    state = state_of(ctx)
    print_report(state.console, STEP_TITLES["prepare"], prepare_shortlisted(state.app, limit=limit, job_ids=job))


def ats(
    ctx: typer.Context, job_id: Annotated[str, typer.Argument(help="Id de l'offre.")],
    cv: Annotated[Path | None, typer.Option("--cv", exists=True, dir_okay=False,
                                            help="Noter un autre CV PDF contre cette offre.")] = None,
) -> None:
    """Explique le score ATS du CV et de la lettre pour une offre."""
    from jobbot.cli_tools import ats_lines

    state = state_of(ctx)
    for line in ats_lines(state.app, job_id, cv):
        state.console.print(escape(line))


def apply(
    ctx: typer.Context,
    channel: Annotated[list[str] | None, typer.Option("--channel", "-c", help=f"Canal : {', '.join(CHANNELS)}.")]
    = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Nombre maximum de candidatures.")] = None,
    job: Annotated[list[str] | None, typer.Option("--job", "-j", help="Id d'offre (vaut validation).")] = None,
) -> None:
    """Postule aux offres prêtes (mode auto) ou les met en file « à valider » (mode review)."""
    from jobbot.apply.service import apply_to_jobs

    state = state_of(ctx)
    bad = [c for c in channel or [] if c not in CHANNELS]
    if bad:
        fail(state.console, f"Canal inconnu : {', '.join(bad)} (possibles : {', '.join(CHANNELS)})")
    dry_banner(state)
    report = apply_to_jobs(state.app, channels=channel, limit=limit, job_ids=job)
    print_report(state.console, STEP_TITLES["apply"], report)


def assist(ctx: typer.Context,
           limit: Annotated[int, typer.Option("--limit", "-n", help="Nombre d'offres à traiter.")] = 10) -> None:
    """Candidatures externes : ouvre chaque offre pré-remplie, tu vérifies et tu cliques « Envoyer »."""
    from jobbot.cli_tools import assist_loop
    from jobbot.cli_wizard import FrPrompt

    state = state_of(ctx)
    dry_banner(state)

    def ask(question: str) -> str:
        return FrPrompt.ask(question, choices=["o", "n", "passer", "q"], default="passer", console=state.console)

    counts = assist_loop(state.app, state.console, ask, limit=limit)
    state.console.print(" · ".join(f"{v} {k}" for k, v in counts.items()))


def approve(
    ctx: typer.Context,
    ids: Annotated[list[str] | None, typer.Argument(help="Id(s) d'offre ou d'entreprise.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Valider toute la file d'attente.")] = False,
) -> None:
    """Valide des offres (ou entreprises) en attente : elles partiront au prochain « apply »."""
    _review(ctx, ids or [], approve=True, everything=all_)


def reject(
    ctx: typer.Context,
    ids: Annotated[list[str], typer.Argument(help="Id(s) d'offre ou d'entreprise.")],
    blacklist: Annotated[bool, typer.Option("--blacklist", help="Ne plus jamais contacter l'entreprise.")] = False,
) -> None:
    """Écarte des offres (ou entreprises) de la file d'attente."""
    _review(ctx, ids, approve=False, blacklist=blacklist)


def _review(ctx: typer.Context, ids: list[str], *, approve: bool, everything: bool = False,
            blacklist: bool = False) -> None:
    from jobbot.cli_tools import jobs_table
    from jobbot.models import JobStatus

    state = state_of(ctx)
    repo = state.app.repo
    targets = list(ids)
    if everything:
        targets += [j.id for j in repo.list_jobs(JobStatus.PENDING_REVIEW)]
        targets += [c.id for c in repo.list_companies("pending_review")]
    if not targets:
        state.console.print(jobs_table(repo.list_jobs(JobStatus.PENDING_REVIEW), "Offres à valider"))
        return
    for target in dict.fromkeys(targets):
        state.console.print(escape(_review_one(state.app, target, approve, blacklist)))


def _review_one(app_ctx: Any, target: str, approve: bool, blacklist: bool) -> str:
    """Job id or company id → approve / reject (optionally blacklisting the company)."""
    from jobbot.review import approve_job, reject_job, review_company
    from jobbot.textutil import normalize_company

    job = app_ctx.repo.get_job(target)
    if job is not None:
        name = job.company
        message = approve_job(app_ctx, target) if approve else reject_job(app_ctx, target)
    else:
        company = app_ctx.repo.get_company(target)
        name = company.name if company else ""
        message = review_company(app_ctx, target, approve)
    if blacklist and name:
        for value in {normalize_company(name), name}:
            app_ctx.repo.blacklist_add(value, "écartée par l'utilisateur")
    return message


def discover(ctx: typer.Context,
             limit: Annotated[int, typer.Option("--limit", "-n", help="Nombre maximum d'entreprises.")] = 300) -> None:
    """Trouve des entreprises à contacter (open data + recruteurs La Bonne Alternance)."""
    from jobbot.outreach.companies import discover_companies

    state = state_of(ctx)
    print_report(state.console, STEP_TITLES["discover"], discover_companies(state.app, limit=limit))


def enrich(ctx: typer.Context,
           limit: Annotated[int, typer.Option("--limit", "-n", help="Nombre d'entreprises à traiter.")] = 50) -> None:
    """Cherche le site et l'adresse e-mail de recrutement des entreprises découvertes."""
    from jobbot.outreach.emails import enrich_companies

    state = state_of(ctx)
    print_report(state.console, STEP_TITLES["enrich"], enrich_companies(state.app, limit=limit))


def spontaneous(ctx: typer.Context,
                limit: Annotated[int | None, typer.Option("--limit", "-n", help="Nombre maximum d'envois.")]
                = None) -> None:
    """Candidatures spontanées : relais La Bonne Alternance puis e-mails (en mode review : préparées)."""
    from jobbot.mail.mailer import build_mailer
    from jobbot.pipeline import spontaneous_applications

    state = state_of(ctx)
    dry_banner(state)
    report = spontaneous_applications(state.app, build_mailer(state.app), limit=limit)
    print_report(state.console, STEP_TITLES["spontaneous"], report)


def inbox(ctx: typer.Context) -> None:
    """Lit les réponses (Gmail) et met à jour les candidatures."""
    from jobbot.mail.inbox import process_inbox

    state = state_of(ctx)
    print_report(state.console, STEP_TITLES["inbox"], process_inbox(state.app))


def followup(ctx: typer.Context) -> None:
    """Envoie les relances dues (dans le même fil) et liste celles à faire à la main."""
    from jobbot.followup import run_followups
    from jobbot.mail.mailer import build_mailer

    state = state_of(ctx)
    dry_banner(state)
    print_report(state.console, STEP_TITLES["followup"], run_followups(state.app, build_mailer(state.app)))


def run(
    ctx: typer.Context,
    step: Annotated[list[str] | None, typer.Option("--step", "--steps", "-s",
                                                   help="Étape(s) à lancer (défaut : toutes).")] = None,
) -> None:
    """Journée complète : collecte, tri, CV/lettres, candidatures, spontanées, boîte mail, relances."""
    from jobbot.pipeline import STEPS, PipelineBusy, run_daily

    state = state_of(ctx)
    dry_banner(state)
    try:
        report = run_daily(state.app, steps=step or None)
    except ValueError as exc:
        fail(state.console, str(exc))
    except PipelineBusy as exc:
        fail(state.console, str(exc), code=2)
    for name in STEPS:
        print_report(state.console, STEP_TITLES[name], getattr(report, name))
    state.console.print(f"\n[bold]Bilan[/] : {escape(report.summary())}")
    _print_weekly(state.console, report.weekly)


def night(
    ctx: typer.Context,
    budget: Annotated[int | None, typer.Option("--budget", min=0, help="Durée maximale en minutes (défaut : "
                                                                      "jusqu'à night.until, 06:30).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Simulation : aucun dossier généré (rien n'est "
                                                            "jamais envoyé la nuit).")] = False,
) -> None:
    """Nuit : collecte d'entreprises et d'adresses, priorités, dossiers du lendemain (aucun envoi)."""
    import dataclasses

    from jobbot.night.run import run_night
    from jobbot.pipeline import PipelineBusy

    state = state_of(ctx)
    app = dataclasses.replace(state.app, dry_run=True) if dry_run and not state.app.dry_run else state.app
    if app.dry_run:
        state.console.print("[yellow]Mode simulation : aucun dossier généré (rien n'est envoyé la nuit).[/]")
    try:
        report = run_night(app, budget_minutes=budget)
    except PipelineBusy as exc:
        fail(state.console, str(exc), code=2)
        return
    print_report(state.console, "Nuit", report)
    state.console.print(f"\n[bold]Bilan[/] : {escape(report.summary())}")


def _print_weekly(console: Any, weekly: Any) -> None:
    """The weekly report written after the run (when due): its summary and file, or its error."""
    if weekly is None:
        return
    if isinstance(weekly, str):
        console.print(f"[bold]Rapport hebdomadaire[/] : [red]{escape(weekly)}[/]")
        return
    console.print(f"[bold]Rapport hebdomadaire[/] : {escape(weekly.summary())} — {escape(weekly.path)}")
