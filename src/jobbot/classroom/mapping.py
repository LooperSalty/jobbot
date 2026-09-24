"""Extracted offer → ``JobOffer(source="classroom")`` with the right application route.

Route: EMAIL when a valid address to send applications to is given (the school's own address included);
otherwise the linked board decides (LinkedIn Easy Apply / HelloWork / La Bonne Alternance in-site application
through ``raw["board"]``, see ``apply.board``); otherwise EXTERNAL (apply link, or the post itself). A board
offer read through its source brings its title / company (cross-board dedup) and its full description.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from jobbot import clock
from jobbot.classroom.extract import ExtractedOffer
from jobbot.classroom.models import ClassroomPost, LinkContent
from jobbot.config import Settings
from jobbot.models import ApplyMethod, ContractType, JobOffer, JobStatus
from jobbot.textutil import normalize, stable_id, truncate

SOURCE = "classroom"
IN_SITE_METHODS = frozenset({ApplyMethod.LINKEDIN_EASY_APPLY, ApplyMethod.HELLOWORK, ApplyMethod.LBA})
MAX_POST_TEXT = 3000
MAX_DESCRIPTION = 12000
_POSTAL_RE = re.compile(r"\b(\d{5})\b")
_CONTRACTS: tuple[tuple[re.Pattern[str], ContractType], ...] = (
    (re.compile(r"alternan|apprenti|apprentissage|professionnalisation|contrat pro\b|work.?study"),
     ContractType.ALTERNANCE),
    (re.compile(r"\bcdi\b|indetermin"), ContractType.CDI),
    (re.compile(r"\bcdd\b|determin"), ContractType.CDD),
    (re.compile(r"\bstage|intern"), ContractType.STAGE),
    (re.compile(r"freelance|independant"), ContractType.FREELANCE),
    (re.compile(r"interim"), ContractType.INTERIM),
)


def contract_of(label: str, title: str, text: str) -> ContractType:
    """Contract written by the school, else guessed from the title / text (``linkedin.guess_contract_type``)."""
    from jobbot.sources.linkedin import guess_contract_type

    folded = normalize(label)
    found = next((contract for rx, contract in _CONTRACTS if rx.search(folded)), None) if folded else None
    return found or guess_contract_type(title, text)


def valid_email(address: str) -> str | None:
    from jobbot.mail.mailer import is_valid_address

    cleaned = (address or "").strip().strip("<>.,;").lower()
    return cleaned if cleaned and is_valid_address(cleaned) else None


def _board_key(url: str) -> tuple[str, str] | None:
    from jobbot.sources.alerts import match_offer_url

    hit = match_offer_url(url) if url else None
    return hit.key if hit is not None else None


def board_for(offer: ExtractedOffer, contents: list[LinkContent], only_offer: bool) -> LinkContent | None:
    """The board offer this extracted offer points to (its apply link, or the post's single board link)."""
    boards = [c for c in contents if c.board is not None]
    key = _board_key(offer.apply_url)
    if key is not None:
        return next((c for c in boards if (c.board.source, c.board.external_id) == key), None)  # type: ignore[union-attr]
    return boards[0] if only_offer and len(boards) == 1 and not offer.apply_url else None


def _route(offer: ExtractedOffer, board: JobOffer | None) -> tuple[ApplyMethod, str | None, dict[str, Any] | None]:
    """(apply method, apply URL, board reference for ``raw["board"]``)."""
    email = valid_email(offer.contact_email)
    expired = board is not None and board.status == JobStatus.EXPIRED
    if email:
        return ApplyMethod.EMAIL, offer.apply_url or (board.url if board else None), None
    if board is not None and not expired and board.apply_method in IN_SITE_METHODS:
        ref = {"source": board.source, "external_id": board.external_id, "url": board.url,
               "raw": board.model_dump(mode="json")["raw"]}
        return board.apply_method, board.url, ref
    if board is not None:
        return ApplyMethod.EXTERNAL, board.apply_url or board.url, None
    return ApplyMethod.EXTERNAL, offer.apply_url or None, None


def _description(offer: ExtractedOffer, post: ClassroomPost, board: JobOffer | None) -> str:
    base = offer.description or post.text
    if board is not None and len(board.description or "") > len(base):
        base = board.description
    extra = [
        f"Compétences : {', '.join(offer.skills)}" if offer.skills else "",
        f"Niveau visé : {offer.target_level}" if offer.target_level else "",
        f"Candidature : {offer.apply_instructions}" if offer.apply_instructions else "",
        f"Date limite : {offer.deadline}" if offer.deadline else "",
    ]
    return truncate("\n\n".join(x for x in (base.strip(), "\n".join(e for e in extra if e)) if x), MAX_DESCRIPTION)


def _raw(settings: Settings, post: ClassroomPost, offer: ExtractedOffer, board: JobOffer | None,
         ref: dict[str, Any] | None) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "post_id": post.post_id, "course_id": post.course_id, "post_url": post.url, "post_author": post.author,
        "post_date": post.date_text, "post_text": truncate(post.text, MAX_POST_TEXT),
        "apply_instructions": offer.apply_instructions, "deadline": offer.deadline,
        "target_level": offer.target_level, "skills": offer.skills[:30], "relayed_by_school": True,
        "apply_via_school": offer.apply_via_school, "school_bonus": settings.classroom.school_bonus,
        "needs_details": False,
    }
    if board is not None:
        raw.update(board_source=board.source, board_url=board.url)
        if board.status == JobStatus.EXPIRED:
            raw["board_expired"] = True
    if ref is not None:
        raw["board"] = ref
    return raw


def to_job(settings: Settings, post: ClassroomPost, offer: ExtractedOffer, index: int,
           contents: list[LinkContent], *, only_offer: bool, now: datetime | None = None) -> JobOffer:
    """The ``index``-th offer of ``post`` (1-based) as a JobOffer (status NEW)."""
    from jobbot.sources.linkedin import detect_remote

    link = board_for(offer, contents, only_offer)
    board = link.board if link is not None else None
    method, apply_url, ref = _route(offer, board)
    title = (board.title if board and board.title else "") or offer.title or offer.company or "Offre de l'école"
    company = (board.company if board and board.company else "") or offer.company
    location = offer.location or (board.location if board else "")
    description = _description(offer, post, board)
    external_id = f"{post.post_id}-{index}"
    postal = _POSTAL_RE.search(location)
    return JobOffer(
        id=stable_id(SOURCE, external_id), source=SOURCE, external_id=external_id, title=truncate(title, 200),
        company=truncate(company, 120), location=truncate(location, 120), postal_code=postal.group(1) if postal else None,
        latitude=board.latitude if board else None, longitude=board.longitude if board else None,
        description=description, url=post.url, apply_url=apply_url, apply_method=method,
        contract_type=contract_of(offer.contract_type, title, f"{description}\n{post.text}"),
        remote=detect_remote(f"{title}\n{description}"), contact_email=valid_email(offer.contact_email),
        contact_name=offer.contact_name or None, published_at=post.published_at or (board.published_at if board else None),
        found_at=now or clock.now(), raw=_raw(settings, post, offer, board, ref),
    )
