"""« jobbot classroom test »: read the latest posts, extract and score the offers, say what would happen.

Read-only: nothing is stored (no offer, no « seen » state), nothing is prepared or sent.
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict

from jobbot.apply.service import channel_of, dedup_reason
from jobbot.classroom.models import ClassroomPost
from jobbot.classroom.process import review_reason
from jobbot.classroom.watch import (
    LOGIN_MESSAGE,
    NOT_CONFIGURED,
    Backend,
    configured_courses,
    make_backend,
    offers_for_post,
)
from jobbot.context import AppContext
from jobbot.models import JobOffer
from jobbot.textutil import truncate

METHOD_FR = {"linkedin": "LinkedIn (simplifiée)", "hellowork": "HelloWork", "lba": "La Bonne Alternance",
             "email": "e-mail", "external": "site externe"}


class PreviewRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    post: str  # "date · author"
    post_id: str
    title: str = ""
    company: str = ""
    level: str = ""
    channel: str = ""
    score: float | None = None
    verdict: str = ""


def verdict(ctx: AppContext, job: JobOffer, score: float, reasons: list[str]) -> str:
    """What the watcher would do with this offer (read-only checks)."""
    if score < ctx.settings.thresholds.min_match_score:
        return "écartée : " + (reasons[0] if score == 0 and reasons else f"score {score:.0f} trop bas")
    dup = dedup_reason(ctx, job)
    if dup is not None:
        return f"ignorée : {dup[0]}"
    hold = review_reason(ctx, job, check_truth=False)
    if hold is not None:
        return f"à valider : {hold}"
    channel = channel_of(job)
    if channel == "external":
        return f"à faire à la main : {job.apply_url or job.url}"
    return f"candidature immédiate ({METHOD_FR.get(channel, channel)})"


def preview_post(ctx: AppContext, post: ClassroomPost) -> list[PreviewRow]:
    from jobbot.matching.match import match_job

    head = f"{post.date_text or '?'} · {post.author or '?'}"
    jobs = offers_for_post(ctx, post)
    if not jobs:
        first = next((line for line in post.text.splitlines() if line.strip()), "")
        return [PreviewRow(post=head, post_id=post.post_id, title=truncate(first, 80), verdict="pas une offre")]
    rows = []
    for job in jobs:
        score, reasons = match_job(job, ctx.profile)
        scored = job.model_copy(update={"match_score": score, "match_reasons": reasons})
        rows.append(PreviewRow(
            post=head, post_id=post.post_id, title=job.title, company=job.company,
            level=str(job.raw.get("target_level") or ""), channel=METHOD_FR.get(channel_of(job), ""),
            score=score, verdict=verdict(ctx, scored, score, reasons),
        ))
    return rows


def preview_classroom(ctx: AppContext, *, limit: int = 10,
                      backend: Backend | None = None) -> tuple[list[PreviewRow], list[str]]:
    """(rows, notes) for the ``limit`` newest posts of each configured course."""
    courses = configured_courses(ctx)
    if not courses:
        return [], [NOT_CONFIGURED]
    taken: Counter[str] = Counter()

    def first_posts(course_id: str, post_id: str, fingerprint: str) -> bool:
        taken[course_id] += 1
        return taken[course_id] <= limit

    rows: list[PreviewRow] = []
    notes: list[str] = []
    for fetch in (backend or make_backend(ctx)).fetch(ctx, courses, first_posts):
        if fetch.login_required:
            notes.append(LOGIN_MESSAGE)
        elif fetch.error:
            notes.append(f"Classroom : {fetch.error}")
        else:
            notes.append(f"{fetch.seen_in_stream} publication(s) dans le flux, {len(fetch.posts)} analysée(s).")
            for post in fetch.posts:
                rows.extend(preview_post(ctx, post))
    return rows, notes
