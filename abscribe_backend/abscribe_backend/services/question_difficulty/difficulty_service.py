"""Rasch/IRT-based difficulty: anchor FIPC calibration and filtering."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from abscribe_backend.services.question_difficulty.anchors import (
    anchor_calibration_enabled,
    anchor_paths,
    load_anchor_bundle,
    load_informative_anchor_item_ids,
)
from abscribe_backend.services.question_difficulty.autograder import is_plain_numeric_key
from abscribe_backend.services.question_difficulty.generate_responses import (
    generate_responses_async_bounded,
)
from abscribe_backend.services.question_difficulty.vision_figures import (
    figures_summary_line,
    prepare_items_for_vision,
    save_figures_cache,
)
from abscribe_backend.services.question_difficulty.human_anchors import (
    HUMAN_ITEM_PARAMS_PATH,
    identify_degenerate_items,
    prepare_fipc_responses,
)
from abscribe_backend.services.question_difficulty.respondents import (
    DEFAULT_CALIBRATION_MODEL,
    DEFAULT_CALIBRATION_TEMPERATURE,
    RESPONDENT_MODE_PERSONA,
    get_respondent_mode,
    respondent_id_order,
    respondent_reasoning_enabled,
)

from abscribe_backend.services.question_difficulty.difficulty_levels import (
    DIFFICULTY_LEVELS,
    label_for_logit,
)

CALIBRATION_CALIBRATED = "calibrated"
CALIBRATION_INSUFFICIENT = "insufficient_informative_items"
CALIBRATION_FAILED = "calibration_failed"
CALIBRATION_ANCHOR_MISSING = "anchor_bank_missing"

FREE_RASCH_INSUFFICIENT_MESSAGE = (
    "Not enough data to measure difficulty. Try increasing to at least 20 "
    "respondents and 4 questions."
)

DEFAULT_MIN_INFORMATIVE_NEW = int(
    os.getenv("QUESTION_DIFFICULTY_MIN_INFORMATIVE_NEW", "1")
)
DEFAULT_N_RESPONDENTS = 50
MAX_N_RESPONDENTS = 300


def _resolve_n_respondents(n_respondents: int | None = None) -> int:
    """Request override → env → paper default (150)."""
    if n_respondents is not None:
        return max(1, min(int(n_respondents), MAX_N_RESPONDENTS))
    env_val = os.getenv("QUESTION_DIFFICULTY_N_RESPONDENTS")
    if env_val is not None and str(env_val).strip() != "":
        return max(1, min(int(env_val), MAX_N_RESPONDENTS))
    return DEFAULT_N_RESPONDENTS

# Step row whose answer key the checker could not confirm: kept as unscored
# (when include_unscored) but excluded from the Rasch run.
ANSWER_KEY_UNVERIFIED = "answer_key_unverified"
ANSWER_IN_TEXT = "answer_in_text"
KEY_NOT_GRADABLE = "key_not_gradable"

# Skip reasons that exclude a step from the Rasch run but keep it visible.
KEEP_VISIBLE_UNSCORED_REASONS = frozenset(
    {
        ANSWER_KEY_UNVERIFIED,
        ANSWER_IN_TEXT,
        KEY_NOT_GRADABLE,
    }
)

UNSCORED_FILTER_REASONS = frozenset(
    {
        CALIBRATION_INSUFFICIENT,
        CALIBRATION_FAILED,
        CALIBRATION_ANCHOR_MISSING,
        "zero_variance",
        "no_rasch_estimate",
        ANSWER_KEY_UNVERIFIED,
        ANSWER_IN_TEXT,
        KEY_NOT_GRADABLE,
    }
)

# Reasons for difficulty labels inferred from saturated (zero-variance) items when
# anchor FIPC recovery is enabled (Feature 2 / score_existing_questions only).
SATURATION_LOW_CONFIDENCE = "saturated_low_confidence"
SATURATION_FALLBACK_NO_RASCH = "saturated_fallback_no_rasch"
# Legacy reason codes (pre–Phase 2); retained for API/tooltip compatibility only.
SATURATION_INFERRED_EASY = "saturated_inferred_easy"
SATURATION_INFERRED_HARD = "saturated_inferred_hard"
GRADING_OR_SATURATION_UNCERTAIN = "grading_or_saturation_uncertain"


def _calibration_model() -> str:
    """Respondent/calibration model (independent of generation OPENAI_MODEL)."""
    explicit = os.getenv("QUESTION_DIFFICULTY_CALIBRATION_MODEL")
    if explicit is not None and str(explicit).strip() != "":
        return str(explicit).strip()
    return DEFAULT_CALIBRATION_MODEL


def _calibration_temperature() -> float:
    return float(
        os.getenv(
            "QUESTION_DIFFICULTY_CALIBRATION_TEMPERATURE",
            str(DEFAULT_CALIBRATION_TEMPERATURE),
        )
    )


# Feature 2 only: lower temp reduces garbage/wild wrong answers when scoring existing
# sheet items. Feature 1 / anchor calibration keeps QUESTION_DIFFICULTY_CALIBRATION_TEMPERATURE.
DEFAULT_EXISTING_SCORE_TEMPERATURE = 0.5


def _existing_score_temperature() -> float:
    return float(
        os.getenv(
            "QUESTION_DIFFICULTY_EXISTING_SCORE_TEMPERATURE",
            str(DEFAULT_EXISTING_SCORE_TEMPERATURE),
        )
    )


def _generation_namespace(
    items_path: str,
    n_respondents: int,
    out_dir: str,
    *,
    temperature: float | None = None,
    progress_callback=None,
) -> argparse.Namespace:
    return argparse.Namespace(
        items=items_path,
        n_respondents=n_respondents,
        temperature=temperature if temperature is not None else _calibration_temperature(),
        seed=int(os.getenv("QUESTION_DIFFICULTY_SEED", "42")),
        model_name=_calibration_model(),
        out_dir=out_dir,
        respondent_mode=get_respondent_mode(),
        progress_callback=progress_callback,
    )


def _emit_progress(progress_callback, done: int, total: int, phase: str) -> None:
    if not progress_callback:
        return
    try:
        progress_callback(done, total, phase)
    except Exception:
        pass


def _sanitize_id_part(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(text or "").strip())
    return cleaned.strip("_") or "unknown"


def _oatutor_notation_to_plain(text: str) -> str:
    text = re.sub(
        r"\\+frac\s*\{([^}]+)\}\s*\{([^}]+)\}",
        r"(\1)/(\2)",
        text,
        flags=re.IGNORECASE,
    )
    for vulgar, ascii_frac in (("¾", "3/4"), ("½", "1/2"), ("¼", "1/4")):
        text = text.replace(vulgar, ascii_frac)
    text = re.sub(r"(\d+)/(\d+)\*([a-zA-Z])", r"(\1/\2)*\3", text)
    text = re.sub(
        r"/mat\{(.+?)\}",
        lambda m: "matrix ["
        + m.group(1).replace("),(", "],[").replace("(", "[").replace(")", "]")
        + "]",
        text,
    )
    text = re.sub(
        r"/lim\{([^,]+),([^,]+),(.+?)\}",
        r"limit as \1 approaches \2 of \3",
        text,
    )
    text = re.sub(
        r"/int\{(.+?),([^,]+),([^,]+),([^,]+)\}",
        r"integral from \2 to \3 of \1 d\4",
        text,
    )
    text = re.sub(r"/int\{(.+?),([^,]+)\}", r"integral of \1 d\2", text)
    text = re.sub(r"log\{([^}]+)\}\{([^}]+)\}", r"log base \1 of \2", text)
    text = re.sub(r"ln\{([^}]+)\}", r"ln(\1)", text)
    text = re.sub(
        r"sum\{([^}]+)\}\{([^}]+)\}\{([^}]+)\}",
        r"sum from \1 to \2 of \3",
        text,
    )
    return text


def _persist_skip_snapshot(
    questions: list[dict[str, Any]],
    debug_rows: list[dict[str, Any]],
) -> None:
    """Write non-kept evaluation rows for the run-summary script (survives temp cleanup)."""
    try:
        skips: list[dict[str, Any]] = []
        for row in debug_rows:
            if row.get("kept"):
                continue
            reason = str(row.get("reason") or "")
            if reason == "companion_problem_row":
                continue
            idx = row.get("index")
            answer_key = title = ""
            if isinstance(idx, int) and 0 <= idx < len(questions):
                q = questions[idx]
                answer_key = str(q.get("Answer") or q.get("answer_key") or "")
                title = str(q.get("Title") or "")
            skips.append(
                {
                    "index": idx,
                    "item_id": row.get("itemId"),
                    "problem_name": row.get("problemName"),
                    "row_type": row.get("rowType"),
                    "reason": reason,
                    "answer_key": answer_key,
                    "title": title,
                }
            )
        out_dir = Path.home() / "prompthive-data-quality"
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "n_skips": len(skips),
            "skips": skips,
        }
        (out_dir / "latest_skips.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _oatutor_row_stem_text(q: dict[str, Any], *, use_title_stem_fallback: bool) -> str:
    """Build row text for calibration stems (Title + Body Text)."""
    body = str(q.get("Body Text", "") or "").strip()
    title = str(q.get("Title", "") or "").strip()
    if body and title and body != title:
        return f"{title} {body}".strip()
    return body or title


def _oatutor_questions_to_items_df(
    questions: list[dict[str, Any]],
    *,
    use_title_stem_fallback: bool = False,
) -> tuple[pd.DataFrame, dict[int, str | None], dict[str, int], dict[int, str]]:
    """Build calibratable items (step rows) with stable IDs and index maps."""
    rows: list[dict[str, Any]] = []
    index_to_item_id: dict[int, str | None] = {}
    index_to_skip_reason: dict[int, str] = {}
    item_id_to_question_index: dict[str, int] = {}
    current_problem_stem = ""
    current_problem_images = ""
    step_counter = 0

    for i, q in enumerate(questions):
        row_type = str(q.get("Row Type", "") or "").strip().lower()
        if row_type == "problem":
            current_problem_stem = _oatutor_row_stem_text(
                q, use_title_stem_fallback=use_title_stem_fallback
            )
            current_problem_images = str(
                q.get("Images", "") or q.get("images", "") or ""
            ).strip()
            index_to_item_id[i] = None
            continue

        if row_type != "step":
            index_to_item_id[i] = None
            continue

        at = str(q.get("answerType", "") or q.get("answer_type", "")).strip().lower()
        if at not in ("numeric", "algebra", "mc", "string"):
            at = "algebra"
        key = str(q.get("Answer", "") or "")
        if not key.strip():
            index_to_item_id[i] = None
            continue

        # Answer-key check outcomes that must not go to the respondents.
        check_status = str(q.get("answerKeyCheck", "") or "").strip().lower()
        check_reason = str(q.get("answerKeyCheckReason", "") or "").strip().lower()
        if check_status == "unverified":
            index_to_item_id[i] = None
            index_to_skip_reason[i] = ANSWER_KEY_UNVERIFIED
            continue
        if check_reason == ANSWER_IN_TEXT:
            index_to_item_id[i] = None
            index_to_skip_reason[i] = ANSWER_IN_TEXT
            continue
        if check_reason == KEY_NOT_GRADABLE:
            index_to_item_id[i] = None
            index_to_skip_reason[i] = KEY_NOT_GRADABLE
            continue

        if at == "numeric" and not is_plain_numeric_key(key):
            index_to_item_id[i] = None
            index_to_skip_reason[i] = "intermediate_step_key"
            continue

        step_counter += 1
        problem_name = _sanitize_id_part(q.get("Problem Name", "problem"))
        item_id = f"NEW_{problem_name}_{step_counter}"

        step_stem = _oatutor_notation_to_plain(
            _oatutor_row_stem_text(q, use_title_stem_fallback=use_title_stem_fallback)
        )
        problem_stem = _oatutor_notation_to_plain(current_problem_stem)
        stem = f"{problem_stem} {step_stem}".strip() if problem_stem else step_stem

        step_images = str(q.get("Images", "") or q.get("images", "") or "").strip()
        image_urls = " ".join(
            part for part in (current_problem_images, step_images) if part
        )

        mc_raw = q.get("mcChoices")
        if mc_raw is None:
            mc_raw = q.get("mc_choices", "")
        mc = str(mc_raw or "")
        if at == "mc" and not mc.strip():
            mc = f"{key}|other"

        rows.append(
            {
                "item_id": item_id,
                "stem": stem,
                "answer_key": key,
                "answer_type": at,
                "mc_choices": mc if at == "mc" else "",
                "image_urls": image_urls,
            }
        )
        index_to_item_id[i] = item_id
        item_id_to_question_index[item_id] = i

    return pd.DataFrame(rows), index_to_item_id, item_id_to_question_index, index_to_skip_reason


def _item_calibration_lookup(
    questions: list[dict[str, Any]],
    *,
    use_title_stem_fallback: bool = False,
) -> tuple[dict[str, dict[str, str]], dict[int, dict[str, str]], list[dict[str, str]]]:
    """Map item_id and question index -> exact stem/key written to items.csv."""
    items_df, _index_to_item_id, item_id_to_question_index, _ = (
        _oatutor_questions_to_items_df(
            questions, use_title_stem_fallback=use_title_stem_fallback
        )
    )
    lookup: dict[str, dict[str, str]] = {}
    index_lookup: dict[int, dict[str, str]] = {}
    items: list[dict[str, str]] = []
    for _, row in items_df.iterrows():
        item_id = str(row["item_id"])
        entry = {
            "calibrationStem": str(row["stem"]),
            "calibrationKey": str(row["answer_key"]),
        }
        lookup[item_id] = entry
        qi = item_id_to_question_index.get(item_id)
        if qi is not None:
            index_lookup[int(qi)] = entry
        items.append({"itemId": item_id, **entry})
    return lookup, index_lookup, items


def _attach_calibration_fields(
    debug_rows: list[dict[str, Any]],
    item_calibration_lookup: dict[str, dict[str, str]] | None,
    index_calibration_lookup: dict[int, dict[str, str]] | None = None,
) -> None:
    """Attach stem/key by itemId, else by question index (never looks like a missing key)."""
    for row in debug_rows:
        item_id = row.get("itemId")
        if item_id and item_calibration_lookup and item_id in item_calibration_lookup:
            row.update(item_calibration_lookup[item_id])
            continue
        idx = row.get("index")
        if (
            index_calibration_lookup
            and idx is not None
            and int(idx) in index_calibration_lookup
        ):
            row.update(index_calibration_lookup[int(idx)])


def _apply_figure_status(
    row: dict[str, Any],
    item_id: str | None,
    figure_status_by_item_id: dict[str, dict[str, Any]] | None,
) -> None:
    """Attach loud figure fields when the item had Images URLs this run."""
    if not item_id or not figure_status_by_item_id:
        return
    status = figure_status_by_item_id.get(item_id)
    if not status:
        return
    row["figureAttached"] = bool(status.get("figureAttached"))
    row["figureCount"] = int(status.get("figureCount") or 0)
    err = status.get("figureError")
    if err:
        row["figureError"] = str(err)
    # Back-compat alias used by older UI checks
    if not row["figureAttached"]:
        row["figureUnavailable"] = True


def load_item_params_lookup(item_params_csv: str | Path) -> dict[str, dict[str, Any]]:
    params = pd.read_csv(item_params_csv)
    if "item_id" not in params.columns or "difficulty_logit" not in params.columns:
        raise ValueError("item_params CSV must contain item_id and difficulty_logit")
    out: dict[str, dict[str, Any]] = {}
    for _, row in params.iterrows():
        logit = row["difficulty_logit"]
        if pd.isna(logit):
            continue
        out[str(row["item_id"])] = {
            "item_id": str(row["item_id"]),
            "difficulty_logit": float(logit),
            "se_difficulty": (
                float(row["se_difficulty"])
                if "se_difficulty" in params.columns and not pd.isna(row.get("se_difficulty"))
                else None
            ),
        }
    return out


def run_rasch_fit(
    responses_csv: str | Path,
    item_params_csv: str | Path,
    fit_summary_json: str | Path,
    anchor_params_csv: str | Path | None = None,
    new_item_ids: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    r_script = Path(__file__).resolve().parent / "run_rasch.R"
    if not r_script.is_file():
        raise FileNotFoundError(f"run_rasch.R not found at {r_script}")

    cmd = [
        "Rscript",
        str(r_script),
        str(Path(responses_csv)),
        str(Path(item_params_csv)),
        str(Path(fit_summary_json)),
    ]
    if anchor_params_csv:
        cmd.append(str(Path(anchor_params_csv)))
        if new_item_ids:
            ids_path = Path(responses_csv).parent / "new_item_ids.txt"
            ids_path.write_text("\n".join(new_item_ids), encoding="utf-8")
            cmd.append(str(ids_path))
        else:
            cmd.append("")
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _record_response_matrix_stats(
    meta: dict[str, Any],
    response_matrix: pd.DataFrame | None,
) -> None:
    """Stamp cell-level counts for the Rasch response matrix onto ``meta``.

    Written in meta as ``cellsTotal`` (respondents x calibrated items) and
    ``cellsMissingCount`` (NA / truncated-wrong cells in the matrix). The
    event logger prefers these over OATutor-row counts so Firestore records
    reflect the actual grading workload, not the raw row count. Silently
    skipped if the matrix is unavailable (e.g. generator failed before any
    responses landed) so the service never fails because of logging bookkeeping.
    """
    if response_matrix is None:
        return
    try:
        total_cells = int(response_matrix.size)
        missing_cells = int(response_matrix.isna().sum().sum())
    except Exception:
        return
    meta["cellsTotal"] = total_cells
    meta["cellsMissingCount"] = missing_cells


def _split_informative_new_items(
    new_responses: pd.DataFrame,
    new_item_ids: list[str],
) -> tuple[list[str], list[str]]:
    informative: list[str] = []
    zero_variance: list[str] = []
    for item_id in new_item_ids:
        if item_id not in new_responses.columns:
            zero_variance.append(item_id)
            continue
        series = new_responses[item_id].dropna()
        if len(series) >= 2 and series.nunique() >= 2:
            informative.append(item_id)
        else:
            zero_variance.append(item_id)
    return informative, zero_variance


def _saturation_directions(
    responses: pd.DataFrame,
    item_ids: list[str],
) -> dict[str, str]:
    """Map saturated item_id -> 'all_correct' or 'all_wrong'."""
    directions: dict[str, str] = {}
    for item_id in item_ids:
        if item_id not in responses.columns:
            continue
        series = responses[item_id].dropna()
        if len(series) < 1 or series.nunique() >= 2:
            continue
        directions[str(item_id)] = "all_correct" if int(series.iloc[0]) == 1 else "all_wrong"
    return directions


def _fallback_saturation_logit(direction: str) -> float:
    """Anchor-free fallback when Rasch does not return an estimate."""
    if direction == "all_correct":
        level = DIFFICULTY_LEVELS[0]
        return (level.min_logit + level.max_logit) / 2
    if direction == "all_wrong":
        level = DIFFICULTY_LEVELS[-1]
        return (level.min_logit + level.max_logit) / 2
    return 0.0


def _finite_difficulty_logit(entry: dict[str, Any] | None) -> float | None:
    if not entry:
        return None
    logit = entry.get("difficulty_logit")
    if logit is None or (isinstance(logit, float) and pd.isna(logit)):
        return None
    try:
        value = float(logit)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _se_difficulty_from_entry(entry: dict[str, Any] | None) -> float | None:
    if not entry:
        return None
    se = entry.get("se_difficulty")
    if se is None or (isinstance(se, float) and pd.isna(se)):
        return None
    try:
        value = float(se)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _summarize_respondent_abilities(abilities_csv: str | Path) -> dict[str, Any] | None:
    path = Path(abilities_csv)
    if not path.is_file():
        return None
    df = pd.read_csv(path)
    if "theta" not in df.columns:
        return None
    theta = pd.to_numeric(df["theta"], errors="coerce").dropna()
    if theta.empty:
        return None
    sd = float(theta.std(ddof=1)) if len(theta) > 1 else 0.0
    return {
        "meanTheta": float(theta.mean()),
        "sdTheta": sd,
        "nRespondents": int(len(theta)),
    }


def _build_evaluation_rows(
    questions: list[dict[str, Any]],
    index_to_item_id: dict[int, str | None],
    params_lookup: dict[str, dict[str, Any]],
    min_logit: float,
    max_logit: float,
    *,
    calibration_status: str,
    zero_variance_item_ids: set[str] | None = None,
    saturation_directions: dict[str, str] | None = None,
    recover_saturated_scores: bool = False,
    problem_indices_for_steps: dict[int, int] | None = None,
    include_unscored: bool = True,
    index_to_skip_reason: dict[int, str] | None = None,
    item_calibration_lookup: dict[str, dict[str, str]] | None = None,
    index_calibration_lookup: dict[int, dict[str, str]] | None = None,
    figure_status_by_item_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    zero_variance_item_ids = zero_variance_item_ids or set()
    saturation_directions = saturation_directions or {}
    skip_reasons = index_to_skip_reason or {}
    figure_status_by_item_id = figure_status_by_item_id or {}
    kept_step_indices: set[int] = set()
    debug_rows: list[dict[str, Any]] = []

    for idx, question in enumerate(questions):
        item_id = index_to_item_id.get(idx)
        row_type = str(question.get("Row Type", "") or "").strip().lower()

        if row_type == "problem":
            debug_rows.append(
                {
                    "index": idx,
                    "problemName": question.get("Problem Name"),
                    "rowType": question.get("Row Type"),
                    "difficultyLogit": None,
                    "itemId": None,
                    "kept": False,
                    "reason": "companion_problem_row",
                }
            )
            continue

        if not item_id:
            skip_reason = skip_reasons.get(idx, "not_calibratable")
            if skip_reason in KEEP_VISIBLE_UNSCORED_REASONS:
                # Excluded from the Rasch run (no item id) but kept visible as
                # an unscored row so the user can fix the key by hand.
                debug_rows.append(
                    {
                        "index": idx,
                        "problemName": question.get("Problem Name"),
                        "rowType": question.get("Row Type"),
                        "difficultyLogit": None,
                        "itemId": None,
                        "kept": include_unscored,
                        "reason": skip_reason,
                        "scored": False,
                    }
                )
                if include_unscored:
                    kept_step_indices.add(idx)
                continue
            debug_rows.append(
                {
                    "index": idx,
                    "problemName": question.get("Problem Name"),
                    "rowType": question.get("Row Type"),
                    "difficultyLogit": None,
                    "itemId": None,
                    "kept": False,
                    "reason": skip_reason,
                }
            )
            continue

        if calibration_status == CALIBRATION_INSUFFICIENT:
            row: dict[str, Any] = {
                "index": idx,
                "problemName": question.get("Problem Name"),
                "rowType": question.get("Row Type"),
                "difficultyLogit": None,
                "itemId": item_id,
                "kept": include_unscored,
                "reason": CALIBRATION_INSUFFICIENT,
                "scored": False,
            }
            if item_id and item_id in zero_variance_item_ids:
                direction = saturation_directions.get(item_id)
                if direction:
                    row["saturationDirection"] = direction
            debug_rows.append(row)
            if include_unscored:
                kept_step_indices.add(idx)
            continue

        if calibration_status in (CALIBRATION_FAILED, CALIBRATION_ANCHOR_MISSING):
            debug_rows.append(
                {
                    "index": idx,
                    "problemName": question.get("Problem Name"),
                    "rowType": question.get("Row Type"),
                    "difficultyLogit": None,
                    "itemId": item_id,
                    "kept": include_unscored,
                    "reason": calibration_status,
                    "scored": False,
                }
            )
            if include_unscored:
                kept_step_indices.add(idx)
            continue

        if item_id in zero_variance_item_ids:
            direction = saturation_directions.get(item_id)
            entry = params_lookup.get(item_id) if recover_saturated_scores else None

            if recover_saturated_scores and (
                _finite_difficulty_logit(entry) is not None or direction
            ):
                logit = _finite_difficulty_logit(entry)
                se_difficulty = _se_difficulty_from_entry(entry)
                if logit is not None:
                    reason = SATURATION_LOW_CONFIDENCE
                elif direction:
                    logit = _fallback_saturation_logit(direction)
                    reason = SATURATION_FALLBACK_NO_RASCH
                    se_difficulty = None
                else:
                    logit = None
                    reason = "zero_variance"

                if logit is not None:
                    keep = min_logit <= logit <= max_logit
                    debug_rows.append(
                        {
                            "index": idx,
                            "problemName": question.get("Problem Name"),
                            "rowType": question.get("Row Type"),
                            "difficultyLogit": logit,
                            "difficultyLabel": label_for_logit(logit),
                            "seDifficulty": se_difficulty,
                            "saturationDirection": direction,
                            "itemId": item_id,
                            "kept": keep or include_unscored,
                            "reason": reason,
                            "scored": True,
                            "lowConfidence": True,
                            "difficultyUncertain": True,
                        }
                    )
                    if keep or include_unscored:
                        kept_step_indices.add(idx)
                    continue

            row: dict[str, Any] = {
                "index": idx,
                "problemName": question.get("Problem Name"),
                "rowType": question.get("Row Type"),
                "difficultyLogit": None,
                "itemId": item_id,
                "kept": include_unscored,
                "reason": "zero_variance",
                "scored": False,
            }
            if direction:
                row["saturationDirection"] = direction
            debug_rows.append(row)
            if include_unscored:
                kept_step_indices.add(idx)
            continue

        entry = params_lookup.get(item_id)
        if entry is None:
            debug_rows.append(
                {
                    "index": idx,
                    "problemName": question.get("Problem Name"),
                    "rowType": question.get("Row Type"),
                    "difficultyLogit": None,
                    "itemId": item_id,
                    "kept": include_unscored,
                    "reason": "no_rasch_estimate",
                    "scored": False,
                }
            )
            if include_unscored:
                kept_step_indices.add(idx)
            continue

        logit = entry["difficulty_logit"]
        keep = min_logit <= logit <= max_logit
        debug_rows.append(
            {
                "index": idx,
                "problemName": question.get("Problem Name"),
                "rowType": question.get("Row Type"),
                "difficultyLogit": logit,
                "difficultyLabel": label_for_logit(logit),
                "seDifficulty": _se_difficulty_from_entry(entry),
                "itemId": item_id,
                "kept": keep,
                "reason": "in_range" if keep else "outside_logit_range",
                "scored": True,
            }
        )
        if keep:
            kept_step_indices.add(idx)

    # Companion problem rows: keep if any step under that problem is kept.
    if problem_indices_for_steps:
        for step_idx, problem_idx in problem_indices_for_steps.items():
            if step_idx in kept_step_indices:
                for row in debug_rows:
                    if row["index"] == problem_idx:
                        row["kept"] = True
                        row["reason"] = "companion_problem_row"
                        break

    for row in debug_rows:
        _apply_figure_status(
            row, row.get("itemId"), figure_status_by_item_id
        )

    _attach_calibration_fields(
        debug_rows, item_calibration_lookup, index_calibration_lookup
    )
    return debug_rows

def _questions_from_debug_rows(
    questions: list[dict[str, Any]],
    debug_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in debug_rows:
        if not row.get("kept"):
            continue
        q = dict(questions[row["index"]])
        logit = row.get("difficultyLogit")
        q["difficultyLogit"] = logit
        q["difficultyLabel"] = row.get("difficultyLabel") or label_for_logit(logit)
        q["difficultyScored"] = row.get("scored", logit is not None)
        q["difficultyStatus"] = row.get("reason")
        enriched.append(q)
    return enriched


def _apply_question_count_cap(
    debug_rows: list[dict[str, Any]],
    problem_indices_for_steps: dict[int, int] | None,
    max_count: int | None,
) -> None:
    """Limit in-range scored step rows; refresh companion problem rows."""
    if max_count is None or max_count < 0:
        return
    in_range_steps = [
        r
        for r in debug_rows
        if str(r.get("rowType", "")).lower() == "step" and r.get("reason") == "in_range"
    ]
    allowed = {r["index"] for r in in_range_steps[:max_count]}
    for row in debug_rows:
        if str(row.get("rowType", "")).lower() != "step":
            continue
        if row.get("reason") == "in_range":
            row["kept"] = row["index"] in allowed
    kept_steps = {r["index"] for r in debug_rows if r.get("kept") and str(r.get("rowType", "")).lower() == "step"}
    if problem_indices_for_steps:
        for step_idx, problem_idx in problem_indices_for_steps.items():
            for row in debug_rows:
                if row["index"] == problem_idx:
                    row["kept"] = step_idx in kept_steps
                    if row["kept"]:
                        row["reason"] = "companion_problem_row"
                    break


def _map_problem_indices(
    questions: list[dict[str, Any]],
) -> dict[int, int]:
    """Map step index -> preceding problem row index."""
    mapping: dict[int, int] = {}
    last_problem_idx: int | None = None
    for idx, q in enumerate(questions):
        row_type = str(q.get("Row Type", "") or "").strip().lower()
        if row_type == "problem":
            last_problem_idx = idx
        elif row_type == "step" and last_problem_idx is not None:
            mapping[idx] = last_problem_idx
    return mapping


def filter_questions_by_difficulty_range(
    questions: list[dict[str, Any]],
    min_logit: float,
    max_logit: float,
    params_lookup: dict[str, dict[str, Any]] | None = None,
    index_to_item_id: dict[int, str | None] | None = None,
    calibration_status: str = CALIBRATION_CALIBRATED,
    zero_variance_item_ids: set[str] | None = None,
    *,
    saturation_directions: dict[str, str] | None = None,
    recover_saturated_scores: bool = False,
    max_count: int | None = None,
    include_unscored: bool = True,
    index_to_skip_reason: dict[int, str] | None = None,
    figure_status_by_item_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    meta: dict[str, Any] = {
        "mode": "rasch_logit_item_id",
        "minLogit": min_logit,
        "maxLogit": max_logit,
        "calibrationStatus": calibration_status,
        "maxCount": max_count,
        "includeUnscored": include_unscored,
    }

    item_calibration_lookup, index_calibration_lookup, calibration_items = (
        _item_calibration_lookup(questions)
    )
    meta["calibrationItems"] = calibration_items

    if index_to_item_id is None:
        _, index_to_item_id, _, index_to_skip_reason = _oatutor_questions_to_items_df(
            questions
        )
    elif index_to_skip_reason is None:
        _, _, _, index_to_skip_reason = _oatutor_questions_to_items_df(questions)

    problem_map = _map_problem_indices(questions)
    debug_rows = _build_evaluation_rows(
        questions,
        index_to_item_id,
        params_lookup or {},
        min_logit,
        max_logit,
        calibration_status=calibration_status,
        zero_variance_item_ids=zero_variance_item_ids,
        saturation_directions=saturation_directions,
        recover_saturated_scores=recover_saturated_scores,
        problem_indices_for_steps=problem_map,
        include_unscored=include_unscored,
        index_to_skip_reason=index_to_skip_reason,
        item_calibration_lookup=item_calibration_lookup,
        index_calibration_lookup=index_calibration_lookup,
        figure_status_by_item_id=figure_status_by_item_id,
    )
    _apply_question_count_cap(debug_rows, problem_map, max_count)
    filtered = _questions_from_debug_rows(questions, debug_rows)
    meta["totalBefore"] = len(questions)
    meta["totalAfter"] = len(filtered)
    meta["keptInRange"] = sum(
        1 for r in debug_rows if r.get("reason") == "in_range" and r.get("kept")
    )
    meta["keptUnscored"] = sum(
        1
        for r in debug_rows
        if r.get("kept") and r.get("reason") in UNSCORED_FILTER_REASONS
    )
    _persist_skip_snapshot(questions, debug_rows)
    return filtered, debug_rows, meta


def _is_rasch_dof_error(exc: subprocess.CalledProcessError) -> bool:
    text = f"{exc.stderr or ''}{exc.stdout or ''}{exc}".lower()
    return "degrees of freedom" in text or "too few degrees" in text


def _legacy_insufficient_result(
    questions: list[dict[str, Any]],
    min_logit: float,
    max_logit: float,
    meta: dict[str, Any],
    *,
    index_to_item_id: dict[int, str | None],
    zero_var: list[str] | None = None,
    saturation_directions: dict[str, str] | None = None,
    n_informative: int | None = None,
    max_count: int | None = None,
    include_unscored: bool = True,
    index_to_skip_reason: dict[int, str] | None = None,
    figure_status_by_item_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    meta["calibrationStatus"] = CALIBRATION_INSUFFICIENT
    meta["skipReason"] = "free_rasch_insufficient_data"
    meta["userMessage"] = FREE_RASCH_INSUFFICIENT_MESSAGE
    if n_informative is not None:
        meta["nInformativeNewItems"] = n_informative
    if zero_var:
        meta["zeroVarianceNewItems"] = list(zero_var)
    filtered, debug, fmeta = filter_questions_by_difficulty_range(
        questions,
        min_logit,
        max_logit,
        params_lookup={},
        index_to_item_id=index_to_item_id,
        index_to_skip_reason=index_to_skip_reason,
        figure_status_by_item_id=figure_status_by_item_id,
        calibration_status=CALIBRATION_INSUFFICIENT,
        zero_variance_item_ids=set(zero_var or []),
        saturation_directions=saturation_directions,
        max_count=max_count,
        include_unscored=include_unscored,
    )
    return filtered, debug, {**meta, **fmeta}


def _write_items_and_figures(
    items_df: pd.DataFrame,
    items_path: Path,
    index_to_item_id: dict[int, str | None],
    item_id_to_question_index: dict[str, int],
    index_to_skip_reason: dict[int, str],
) -> tuple[
    pd.DataFrame, dict[int, str | None], dict[int, str], dict[str, dict[str, Any]]
]:
    """Fetch figures once; keep text-only items when attach fails; write CSV + cache."""
    (
        items_df,
        index_to_item_id,
        index_to_skip_reason,
        cache,
        figure_status_by_item_id,
    ) = prepare_items_for_vision(
        items_df,
        index_to_item_id,
        item_id_to_question_index,
        index_to_skip_reason,
    )
    items_path.parent.mkdir(parents=True, exist_ok=True)
    items_df.to_csv(items_path, index=False)
    save_figures_cache(items_path.parent / "figures_cache.json", cache)
    return items_df, index_to_item_id, index_to_skip_reason, figure_status_by_item_id


def _legacy_auto_calibrate(
    questions: list[dict[str, Any]],
    min_logit: float,
    max_logit: float,
    meta: dict[str, Any],
    *,
    max_count: int | None = None,
    include_unscored: bool = True,
    n_respondents: int | None = None,
    progress_callback=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Free Rasch path (no anchors) — default live calibration."""
    items_df, index_to_item_id, item_id_to_question_index, index_to_skip_reason = (
        _oatutor_questions_to_items_df(questions)
    )
    figure_status_by_item_id: dict[str, dict[str, Any]] = {}
    if items_df.empty:
        meta["calibrationStatus"] = CALIBRATION_INSUFFICIENT
        meta["skipReason"] = "empty_items"
        filtered, debug, fmeta = filter_questions_by_difficulty_range(
            questions,
            min_logit,
            max_logit,
            params_lookup={},
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status_by_item_id,
            calibration_status=CALIBRATION_INSUFFICIENT,
            max_count=max_count,
            include_unscored=include_unscored,
        )
        return filtered, debug, {**meta, **fmeta}

    n_resp = _resolve_n_respondents(n_respondents)
    # Record how the respondents actually ran so the event logger stamps the
    # same values the Rasch job used (not stale env defaults).
    meta["nRespondents"] = n_resp
    meta["respondentTemperature"] = _calibration_temperature()
    meta["calibrationTemperature"] = _calibration_temperature()
    meta["calibrationModel"] = _calibration_model()
    meta["respondentMode"] = get_respondent_mode()
    meta["respondentReasoning"] = respondent_reasoning_enabled()
    temp_dir = tempfile.mkdtemp(prefix="prompthive_rasch_legacy_")
    zero_var: list[str] = []
    saturation_dirs: dict[str, str] = {}
    try:
        items_path = Path(temp_dir) / "items.csv"
        run_dir = Path(temp_dir) / "run"
        run_dir.mkdir(parents=True)
        (
            items_df,
            index_to_item_id,
            index_to_skip_reason,
            figure_status_by_item_id,
        ) = _write_items_and_figures(
            items_df,
            items_path,
            index_to_item_id,
            item_id_to_question_index,
            index_to_skip_reason,
        )
        meta["figuresSummary"] = figures_summary_line(figure_status_by_item_id)
        if figure_status_by_item_id:
            meta["figureStatusByItemId"] = figure_status_by_item_id
        if items_df.empty:
            meta["calibrationStatus"] = CALIBRATION_INSUFFICIENT
            meta["skipReason"] = "empty_items_after_vision"
            filtered, debug, fmeta = filter_questions_by_difficulty_range(
                questions,
                min_logit,
                max_logit,
                params_lookup={},
                index_to_item_id=index_to_item_id,
                index_to_skip_reason=index_to_skip_reason,
                figure_status_by_item_id=figure_status_by_item_id,
                calibration_status=CALIBRATION_INSUFFICIENT,
                max_count=max_count,
                include_unscored=include_unscored,
            )
            return filtered, debug, {**meta, **fmeta}
        args = _generation_namespace(
            str(items_path),
            n_resp,
            str(run_dir),
            progress_callback=progress_callback,
        )
        asyncio.run(generate_responses_async_bounded(args))
        _emit_progress(
            progress_callback,
            n_resp * max(len(items_df), 1),
            n_resp * max(len(items_df), 1),
            "computing",
        )
        responses_csv = run_dir / "responses.csv"
        item_params_csv = run_dir / "item_params.csv"
        fit_summary_json = run_dir / "fit_summary.json"
        new_resp = pd.read_csv(responses_csv)
        if "respondent_id" in new_resp.columns:
            new_resp = new_resp.set_index("respondent_id")
        _record_response_matrix_stats(meta, new_resp)
        informative, zero_var = _split_informative_new_items(
            new_resp, items_df["item_id"].astype(str).tolist()
        )
        saturation_dirs = _saturation_directions(new_resp, zero_var)
        if len(informative) < DEFAULT_MIN_INFORMATIVE_NEW:
            return _legacy_insufficient_result(
                questions,
                min_logit,
                max_logit,
                meta,
                index_to_item_id=index_to_item_id,
                zero_var=zero_var,
                saturation_directions=saturation_dirs,
                n_informative=len(informative),
                max_count=max_count,
                include_unscored=include_unscored,
                index_to_skip_reason=index_to_skip_reason,
                figure_status_by_item_id=figure_status_by_item_id,
            )

        try:
            run_rasch_fit(responses_csv, item_params_csv, fit_summary_json)
        except subprocess.CalledProcessError as exc:
            if _is_rasch_dof_error(exc):
                meta["error"] = (exc.stderr or exc.stdout or str(exc))[:4000]
                return _legacy_insufficient_result(
                    questions,
                    min_logit,
                    max_logit,
                    meta,
                    index_to_item_id=index_to_item_id,
                    zero_var=zero_var,
                    saturation_directions=saturation_dirs,
                    n_informative=len(informative),
                    max_count=max_count,
                    include_unscored=include_unscored,
                    index_to_skip_reason=index_to_skip_reason,
                    figure_status_by_item_id=figure_status_by_item_id,
                )
            raise

        lookup = load_item_params_lookup(item_params_csv)
        meta["calibrationStatus"] = CALIBRATION_CALIBRATED
        filtered, debug, fmeta = filter_questions_by_difficulty_range(
            questions,
            min_logit,
            max_logit,
            params_lookup=lookup,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status_by_item_id,
            calibration_status=CALIBRATION_CALIBRATED,
            zero_variance_item_ids=set(zero_var),
            saturation_directions=saturation_dirs,
            max_count=max_count,
            include_unscored=include_unscored,
        )
        return filtered, debug, {**meta, **fmeta}
    except subprocess.CalledProcessError as exc:
        if _is_rasch_dof_error(exc):
            meta["error"] = (exc.stderr or exc.stdout or str(exc))[:4000]
            return _legacy_insufficient_result(
                questions,
                min_logit,
                max_logit,
                meta,
                index_to_item_id=index_to_item_id,
                zero_var=zero_var,
                saturation_directions=saturation_dirs,
                max_count=max_count,
                include_unscored=include_unscored,
                index_to_skip_reason=index_to_skip_reason,
                figure_status_by_item_id=figure_status_by_item_id,
            )
        meta["calibrationStatus"] = CALIBRATION_FAILED
        meta["error"] = (exc.stderr or exc.stdout or str(exc))[:4000]
        filtered, debug, fmeta = filter_questions_by_difficulty_range(
            questions,
            min_logit,
            max_logit,
            params_lookup={},
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status_by_item_id,
            calibration_status=CALIBRATION_FAILED,
            max_count=max_count,
            include_unscored=include_unscored,
        )
        return filtered, debug, {**meta, **fmeta}
    finally:
        if os.getenv("QUESTION_DIFFICULTY_KEEP_TEMP_DIR", "").lower() not in (
            "1",
            "true",
            "yes",
        ):
            shutil.rmtree(temp_dir, ignore_errors=True)


