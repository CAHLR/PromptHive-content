#!/usr/bin/env python3
"""Generate synthetic LLM respondent data and grade responses."""
import argparse
import json
import os
import re
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from tqdm import tqdm
from dotenv import load_dotenv

from .batched_responses import (
    item_index_to_id,
    parse_batched_response,
    render_batched_prompt,
)
from .io import load_items, save_csv, clean_text
from .client import ask_llm, ask_llm_async
from .llm_providers import (
    CompletionResult,
    LLMCallError,
    ask_provider,
    ask_provider_async,
    shared_async_openai_client,
    shared_sync_openai_client,
)

# Prefer gunicorn's error logger so truncation-missing lines land in --error-logfile.
import logging
import sys

_logger = logging.getLogger("gunicorn.error")
if not _logger.handlers:
    _logger = logging.getLogger(
        "abscribe_backend.services.question_difficulty.generate_responses"
    )
    if not _logger.handlers:
        _handler = logging.StreamHandler(sys.stderr)
        _handler.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(_handler)
        _logger.setLevel(logging.INFO)
        _logger.propagate = False


def _raise_llm_failure(
    *,
    respondent_id: str,
    item_id: str | None,
    exc: BaseException,
) -> None:
    """Never record transport/LLM failures as is_correct=0 / non_response."""
    where = (
        f"respondent {respondent_id} item {item_id}"
        if item_id is not None
        else f"respondent {respondent_id}"
    )
    raise LLMCallError(
        f"LLM call failed for {where}: {exc}. "
        "Refusing to record a network/provider failure as an incorrect answer."
    ) from exc
from .model_pool import DEFAULT_MODEL_POOL, pool_to_manifest
from .autograder import grade
from .final_answer import parse_final_answer
from .respondents import (
    DEFAULT_CALIBRATION_MODEL,
    DEFAULT_CALIBRATION_TEMPERATURE,
    DEFAULT_TEMPERATURE_TIERS,
    RESPONDENT_MODE_MULTI_MODEL,
    RESPONDENT_MODE_PERSONA,
    RESPONDENT_MODE_TEMPERATURE,
    RESPONDENT_REASONING_TOKEN_CAP,
    build_respondents,
    get_respondent_mode,
    respondent_id_order,
    respondent_reasoning_enabled,
)
from .vision_figures import (
    build_multimodal_user_content,
    load_figures_cache,
    resolve_item_data_urls,
)


def _answer_format_rule(item) -> str:
    """Per-type output rule shared by the answer-only and reasoning prompts."""
    answer_type = item["answer_type"]
    if answer_type == "numeric":
        return "- Output a single number or fraction (e.g., -4/7 or 0.5714). No words."
    if answer_type == "algebra":
        return "- Output a single simplified expression or equation (e.g., x=3 or y=4x-3). No words."
    if answer_type == "string":
        return "- Output a single word or short phrase (e.g., perpendicular). No extra punctuation."
    if answer_type == "mc":
        choices = item.get("mc_choices", "")
        return f"- Output exactly one of: {choices}"
    return ""


def render_reasoning_user_prompt(item) -> str:
    """Reasoning-mode item prompt: brief working, then one FINAL ANSWER line.

    Only used when ``QUESTION_DIFFICULTY_RESPONDENT_REASONING=1``. The
    FINAL ANSWER line must follow the same per-type format as the answer-only
    prompt so the same autograder applies to it.
    """
    stem = item["stem"]
    prompt_parts = [
        "Solve the following item.",
        f"Question: {stem}",
        "",
        "Write a few brief lines of working, then end with one last line exactly:",
        "FINAL ANSWER: <answer>",
        "",
        "Format rules for <answer>:",
    ]
    rule = _answer_format_rule(item)
    if rule:
        prompt_parts.append(rule)
    prompt_parts.append("Only the FINAL ANSWER line is graded.")
    return "\n".join(prompt_parts)


def render_user_prompt(item, figure_cache=None) -> str | list:
    """Render the item prompt (persona lives in the system message).

    Returns a plain string, or a multimodal content list when figures are attached.
    With ``QUESTION_DIFFICULTY_RESPONDENT_REASONING=1`` the respondent is asked
    for brief working plus a FINAL ANSWER line; otherwise (default) the prompt
    is the answer-only one, unchanged.
    """
    if respondent_reasoning_enabled():
        text = render_reasoning_user_prompt(item)
        if figure_cache is None:
            return text
        data_urls = resolve_item_data_urls(item, figure_cache)
        return build_multimodal_user_content(text, data_urls)

    stem = item["stem"]
    answer_type = item["answer_type"]

    prompt_parts = [
        "Solve the following college algebra item.",
        f"Question: {stem}",
        "",
        "Output format rules:",
    ]

    if answer_type == "numeric":
        prompt_parts.append(
            "- Output a single number or fraction (e.g., -4/7 or 0.5714). No words."
        )
    elif answer_type == "algebra":
        prompt_parts.append(
            "- Output a single simplified expression or equation (e.g., x=3 or y=4x-3). No words."
        )
    elif answer_type == "string":
        prompt_parts.append(
            "- Output a single word or short phrase (e.g., perpendicular). No extra punctuation."
        )
    elif answer_type == "mc":
        choices = item.get("mc_choices", "")
        prompt_parts.append(f"- Output exactly one of: {choices}")

    prompt_parts.append("Answer only. No prose.")
    text = "\n".join(prompt_parts)
    if figure_cache is None:
        return text
    data_urls = resolve_item_data_urls(item, figure_cache)
    return build_multimodal_user_content(text, data_urls)


