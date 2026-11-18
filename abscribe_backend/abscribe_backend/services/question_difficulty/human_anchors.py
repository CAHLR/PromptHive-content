"""Human-calibrated item difficulties for FIPC anchoring (paper ground truth)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd

REFERENCE_DIR = Path(__file__).resolve().parent / "reference"
HUMAN_ITEM_PARAMS_PATH = REFERENCE_DIR / "human_item_params.csv"

# items.csv row i -> ANCHOR_{i:03d} <-> Qi
ANCHOR_TO_Q: dict[str, str] = {f"ANCHOR_{i:03d}": f"Q{i}" for i in range(1, 21)}
Q_TO_ANCHOR: dict[str, str] = {v: k for k, v in ANCHOR_TO_Q.items()}


def load_human_item_params_csv(path: Path | None = None) -> pd.DataFrame:
    csv_path = path or HUMAN_ITEM_PARAMS_PATH
    if not csv_path.is_file():
        raise FileNotFoundError(f"Human item params not found: {csv_path}")
    df = pd.read_csv(csv_path)
    if "item_id" not in df.columns or "difficulty_logit" not in df.columns:
        raise ValueError("human_item_params.csv must contain item_id and difficulty_logit")
    df["item_id"] = df["item_id"].astype(str)
    return df


def human_difficulty_by_anchor_id(path: Path | None = None) -> dict[str, float]:
    """Map ANCHOR_00N -> human difficulty_logit."""
    df = load_human_item_params_csv(path)
    out: dict[str, float] = {}
    for _, row in df.iterrows():
        qid = str(row["item_id"])
        anchor_id = Q_TO_ANCHOR.get(qid)
        if anchor_id is not None and pd.notna(row["difficulty_logit"]):
            out[anchor_id] = float(row["difficulty_logit"])
    return out


def identify_degenerate_items(
    responses: pd.DataFrame,
    item_ids: list[str],
) -> list[str]:
    """Items with no variance (all-correct or all-wrong) in the response matrix."""
    degenerate: list[str] = []
    for item_id in item_ids:
        if item_id not in responses.columns:
            continue
        series = responses[item_id].dropna()
        if len(series) < 2 or series.nunique() < 2:
            degenerate.append(str(item_id))
    return degenerate


def prepare_fipc_responses(
    responses: pd.DataFrame,
    flip_item_ids: list[str],
) -> pd.DataFrame:
    """Flip one response on zero-variance columns so mirt retains them for FIPC."""
    out = responses.copy()
    for item_id in flip_item_ids:
        if item_id not in out.columns:
            continue
        series = out[item_id]
        if series.nunique() >= 2:
            continue
        if len(series) == 0:
            continue
        val = int(series.iloc[0])
        out.iloc[0, out.columns.get_loc(item_id)] = 1 - val
    return out


def calibrate_item_params_mode_c(
    responses: pd.DataFrame,
    item_ids: list[str],
    work_dir: Path,
    *,
    run_rasch_fit: Callable[..., object],
) -> pd.DataFrame:
    """FIPC mode (c): fix LLM-degenerate anchors to human difficulty; free-estimate the rest."""
    work_dir.mkdir(parents=True, exist_ok=True)
    human = human_difficulty_by_anchor_id()
    degenerate = identify_degenerate_items(responses, item_ids)
    fixed_ids = [i for i in degenerate if i in human]
    free_ids = [i for i in item_ids if i not in fixed_ids]

    if len(free_ids) < 1:
        raise RuntimeError("No free items remaining for Rasch calibration")

    resp_fipc = prepare_fipc_responses(responses, fixed_ids)
    responses_path = work_dir / "responses.csv"
    resp_fipc.reset_index().rename(columns={"index": "respondent_id"}).to_csv(
        responses_path, index=False
    )

    anchor_params_path = work_dir / "anchor_params.csv"
    pd.DataFrame(
        [
            {
                "item_id": aid,
                "difficulty_logit": human[aid],
                "se_difficulty": pd.NA,
            }
            for aid in fixed_ids
        ]
    ).to_csv(anchor_params_path, index=False)

    item_params_path = work_dir / "item_params.csv"
    fit_summary_path = work_dir / "fit_summary.json"

    if fixed_ids:
        run_rasch_fit(
            responses_path,
            item_params_path,
            fit_summary_path,
            anchor_params_csv=anchor_params_path,
            new_item_ids=free_ids,
        )
    else:
        run_rasch_fit(responses_path, item_params_path, fit_summary_path)

    params = pd.read_csv(item_params_path)
    params["item_id"] = params["item_id"].astype(str)

    # Ensure fixed items carry exact human values in output.
    for aid in fixed_ids:
        mask = params["item_id"] == aid
        if mask.any():
            params.loc[mask, "difficulty_logit"] = human[aid]
        else:
            params = pd.concat(
                [
                    params,
                    pd.DataFrame(
                        [
                            {
                                "item_id": aid,
                                "difficulty_logit": human[aid],
                                "se_difficulty": pd.NA,
                                "is_anchor": True,
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )

    return params[params["item_id"].isin(item_ids)].reset_index(drop=True)


def build_live_fipc_anchor_params(
    anchor_responses: pd.DataFrame,
    anchor_item_ids: list[str],
    anchor_params: pd.DataFrame,
) -> pd.DataFrame:
    """Live calibration: human difficulty for degenerate anchors; bundle params for the rest."""
    human = human_difficulty_by_anchor_id()
    degenerate = set(identify_degenerate_items(anchor_responses, anchor_item_ids))
    params_by_id = {
        str(row.item_id): row
        for row in anchor_params.itertuples()
        if str(row.item_id) in anchor_item_ids
    }
    rows: list[dict] = []
    for aid in anchor_item_ids:
        if aid in degenerate and aid in human:
            rows.append(
                {
                    "item_id": aid,
                    "difficulty_logit": human[aid],
                    "se_difficulty": pd.NA,
                }
            )
        elif aid in params_by_id:
            row = params_by_id[aid]
            rows.append(
                {
                    "item_id": aid,
                    "difficulty_logit": float(row.difficulty_logit),
                    "se_difficulty": (
                        float(row.se_difficulty)
                        if hasattr(row, "se_difficulty")
                        and pd.notna(getattr(row, "se_difficulty", None))
                        else pd.NA
                    ),
                }
            )
        elif aid in human:
            rows.append(
                {
                    "item_id": aid,
                    "difficulty_logit": human[aid],
                    "se_difficulty": pd.NA,
                }
            )
    return pd.DataFrame(rows)
