"""« jobbot classroom watch » (one check of the school's Classroom stream) and « jobbot classroom test » (preview).

One check: auto-approved offers left over by a previous check are retried (real runs only), then every course
is read (posts never processed, or whose card changed), each post becomes offers (links read, LLM extraction,
mapping), and each NEW offer is scored, prepared and applied to at once (``process``). A post is processed once:
it is remembered per course in the repo kv and processed again only when its text changes. Outside
``classroom.active_hours`` the watcher returns at once (the daily pipeline step ignores the window).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from jobbot.classroom.content import link_contents
from jobbot.classroom.extract import extract_offers
from jobbot.classroom.mapping import to_job
from jobbot.classroom.models import ClassroomPost, Course, CourseFetch, Wants, parse_course_urls, text_hash
from jobbot.classroom.process import Outcome, label, notify_outcome, process_offer, retry_approved
from jobbot.classroom.state import (
    MAX_POST_FAILURES,
    ClassroomBusy,
    already_processed,
    classroom_lock,
    failures,
    in_active_hours,
    load_seen,
    login_alert_due,
    record_check,
    remember,
    save_seen,
    wants,
)
from jobbot.context import AppContext
from jobbot.models import JobOffer, JobStatus
from jobbot.textutil import truncate

log = logging.getLogger(__name__)

NOT_CONFIGURED = ("Aucun cours Classroom configuré : ajoute le lien du cours avec « jobbot setup --section "
                  "classroom » (clé CLASSROOM_COURSE_URLS du .env).")
LOGIN_MESSAGE = "Google demande de te reconnecter à Classroom : lance « jobbot login classroom »."
OUTCOME_FIELDS = ("applied", "simulated", "pending_review", "needs_manual", "filtered_out", "skipped", "deferred",
                  "failed")


class ClassroomReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    posts: int = 0  # posts seen in the streams
    new_posts: int = 0  # new or edited posts processed
    offers: int = 0
    applied: int = 0
    simulated: int = 0  # dry-run: e-mail written to output/outbox, form not submitted
    pending_review: int = 0
    needs_manual: int = 0
    filtered_out: int = 0
    skipped: int = 0
    deferred: int = 0
    failed: int = 0
    details: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class Backend(Protocol):
    name: str

    def fetch(self, ctx: Any, courses: list[Course], wants: Wants) -> list[CourseFetch]: ...


def make_backend(ctx: AppContext) -> Backend:
    if ctx.settings.classroom.backend == "api":
        from jobbot.classroom.api import ApiBackend

        return ApiBackend()
    from jobbot.classroom.browser import BrowserBackend

    return BrowserBackend()


def configured_courses(ctx: AppContext) -> list[Course]:
    courses, invalid = parse_course_urls(ctx.secrets.classroom_course_urls)
    if invalid:
        log.warning("%d lien(s) de cours invalide(s) ignoré(s) dans CLASSROOM_COURSE_URLS", invalid)
    return courses


def offers_for_post(ctx: AppContext, post: ClassroomPost) -> list[JobOffer]:
    """Links read, offers extracted (LLM or rules) and mapped to JobOffers (status NEW, not stored)."""
    contents = link_contents(ctx, post)
    extraction, _ = extract_offers(ctx.llm, post, contents)
    offers = extraction.offers
    return [to_job(ctx.settings, post, offer, n, contents, only_offer=len(offers) == 1)
            for n, offer in enumerate(offers, 1)]


# --------------------------------------------------------------------------- one check


@dataclass
class _Tally:
    counts: Counter[str] = field(default_factory=Counter)
    details: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, job: JobOffer, outcome: Outcome) -> None:
        self.counts[outcome.kind] += 1
        self.details.append(f"{label(job)} : {outcome.kind_fr()} — {truncate(outcome.detail, 200)}")

    def report(self) -> ClassroomReport:
        base = {name: self.counts[name] for name in ("posts", "new_posts", "offers", *OUTCOME_FIELDS)}
        return ClassroomReport(**base, details=self.details, errors=self.errors)


def run_classroom(ctx: AppContext, *, respect_hours: bool = True, backend: Backend | None = None,
                  now: datetime | None = None) -> ClassroomReport:
    """One check of the configured courses (see module doc). Never raises for a source problem."""
    cfg = ctx.settings.classroom
    if not cfg.enabled:
        return ClassroomReport(details=["Surveillance Classroom désactivée (classroom.enabled)."])
    courses = configured_courses(ctx)
    if not courses:
        return ClassroomReport(details=[NOT_CONFIGURED])
    if respect_hours and not in_active_hours(cfg.active_hours, now):
        return ClassroomReport(details=[f"Hors de la plage horaire ({cfg.active_hours}) : rien à faire."])
    try:
        with classroom_lock(ctx):
            return _cycle(ctx, courses, backend or make_backend(ctx))
    except ClassroomBusy as exc:
        return ClassroomReport(details=[str(exc)])


def _cycle(ctx: AppContext, courses: list[Course], backend: Backend) -> ClassroomReport:
    tally = _Tally()
    if not ctx.dry_run:
        for job, outcome in retry_approved(ctx):
            tally.add(job, outcome)
            notify_outcome(ctx, job, outcome)
    seen = {course.course_id: load_seen(ctx.repo, course.course_id) for course in courses}
    fetches = backend.fetch(ctx, courses, lambda cid, pid, fp: wants(seen.get(cid, {}), pid, fp))
    for fetch in fetches:
        course_id = fetch.course.course_id
        updated = _course(ctx, fetch, seen.get(course_id, {}), tally)
        save_seen(ctx.repo, course_id, updated)
    _record(ctx, fetches, tally)
    return tally.report()


def _course(ctx: AppContext, fetch: CourseFetch, seen: dict[str, dict[str, Any]], tally: _Tally) -> dict:
    tally.counts["posts"] += fetch.seen_in_stream
    if fetch.login_required:
        tally.errors.append(LOGIN_MESSAGE)
        if login_alert_due(ctx.repo):
            from jobbot.notify import notify

            notify(ctx, "Classroom : reconnecte-toi", LOGIN_MESSAGE)
        return seen
    if fetch.error:
        tally.errors.append(f"Classroom : {fetch.error}")
        return seen
    for post in fetch.posts:
        seen = _post(ctx, post, seen, tally)
    return seen


def _post(ctx: AppContext, post: ClassroomPost, seen: dict[str, dict[str, Any]], tally: _Tally) -> dict:
    digest = text_hash(post)
    if already_processed(seen, post.post_id, digest):
        return remember(seen, post.post_id, s=post.stream_hash, h=digest)
    edited = bool((seen.get(post.post_id) or {}).get("h"))
    try:
        jobs = offers_for_post(ctx, post)
    except Exception as exc:  # LLM / network trouble: the post is read again next time (bounded)
        log.exception("Publication Classroom %s non traitée", post.post_id)
        fails = failures(seen, post.post_id) + 1
        tally.errors.append(truncate(f"Publication non traitée ({exc})", 200))
        give_up = fails >= MAX_POST_FAILURES
        return remember(seen, post.post_id, s=post.stream_hash if give_up else "", h=digest if give_up else "",
                        f=fails)
    tally.counts["new_posts"] += 1
    tally.counts["offers"] += len(jobs)
    for job in jobs:
        _offer(ctx, job, edited, tally)
    return remember(seen, post.post_id, s=post.stream_hash, h=digest)


def _stored_new(ctx: AppContext, job: JobOffer, edited: bool) -> JobOffer | None:
    """Upsert; the stored offer when it still has to be processed (NEW, or filtered before an edit)."""
    ctx.repo.upsert_job(job)
    stored = ctx.repo.get_job(job.id) or job
    if edited and stored.status == JobStatus.FILTERED_OUT:
        ctx.repo.update_job_status(job.id, JobStatus.NEW)
        stored = ctx.repo.get_job(job.id) or stored
    return stored if stored.status == JobStatus.NEW else None


def _offer(ctx: AppContext, job: JobOffer, edited: bool, tally: _Tally) -> None:
    try:
        stored = _stored_new(ctx, job, edited)
        if stored is None:
            return
        outcome = process_offer(ctx, stored)
    except Exception as exc:  # one offer never stops the others
        log.exception("Offre Classroom %s non traitée", job.id)
        stored, outcome = job, Outcome(kind="failed", detail=truncate(f"erreur : {exc}", 200))
    tally.add(stored, outcome)
    notify_outcome(ctx, stored, outcome)


def _record(ctx: AppContext, fetches: list[CourseFetch], tally: _Tally) -> None:
    login = any(f.login_required for f in fetches)
    problems = [f.error for f in fetches if f.error and not f.login_required]
    ok = not login and not problems
    detail = LOGIN_MESSAGE if login else (problems[0] if problems else f"{tally.counts['posts']} publication(s) lue(s)")
    try:
        record_check(ctx.repo, ok=ok, detail=str(detail), posts=tally.counts["posts"], login_required=login)
    except Exception:  # bookkeeping only
        log.debug("Dernière vérification Classroom non enregistrée", exc_info=True)