def _respondent_max_tokens() -> int | None:
    """Per-call cap for respondent calls: 600 in reasoning mode, else the provider default."""
    return RESPONDENT_REASONING_TOKEN_CAP if respondent_reasoning_enabled() else None


def _load_run_figure_cache(items_path: str, out_dir: Path):
    """Load figures_cache.json written beside items.csv (or under out_dir)."""
    candidates = [
        Path(items_path).parent / "figures_cache.json",
        out_dir / "figures_cache.json",
    ]
    for path in candidates:
        if path.is_file():
            return load_figures_cache(path)
    return load_figures_cache(candidates[0])


def _personas_for_run(
    n_respondents: int,
    base_seed: int,
    *,
    mode: str | None = None,
    temperature_tiers: tuple[float, ...] = DEFAULT_TEMPERATURE_TIERS,
    model_pool=DEFAULT_MODEL_POOL,
):
    return build_respondents(
        n_respondents=n_respondents,
        base_seed=base_seed,
        mode=mode,
        temperature_tiers=temperature_tiers,
        model_pool=model_pool,
    )


def _effective_temperature(persona, args) -> float:
    if persona.temperature is not None:
        return persona.temperature
    return args.temperature


def _respondent_model(persona, args) -> str:
    if persona.model_name:
        return persona.model_name
    return args.model_name or os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")


def _respondent_provider(persona) -> str:
    return persona.provider or "openai"


def _data_quality_events_path() -> Path:
    override = os.getenv("QUESTION_DIFFICULTY_DATA_QUALITY_LOG")
    if override:
        return Path(override)
    return Path.home() / "prompthive-data-quality" / "events.jsonl"


def _append_data_quality_event(event: dict) -> None:
    """Append a durable JSONL record (survives KEEP_TEMP_DIR cleanup)."""
    try:
        path = _data_quality_events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _grade_parsed_answer(item, raw_answer: str) -> tuple[str, int, str, str | None]:
    cleaned = clean_text(raw_answer)
    is_correct, rule_used, error = grade(
        answer_type=item["answer_type"],
        student=cleaned,
        key=item["answer_key"],
        mc_choices=item.get("mc_choices"),
    )
    return cleaned, is_correct, rule_used, error


def _unpack_completion(raw) -> tuple[str, str | None]:
    """Normalize provider / mock returns to (text, finish_reason)."""
    if raw is None:
        return "", None
    if isinstance(raw, CompletionResult):
        return raw.text, raw.finish_reason
    if isinstance(raw, tuple) and len(raw) == 2:
        return str(raw[0] or ""), raw[1]
    return str(raw), None


def _maybe_missing_truncated(
    is_correct: int,
    rule_used: str,
    *,
    finish_reason: str | None,
    respondent_id: str,
    item_id: str | None,
    raw_answer: str = "",
) -> tuple[float | int, str]:
    """If truncated at max_tokens and graded wrong, record NaN (missing).

    Truncated garbage is model pathology, not evidence the respondent got it
    wrong — same principle as never recording a transport failure as incorrect.
    If the truncated text still grades correct (answer-then-ramble), keep it.
    """
    if finish_reason == "length" and int(is_correct) == 0:
        preview = re.sub(r"\s+", " ", (raw_answer or "").strip())[:240]
        _logger.warning(
            "[generate_responses] truncated incorrect answer recorded as missing "
            "(finish_reason=length) | respondent=%s item=%s | answer_preview=%r",
            respondent_id,
            item_id,
            preview,
        )
        _append_data_quality_event(
            {
                "type": "truncated_missing",
                "respondent_id": respondent_id,
                "item_id": item_id,
                "finish_reason": finish_reason,
                "rule_used": "truncated_missing",
                "raw_answer": raw_answer or "",
            }
        )
        return float("nan"), "truncated_missing"
    return is_correct, rule_used


def _missing_no_final_answer(
    *,
    finish_reason: str | None,
    respondent_id: str,
    item_id: str | None,
    raw_answer: str,
) -> tuple[str, float, str, str | None]:
    """Reasoning mode: a reply with no FINAL ANSWER line is missing (NaN), not wrong.

    Same principle as ``_maybe_missing_truncated``: nothing was answered, so
    there is no evidence the respondent got it wrong.
    """
    preview = re.sub(r"\s+", " ", (raw_answer or "").strip())[:240]
    _logger.warning(
        "[generate_responses] reply with no FINAL ANSWER line recorded as missing "
        "(finish_reason=%s) | respondent=%s item=%s | answer_preview=%r",
        finish_reason,
        respondent_id,
        item_id,
        preview,
    )
    _append_data_quality_event(
        {
            "type": "no_final_answer_missing",
            "respondent_id": respondent_id,
            "item_id": item_id,
            "finish_reason": finish_reason,
            "rule_used": "no_final_answer_missing",
            "raw_answer": raw_answer or "",
        }
    )
    return "", float("nan"), "no_final_answer_missing", None


