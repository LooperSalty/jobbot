"""One-page fitting: a continuous density scale (CSS variables interpolated between an airy and a tight end —
body text never under 10 pt) and the content trimming order used when even the tightest density overflows
(least relevant content first: tailoring already sorted bullets and projects by relevance, so the last ones go
first)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from jobbot.documents.models import TailoredCV


@dataclass(frozen=True)
class Density:
    """CSS variable → (airy value, tight value, unit); ``at(t)`` interpolates, t=0 airy … t=1 tight."""

    ends: dict[str, tuple[float, float, str]]

    def at(self, t: float) -> dict[str, str]:
        t = min(1.0, max(0.0, t))
        return {name: f"{airy + (tight - airy) * t:.3f}{unit}" for name, (airy, tight, unit) in self.ends.items()}


CV_DENSITY = Density({
    "--fs": (11.0, 10.0, "pt"), "--lh": (1.45, 1.2, ""), "--pad-y": (16.0, 9.0, "mm"), "--pad-x": (17.0, 12.0, "mm"),
    "--sec-gap": (13.0, 5.0, "pt"), "--item-gap": (7.0, 3.0, "pt"), "--bullet-gap": (2.5, 0.0, "pt"),
    "--name-fs": (23.0, 18.0, "pt"),
})
LETTER_DENSITY = Density({
    "--fs": (12.0, 10.5, "pt"), "--lh": (1.5, 1.3, ""), "--pad-y": (22.0, 13.0, "mm"), "--pad-x": (24.0, 16.0, "mm"),
    "--par-gap": (10.0, 5.0, "pt"),
})
BISECTION_STEPS = 7

MIN_SKILL_GROUPS = 4
MIN_EXPERIENCES = 2
MIN_INTERESTS = 3


def _trim_bullets(cv: TailoredCV, above: int) -> TailoredCV | None:
    """Drop the last bullet of the experience with the most bullets (> ``above``); ties → the older one."""
    candidates = [(len(e.bullets), i) for i, e in enumerate(cv.experiences) if len(e.bullets) > above]
    if not candidates:
        return None
    _, index = max(candidates)
    exps = list(cv.experiences)
    exps[index] = exps[index].model_copy(update={"bullets": exps[index].bullets[:-1]})
    return cv.model_copy(update={"experiences": exps})


def _drop_project(cv: TailoredCV, keep: int) -> TailoredCV | None:
    if len(cv.projects) <= keep:
        return None
    return cv.model_copy(update={"projects": cv.projects[:-1]})


def _trim_interests(cv: TailoredCV) -> TailoredCV | None:
    if len(cv.interests) <= MIN_INTERESTS:
        return None
    return cv.model_copy(update={"interests": cv.interests[:MIN_INTERESTS]})


def _drop_seasonal(cv: TailoredCV) -> TailoredCV | None:
    kept = [e for e in cv.experiences if e.kind != "seasonal"]
    if len(kept) == len(cv.experiences) or len(kept) < MIN_EXPERIENCES:
        return None
    return cv.model_copy(update={"experiences": kept})


def _drop_skill_group(cv: TailoredCV) -> TailoredCV | None:
    if len(cv.skills) <= MIN_SKILL_GROUPS:
        return None
    return cv.model_copy(update={"skills": cv.skills[:-1]})


def _drop_experience(cv: TailoredCV) -> TailoredCV | None:
    if len(cv.experiences) <= MIN_EXPERIENCES:
        return None
    return cv.model_copy(update={"experiences": cv.experiences[:-1]})


TRIM_STEPS: tuple[Callable[[TailoredCV], TailoredCV | None], ...] = (
    lambda cv: _trim_bullets(cv, 4),
    lambda cv: _drop_project(cv, 1),
    _trim_interests,
    lambda cv: _trim_bullets(cv, 3),
    lambda cv: _drop_project(cv, 0),
    lambda cv: _trim_bullets(cv, 2),
    _drop_seasonal,
    lambda cv: _trim_bullets(cv, 1),
    _drop_skill_group,
    _drop_experience,
)


def trim_cv(cv: TailoredCV) -> TailoredCV | None:
    """The CV with the least relevant piece of content removed, or None when nothing more can go."""
    for step in TRIM_STEPS:
        trimmed = step(cv)
        if trimmed is not None:
            return trimmed
    return None
