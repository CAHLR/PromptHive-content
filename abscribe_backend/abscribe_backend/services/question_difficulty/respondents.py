"""Synthetic respondent definitions for anchor build and live calibration.

Modes (``QUESTION_DIFFICULTY_RESPONDENT_MODE``):
- ``persona`` (legacy): fixed ability-tier system prompts, shared temperature.
- ``temperature``: neutral instruction, per-respondent fixed temperature tiers.
- ``multi_model``: neutral instruction, each respondent bound to a model from the pool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .model_pool import (
    DEFAULT_MODEL_POOL,
    ModelPoolEntry,
    pool_total_respondents,
)

RESPONDENT_MODE_PERSONA = "persona"
RESPONDENT_MODE_TEMPERATURE = "temperature"
RESPONDENT_MODE_MULTI_MODEL = "multi_model"

ALL_RESPONDENT_MODES = frozenset(
    {
        RESPONDENT_MODE_PERSONA,
        RESPONDENT_MODE_TEMPERATURE,
        RESPONDENT_MODE_MULTI_MODEL,
    }
)

# Tunable temperature tiers: 5 tiers x 10 respondents = 50 (low -> high temp).
DEFAULT_TEMPERATURE_TIERS: tuple[float, ...] = (0.2, 0.45, 0.7, 0.95, 1.2)
RESPONDENTS_PER_TEMPERATURE_TIER = 10

NEUTRAL_SYSTEM_PROMPT = (
    "Solve each problem and give the final answer. "
    "Follow the requested output format for every question."
)


# Canonical live calibration defaults (validated: Spearman 0.902 vs human difficulty).
DEFAULT_CALIBRATION_MODEL = "gpt-4o"
DEFAULT_CALIBRATION_TEMPERATURE = 1.75


def get_respondent_mode() -> str:
    mode = os.getenv(
        "QUESTION_DIFFICULTY_RESPONDENT_MODE", RESPONDENT_MODE_PERSONA
    ).strip().lower()
    if mode not in ALL_RESPONDENT_MODES:
        raise ValueError(
            f"Invalid QUESTION_DIFFICULTY_RESPONDENT_MODE: {mode!r}. "
            f"Use one of: {sorted(ALL_RESPONDENT_MODES)}."
        )
    return mode


# Respondent reasoning mode (``QUESTION_DIFFICULTY_RESPONDENT_REASONING=1``):
# respondents write a few brief lines of working and end with one
# ``FINAL ANSWER: <answer>`` line, which is the only thing graded. Off by
# default; with the flag off every prompt is byte-identical to before.
RESPONDENT_REASONING_ENV = "QUESTION_DIFFICULTY_RESPONDENT_REASONING"
RESPONDENT_REASONING_TOKEN_CAP = 600


def respondent_reasoning_enabled() -> bool:
    value = os.getenv(RESPONDENT_REASONING_ENV, "0").strip().lower()
    return value in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RespondentPersona:
    respondent_id: str
    seed: int
    system_prompt: str
    ability_tier: str
    temperature: float | None = None
    model_name: str | None = None
    provider: str | None = None


def _tier_prompt(tier: str, index: int) -> str:
    tier_specs = {
        "struggling": (
            "You are a college algebra student who often makes arithmetic mistakes "
            "and finds word problems difficult. Answer to the best of your ability."
        ),
        "developing": (
            "You are a college algebra student with partial understanding. "
            "You get easier problems right but struggle on multi-step items."
        ),
        "average": (
            "You are a typical college algebra student. "
            "You solve routine problems correctly but miss tricky ones."
        ),
        "strong": (
            "You are a strong college algebra student who usually gets problems right "
            "unless they require careful attention to detail."
        ),
        "expert": (
            "You are an excellent college algebra student who almost always finds "
            "the correct answer quickly."
        ),
    }
    base = tier_specs[tier]
    if respondent_reasoning_enabled():
        # Calculus and physics sheets go through the same respondents.
        base = base.replace("college algebra student", "student")
    return f"{base} Your persistent respondent code is R{index:03d}."


def build_respondent_personas(
    n_respondents: int = 50,
    base_seed: int = 42,
) -> list[RespondentPersona]:
    """Build persona-tier respondents (legacy path; unchanged behavior)."""
    if n_respondents < 1:
        raise ValueError("n_respondents must be >= 1")

    tier_cycle = (
        ["struggling"] * 8
        + ["developing"] * 10
        + ["average"] * 12
        + ["strong"] * 12
        + ["expert"] * 8
    )
    personas: list[RespondentPersona] = []
    for resp_id in range(1, n_respondents + 1):
        tier = tier_cycle[(resp_id - 1) % len(tier_cycle)]
        personas.append(
            RespondentPersona(
                respondent_id=f"R{resp_id:03d}",
                seed=base_seed + resp_id,
                system_prompt=_tier_prompt(tier, resp_id),
                ability_tier=tier,
            )
        )
    return personas


def build_temperature_respondents(
    n_respondents: int = 50,
    base_seed: int = 42,
    *,
    temperature_tiers: tuple[float, ...] = DEFAULT_TEMPERATURE_TIERS,
    per_tier: int = RESPONDENTS_PER_TEMPERATURE_TIER,
) -> list[RespondentPersona]:
    """Build respondents with fixed temperature tiers and a neutral system prompt."""
    if n_respondents < 1:
        raise ValueError("n_respondents must be >= 1")

    expected = len(temperature_tiers) * per_tier
    if n_respondents != expected:
        raise ValueError(
            f"n_respondents={n_respondents} but "
            f"{len(temperature_tiers)} tiers x {per_tier} = {expected}"
        )

    personas: list[RespondentPersona] = []
    resp_id = 1
    for temp in temperature_tiers:
        tier_label = f"temp_{temp:g}"
        for _ in range(per_tier):
            personas.append(
                RespondentPersona(
                    respondent_id=f"R{resp_id:03d}",
                    seed=base_seed + resp_id,
                    system_prompt=NEUTRAL_SYSTEM_PROMPT,
                    ability_tier=tier_label,
                    temperature=temp,
                )
            )
            resp_id += 1
    return personas


def build_multi_model_respondents(
    n_respondents: int = 50,
    base_seed: int = 42,
    *,
    model_pool: tuple[ModelPoolEntry, ...] = DEFAULT_MODEL_POOL,
) -> list[RespondentPersona]:
    """Build respondents each bound to a model from the pool."""
    expected = pool_total_respondents(model_pool)
    if n_respondents != expected:
        raise ValueError(
            f"n_respondents={n_respondents} but model pool totals {expected}"
        )

    personas: list[RespondentPersona] = []
    resp_id = 1
    for entry in model_pool:
        tier_label = f"model_{entry.model_name}"
        for _ in range(entry.count):
            personas.append(
                RespondentPersona(
                    respondent_id=f"R{resp_id:03d}",
                    seed=base_seed + resp_id,
                    system_prompt=NEUTRAL_SYSTEM_PROMPT,
                    ability_tier=tier_label,
                    temperature=entry.temperature,
                    model_name=entry.model_name,
                    provider=entry.provider,
                )
            )
            resp_id += 1
    return personas


def build_respondents(
    n_respondents: int = 50,
    base_seed: int = 42,
    *,
    mode: str | None = None,
    temperature_tiers: tuple[float, ...] = DEFAULT_TEMPERATURE_TIERS,
    model_pool: tuple[ModelPoolEntry, ...] = DEFAULT_MODEL_POOL,
) -> list[RespondentPersona]:
    """Factory: persona, temperature, or multi-model respondents."""
    mode = mode or get_respondent_mode()
    if mode == RESPONDENT_MODE_PERSONA:
        return build_respondent_personas(n_respondents, base_seed)
    if mode == RESPONDENT_MODE_TEMPERATURE:
        return build_temperature_respondents(
            n_respondents,
            base_seed,
            temperature_tiers=temperature_tiers,
        )
    if mode == RESPONDENT_MODE_MULTI_MODEL:
        return build_multi_model_respondents(
            n_respondents,
            base_seed,
            model_pool=model_pool,
        )
    raise ValueError(f"Unknown respondent mode: {mode!r}")


def respondent_id_order(n_respondents: int = 50) -> list[str]:
    return [f"R{i:03d}" for i in range(1, n_respondents + 1)]