def _grade_with_truncation_policy(
    item,
    raw_answer: str,
    *,
    finish_reason: str | None,
    respondent_id: str,
    item_id: str | None,
    reasoning: bool = False,
) -> tuple[str, float | int, str, str | None]:
    """Grade one reply. ``reasoning=True`` grades only the last FINAL ANSWER line."""
    if reasoning:
        final = parse_final_answer(raw_answer or "")
        if final is None:
            return _missing_no_final_answer(
                finish_reason=finish_reason,
                respondent_id=respondent_id,
                item_id=str(item_id) if item_id is not None else None,
                raw_answer=raw_answer or "",
            )
        raw_answer = final
    if raw_answer:
        cleaned, is_correct, rule_used, error = _grade_parsed_answer(item, raw_answer)
    else:
        cleaned, is_correct, rule_used, error = "", 0, "non_response", None
    is_correct, rule_used = _maybe_missing_truncated(
        is_correct,
        rule_used,
        finish_reason=finish_reason,
        respondent_id=respondent_id,
        item_id=str(item_id) if item_id is not None else None,
        raw_answer=raw_answer or "",
    )
    return cleaned, is_correct, rule_used, error


def generate_responses_batched(args):
    """Generate one batched prompt per respondent (all items in a single call)."""
    load_dotenv()
    with shared_sync_openai_client(max_connections=1):
        _generate_responses_batched_impl(args)


def _generate_responses_batched_impl(args):
    items_df = load_items(args.items)
    n_items = len(items_df)
    print(f"Loaded {n_items} items from {args.items}")

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("runs") / f"run_{timestamp}"

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out_dir}")

    mode = getattr(args, "respondent_mode", None) or get_respondent_mode()
    tiers = getattr(args, "temperature_tiers", None) or DEFAULT_TEMPERATURE_TIERS
    pool = getattr(args, "model_pool", None) or DEFAULT_MODEL_POOL
    personas = _personas_for_run(
        args.n_respondents, args.seed, mode=mode, temperature_tiers=tiers, model_pool=pool
    )
    batched_prompt = render_batched_prompt(items_df)
    index_to_item_id = item_index_to_id(items_df)
    item_by_id = {str(row["item_id"]): row for _, row in items_df.iterrows()}

    long_rows = []
    accuracy_by_item = {item_id: [] for item_id in items_df["item_id"]}

    print(
        f"\nGenerating {args.n_respondents} batched respondents "
        f"({n_items} items/prompt, mode={mode})..."
    )

    for persona in tqdm(personas, desc="Respondents (batched)"):
        temp = _effective_temperature(persona, args)
        model = _respondent_model(persona, args)
        provider = _respondent_provider(persona)
        try:
            completion = ask_provider(
                provider=provider,
                prompt=batched_prompt,
                seed=persona.seed,
                temperature=temp,
                model_name=model,
                system_prompt=persona.system_prompt,
                respondent_id=persona.respondent_id,
            )
        except Exception as exc:
            _raise_llm_failure(
                respondent_id=persona.respondent_id, item_id=None, exc=exc
            )

        raw_batch, finish_reason = _unpack_completion(completion)
        parsed = parse_batched_response(raw_batch or "", n_items)

        for qidx in range(1, n_items + 1):
            item_id = index_to_item_id[qidx]
            item = item_by_id[item_id]
            raw_answer = parsed.get(qidx, "")
            cleaned, is_correct, rule_used, error = _grade_with_truncation_policy(
                item,
                raw_answer,
                finish_reason=finish_reason,
                respondent_id=persona.respondent_id,
                item_id=item_id,
            )

            long_rows.append(
                {
                    "respondent_id": persona.respondent_id,
                    "item_id": item_id,
                    "raw_answer": raw_answer,
                    "cleaned_answer": cleaned,
                    "is_correct": is_correct,
                    "rule_used": rule_used,
                    "error": error if error else "",
                    "model": model,
                    "provider": provider,
                    "temperature": temp,
                    "seed": persona.seed,
                    "ability_tier": persona.ability_tier,
                    "batched_raw_response": raw_batch if qidx == 1 else "",
                }
            )
            accuracy_by_item[item_id].append(is_correct)

    _save_results(
        long_rows,
        accuracy_by_item,
        items_df,
        args,
        out_dir,
        n_items,
        personas,
        generation_method="batched_single_prompt",
        respondent_mode=mode,
        temperature_tiers=list(tiers) if mode == RESPONDENT_MODE_TEMPERATURE else None,
        model_pool=pool_to_manifest(pool) if mode == RESPONDENT_MODE_MULTI_MODEL else None,
    )


async def _run_with_semaphore(sem: asyncio.Semaphore, coro):
    async with sem:
        return await coro


