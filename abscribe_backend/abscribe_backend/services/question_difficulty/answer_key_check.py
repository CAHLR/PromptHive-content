"""Verify (and, when safe, correct) answer keys on generated OATutor rows.

Why: the structured-output question generator writes ``Answer`` right after
``Body Text`` with no room to work, so at temperature 0 it often guesses the
number. Nothing downstream re-checks the key before the simulated respondents
are graded against it. This module asks a careful solver model to solve each
step item a few times (distinct seeds, temperature 0) and compares the solves
with the key using the same autograder the respondents are graded with.

Per-row outcome (``answerKeyCheck``):

- ``verified``   at least 2 successful solves match the key
- ``corrected``  no solve matches the key, >= 2 successful solves all agree
                 with each other; ``Answer`` is replaced and the old key is
                 kept in ``answerKeyOriginal``
- ``unverified`` anything else: solves disagree, only one matches, or fewer
                 than 2 solves parsed (``insufficient_solves``: call errors,
                 timeouts, or replies cut off before the FINAL ANSWER line).
                 An unverified row keeps its key, stays visible, and is held
                 out of scoring, so an unchecked key is never graded against.
- ``skipped``    mc rows, rows with images, keys that are not one gradable
                 answer (``key_not_gradable``), keys leaked into the question
                 text (``answer_in_text``), or a whole-run failure
                 (``check_failed``). The check must never block scoring
                 because of a network problem.

A solve that stops at the token cap (``finish_reason == "length"``) with no
FINAL ANSWER line is retried once with a plain-text, few-lines instruction,
then given up.

Every checked row also carries ``answerKeySolves`` (the cleaned final answers)
and ``answerKeyCheckModel``. Problem rows and non-step rows pass through
untouched. The input list is never mutated; new dicts are returned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Callable

from abscribe_backend.models.oatutor_hints import _convert_rendered, _convert_tutoring
from abscribe_backend.services.question_difficulty.autograder import (
    _unicode_to_ascii,
    algebra_key_parses,
    grade,
    is_plain_numeric_key,
    normalize_algebra,
    normalize_numeric,
)
from abscribe_backend.services.question_difficulty.client import ask_llm_async
from abscribe_backend.services.question_difficulty.difficulty_service import (
    _calibration_model,
    _emit_progress,
    _oatutor_notation_to_plain,
    _oatutor_row_stem_text,
)
from abscribe_backend.services.question_difficulty.final_answer import (
    FINAL_ANSWER_RE,
    parse_final_answer,
)
from abscribe_backend.services.question_difficulty.llm_providers import (
    _async_concurrency_limit,
    shared_async_openai_client,
)

_logger = logging.getLogger("gunicorn.error")

STATUS_VERIFIED = "verified"
STATUS_CORRECTED = "corrected"
STATUS_UNVERIFIED = "unverified"
STATUS_SKIPPED = "skipped"

REASON_MC = "mc"
REASON_HAS_IMAGE = "has_image"
REASON_INSUFFICIENT_SOLVES = "insufficient_solves"
REASON_CHECK_FAILED = "check_failed"
REASON_KEY_NOT_GRADABLE = "key_not_gradable"
REASON_ANSWER_IN_TEXT = "answer_in_text"

# Keys made of labelled parts ("Degree: 4, leading: 5x^4, ...") are not one
# gradable answer; the autograder cannot match them, so checking would only
# produce noise (or a bogus "correction").
_LABELLED_PARTS_RE = re.compile(
    r"[A-Za-z][A-Za-z ]*\s*:|\b(?:degree|leading|coefficient)\b",
    re.IGNORECASE,
)

KEYCHECK_MODEL_ENV = "QUESTION_DIFFICULTY_KEYCHECK_MODEL"
# gpt-4o at temperature 0 writes synthetic division in LaTeX; 400 tokens cut
# off cubic-factoring solves before the FINAL ANSWER line (LimitLaw2_b1_e4).
KEYCHECK_TOKEN_CAP = 1200
KEYCHECK_TEMPERATURE = 0.0
KEYCHECK_SEED_BASE = 7000
PROGRESS_PHASE = "checking_keys"

# Appended to the prompt when a solve hit the cap with no FINAL ANSWER line.
TRUNCATION_RETRY_INSTRUCTION = (
    "Plain text only, no LaTeX. Write at most five short lines of working "
    "before the FINAL ANSWER line."
)

SOLVER_SYSTEM_PROMPT = (
    "You are a careful solver for college algebra items. Work the problem "
    "step by step, showing your steps briefly. Then the last line of your "
    "reply must be exactly:\n"
    "FINAL ANSWER: <answer>\n"
    "Write the answer in plain form: a number, a fraction like -1/18, or an "
    "expression in the same notation as the item. No units, no LaTeX, no "
    "extra words on that line."
)

# Kept as module names: ``parse_final_answer`` / ``_FINAL_ANSWER_RE`` are the
# public extraction for solver replies (the respondents reuse the same code).
_FINAL_ANSWER_RE = FINAL_ANSWER_RE

ANSWER_TYPE_HINTS = {
    "numeric": "The expected answer is a single number or fraction.",
    "algebra": (
        "The expected answer is a single simplified expression or equation "
        "(for example x=3 or y=4x-3)."
    ),
    "string": "The expected answer is a single word or short phrase (for example perpendicular).",
}


def keycheck_model() -> str:
    """Solver model: ``QUESTION_DIFFICULTY_KEYCHECK_MODEL`` or the calibration model."""
    explicit = os.getenv(KEYCHECK_MODEL_ENV)
    if explicit is not None and str(explicit).strip() != "":
        return str(explicit).strip()
    return _calibration_model()


def _row_type(q: dict[str, Any]) -> str:
    return str(q.get("Row Type", "") or "").strip().lower()


def _answer_type(q: dict[str, Any]) -> str:
    at = str(q.get("answerType", "") or q.get("answer_type", "")).strip().lower()
    if at not in ("numeric", "algebra", "mc", "string"):
        at = "algebra"
    return at


def _images(q: dict[str, Any]) -> str:
    return str(q.get("Images", "") or q.get("images", "") or "").strip()


def build_item_text(problem_stem: str, step_row: dict[str, Any]) -> str:
    """Problem stem plus step stem, OATutor notation turned into plain text.

    Mirrors ``difficulty_service._oatutor_questions_to_items_df`` so the
    solver sees the same item text the respondents see.
    """
    step_stem = _oatutor_notation_to_plain(
        _oatutor_row_stem_text(step_row, use_title_stem_fallback=False)
    )
    problem_plain = _oatutor_notation_to_plain(problem_stem)
    return f"{problem_plain} {step_stem}".strip() if problem_plain else step_stem


def build_solver_prompt(item_text: str, answer_type: str) -> str:
    parts = [
        "Solve the following college algebra item.",
        f"Question: {item_text}",
        "",
    ]
    hint = ANSWER_TYPE_HINTS.get(answer_type)
    if hint:
        parts.append(hint)
    parts.append(
        'Show your steps briefly, then end with one line "FINAL ANSWER: <answer>".'
    )
    return "\n".join(parts)


def _matches(answer_type: str, student: str, key: str) -> bool:
    """Same grader the respondents use, including labelled keys (``m=2`` vs ``2``)."""
    is_correct, _rule, _err = grade(answer_type, student=student, key=key)
    return bool(is_correct)


def normalize_corrected_key(answer_type: str, solve: str) -> str:
    """Normalize a solve before storing it as the corrected ``Answer``.

    ``normalize_algebra`` rewrites ``a=b`` as ``(a) - (b)`` for sympy, which is
    not a usable stored key, so equations are kept as the cleaned solve text.
    """
    solve = str(solve or "").strip()
    if answer_type == "numeric":
        return normalize_numeric(solve) or solve
    if answer_type == "algebra":
        if is_plain_numeric_key(solve):
            return normalize_numeric(solve) or solve
        if "=" in solve:
            return solve
        return normalize_algebra(solve) or solve
    return solve


def decide(
    answer_type: str,
    key: str,
    solves: list[str],
) -> tuple[str, str | None, str | None]:
    """Return ``(status, corrected_answer, reason)`` for one row.

    ``solves`` are the successfully parsed, cleaned final answers. Fewer than
    two of them means the key was never actually checked, so the row is
    ``unverified`` (held out of scoring) rather than ``skipped`` (scored
    against the unchecked key).
    """
    if len(solves) < 2:
        return STATUS_UNVERIFIED, None, REASON_INSUFFICIENT_SOLVES

    n_match = sum(1 for s in solves if _matches(answer_type, s, key))
    if n_match >= 2:
        return STATUS_VERIFIED, None, None

    if n_match == 0:
        first = solves[0]
        all_agree = all(_matches(answer_type, s, first) for s in solves[1:])
        if all_agree:
            corrected = normalize_corrected_key(answer_type, first)
            # Form-only difference (m=2 vs 2, extra spaces): keep the original key.
            if _matches(answer_type, corrected, key) or _matches(
                answer_type, key, corrected
            ):
                return STATUS_VERIFIED, None, None
            return STATUS_CORRECTED, corrected, None

    return STATUS_UNVERIFIED, None, None


RENDERED_ANSWER_CONVERTERS = (
    ("Answer Rendered", _convert_rendered),
    ("Answer Tutoring", _convert_tutoring),
)


def _refresh_rendered_answer(row: dict[str, Any], corrected: str) -> None:
    """Re-render "Answer Rendered"/"Answer Tutoring" (same converters as
    ``OatutorQuestion.to_json``) for keys already present on the row."""
    for field, convert in RENDERED_ANSWER_CONVERTERS:
        if field not in row:
            continue
        try:
            row[field] = convert(corrected)[0]
        except Exception as exc:  # converter failure must not block the check
            _logger.warning(
                "[answer_key_check] could not re-render %s for %r: %s",
                field,
                corrected,
                exc,
            )


def _has_top_level_comma(text: str) -> bool:
    """True for ``2,3`` or ``x=2, x=3``; false for ``f(x, y)`` style nesting."""
    depth = 0
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            return True
    return False


def _compact_for_leak(text: str) -> str:
    """Lowercase, Unicode-to-ASCII, no spaces — for leaked-answer detection."""
    return re.sub(r"\s+", "", _unicode_to_ascii(text).lower())


def answer_appears_in_text(key: str, item_text: str) -> bool:
    """True when the compact key is already written in the problem/step text.

    Short keys (under 6 compact characters) are ignored so a lone ``2`` in a
    word problem does not trip the guard.
    """
    compact_key = _compact_for_leak(key)
    if len(compact_key) < 6:
        return False
    return compact_key in _compact_for_leak(item_text)


def key_is_gradable(answer_type: str, key: str) -> bool:
    """True when ``key`` is one answer the autograder can match.

    numeric: a plain number (``is_plain_numeric_key``), so ``3,1`` or
             ``(5,-1)`` are never collapsed to their last number.
    algebra: no labelled parts, no multi-answer form (top-level comma, the
             word "or", or a tuple), and parses as one expression or ``a=b``.
    """
    key = str(key or "").strip()
    if not key:
        return False
    if answer_type == "numeric":
        return is_plain_numeric_key(key)
    if answer_type == "algebra":
        if _LABELLED_PARTS_RE.search(key):
            return False
        if is_plain_numeric_key(key):
            return True
        if _has_top_level_comma(key) or re.search(r"\bor\b", key, re.IGNORECASE):
            return False
        return algebra_key_parses(key)
    return True


def _plan_rows(questions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """Copy rows, pre-mark skipped mc/image/ungradable-key rows, return indices to solve."""
    out: list[dict[str, Any]] = []
    to_solve: list[int] = []
    current_problem_stem = ""
    current_problem_images = ""

    for i, q in enumerate(questions):
        row = dict(q)
        out.append(row)
        rt = _row_type(q)
        if rt == "problem":
            current_problem_stem = _oatutor_row_stem_text(q, use_title_stem_fallback=False)
            current_problem_images = _images(q)
            continue
        if rt != "step":
            continue
        key = str(q.get("Answer", "") or "")
        if not key.strip():
            continue
        at = _answer_type(q)
        if at == "mc":
            row["answerKeyCheck"] = STATUS_SKIPPED
            row["answerKeyCheckReason"] = REASON_MC
            continue
        if current_problem_images or _images(q):
            row["answerKeyCheck"] = STATUS_SKIPPED
            row["answerKeyCheckReason"] = REASON_HAS_IMAGE
            continue
        if not key_is_gradable(at, key):
            # Answer is left untouched, so scoring treats the row as before.
            row["answerKeyCheck"] = STATUS_SKIPPED
            row["answerKeyCheckReason"] = REASON_KEY_NOT_GRADABLE
            continue
        item_text = build_item_text(current_problem_stem, q)
        if answer_appears_in_text(key, item_text):
            row["answerKeyCheck"] = STATUS_SKIPPED
            row["answerKeyCheckReason"] = REASON_ANSWER_IN_TEXT
            _logger.warning(
                "[answer_key_check] answer appears in question text for %r: key=%r",
                q.get("Problem Name"),
                key,
            )
            continue
        row["_keycheck_item_text"] = item_text
        to_solve.append(i)
    return out, to_solve


async def _solve_once(
    sem: asyncio.Semaphore,
    *,
    prompt: str,
    seed: int,
    model: str,
    item_id: str,
) -> str | None:
    """One solver call; returns the cleaned final answer or None on any failure.

    A reply cut off at the token cap (``finish_reason == "length"``) with no
    FINAL ANSWER line is retried once with ``TRUNCATION_RETRY_INSTRUCTION``.
    """
    attempt_prompt = prompt
    for attempt in range(2):
        async with sem:
            try:
                result = await ask_llm_async(
                    attempt_prompt,
                    seed=seed,
                    temperature=KEYCHECK_TEMPERATURE,
                    model_name=model,
                    system_prompt=SOLVER_SYSTEM_PROMPT,
                    respondent_id="keycheck",
                    item_id=item_id,
                    max_tokens=KEYCHECK_TOKEN_CAP,
                )
            except Exception as exc:
                _logger.warning(
                    "[answer_key_check] solve failed for %s seed=%s: %s: %s",
                    item_id,
                    seed,
                    type(exc).__name__,
                    exc,
                )
                return None
        text = getattr(result, "text", result)
        answer = parse_final_answer(text if isinstance(text, str) else str(text or ""))
        if answer is not None:
            return answer
        finish_reason = getattr(result, "finish_reason", None)
        if finish_reason != "length" or attempt == 1:
            return None
        _logger.warning(
            "[answer_key_check] solve for %s seed=%s hit the %s-token cap with no "
            "FINAL ANSWER line; retrying once in plain text",
            item_id,
            seed,
            KEYCHECK_TOKEN_CAP,
        )
        attempt_prompt = f"{prompt}\n{TRUNCATION_RETRY_INSTRUCTION}"
    return None


async def _solve_all(
    rows: list[dict[str, Any]],
    to_solve: list[int],
    *,
    model: str,
    n_solves: int,
) -> dict[int, list[str]]:
    limit = _async_concurrency_limit()
    sem = asyncio.Semaphore(limit)
    solves_by_index: dict[int, list[str]] = {i: [] for i in to_solve}

    async def solve_row(i: int) -> None:
        row = rows[i]
        prompt = build_solver_prompt(row["_keycheck_item_text"], _answer_type(row))
        item_id = f"keycheck_{i}"
        results = await asyncio.gather(
            *(
                _solve_once(
                    sem,
                    prompt=prompt,
                    seed=KEYCHECK_SEED_BASE + k,
                    model=model,
                    item_id=item_id,
                )
                for k in range(n_solves)
            )
        )
        solves_by_index[i] = [r for r in results if r]

    async with shared_async_openai_client(max_connections=limit):
        await asyncio.gather(*(solve_row(i) for i in to_solve))
    return solves_by_index


def _run_async(coro):
    """Run ``coro`` to completion from sync code (worker thread or request)."""
    return asyncio.run(coro)


def verify_answer_keys(
    questions: list[dict[str, Any]],
    *,
    model: str | None = None,
    n_solves: int = 3,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Check step-row answer keys; return ``(new_rows, report)``.

    ``report`` = ``{"checked", "verified", "corrected", "unverified",
    "skipped", "model", "durationMs"}`` where ``checked`` is the number of
    step rows with a non-empty ``Answer`` that were examined, and
    ``verified + corrected + unverified + skipped == checked``.
    """
    started = time.monotonic()
    model = model or keycheck_model()
    n_solves = max(1, int(n_solves))
    questions = list(questions or [])

    rows, to_solve = _plan_rows(questions)
    counts = {
        STATUS_VERIFIED: 0,
        STATUS_CORRECTED: 0,
        STATUS_UNVERIFIED: 0,
        STATUS_SKIPPED: sum(1 for r in rows if r.get("answerKeyCheck") == STATUS_SKIPPED),
    }

    solves_by_index: dict[int, list[str]] = {}
    if to_solve:
        _emit_progress(progress_callback, 0, len(to_solve), PROGRESS_PHASE)
        try:
            solves_by_index = _run_async(
                _solve_all(rows, to_solve, model=model, n_solves=n_solves)
            )
        except Exception as exc:
            # Whole-run failure (event loop, client construction, ...): never
            # block scoring; mark every candidate row as skipped.
            _logger.warning(
                "[answer_key_check] check failed, skipping: %s: %s",
                type(exc).__name__,
                exc,
            )
            for i in to_solve:
                row = rows[i]
                row.pop("_keycheck_item_text", None)
                row["answerKeyCheck"] = STATUS_SKIPPED
                row["answerKeyCheckReason"] = REASON_CHECK_FAILED
                row["answerKeySolves"] = []
                row["answerKeyCheckModel"] = model
                counts[STATUS_SKIPPED] += 1
            to_solve = []

    for i in to_solve:
        row = rows[i]
        row.pop("_keycheck_item_text", None)
        at = _answer_type(row)
        key = str(row.get("Answer", "") or "")
        solves = solves_by_index.get(i, [])
        status, corrected, reason = decide(at, key, solves)
        row["answerKeyCheck"] = status
        row["answerKeySolves"] = list(solves)
        row["answerKeyCheckModel"] = model
        if reason:
            row["answerKeyCheckReason"] = reason
        if status == STATUS_CORRECTED and corrected is not None:
            row["answerKeyOriginal"] = key
            row["Answer"] = corrected
            # Generated rows also carry pre-rendered copies of the key (the
            # sheet view prefers "Answer Rendered"); re-render them from the
            # corrected key, but only when the row already has those columns.
            _refresh_rendered_answer(row, corrected)
            _logger.warning(
                "[answer_key_check] corrected key for %r: %r -> %r (solves=%r)",
                row.get("Problem Name"),
                key,
                corrected,
                solves,
            )
        elif status == STATUS_UNVERIFIED:
            _logger.warning(
                "[answer_key_check] unverified key for %r: key=%r solves=%r reason=%s",
                row.get("Problem Name"),
                key,
                solves,
                reason or "solves_disagree",
            )
        counts[status] += 1

    for row in rows:
        row.pop("_keycheck_item_text", None)

    report = {
        "checked": sum(counts.values()),
        "verified": counts[STATUS_VERIFIED],
        "corrected": counts[STATUS_CORRECTED],
        "unverified": counts[STATUS_UNVERIFIED],
        "skipped": counts[STATUS_SKIPPED],
        "model": model,
        "durationMs": round((time.monotonic() - started) * 1000.0, 1),
    }
    return rows, report