def sanitize_for_json(value: Any) -> Any:
    """Recursively replace non-JSON-safe floats (inf, -inf, nan) with null."""
    if isinstance(value, float):
        if math.isinf(value) or math.isnan(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_for_json(v) for v in value]
    if isinstance(value, tuple):
        return [sanitize_for_json(v) for v in value]
    return value


def difficulty_meta_for_api(meta: dict[str, Any]) -> dict[str, Any]:
    """Return difficulty metadata safe for browser JSON.parse (no Infinity/NaN)."""
    return sanitize_for_json(meta)


def resolve_difficulty_filter(
    difficulty_filter: dict[str, Any] | None,
    *,
    default_min_logit: float,
    default_max_logit: float,
) -> dict[str, Any]:
    """Parse API difficultyFilter payload into logit bounds and filter options."""
    from abscribe_backend.services.question_difficulty.difficulty_levels import (
        logit_range_from_levels,
    )

    payload = dict(difficulty_filter or {})
    no_filter = payload.get("noFilter")
    if isinstance(no_filter, str):
        no_filter = no_filter.strip().lower() in ("1", "true", "yes")
    extra: dict[str, Any] = {}
    if no_filter:
        max_count = payload.get("maxCount")
        if max_count is not None and max_count != "":
            max_count = int(max_count)
        else:
            max_count = None

        include_unscored = payload.get("includeUnscored", True)
        if isinstance(include_unscored, str):
            include_unscored = include_unscored.strip().lower() in ("1", "true", "yes")

        n_respondents_raw = payload.get("nRespondents", payload.get("n_respondents"))
        if n_respondents_raw is not None and n_respondents_raw != "":
            n_respondents = max(1, min(int(n_respondents_raw), MAX_N_RESPONDENTS))
        else:
            n_respondents = _resolve_n_respondents(None)

        questions_to_generate = payload.get("questionsToGenerate")
        if questions_to_generate is not None and questions_to_generate != "":
            extra["questionsToGenerate"] = max(1, int(questions_to_generate))

        return {
            "min_logit": float("-inf"),
            "max_logit": float("inf"),
            "max_count": max_count,
            "include_unscored": bool(include_unscored),
            "n_respondents": n_respondents,
            "noFilter": True,
            **extra,
        }

    min_level = payload.get("minLevel")
    max_level = payload.get("maxLevel")
    if min_level is not None and max_level is not None:
        min_logit, max_logit, labels = logit_range_from_levels(
            int(min_level), int(max_level)
        )
        extra["minLevel"] = int(min(min_level, max_level))
        extra["maxLevel"] = int(max(min_level, max_level))
        extra["difficultyLabels"] = labels
    else:
        min_logit = float(payload.get("minLogit", default_min_logit))
        max_logit = float(payload.get("maxLogit", default_max_logit))

    max_count = payload.get("maxCount")
    if max_count is not None and max_count != "":
        max_count = int(max_count)
    else:
        max_count = None

    include_unscored = payload.get("includeUnscored", True)
    if isinstance(include_unscored, str):
        include_unscored = include_unscored.strip().lower() in ("1", "true", "yes")

    n_respondents_raw = payload.get("nRespondents", payload.get("n_respondents"))
    if n_respondents_raw is not None and n_respondents_raw != "":
        n_respondents = max(1, min(int(n_respondents_raw), MAX_N_RESPONDENTS))
    else:
        n_respondents = _resolve_n_respondents(None)

    questions_to_generate = payload.get("questionsToGenerate")
    if questions_to_generate is not None and questions_to_generate != "":
        extra["questionsToGenerate"] = max(1, int(questions_to_generate))

    return {
        "min_logit": min_logit,
        "max_logit": max_logit,
        "max_count": max_count,
        "include_unscored": bool(include_unscored),
        "n_respondents": n_respondents,
        **extra,
    }