async def generate_responses_batched_async_bounded(args, max_concurrency: int | None = None):
    """Batched async generation with a semaphore cap on concurrent API calls.

    Each respondent gets one API call containing all items in a single prompt
    (paper methodology).  Up to ``max_concurrency`` calls run in parallel;
    the rest queue behind the semaphore so we don't flood the OpenAI rate
    limiter.

    ``max_concurrency`` defaults to the ``QUESTION_DIFFICULTY_ASYNC_CONCURRENCY``
    env var, or 40 if unset.
    """
    load_dotenv()

    if max_concurrency is None:
        max_concurrency = int(
            os.getenv("QUESTION_DIFFICULTY_ASYNC_CONCURRENCY", "40")
        )

    async with shared_async_openai_client(max_connections=max_concurrency):
        await _generate_responses_batched_async_bounded_impl(args, max_concurrency)


async def _generate_responses_batched_async_bounded_impl(args, max_concurrency: int):
    items_df = load_items(args.items)
    n_items = len(items_df)
    print(f"Loaded {n_items} items from {args.items}")

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("runs") / f"run_{timestamp}"

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out_dir}")

    mode = getattr(args, "respondent_mode", None) or get_respondent_mode()
    tiers = getattr(args, "temperature_tiers", None) or DEFAULT_TEMPERATURE_TIERS
    pool = getattr(args, "model_pool", None) or DEFAULT_MODEL_POOL
    personas = _personas_for_run(
        args.n_respondents, args.seed, mode=mode, temperature_tiers=tiers, model_pool=pool
    )
    figure_cache = _load_run_figure_cache(args.items, out_dir)
    batched_prompt = render_batched_prompt(items_df, figure_cache)
    index_to_item_id = item_index_to_id(items_df)
    item_by_id = {str(row["item_id"]): row for _, row in items_df.iterrows()}

    long_rows = []
    accuracy_by_item = {item_id: [] for item_id in items_df["item_id"]}

    print(
        f"\nGenerating {args.n_respondents} batched respondents "
        f"({n_items} items/prompt, async, concurrency={max_concurrency}, mode={mode})..."
    )

    sem = asyncio.Semaphore(max_concurrency)
    tasks = []
    for persona in personas:
        temp = _effective_temperature(persona, args)
        model = _respondent_model(persona, args)
        provider = _respondent_provider(persona)
        tasks.append(
            _run_with_semaphore(
                sem,
                ask_provider_async(
                    provider=provider,
                    prompt=batched_prompt,
                    seed=persona.seed,
                    temperature=temp,
                    model_name=model,
                    system_prompt=persona.system_prompt,
                    respondent_id=persona.respondent_id,
                ),
            )
        )

    print(f"Running {len(tasks)} batched API calls (max {max_concurrency} concurrent)...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for persona, raw_batch in zip(personas, results):
        temp = _effective_temperature(persona, args)
        model = _respondent_model(persona, args)
        provider = _respondent_provider(persona)
        if isinstance(raw_batch, BaseException):
            _raise_llm_failure(
                respondent_id=persona.respondent_id, item_id=None, exc=raw_batch
            )

        raw_batch, finish_reason = _unpack_completion(raw_batch)
        parsed = parse_batched_response(raw_batch or "", n_items)

        for qidx in range(1, n_items + 1):
            item_id = index_to_item_id[qidx]
            item = item_by_id[item_id]
            raw_answer = parsed.get(qidx, "")
            cleaned, is_correct, rule_used, error = _grade_with_truncation_policy(
                item,
                raw_answer,
                finish_reason=finish_reason,
                respondent_id=persona.respondent_id,
                item_id=item_id,
            )

            long_rows.append(
                {
                    "respondent_id": persona.respondent_id,
                    "item_id": item_id,
                    "raw_answer": raw_answer,
                    "cleaned_answer": cleaned,
                    "is_correct": is_correct,
                    "rule_used": rule_used,
                    "error": error if error else "",
                    "model": model,
                    "provider": provider,
                    "temperature": temp,
                    "seed": persona.seed,
                    "ability_tier": persona.ability_tier,
                    "batched_raw_response": raw_batch if qidx == 1 else "",
                }
            )
            accuracy_by_item[item_id].append(is_correct)

    _save_results(
        long_rows,
        accuracy_by_item,
        items_df,
        args,
        out_dir,
        n_items,
        personas,
        generation_method="batched_single_prompt_bounded",
        respondent_mode=mode,
        temperature_tiers=list(tiers) if mode == RESPONDENT_MODE_TEMPERATURE else None,
        model_pool=pool_to_manifest(pool) if mode == RESPONDENT_MODE_MULTI_MODEL else None,
    )


async def generate_responses_batched_async(args):
    """Async batched generation: one concurrent call per respondent."""
    load_dotenv()
    async with shared_async_openai_client():
        await _generate_responses_batched_async_impl(args)


async def _generate_responses_batched_async_impl(args):
    items_df = load_items(args.items)
    n_items = len(items_df)
    print(f"Loaded {n_items} items from {args.items}")

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("runs") / f"run_{timestamp}"

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out_dir}")

    mode = getattr(args, "respondent_mode", None) or get_respondent_mode()
    tiers = getattr(args, "temperature_tiers", None) or DEFAULT_TEMPERATURE_TIERS
    pool = getattr(args, "model_pool", None) or DEFAULT_MODEL_POOL
    personas = _personas_for_run(
        args.n_respondents, args.seed, mode=mode, temperature_tiers=tiers, model_pool=pool
    )
    batched_prompt = render_batched_prompt(items_df)
    index_to_item_id = item_index_to_id(items_df)
    item_by_id = {str(row["item_id"]): row for _, row in items_df.iterrows()}

    long_rows = []
    accuracy_by_item = {item_id: [] for item_id in items_df["item_id"]}

    print(
        f"\nGenerating {args.n_respondents} batched respondents "
        f"({n_items} items/prompt, async, mode={mode})..."
    )

    tasks = []
    for persona in personas:
        temp = _effective_temperature(persona, args)
        model = _respondent_model(persona, args)
        provider = _respondent_provider(persona)
        tasks.append(
            ask_provider_async(
                provider=provider,
                prompt=batched_prompt,
                seed=persona.seed,
                temperature=temp,
                model_name=model,
                system_prompt=persona.system_prompt,
                respondent_id=persona.respondent_id,
            )
        )

    print(f"Running {len(tasks)} batched API calls concurrently (asyncio.gather)...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for persona, raw_batch in zip(personas, results):
        temp = _effective_temperature(persona, args)
        model = _respondent_model(persona, args)
        provider = _respondent_provider(persona)
        if isinstance(raw_batch, BaseException):
            _raise_llm_failure(
                respondent_id=persona.respondent_id, item_id=None, exc=raw_batch
            )

        raw_batch, finish_reason = _unpack_completion(raw_batch)
        parsed = parse_batched_response(raw_batch or "", n_items)

        for qidx in range(1, n_items + 1):
            item_id = index_to_item_id[qidx]
            item = item_by_id[item_id]
            raw_answer = parsed.get(qidx, "")
            cleaned, is_correct, rule_used, error = _grade_with_truncation_policy(
                item,
                raw_answer,
                finish_reason=finish_reason,
                respondent_id=persona.respondent_id,
                item_id=item_id,
            )

            long_rows.append(
                {
                    "respondent_id": persona.respondent_id,
                    "item_id": item_id,
                    "raw_answer": raw_answer,
                    "cleaned_answer": cleaned,
                    "is_correct": is_correct,
                    "rule_used": rule_used,
                    "error": error if error else "",
                    "model": model,
                    "provider": provider,
                    "temperature": temp,
                    "seed": persona.seed,
                    "ability_tier": persona.ability_tier,
                    "batched_raw_response": raw_batch if qidx == 1 else "",
                }
            )
            accuracy_by_item[item_id].append(is_correct)

    _save_results(
        long_rows,
        accuracy_by_item,
        items_df,
        args,
        out_dir,
        n_items,
        personas,
        generation_method="batched_single_prompt",
        respondent_mode=mode,
        temperature_tiers=list(tiers) if mode == RESPONDENT_MODE_TEMPERATURE else None,
        model_pool=pool_to_manifest(pool) if mode == RESPONDENT_MODE_MULTI_MODEL else None,
    )


async def generate_responses_async(args):
    """Main generation loop with async requests."""
    load_dotenv()
    async with shared_async_openai_client():
        await _generate_responses_async_impl(args)


async def _generate_responses_async_impl(args):
    items_df = load_items(args.items)
    n_items = len(items_df)
    print(f"Loaded {n_items} items from {args.items}")

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("runs") / f"run_{timestamp}"

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out_dir}")

    mode = getattr(args, "respondent_mode", None) or get_respondent_mode()
    tiers = getattr(args, "temperature_tiers", None) or DEFAULT_TEMPERATURE_TIERS
    pool = getattr(args, "model_pool", None) or DEFAULT_MODEL_POOL
    personas = _personas_for_run(
        args.n_respondents, args.seed, mode=mode, temperature_tiers=tiers, model_pool=pool
    )
    long_rows = []
    accuracy_by_item = {item_id: [] for item_id in items_df["item_id"]}

    reasoning = respondent_reasoning_enabled()
    max_tokens = _respondent_max_tokens()
    print(
        f"\nGenerating {args.n_respondents} respondents × {n_items} items "
        f"(async, mode={mode})..."
    )

    tasks = []
    task_metadata = []

    for persona in personas:
        for _, item in items_df.iterrows():
            item_id = item["item_id"]
            prompt = render_user_prompt(item)
            task = ask_llm_async(
                prompt=prompt,
                seed=persona.seed,
                temperature=_effective_temperature(persona, args),
                model_name=_respondent_model(persona, args),
                system_prompt=persona.system_prompt,
                provider=_respondent_provider(persona),
                respondent_id=persona.respondent_id,
                item_id=str(item_id),
                max_tokens=max_tokens,
            )
            tasks.append(task)
            task_metadata.append(
                {
                    "persona": persona,
                    "item": item,
                    "item_id": item_id,
                }
            )

    total_tasks = len(tasks)
    print(f"Running {total_tasks} API calls concurrently (asyncio.gather)...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for raw_answer, metadata in zip(results, task_metadata):
        persona = metadata["persona"]
        item = metadata["item"]
        item_id = metadata["item_id"]

        if isinstance(raw_answer, BaseException):
            _raise_llm_failure(
                respondent_id=persona.respondent_id,
                item_id=item_id,
                exc=raw_answer,
            )

        text, finish_reason = _unpack_completion(raw_answer)
        cleaned, is_correct, rule_used, error = _grade_with_truncation_policy(
            item,
            text,
            finish_reason=finish_reason,
            respondent_id=persona.respondent_id,
            item_id=item_id,
            reasoning=reasoning,
        )

        long_rows.append(
            {
                "respondent_id": persona.respondent_id,
                "item_id": item_id,
                "raw_answer": text,
                "cleaned_answer": cleaned,
                "is_correct": is_correct,
                "rule_used": rule_used,
                "error": error if error else "",
                "model": _respondent_model(persona, args),
                "provider": _respondent_provider(persona),
                "temperature": _effective_temperature(persona, args),
                "seed": persona.seed,
                "ability_tier": persona.ability_tier,
            }
        )
        accuracy_by_item[item_id].append(is_correct)

    _save_results(
        long_rows,
        accuracy_by_item,
        items_df,
        args,
        out_dir,
        n_items,
        personas,
        generation_method="per_item",
        respondent_mode=mode,
        temperature_tiers=list(tiers) if mode == RESPONDENT_MODE_TEMPERATURE else None,
        model_pool=pool_to_manifest(pool) if mode == RESPONDENT_MODE_MULTI_MODEL else None,
    )


async def generate_responses_async_bounded(args, max_concurrency: int | None = None):
    """Per-item async generation with bounded concurrent API calls.

    Each (respondent, item) gets its own ``render_user_prompt`` call — same
    prompts as sequential ``generate_responses``.  Up to ``max_concurrency``
    calls run in parallel; the rest queue behind a semaphore.

    ``max_concurrency`` defaults to ``QUESTION_DIFFICULTY_ASYNC_CONCURRENCY``
    env var, or 40 if unset.
    """
    load_dotenv()

    if max_concurrency is None:
        max_concurrency = int(
            os.getenv("QUESTION_DIFFICULTY_ASYNC_CONCURRENCY", "40")
        )

    async with shared_async_openai_client(max_connections=max_concurrency):
        await _generate_responses_async_bounded_impl(args, max_concurrency)


async def _generate_responses_async_bounded_impl(args, max_concurrency: int):
    items_df = load_items(args.items)
    n_items = len(items_df)
    print(f"Loaded {n_items} items from {args.items}")

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("runs") / f"run_{timestamp}"

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out_dir}")

    figure_cache = _load_run_figure_cache(args.items, out_dir)

    mode = getattr(args, "respondent_mode", None) or get_respondent_mode()
    tiers = getattr(args, "temperature_tiers", None) or DEFAULT_TEMPERATURE_TIERS
    pool = getattr(args, "model_pool", None) or DEFAULT_MODEL_POOL
    personas = _personas_for_run(
        args.n_respondents, args.seed, mode=mode, temperature_tiers=tiers, model_pool=pool
    )
    long_rows = []
    accuracy_by_item = {item_id: [] for item_id in items_df["item_id"]}

    reasoning = respondent_reasoning_enabled()
    max_tokens = _respondent_max_tokens()
    print(
        f"\nGenerating {args.n_respondents} respondents × {n_items} items "
        f"(per-item async, concurrency={max_concurrency}, mode={mode}"
        f"{', reasoning=on' if reasoning else ''})..."
    )

    sem = asyncio.Semaphore(max_concurrency)
    tasks = []
    task_metadata = []

    for persona in personas:
        temp = _effective_temperature(persona, args)
        model = _respondent_model(persona, args)
        provider = _respondent_provider(persona)
        for _, item in items_df.iterrows():
            item_id = item["item_id"]
            prompt = render_user_prompt(item, figure_cache)
            tasks.append(
                _run_with_semaphore(
                    sem,
                    ask_provider_async(
                        provider=provider,
                        prompt=prompt,
                        seed=persona.seed,
                        temperature=temp,
                        model_name=model,
                        system_prompt=persona.system_prompt,
                        respondent_id=persona.respondent_id,
                        item_id=str(item_id),
                        max_tokens=max_tokens,
                    ),
                )
            )
            task_metadata.append(
                {
                    "persona": persona,
                    "item": item,
                    "item_id": item_id,
                    "model": model,
                    "provider": provider,
                    "temperature": temp,
                }
            )

    total_tasks = len(tasks)
    print(
        f"Running {total_tasks} per-item API calls "
        f"(max {max_concurrency} concurrent)..."
    )

    progress_callback = getattr(args, "progress_callback", None)

    async def _run_indexed(index: int, coro):
        try:
            return index, await coro
        except BaseException as exc:
            return index, exc

    results: list = [None] * total_tasks
    completed = 0
    if total_tasks == 0:
        if progress_callback:
            try:
                progress_callback(0, 0, "simulating")
            except Exception:
                pass
    else:
        for finished in asyncio.as_completed(
            [_run_indexed(i, coro) for i, coro in enumerate(tasks)]
        ):
            index, value = await finished
            if isinstance(value, BaseException):
                meta = task_metadata[index]
                _raise_llm_failure(
                    respondent_id=meta["persona"].respondent_id,
                    item_id=meta["item_id"],
                    exc=value,
                )
            results[index] = value
            completed += 1
            if progress_callback:
                try:
                    progress_callback(completed, total_tasks, "simulating")
                except Exception:
                    pass

    for raw_answer, metadata in zip(results, task_metadata):
        persona = metadata["persona"]
        item = metadata["item"]
        item_id = metadata["item_id"]

        if isinstance(raw_answer, BaseException):
            _raise_llm_failure(
                respondent_id=persona.respondent_id,
                item_id=item_id,
                exc=raw_answer,
            )

        text, finish_reason = _unpack_completion(raw_answer)
        cleaned, is_correct, rule_used, error = _grade_with_truncation_policy(
            item,
            text,
            finish_reason=finish_reason,
            respondent_id=persona.respondent_id,
            item_id=item_id,
            reasoning=reasoning,
        )

        long_rows.append(
            {
                "respondent_id": persona.respondent_id,
                "item_id": item_id,
                "raw_answer": text,
                "cleaned_answer": cleaned,
                "is_correct": is_correct,
                "rule_used": rule_used,
                "error": error if error else "",
                "model": metadata["model"],
                "provider": metadata["provider"],
                "temperature": metadata["temperature"],
                "seed": persona.seed,
                "ability_tier": persona.ability_tier,
            }
        )
        accuracy_by_item[item_id].append(is_correct)

    _save_results(
        long_rows,
        accuracy_by_item,
        items_df,
        args,
        out_dir,
        n_items,
        personas,
        generation_method="per_item_async_bounded",
        respondent_mode=mode,
        temperature_tiers=list(tiers) if mode == RESPONDENT_MODE_TEMPERATURE else None,
        model_pool=pool_to_manifest(pool) if mode == RESPONDENT_MODE_MULTI_MODEL else None,
    )


