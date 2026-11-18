#!/usr/bin/env python3
"""Extract and report per-respondent Rasch theta from saved response bundles."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

R_SCRIPT = Path(__file__).resolve().parent / "run_rasch.R"

BUNDLES = {
    "persona": {
        "label": "Persona (gpt-3.5, per-item)",
        "dir": "anchors",
        "group_col": "ability_tier",
    },
    "temperature": {
        "label": "Temperature (gpt-3.5, batched)",
        "dir": "anchors_temp",
        "group_col": "ability_tier",
    },
    "multi_model": {
        "label": "Multi-model (batched pool)",
        "dir": "anchors_multi_model",
        "group_col": "model",
    },
}


def _ensure_respondent_ids(responses_path: Path, run_json: Path) -> Path:
    """Return path to responses CSV with respondent_id column."""
    df = pd.read_csv(responses_path)
    if "respondent_id" in df.columns:
        return responses_path

    meta = json.loads(run_json.read_text(encoding="utf-8"))
    order = meta.get("respondent_order") or [f"R{i:03d}" for i in range(1, len(df) + 1)]
    if len(order) != len(df):
        order = [f"R{i:03d}" for i in range(1, len(df) + 1)]
    df.insert(0, "respondent_id", order)
    fixed = responses_path.parent / "_responses_with_ids.csv"
    df.to_csv(fixed, index=False)
    return fixed


def _run_rasch(responses_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    params_path = out_dir / "item_params.csv"
    fit_path = out_dir / "fit_summary.json"
    abilities_path = out_dir / "respondent_abilities.csv"
    subprocess.run(
        ["Rscript", str(R_SCRIPT), str(responses_path), str(params_path), str(fit_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    if not abilities_path.is_file():
        raise FileNotFoundError(f"Expected {abilities_path} after Rasch fit")
    return abilities_path


def _respondent_metadata(long_path: Path) -> pd.DataFrame:
    long_df = pd.read_csv(long_path)
    cols = ["respondent_id"]
    for c in ("model", "ability_tier", "temperature", "provider"):
        if c in long_df.columns:
            cols.append(c)
    meta = long_df[cols].drop_duplicates(subset=["respondent_id"])
    return meta.set_index("respondent_id")


def _text_histogram(values: np.ndarray, bins: int = 8) -> str:
    counts, edges = np.histogram(values, bins=bins)
    lines = []
    for i, count in enumerate(counts):
        lo, hi = edges[i], edges[i + 1]
        bar = "#" * int(round(count / max(counts.max(), 1) * 20))
        lines.append(f"  [{lo:+.2f}, {hi:+.2f}): {count:2d} {bar}")
    return "\n".join(lines)


def _theta_stats(theta: pd.Series) -> dict:
    arr = theta.astype(float).values
    q = np.percentile(arr, [25, 50, 75])
    return {
        "n": len(arr),
        "mean": float(np.mean(arr)),
        "sd": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "range": float(np.max(arr) - np.min(arr)),
        "q25": float(q[0]),
        "median": float(q[1]),
        "q75": float(q[2]),
    }


def analyze_bundle(key: str, services_dir: Path) -> dict | None:
    spec = BUNDLES[key]
    bundle_dir = services_dir / spec["dir"]
    build_dir = bundle_dir / "_build_run"
    responses_path = build_dir / "responses.csv"
    long_path = build_dir / "responses_long.csv"
    run_json = build_dir / "run.json"

    if not responses_path.is_file():
        print(f"[skip] {key}: missing {responses_path}")
        return None
    if not long_path.is_file():
        print(f"[skip] {key}: missing {long_path}")
        return None

    resp_path = _ensure_respondent_ids(responses_path, run_json)
    theta_dir = build_dir / "_theta_fit"
    abilities_path = _run_rasch(resp_path, theta_dir)

    abilities = pd.read_csv(abilities_path)
    meta = _respondent_metadata(long_path)
    merged = abilities.merge(meta, left_on="respondent_id", right_index=True, how="left")

    overall = _theta_stats(merged["theta"])
    by_group = {}
    group_col = spec["group_col"]
    if group_col in merged.columns:
        for name, grp in merged.groupby(group_col, sort=False):
            by_group[str(name)] = _theta_stats(grp["theta"])

    return {
        "key": key,
        "label": spec["label"],
        "overall": overall,
        "by_group": by_group,
        "group_col": group_col,
        "merged": merged,
    }


def _print_bundle_report(result: dict) -> None:
    o = result["overall"]
    print(f"\n{'=' * 70}")
    print(f"{result['label']} ({result['key']})")
    print(f"{'=' * 70}")
    print(f"  N respondents: {o['n']}")
    print(f"  Theta mean:    {o['mean']:+.3f}")
    print(f"  Theta SD:      {o['sd']:.3f}  <-- headline")
    print(f"  Min / Max:     {o['min']:+.3f} / {o['max']:+.3f}")
    print(f"  Range:         {o['range']:.3f}")
    print(f"  Quartiles:     Q1={o['q25']:+.3f}  Median={o['median']:+.3f}  Q3={o['q75']:+.3f}")
    print("  Histogram:")
    print(_text_histogram(result["merged"]["theta"].values))

    if result["by_group"]:
        print(f"\n  By {result['group_col']}:")
        for name, stats in result["by_group"].items():
            print(
                f"    {name:20s}: mean={stats['mean']:+.3f}  "
                f"SD={stats['sd']:.3f}  n={stats['n']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Report Rasch theta spread per bundle")
    parser.add_argument(
        "--services-dir",
        default=str(Path(__file__).resolve().parent),
        help="question_difficulty directory containing anchor bundles",
    )
    parser.add_argument(
        "--bundles",
        nargs="*",
        default=list(BUNDLES.keys()),
        choices=list(BUNDLES.keys()),
    )
    args = parser.parse_args()
    services_dir = Path(args.services_dir)

    results: list[dict] = []
    for key in args.bundles:
        result = analyze_bundle(key, services_dir)
        if result:
            results.append(result)
            _print_bundle_report(result)
            out_csv = services_dir / BUNDLES[key]["dir"] / "_build_run" / "_theta_fit" / "respondent_abilities_enriched.csv"
            result["merged"].to_csv(out_csv, index=False)

    if len(results) >= 2:
        print(f"\n{'=' * 70}")
        print("CROSS-BUNDLE COMPARISON (theta SD)")
        print(f"{'=' * 70}")
        print(f"  {'Bundle':<22} {'Theta SD':>10} {'Mean':>10} {'Range':>10}")
        for r in results:
            o = r["overall"]
            print(
                f"  {r['label']:<22} {o['sd']:10.3f} {o['mean']:10.3f} {o['range']:10.3f}"
            )
        print("\n  Paper reference: single-model theta SD ~0.29-0.58; human ~0.98")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
