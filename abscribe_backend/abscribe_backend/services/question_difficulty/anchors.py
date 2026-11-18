"""Anchor bank paths, validation, and bundle loading."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

ANCHORS_DIR = Path(__file__).resolve().parent / "anchors"
DEFAULT_ANCHOR_ITEMS = ANCHORS_DIR / "anchor_items.csv"
DEFAULT_ANCHOR_RESPONSES = ANCHORS_DIR / "anchor_responses.csv"
DEFAULT_ANCHOR_PARAMS = ANCHORS_DIR / "anchor_params.csv"
DEFAULT_ANCHOR_MANIFEST = ANCHORS_DIR / "manifest.json"


def anchor_calibration_enabled() -> bool:
    explicit = os.getenv("QUESTION_DIFFICULTY_USE_ANCHOR_CALIBRATION", "").strip().lower()
    if explicit in ("0", "false", "no"):
        return False
    if explicit in ("1", "true", "yes"):
        return _bundle_present_and_valid()[0]
    # Default: off — live scoring uses free Rasch unless explicitly opted in.
    return False


def anchor_paths() -> dict[str, Path]:
    return {
        "items": Path(
            os.getenv("QUESTION_DIFFICULTY_ANCHOR_ITEMS_CSV", str(DEFAULT_ANCHOR_ITEMS))
        ),
        "responses": Path(
            os.getenv(
                "QUESTION_DIFFICULTY_ANCHOR_RESPONSES_CSV",
                str(DEFAULT_ANCHOR_RESPONSES),
            )
        ),
        "params": Path(
            os.getenv(
                "QUESTION_DIFFICULTY_ANCHOR_PARAMS_CSV", str(DEFAULT_ANCHOR_PARAMS)
            )
        ),
        "manifest": Path(
            os.getenv(
                "QUESTION_DIFFICULTY_ANCHOR_MANIFEST", str(DEFAULT_ANCHOR_MANIFEST)
            )
        ),
    }


def _bundle_present_and_valid() -> tuple[bool, str | None]:
    paths = anchor_paths()
    for key in ("items", "responses", "params"):
        if not paths[key].is_file():
            return False, f"missing_{key}"
    try:
        items = pd.read_csv(paths["items"])
        responses = pd.read_csv(paths["responses"])
        params = pd.read_csv(paths["params"])
    except (OSError, ValueError) as exc:
        return False, f"read_error:{exc}"

    if "item_id" not in items.columns:
        return False, "items_missing_item_id"
    item_ids = items["item_id"].astype(str).tolist()
    if not item_ids:
        return False, "items_empty"

    resp_cols = [c for c in responses.columns if c != responses.columns[0] or responses.columns[0] != "Unnamed: 0"]
    # Wide matrix: columns are item ids (may have respondent_id as index column)
    if "respondent_id" in responses.columns:
        resp_item_cols = [c for c in responses.columns if c != "respondent_id"]
    else:
        resp_item_cols = list(responses.columns)

    param_ids = set(params["item_id"].astype(str))
    missing_resp = [i for i in item_ids if i not in resp_item_cols]
    missing_params = [i for i in item_ids if i not in param_ids]
    if missing_resp:
        return False, f"responses_missing_items:{','.join(missing_resp[:3])}"
    if missing_params:
        return False, f"params_missing_items:{','.join(missing_params[:3])}"

    informative = load_informative_anchor_item_ids(paths["responses"], item_ids)
    if len(informative) < 3:
        return False, "too_few_informative_anchors"

    return True, None


def load_informative_anchor_item_ids(
    responses_csv: str | Path,
    item_ids: list[str] | None = None,
) -> list[str]:
    df = pd.read_csv(responses_csv)
    if "respondent_id" in df.columns:
        df = df.set_index("respondent_id")
    cols = item_ids or list(df.columns)
    informative: list[str] = []
    for col in cols:
        if col not in df.columns:
            continue
        series = df[col].dropna()
        if len(series) >= 2 and series.nunique() >= 2:
            informative.append(str(col))
    return informative


def load_anchor_bundle() -> dict[str, Any]:
    ok, reason = _bundle_present_and_valid()
    if not ok:
        raise FileNotFoundError(reason or "anchor_bank_missing")
    paths = anchor_paths()
    items = pd.read_csv(paths["items"])
    responses = pd.read_csv(paths["responses"])
    params = pd.read_csv(paths["params"])
    manifest: dict[str, Any] = {}
    if paths["manifest"].is_file():
        manifest = json.loads(paths["manifest"].read_text())
    item_ids = items["item_id"].astype(str).tolist()
    informative = load_informative_anchor_item_ids(paths["responses"], item_ids)
    if "respondent_id" in responses.columns:
        responses = responses.set_index("respondent_id")
    # Keep full 20-item bundle; FIPC uses human anchors for degenerate items.
    responses = responses.reindex(columns=item_ids)
    params = params[params["item_id"].astype(str).isin(item_ids)].reset_index(
        drop=True
    )
    return {
        "items": items,
        "responses": responses,
        "params": params,
        "manifest": manifest,
        "informative_item_ids": informative,
        "paths": paths,
    }