def auto_calibrate_and_filter_questions(
    questions: list[dict[str, Any]],
    min_logit: float,
    max_logit: float,
    *,
    max_count: int | None = None,
    include_unscored: bool = True,
    score_saturated_via_anchors: bool = False,
    n_respondents: int | None = None,
    progress_callback=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Free Rasch calibration (default) or anchor FIPC when opted in."""
    meta: dict[str, Any] = {
        "mode": "rasch_auto_legacy",
        "minLogit": min_logit,
        "maxLogit": max_logit,
        "maxCount": max_count,
        "includeUnscored": include_unscored,
    }

    if not questions:
        meta["calibrationStatus"] = CALIBRATION_INSUFFICIENT
        meta["skipReason"] = "no_questions"
        return [], [], meta

    items_df, index_to_item_id, item_id_to_question_index, index_to_skip_reason = (
        _oatutor_questions_to_items_df(
            questions,
            use_title_stem_fallback=score_saturated_via_anchors,
        )
    )
    figure_status_by_item_id: dict[str, dict[str, Any]] = {}

    if items_df.empty:
        meta["calibrationStatus"] = CALIBRATION_INSUFFICIENT
        meta["skipReason"] = "empty_items"
        filtered, debug, fmeta = filter_questions_by_difficulty_range(
            questions,
            min_logit,
            max_logit,
            params_lookup={},
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status_by_item_id,
            calibration_status=CALIBRATION_INSUFFICIENT,
            max_count=max_count,
            include_unscored=include_unscored,
        )
        return filtered, debug, {**meta, **fmeta}

    explicit_flag = os.getenv("QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION", "").strip().lower()
    if explicit_flag not in ("1", "true", "yes"):
        return _legacy_auto_calibrate(
            questions,
            min_logit,
            max_logit,
            meta,
            max_count=max_count,
            include_unscored=include_unscored,
            n_respondents=n_respondents,
            progress_callback=progress_callback,
        )

    meta["mode"] = "rasch_auto_fipc"
    meta["fipcMode"] = "human_degenerate_mode_c"
    meta["humanItemParamsRef"] = str(HUMAN_ITEM_PARAMS_PATH)

    if not anchor_calibration_enabled():
        meta["calibrationStatus"] = CALIBRATION_ANCHOR_MISSING
        meta["skipReason"] = "anchor_bank_missing_or_invalid"
        filtered, debug, fmeta = filter_questions_by_difficulty_range(
            questions,
            min_logit,
            max_logit,
            params_lookup={},
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status_by_item_id,
            calibration_status=CALIBRATION_ANCHOR_MISSING,
            max_count=max_count,
            include_unscored=include_unscored,
        )
        return filtered, debug, {**meta, **fmeta}

    try:
        bundle = load_anchor_bundle()
    except FileNotFoundError as exc:
        meta["calibrationStatus"] = CALIBRATION_ANCHOR_MISSING
        meta["error"] = str(exc)
        filtered, debug, fmeta = filter_questions_by_difficulty_range(
            questions,
            min_logit,
            max_logit,
            params_lookup={},
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status_by_item_id,
            calibration_status=CALIBRATION_ANCHOR_MISSING,
            max_count=max_count,
            include_unscored=include_unscored,
        )
        return filtered, debug, {**meta, **fmeta}

    anchor_items = bundle["items"]
    anchor_responses = bundle["responses"]
    anchor_params = bundle["params"]
    n_resp = _resolve_n_respondents(n_respondents)
    meta["nRespondents"] = n_resp
    meta["nAnchors"] = len(anchor_items)
    meta["calibrationModel"] = _calibration_model()
    meta["calibrationTemperature"] = _calibration_temperature()
    if score_saturated_via_anchors:
        meta["respondentTemperature"] = _existing_score_temperature()
    else:
        meta["respondentTemperature"] = _calibration_temperature()
    meta["respondentMode"] = get_respondent_mode()
    meta["respondentReasoning"] = respondent_reasoning_enabled()
    meta["anchorManifest"] = bundle.get("manifest", {})

    temp_dir = tempfile.mkdtemp(prefix="prompthive_rasch_fipc_")
    run_dir = Path(temp_dir) / "run"
    run_dir.mkdir(parents=True)

    try:
        new_items_path = run_dir / "new_items.csv"
        (
            items_df,
            index_to_item_id,
            index_to_skip_reason,
            figure_status_by_item_id,
        ) = _write_items_and_figures(
            items_df,
            new_items_path,
            index_to_item_id,
            item_id_to_question_index,
            index_to_skip_reason,
        )
        meta["figuresSummary"] = figures_summary_line(figure_status_by_item_id)
        if figure_status_by_item_id:
            meta["figureStatusByItemId"] = figure_status_by_item_id
        if items_df.empty:
            meta["calibrationStatus"] = CALIBRATION_INSUFFICIENT
            meta["skipReason"] = "empty_items_after_vision"
            filtered, debug, fmeta = filter_questions_by_difficulty_range(
                questions,
                min_logit,
                max_logit,
                params_lookup={},
                index_to_item_id=index_to_item_id,
                index_to_skip_reason=index_to_skip_reason,
                figure_status_by_item_id=figure_status_by_item_id,
                calibration_status=CALIBRATION_INSUFFICIENT,
                max_count=max_count,
                include_unscored=include_unscored,
            )
            return filtered, debug, {**meta, **fmeta}

        cal_model = _calibration_model()
        gen_temp = (
            _existing_score_temperature()
            if score_saturated_via_anchors
            else _calibration_temperature()
        )
        args = _generation_namespace(
            str(new_items_path),
            n_resp,
            str(run_dir / "new_only"),
            temperature=gen_temp,
            progress_callback=progress_callback,
        )
        asyncio.run(generate_responses_async_bounded(args))
        _emit_progress(
            progress_callback,
            n_resp * max(len(items_df), 1),
            n_resp * max(len(items_df), 1),
            "computing",
        )

        new_resp_path = run_dir / "new_only" / "responses.csv"
        new_resp = pd.read_csv(new_resp_path)
        if "respondent_id" in new_resp.columns:
            new_resp = new_resp.set_index("respondent_id")
        new_resp = new_resp.reindex(respondent_id_order(n_resp))

        new_item_ids = items_df["item_id"].astype(str).tolist()
        # Response-matrix dimensions for the Firestore event logger
        # (``cellsTotal`` = respondents x calibrated items, ``cellsMissing``
        # = NA/truncated cells). Scoped to the newly calibrated items only.
        try:
            new_item_matrix = new_resp[new_item_ids]
        except Exception:
            new_item_matrix = new_resp
        _record_response_matrix_stats(meta, new_item_matrix)
        informative_new, zero_var_new = _split_informative_new_items(
            new_resp, new_item_ids
        )
        meta["nInformativeNewItems"] = len(informative_new)
        meta["zeroVarianceNewItems"] = zero_var_new
        saturation_dirs = _saturation_directions(new_resp, new_item_ids)

        if (
            len(informative_new) < DEFAULT_MIN_INFORMATIVE_NEW
            and not score_saturated_via_anchors
        ):
            meta["calibrationStatus"] = CALIBRATION_INSUFFICIENT
            filtered, debug, fmeta = filter_questions_by_difficulty_range(
                questions,
                min_logit,
                max_logit,
                params_lookup={},
                index_to_item_id=index_to_item_id,
                index_to_skip_reason=index_to_skip_reason,
                figure_status_by_item_id=figure_status_by_item_id,
                calibration_status=CALIBRATION_INSUFFICIENT,
                zero_variance_item_ids=set(zero_var_new),
                max_count=max_count,
                include_unscored=include_unscored,
            )
            return filtered, debug, {**meta, **fmeta}

        if len(informative_new) < DEFAULT_MIN_INFORMATIVE_NEW:
            meta["saturationRecovery"] = True

        # Merge items and responses: anchors first, then new (informative + zero-var for traceability).
        merged_items = pd.concat([anchor_items, items_df], ignore_index=True)
        anchor_cols = anchor_items["item_id"].astype(str).tolist()
        anchor_resp_aligned = anchor_responses.reindex(respondent_id_order(n_resp))
        merged_resp = pd.concat([anchor_resp_aligned, new_resp[new_item_ids]], axis=1)
        degenerate_anchors = identify_degenerate_items(
            anchor_resp_aligned, anchor_cols
        )
        flip_item_ids = list(degenerate_anchors)
        if score_saturated_via_anchors:
            flip_item_ids.extend(
                i for i in zero_var_new if i not in flip_item_ids
            )
        merged_resp = prepare_fipc_responses(merged_resp, flip_item_ids)

        items_path = run_dir / "items.csv"
        responses_csv = run_dir / "responses.csv"
        anchor_params_csv = run_dir / "anchor_params.csv"
        item_params_csv = run_dir / "item_params.csv"
        fit_summary_json = run_dir / "fit_summary.json"

        merged_items.to_csv(items_path, index=False)
        merged_resp.reset_index().rename(columns={"index": "respondent_id"}).to_csv(
            responses_csv, index=False
        )
        anchor_params.to_csv(anchor_params_csv, index=False)

        proc = run_rasch_fit(
            responses_csv,
            item_params_csv,
            fit_summary_json,
            anchor_params_csv=anchor_params_csv,
            new_item_ids=new_item_ids,
        )
        meta["raschStdout"] = (proc.stdout or "")[-2000:]

        lookup = load_item_params_lookup(item_params_csv)
        meta["calibrationStatus"] = CALIBRATION_CALIBRATED
        meta["calibrationLoaded"] = True
        if score_saturated_via_anchors:
            ability_summary = _summarize_respondent_abilities(
                item_params_csv.parent / "respondent_abilities.csv"
            )
            if ability_summary is not None:
                meta["respondentAbility"] = ability_summary

        filtered, debug, fmeta = filter_questions_by_difficulty_range(
            questions,
            min_logit,
            max_logit,
            params_lookup=lookup,
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status_by_item_id,
            calibration_status=CALIBRATION_CALIBRATED,
            zero_variance_item_ids=set(zero_var_new),
            saturation_directions=saturation_dirs if score_saturated_via_anchors else None,
            recover_saturated_scores=score_saturated_via_anchors,
            max_count=max_count,
            include_unscored=include_unscored,
        )
        out_meta = {**meta, **fmeta}
        out_meta["mode"] = "rasch_auto_fipc"
        if os.getenv("QUESTION_DIFFICULTY_KEEP_TEMP_DIR", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            out_meta["tempDir"] = temp_dir
        return filtered, debug, out_meta

    except subprocess.CalledProcessError as exc:
        meta["calibrationStatus"] = CALIBRATION_FAILED
        meta["error"] = (exc.stderr or exc.stdout or str(exc))[:4000]
        filtered, debug, fmeta = filter_questions_by_difficulty_range(
            questions,
            min_logit,
            max_logit,
            params_lookup={},
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status_by_item_id,
            calibration_status=CALIBRATION_FAILED,
            max_count=max_count,
            include_unscored=include_unscored,
        )
        return filtered, debug, {**meta, **fmeta}
    except Exception as exc:
        meta["calibrationStatus"] = CALIBRATION_FAILED
        meta["error"] = str(exc)
        filtered, debug, fmeta = filter_questions_by_difficulty_range(
            questions,
            min_logit,
            max_logit,
            params_lookup={},
            index_to_item_id=index_to_item_id,
            index_to_skip_reason=index_to_skip_reason,
            figure_status_by_item_id=figure_status_by_item_id,
            calibration_status=CALIBRATION_FAILED,
            max_count=max_count,
            include_unscored=include_unscored,
        )
        return filtered, debug, {**meta, **fmeta}
    finally:
        if os.getenv("QUESTION_DIFFICULTY_KEEP_TEMP_DIR", "").lower() not in (
            "1",
            "true",
            "yes",
        ):
            shutil.rmtree(temp_dir, ignore_errors=True)


# Backward-compatible helpers used by precalibrated CSV path.
def load_rasch_stem_lookup(
    items_csv: str | Path,
    item_params_csv: str | Path,
) -> dict[str, dict[str, Any]]:
    lookup = load_item_params_lookup(item_params_csv)
    items = pd.read_csv(items_csv)
    out: dict[str, dict[str, Any]] = {}
    for _, row in items.iterrows():
        item_id = str(row["item_id"])
        if item_id not in lookup:
            continue
        stem_key = " ".join(str(row.get("stem", "")).lower().split())
        out[stem_key] = {
            "item_id": item_id,
            "difficulty_logit": lookup[item_id]["difficulty_logit"],
        }
    return out


def load_rasch_stem_lookup_from_env() -> dict[str, dict[str, Any]] | None:
    items_csv = os.getenv("QUESTION_DIFFICULTY_ITEMS_CSV")
    params_csv = os.getenv("QUESTION_DIFFICULTY_ITEM_PARAMS_CSV")
    if not items_csv or not params_csv:
        return None
    return load_rasch_stem_lookup(items_csv, params_csv)


def score_existing_questions(
    questions: list[dict[str, Any]],
    *,
    n_respondents: int | None = None,
    progress_callback=None,
) -> dict[str, Any]:
    """Score EXISTING (already-authored) OATutor questions WITHOUT filtering any out.

    Feature 2: put existing sheet questions on the SAME difficulty scale as
    generated ones. This reuses ``auto_calibrate_and_filter_questions`` (the exact
    persona@1.75 + FIPC-against-human-anchors calibration used for generated
    questions) with the widest logit bounds and ``include_unscored=True`` so
    nothing is dropped, then returns the per-row evaluations. No calibration fork.

    ``questions`` must use the OATutor row schema ("Row Type", "Body Text",
    "Answer", "answerType", "Problem Name", "mcChoices"), in the SAME order as the
    caller's rows so each returned ``index`` maps back to the original row.
    """
    widest_min = DIFFICULTY_LEVELS[0].min_logit
    widest_max = DIFFICULTY_LEVELS[-1].max_logit

    _filtered, evaluations, meta = auto_calibrate_and_filter_questions(
        questions=questions,
        min_logit=widest_min,
        max_logit=widest_max,
        max_count=None,
        include_unscored=True,
        score_saturated_via_anchors=True,
        n_respondents=n_respondents,
        progress_callback=progress_callback,
    )

    scores = [
        {
            "index": row.get("index"),
            "itemId": row.get("itemId"),
            "problemName": row.get("problemName"),
            "rowType": row.get("rowType"),
            "difficultyLogit": row.get("difficultyLogit"),
            "difficultyLabel": row.get("difficultyLabel")
            or label_for_logit(row.get("difficultyLogit")),
            "scored": row.get("scored", row.get("difficultyLogit") is not None),
            "reason": row.get("reason"),
            "lowConfidence": row.get("lowConfidence", False),
            "difficultyUncertain": row.get("difficultyUncertain", False),
            "seDifficulty": row.get("seDifficulty"),
            "saturationDirection": row.get("saturationDirection"),
            "calibrationStem": row.get("calibrationStem"),
            "calibrationKey": row.get("calibrationKey"),
            "figureAttached": row.get("figureAttached"),
            "figureCount": row.get("figureCount"),
            "figureError": row.get("figureError"),
            "figureUnavailable": row.get("figureUnavailable", False),
        }
        for row in evaluations
    ]
    return {
        "scores": scores,
        "calibrationStatus": meta.get("calibrationStatus"),
        "mode": meta.get("mode"),
        "respondentTemperature": meta.get("respondentTemperature"),
        "respondentAbility": meta.get("respondentAbility"),
        # Response-matrix dimensions for the event logger. Omitted (left as
        # ``None`` by ``meta.get``) when calibration bailed out before
        # grading any respondents.
        "cellsTotal": meta.get("cellsTotal"),
        "cellsMissingCount": meta.get("cellsMissingCount"),
        "meta": {
            "figuresSummary": meta.get("figuresSummary"),
            "figureStatusByItemId": meta.get("figureStatusByItemId"),
        },
        "figuresSummary": meta.get("figuresSummary"),
    }