def generate_responses(args):
    """Main generation loop with persistent respondent personas."""
    load_dotenv()
    with shared_sync_openai_client(max_connections=1):
        _generate_responses_impl(args)


def _generate_responses_impl(args):
    items_df = load_items(args.items)
    n_items = len(items_df)
    print(f"Loaded {n_items} items from {args.items}")

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("runs") / f"run_{timestamp}"

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {out_dir}")

    mode = getattr(args, "respondent_mode", None) or get_respondent_mode()
    tiers = getattr(args, "temperature_tiers", None) or DEFAULT_TEMPERATURE_TIERS
    pool = getattr(args, "model_pool", None) or DEFAULT_MODEL_POOL
    personas = _personas_for_run(
        args.n_respondents, args.seed, mode=mode, temperature_tiers=tiers, model_pool=pool
    )
    long_rows = []
    accuracy_by_item = {item_id: [] for item_id in items_df["item_id"]}

    reasoning = respondent_reasoning_enabled()
    max_tokens = _respondent_max_tokens()
    print(
        f"\nGenerating {args.n_respondents} respondents × {n_items} items "
        f"(mode={mode})..."
    )

    for persona in tqdm(personas, desc="Respondents"):
        for _, item in items_df.iterrows():
            item_id = item["item_id"]
            prompt = render_user_prompt(item)
            try:
                completion = ask_llm(
                    prompt=prompt,
                    seed=persona.seed,
                    temperature=_effective_temperature(persona, args),
                    model_name=_respondent_model(persona, args),
                    system_prompt=persona.system_prompt,
                    provider=_respondent_provider(persona),
                    respondent_id=persona.respondent_id,
                    item_id=str(item_id),
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                _raise_llm_failure(
                    respondent_id=persona.respondent_id,
                    item_id=item_id,
                    exc=exc,
                )

            text, finish_reason = _unpack_completion(completion)
            cleaned, is_correct, rule_used, error = _grade_with_truncation_policy(
                item,
                text,
                finish_reason=finish_reason,
                respondent_id=persona.respondent_id,
                item_id=item_id,
                reasoning=reasoning,
            )

            long_rows.append(
                {
                    "respondent_id": persona.respondent_id,
                    "item_id": item_id,
                    "raw_answer": text,
                    "cleaned_answer": cleaned,
                    "is_correct": is_correct,
                    "rule_used": rule_used,
                    "error": error if error else "",
                    "model": _respondent_model(persona, args),
                    "provider": _respondent_provider(persona),
                    "temperature": _effective_temperature(persona, args),
                    "seed": persona.seed,
                    "ability_tier": persona.ability_tier,
                }
            )
            accuracy_by_item[item_id].append(is_correct)

    _save_results(
        long_rows,
        accuracy_by_item,
        items_df,
        args,
        out_dir,
        n_items,
        personas,
        generation_method="per_item",
        respondent_mode=mode,
        temperature_tiers=list(tiers) if mode == RESPONDENT_MODE_TEMPERATURE else None,
        model_pool=pool_to_manifest(pool) if mode == RESPONDENT_MODE_MULTI_MODEL else None,
    )


def _save_results(
    long_rows,
    accuracy_by_item,
    items_df,
    args,
    out_dir,
    n_items,
    personas,
    *,
    generation_method: str = "per_item",
    respondent_mode: str | None = None,
    temperature_tiers: list[float] | None = None,
    model_pool: list[dict] | None = None,
):
    long_df = pd.DataFrame(long_rows)
    long_path = out_dir / "responses_long.csv"
    save_csv(long_df, long_path)

    wide_df = long_df.pivot(index="respondent_id", columns="item_id", values="is_correct")
    wide_df = wide_df.reindex(respondent_id_order(args.n_respondents))
    wide_df = wide_df[items_df["item_id"].tolist()]
    wide_df = wide_df.reset_index()
    wide_path = out_dir / "responses.csv"
    save_csv(wide_df, wide_path)

    overall_accuracy = long_df["is_correct"].mean(skipna=True)
    item_accuracies = {
        item_id: (
            float(pd.Series(scores, dtype="float64").mean(skipna=True))
            if scores
            else 0.0
        )
        for item_id, scores in accuracy_by_item.items()
    }

    mode_val = respondent_mode or get_respondent_mode()
    if mode_val == RESPONDENT_MODE_PERSONA:
        persona_scheme = "fixed_ability_tiers_v1"
    elif mode_val == RESPONDENT_MODE_TEMPERATURE:
        persona_scheme = "temperature_tiers_v1"
    else:
        persona_scheme = "model_pool_v1"

    run_meta = {
        "n_respondents": args.n_respondents,
        "n_items": n_items,
        "model": args.model_name or os.getenv("OPENAI_MODEL", "local"),
        "temperature": args.temperature,
        "seed": args.seed,
        "respondent_mode": mode_val,
        "respondent_reasoning": respondent_reasoning_enabled(),
        "generation_method": generation_method,
        "persona_scheme": persona_scheme,
        "temperature_tiers": temperature_tiers,
        "model_pool": model_pool,
        "respondent_order": respondent_id_order(args.n_respondents),
        "overall_accuracy": overall_accuracy,
        "item_accuracies": item_accuracies,
        "timestamp": datetime.now().isoformat(),
    }

    run_json_path = out_dir / "run.json"
    with open(run_json_path, "w", encoding="utf-8") as f:
        json.dump(run_meta, f, indent=2)
    print(f"Saved: {run_json_path}")

    print(f"Generated responses saved to {out_dir}")
    print(f"  - Long format: {long_path}")
    print(f"  - Wide format (for IRT): {wide_path}")
    print(f"  - Metadata: {run_json_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate LLM respondent data")
    parser.add_argument("--items", required=True, help="Path to items.csv")
    parser.add_argument(
        "--n_respondents", type=int, default=150, help="Number of synthetic respondents"
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=float(
            os.getenv(
                "QUESTION_DIFFICULTY_CALIBRATION_TEMPERATURE",
                str(DEFAULT_CALIBRATION_TEMPERATURE),
            )
        ),
        help="LLM temperature",
    )
    parser.add_argument("--seed", type=int, default=42, help="Base random seed")
    parser.add_argument(
        "--model_name",
        default=os.getenv("QUESTION_DIFFICULTY_CALIBRATION_MODEL")
        or os.getenv("OPENAI_MODEL", DEFAULT_CALIBRATION_MODEL),
        help="Model name (overrides OPENAI_MODEL)",
    )
    parser.add_argument(
        "--out_dir",
        default=None,
        help="Output directory (default: runs/run_TIMESTAMP)",
    )
    parser.add_argument(
        "--async",
        dest="use_async",
        action="store_true",
        help="Run API calls asynchronously (faster but may hit rate limits)",
    )
    parser.add_argument(
        "--batched",
        action="store_true",
        help="Send all items in one prompt per respondent",
    )
    parser.add_argument(
        "--respondent-mode",
        choices=[
            RESPONDENT_MODE_PERSONA,
            RESPONDENT_MODE_TEMPERATURE,
            RESPONDENT_MODE_MULTI_MODEL,
        ],
        default=None,
        help="Respondent mode (default: QUESTION_DIFFICULTY_RESPONDENT_MODE env)",
    )

    args = parser.parse_args()
    if args.respondent_mode:
        os.environ["QUESTION_DIFFICULTY_RESPONDENT_MODE"] = args.respondent_mode

    if args.batched:
        if args.use_async:
            asyncio.run(generate_responses_batched_async(args))
        else:
            generate_responses_batched(args)
    elif args.use_async:
        asyncio.run(generate_responses_async(args))
    else:
        generate_responses(args)


if __name__ == "__main__":
    main()
