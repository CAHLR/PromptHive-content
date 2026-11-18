#!/usr/bin/env python3
"""Build the anchor calibration bundle (items + responses + params + manifest).

The three CSV artifacts must always be regenerated together as one versioned unit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from abscribe_backend.services.question_difficulty.anchors import (
    ANCHORS_DIR,
    DEFAULT_ANCHOR_ITEMS,
    DEFAULT_ANCHOR_MANIFEST,
    DEFAULT_ANCHOR_PARAMS,
    DEFAULT_ANCHOR_RESPONSES,
    load_informative_anchor_item_ids,
)
from abscribe_backend.services.question_difficulty.generate_responses import (
    generate_responses,
    generate_responses_async,
    generate_responses_batched,
    generate_responses_batched_async,
)
from abscribe_backend.services.question_difficulty.model_pool import (
    DEFAULT_MODEL_POOL,
    pool_to_manifest,
)
from abscribe_backend.services.question_difficulty.respondents import (
    DEFAULT_CALIBRATION_MODEL,
    DEFAULT_CALIBRATION_TEMPERATURE,
    DEFAULT_TEMPERATURE_TIERS,
    RESPONDENT_MODE_MULTI_MODEL,
    RESPONDENT_MODE_PERSONA,
    RESPONDENT_MODE_TEMPERATURE,
    get_respondent_mode,
    respondent_id_order,
)
from abscribe_backend.services.question_difficulty.difficulty_service import (
    run_rasch_fit,
)
from abscribe_backend.services.question_difficulty.human_anchors import (
    HUMAN_ITEM_PARAMS_PATH,
    calibrate_item_params_mode_c,
    identify_degenerate_items,
)

R_SCRIPT = Path(__file__).resolve().parent / "run_rasch.R"
LLMRESPONDENT_ITEMS = (
    Path(__file__).resolve().parents[5]
    / "LLMrespondent_URAP"
    / "data"
    / "items.csv"
)


def _seed_anchor_items(source: Path) -> pd.DataFrame:
    df = pd.read_csv(source)
    df = df.copy()
    df["item_id"] = [f"ANCHOR_{i:03d}" for i in range(1, len(df) + 1)]
    return df


def _synthetic_responses(
    items_df: pd.DataFrame,
    n_respondents: int,
    seed: int,
) -> pd.DataFrame:
    """Generate a valid 0/1 matrix with spread difficulties (bootstrap without LLM)."""
    rng = np.random.default_rng(seed)
    n_items = len(items_df)
    thetas = np.linspace(-1.8, 1.8, n_respondents)
    difficulties = np.linspace(-1.5, 1.5, n_items)
    rows = []
    for theta in thetas:
        row = []
        for b in difficulties:
            p = 1.0 / (1.0 + np.exp(-(theta - b)))
            row.append(1 if rng.random() < p else 0)
        rows.append(row)
    wide = pd.DataFrame(rows, columns=items_df["item_id"].tolist())
    wide.index = respondent_id_order(n_respondents)
    return wide


def _run_full_rasch(responses: pd.DataFrame, out_dir: Path) -> pd.DataFrame:
    out_dir.mkdir(parents=True, exist_ok=True)
    responses_path = out_dir / "responses.csv"
    params_path = out_dir / "item_params.csv"
    fit_path = out_dir / "fit_summary.json"
    responses.reset_index().rename(columns={"index": "respondent_id"}).to_csv(
        responses_path, index=False
    )
    cmd = [
        "Rscript",
        str(R_SCRIPT),
        str(responses_path),
        str(params_path),
        str(fit_path),
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout[-1500:])
    return pd.read_csv(params_path)


def build_bundle(
    items_df: pd.DataFrame,
    responses: pd.DataFrame,
    out_dir: Path,
    *,
    source: str,
    model: str,
    temperature: float,
    seed: int,
    n_respondents: int,
    respondent_mode: str = RESPONDENT_MODE_TEMPERATURE,
    temperature_tiers: list[float] | None = None,
    model_pool: list[dict] | None = None,
    generation_method: str = "batched_single_prompt",
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    item_ids = items_df["item_id"].astype(str).tolist()
    informative = load_informative_anchor_item_ids(
        _write_temp_responses(responses), item_ids
    )
    if len(informative) < 3:
        raise RuntimeError(
            f"Only {len(informative)} informative anchor items; need at least 3."
        )

    degenerate = identify_degenerate_items(responses, item_ids)
    params = calibrate_item_params_mode_c(
        responses,
        item_ids,
        out_dir / "_human_fipc",
        run_rasch_fit=run_rasch_fit,
    )

    items_path = out_dir / "anchor_items.csv"
    responses_path = out_dir / "anchor_responses.csv"
    params_path = out_dir / "anchor_params.csv"
    manifest_path = out_dir / "manifest.json"

    items_df.to_csv(items_path, index=False)
    responses.reset_index().rename(columns={"index": "respondent_id"}).to_csv(
        responses_path, index=False
    )
    params[["item_id", "difficulty_logit", "se_difficulty"]].to_csv(
        params_path, index=False
    )

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "n_items": len(items_df),
        "n_informative_items": len(informative),
        "n_degenerate_items": len(degenerate),
        "n_respondents": n_respondents,
        "model": model,
        "temperature": temperature,
        "seed": seed,
        "respondent_mode": respondent_mode,
        "generation_method": generation_method,
        "temperature_tiers": temperature_tiers or list(DEFAULT_TEMPERATURE_TIERS),
        "model_pool": model_pool or pool_to_manifest(),
        "persona_scheme": (
            "fixed_ability_tiers_v1"
            if respondent_mode == RESPONDENT_MODE_PERSONA
            else "temperature_tiers_v1"
            if respondent_mode == RESPONDENT_MODE_TEMPERATURE
            else "model_pool_v1"
        ),
        "fipc_mode": "human_degenerate_mode_c",
        "human_item_params_ref": "reference/human_item_params.csv",
        "respondent_order": respondent_id_order(n_respondents),
        "note": "Regenerate items, responses, and params together as one bundle.",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Wrote anchor bundle to {out_dir}")
    print(f"  items: {len(items_df)} | informative: {len(informative)}")


def _write_temp_responses(responses: pd.DataFrame) -> Path:
    tmp = Path("/tmp") / "anchor_informative_check.csv"
    responses.reset_index().rename(columns={"index": "respondent_id"}).to_csv(
        tmp, index=False
    )
    return tmp


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Build anchor calibration bundle")
    parser.add_argument(
        "--items",
        default=str(DEFAULT_ANCHOR_ITEMS),
        help="Source items CSV (default: anchors/anchor_items.csv or LLMrespondent seed)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(ANCHORS_DIR),
        help="Output directory for anchor bundle",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Generate bootstrap IRT responses without LLM calls",
    )
    parser.add_argument(
        "--n-respondents",
        type=int,
        default=int(os.getenv("QUESTION_DIFFICULTY_N_RESPONDENTS", "50")),
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--temperature",
        type=float,
        default=float(
            os.getenv(
                "QUESTION_DIFFICULTY_CALIBRATION_TEMPERATURE",
                str(DEFAULT_CALIBRATION_TEMPERATURE),
            )
        ),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("QUESTION_DIFFICULTY_CALIBRATION_MODEL")
        or os.getenv("OPENAI_MODEL", DEFAULT_CALIBRATION_MODEL),
    )
    parser.add_argument(
        "--async",
        dest="use_async",
        action="store_true",
        help="Run LLM calls concurrently (recommended for anchor rebuild)",
    )
    parser.add_argument(
        "--batched",
        action="store_true",
        help="Send all items in one prompt per respondent (paper-style)",
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
    parser.add_argument(
        "--single-model-neutral",
        action="store_true",
        help="Neutral prompt, gpt-3.5 only, per-item, all respondents at --temperature",
    )
    args = parser.parse_args()

    respondent_mode = args.respondent_mode or get_respondent_mode()
    if args.respondent_mode:
        os.environ["QUESTION_DIFFICULTY_RESPONDENT_MODE"] = args.respondent_mode
    temperature_tiers = list(DEFAULT_TEMPERATURE_TIERS)
    from abscribe_backend.services.question_difficulty.model_pool import (
        ModelPoolEntry,
        PROVIDER_OPENAI,
    )

    model_pool = DEFAULT_MODEL_POOL
    if respondent_mode == RESPONDENT_MODE_MULTI_MODEL:
        model_pool = tuple(
            ModelPoolEntry(e.model_name, e.provider, e.count, args.temperature)
            for e in DEFAULT_MODEL_POOL
        )
    if args.single_model_neutral:
        respondent_mode = RESPONDENT_MODE_MULTI_MODEL
        model_pool = (
            ModelPoolEntry("gpt-3.5-turbo", PROVIDER_OPENAI, args.n_respondents, args.temperature),
        )
    # Temperature defaults to batched (paper-style). Multi-model batches only with --batched.
    use_batched = args.batched or (
        respondent_mode == RESPONDENT_MODE_TEMPERATURE and not args.single_model_neutral
    )

    items_source = Path(args.items)
    if not items_source.is_file():
        if LLMRESPONDENT_ITEMS.is_file():
            items_df = _seed_anchor_items(LLMRESPONDENT_ITEMS)
            source = f"seeded_from:{LLMRESPONDENT_ITEMS}"
        else:
            print(f"Items file not found: {items_source}", file=sys.stderr)
            return 1
    else:
        items_df = pd.read_csv(items_source)
        if not items_df["item_id"].astype(str).str.startswith("ANCHOR_").all():
            items_df = _seed_anchor_items(items_source)
        source = str(items_source)

    out_dir = Path(args.out_dir)

    if args.synthetic:
        responses = _synthetic_responses(items_df, args.n_respondents, args.seed)
        build_bundle(
            items_df,
            responses,
            out_dir,
            source=f"synthetic_irt|{source}",
            model=args.model,
            temperature=args.temperature,
            seed=args.seed,
            n_respondents=args.n_respondents,
            respondent_mode=respondent_mode,
            temperature_tiers=temperature_tiers,
            model_pool=pool_to_manifest(model_pool),
            generation_method="synthetic",
        )
        return 0

    temp_items = out_dir / "_build_items.csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    items_df.to_csv(temp_items, index=False)
    run_dir = out_dir / "_build_run"
    ns = argparse.Namespace(
        items=str(temp_items),
        n_respondents=args.n_respondents,
        temperature=args.temperature,
        seed=args.seed,
        model_name=args.model,
        out_dir=str(run_dir),
        respondent_mode=respondent_mode,
        temperature_tiers=tuple(temperature_tiers),
        model_pool=model_pool,
    )
    if use_batched:
        if args.use_async:
            import asyncio

            asyncio.run(generate_responses_batched_async(ns))
        else:
            generate_responses_batched(ns)
    elif args.use_async:
        import asyncio

        asyncio.run(generate_responses_async(ns))
    else:
        generate_responses(ns)
    resp = pd.read_csv(run_dir / "responses.csv")
    if "respondent_id" in resp.columns:
        resp = resp.set_index("respondent_id")
    build_bundle(
        items_df,
        resp,
        out_dir,
        source=f"llm|{source}",
        model=args.model,
        temperature=args.temperature,
        seed=args.seed,
        n_respondents=args.n_respondents,
        respondent_mode=respondent_mode,
        temperature_tiers=temperature_tiers,
        model_pool=pool_to_manifest(model_pool),
        generation_method="batched_single_prompt" if use_batched else "per_item",
    )
    return 0


if __name__ == "__main__":
    # Allow running without pulling in abscribe_backend.app (heavy Flask deps).
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    raise SystemExit(main())
