"""Relayed offers applied through a job board.

A school post (Google Classroom) may point to a LinkedIn Easy Apply / HelloWork / La Bonne Alternance offer. The
job keeps ``source="classroom"`` (its own id, dedup and history) and stores the board offer it points to in
``raw["board"]`` = ``{source, external_id, url, raw}``. Appliers check the board they know (``job.source``,
``raw["source"]``/``raw["id"]`` for LBA…): they receive this board view of the job, never the stored job.
"""

from __future__ import annotations

from jobbot.models import ApplyMethod, JobOffer


def board_view(job: JobOffer) -> JobOffer:
    """The job as its board's own offer (same id), or the job unchanged when it has no in-site board route."""
    board = job.raw.get("board")
    if not isinstance(board, dict) or not board.get("source") or job.apply_method == ApplyMethod.EMAIL:
        return job
    extra = board.get("raw") if isinstance(board.get("raw"), dict) else {}
    return job.model_copy(update={
        "source": str(board["source"]),
        "external_id": str(board.get("external_id") or job.external_id),
        "url": str(board.get("url") or job.url),
        "raw": {**job.raw, **extra},
    })
