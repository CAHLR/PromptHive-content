"""User-facing difficulty levels mapped to Rasch logit ranges (human-anchored scale)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DifficultyLevel:
    level: int
    label: str
    min_logit: float
    max_logit: float


# Anchored to observed human item difficulties (~ -1.7 easy .. +1.0 hard) with margin.
DIFFICULTY_LEVELS: tuple[DifficultyLevel, ...] = (
    DifficultyLevel(1, "Very Easy", -4.0, -1.0),
    DifficultyLevel(2, "Easy", -1.0, -0.3),
    DifficultyLevel(3, "Medium", -0.3, 0.3),
    DifficultyLevel(4, "Hard", 0.3, 1.0),
    DifficultyLevel(5, "Very Hard", 1.0, 4.0),
)

DEFAULT_MIN_LEVEL = 1
DEFAULT_MAX_LEVEL = 5


def logit_range_from_levels(
    min_level: int,
    max_level: int,
) -> tuple[float, float, list[str]]:
    """Return (min_logit, max_logit, labels) for an inclusive 1-5 level range."""
    lo = max(1, min(min_level, max_level))
    hi = min(5, max(min_level, max_level))
    min_logit = DIFFICULTY_LEVELS[lo - 1].min_logit
    max_logit = DIFFICULTY_LEVELS[hi - 1].max_logit
    labels = [DIFFICULTY_LEVELS[i - 1].label for i in range(lo, hi + 1)]
    return min_logit, max_logit, labels


def label_for_logit(logit: float | None) -> str | None:
    if logit is None:
        return None
    for level in DIFFICULTY_LEVELS:
        if level.min_logit <= logit <= level.max_logit:
            return level.label
    if logit < DIFFICULTY_LEVELS[0].min_logit:
        return DIFFICULTY_LEVELS[0].label
    return DIFFICULTY_LEVELS[-1].label
