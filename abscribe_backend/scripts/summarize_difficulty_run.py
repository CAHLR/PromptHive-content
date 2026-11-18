#!/usr/bin/env python3
"""Summarize data-quality events from a PromptHive difficulty calibrate/score run.

Reads the gunicorn error log (and optional run artifacts) and writes a readable
markdown report for PI / debugging:

  - truncated incorrect answers recorded as missing (not wrong)
  - OpenAI timeout / 429 / connection retries
  - skipped sheet items (e.g. intermediate_step_key), when evaluations are available

Examples:
  python scripts/summarize_difficulty_run.py \\
    --error-log ~/prompthive-backend-error.log \\
    --access-log ~/prompthive-backend-access.log \\
    --out ~/prompthive-data-quality/latest_run.md

  python scripts/summarize_difficulty_run.py \\
    --error-log ~/prompthive-backend-error.log \\
    --run-dir /tmp/prompthive_rasch_legacy_xxx \\
    --evaluations-json /path/to/result.json \\
    --out report.md
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ACCESS_CALIBRATE_RE = re.compile(
    r"\[([^\]]+)\].*\"(?:POST|OPTIONS) /chatGPT/"
    r"(calibrate-generated-questions|score-existing-questions)"
)
GENERATING_RE = re.compile(
    r"Generating\s+(\d+)\s+respondents\s*[×x]\s*(\d+)\s+items",
    re.IGNORECASE,
)
LOADED_RE = re.compile(r"Loaded\s+(\d+)\s+items\s+from\s+(\S+)")
TRUNC_MISSING_RE = re.compile(
    r"truncated incorrect answer recorded as missing.*?respondent=(\S+)\s+item=(\S+)"
    r"(?:\s+\|\s+answer_preview=(.*))?$"
)
POSSIBLE_TRUNC_RE = re.compile(
    r"possible truncation at max_tokens=(\d+).*?respondent=(\S+)\s+item=(\S+)"
)
RETRY_RE = re.compile(
    r"\[llm_providers\]\s+OpenAI\s+(timeout|rate limit|connection)\s+(\S+):"
    r"(.*?)\|\s*respondent=(\S+)\s+item=(\S+)\s+sleep\s+([\d.]+)s\s+\(attempt\s+(\d+)/(\d+)\)",
    re.IGNORECASE,
)


def _parse_access_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None


def _latest_access_meta(access_log: Path | None) -> tuple[datetime, datetime, str] | None:
    if not access_log or not access_log.is_file():
        return None
    posts: list[tuple[datetime, str]] = []
    options: list[tuple[datetime, str]] = []
    for line in access_log.read_text(errors="replace").splitlines():
        m = ACCESS_CALIBRATE_RE.search(line)
        if not m:
            continue
        ts = _parse_access_ts(m.group(1))
        if ts is None:
            continue
        endpoint = m.group(2)
        if "OPTIONS" in line:
            options.append((ts, endpoint))
        elif "POST" in line and " 200 " in line:
            posts.append((ts, endpoint))
    if not posts:
        return None
    end_ts, endpoint = posts[-1]
    start_ts = end_ts
    for ots, oep in reversed(options):
        if oep == endpoint and ots <= end_ts:
            start_ts = ots
            break
    return start_ts, end_ts, endpoint


def _slice_latest_run_lines(lines: list[str]) -> tuple[list[str], int | None, int | None, str | None]:
    """Return log lines for the latest generation block + (n_resp, n_items, temp_path)."""
    last_gen = None
    for i, line in enumerate(lines):
        if GENERATING_RE.search(line):
            last_gen = i
    if last_gen is None:
        return lines, None, None, None

    gen = GENERATING_RE.search(lines[last_gen])
    assert gen
    n_resp = int(gen.group(1))
    n_items = int(gen.group(2))
    temp_path = None
    for j in range(last_gen, -1, -1):
        loaded = LOADED_RE.search(lines[j])
        if loaded:
            temp_path = loaded.group(2)
            break

    end = len(lines)
    for k in range(last_gen + 1, len(lines)):
        if GENERATING_RE.search(lines[k]):
            end = k
            break
    return lines[last_gen:end], n_resp, n_items, temp_path


def _load_run_dir_texts(run_dir: Path | None) -> dict[tuple[str, str], str]:
    if not run_dir:
        return {}
    long_path = run_dir / "run" / "responses_long.csv"
    if not long_path.is_file():
        long_path = run_dir / "responses_long.csv"
    if not long_path.is_file():
        return {}
    try:
        import pandas as pd
    except ImportError:
        return {}
    df = pd.read_csv(long_path)
    if "rule_used" not in df.columns:
        return {}
    texts: dict[tuple[str, str], str] = {}
    for _, row in df.iterrows():
        if str(row.get("rule_used")) != "truncated_missing":
            continue
        texts[(str(row["respondent_id"]), str(row["item_id"]))] = str(
            row.get("raw_answer") or ""
        )
    return texts


def _load_jsonl_texts(path: Path) -> dict[tuple[str, str], str]:
    if not path.is_file():
        return {}
    out: dict[tuple[str, str], str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "truncated_missing":
            continue
        key = (str(ev.get("respondent_id")), str(ev.get("item_id")))
        text = str(ev.get("raw_answer") or "")
        if text:
            out[key] = text
    return out


def _parse_preview(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if (raw.startswith("'") and raw.endswith("'")) or (
        raw.startswith('"') and raw.endswith('"')
    ):
        try:
            return ast.literal_eval(raw)
        except Exception:
            return raw[1:-1]
    return raw


def _load_skips(
    evaluations_json: Path | None,
) -> list[dict[str, Any]]:
    skips: list[dict[str, Any]] = []
    snap = Path.home() / "prompthive-data-quality" / "latest_skips.json"
    for path in (evaluations_json, snap if snap.is_file() else None):
        if not path or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        evaluations = None
        questions = None
        if isinstance(payload, list):
            evaluations = payload
        elif isinstance(payload, dict):
            if "skips" in payload and isinstance(payload["skips"], list):
                return payload["skips"]
            evaluations = payload.get("evaluations") or payload.get("difficultyDebug")
            questions = payload.get("questions")
        if not evaluations:
            continue
        for row in evaluations:
            if row.get("kept"):
                continue
            reason = str(row.get("reason") or "")
            if reason == "companion_problem_row":
                continue
            idx = row.get("index")
            answer_key = title = ""
            if questions is not None and isinstance(idx, int) and 0 <= idx < len(questions):
                q = questions[idx]
                answer_key = str(q.get("Answer") or q.get("answer_key") or "")
                title = str(q.get("Title") or "")
            skips.append(
                {
                    "index": idx,
                    "item_id": row.get("itemId") or row.get("item_id"),
                    "problem_name": row.get("problemName") or row.get("problem_name"),
                    "row_type": row.get("rowType") or row.get("row_type"),
                    "reason": reason,
                    "answer_key": answer_key or row.get("answer_key") or "",
                    "title": title or row.get("title") or "",
                }
            )
        if skips:
            break
    skips.sort(
        key=lambda s: (0 if s.get("reason") == "intermediate_step_key" else 1, str(s.get("index")))
    )
    return skips


def build_report(
    *,
    error_log: Path,
    access_log: Path | None,
    run_dir: Path | None,
    evaluations_json: Path | None,
    events_jsonl: Path | None,
    out_path: Path,
    fallback_examples_dir: Path | None = None,
) -> str:
    access_meta = _latest_access_meta(access_log)
    lines = error_log.read_text(errors="replace").splitlines()
    slice_lines, n_resp, n_items, temp_path = _slice_latest_run_lines(lines)
    n_cells = (n_resp * n_items) if n_resp and n_items else None

    missing: list[dict] = []
    trunc_hits: list[dict] = []
    retries: list[dict] = []

    for line in slice_lines:
        m = TRUNC_MISSING_RE.search(line)
        if m:
            missing.append(
                {
                    "respondent": m.group(1),
                    "item": m.group(2).rstrip("|").strip(),
                    "preview": _parse_preview(m.group(3) if m.lastindex and m.lastindex >= 3 else None),
                }
            )
            continue
        m = POSSIBLE_TRUNC_RE.search(line)
        if m:
            trunc_hits.append(
                {
                    "max_tokens": m.group(1),
                    "respondent": m.group(2),
                    "item": m.group(3),
                }
            )
            continue
        m = RETRY_RE.search(line)
        if m:
            retries.append(
                {
                    "kind": m.group(1),
                    "exc": m.group(2),
                    "respondent": m.group(4),
                    "item": m.group(5),
                    "attempt": int(m.group(7)),
                }
            )

    events_path = events_jsonl or (Path.home() / "prompthive-data-quality" / "events.jsonl")
    texts = _load_jsonl_texts(events_path)
    texts.update(_load_run_dir_texts(run_dir))
    if temp_path:
        texts.update(_load_run_dir_texts(Path(temp_path)))

    for m in missing:
        key = (m["respondent"], m["item"])
        m["full_text"] = texts.get(key)

    examples: list[tuple[str, str, str, str]] = []
    examples_source = "this run"

    def add_example(rid: str, iid: str, text: str, source: str) -> None:
        if text and text.strip() and len(examples) < 2:
            examples.append((rid, iid, text, source))

    for m in missing:
        if m.get("full_text"):
            add_example(m["respondent"], m["item"], m["full_text"], "this run")
    if len(examples) < 2 and fallback_examples_dir:
        for (rid, iid), text in _load_run_dir_texts(fallback_examples_dir).items():
            add_example(rid, iid, text, "same-day kept diagnostic run")
            if len(examples) >= 2:
                break
        if examples and examples[0][3] != "this run":
            examples_source = "fallback"

    skips = _load_skips(evaluations_json)
    n_missing = len(missing)
    n_retries = len(retries)
    n_skips = len(skips)
    scored = (n_cells - n_missing) if n_cells is not None else None

    parts: list[str] = []
    parts.append("# Difficulty run — data-quality summary")
    parts.append("")
    if access_meta:
        start, end, endpoint = access_meta
        parts.append(
            f"**Access log window:** {start.isoformat()} → {end.isoformat()} "
            f"(`{endpoint}`)"
        )
    if temp_path:
        parts.append(f"**Temp dir (from log):** `{temp_path}`")
    parts.append("")
    cells_s = str(n_cells) if n_cells is not None else "?"
    scored_s = str(scored) if scored is not None else "?"
    parts.append(
        f"**Summary:** {cells_s} cells"
        + (f" ({n_resp}×{n_items})" if n_resp and n_items else "")
        + f" · {scored_s} scored · **{n_missing} missing** (truncated) · "
        f"**{n_skips} skipped** (sheet) · **{n_retries} retries**"
    )
    parts.append("")
    parts.append(
        "Missing = `finish_reason=length` + graded incorrect → recorded as "
        "**NaN / missing**, not wrong. Rasch treats NA as an absent response."
    )
    parts.append("")

    parts.append("## Verbatim examples (truncated → missing)")
    parts.append("")
    if examples_source == "fallback":
        parts.append(
            "_Full answer texts for this browser run were not retained "
            "(`KEEP_TEMP_DIR=0` cleaned the temp dir). Examples below are "
            "**verbatim** rejected responses from a same-day kept-artifact run "
            "under the **same** truncation→missing rule — they show model "
            "pathology / gibberish, not a merely wrong algebra answer._"
        )
        parts.append("")
    if not examples:
        parts.append(
            "_No full answer texts available. Next runs append full text to "
            "`~/prompthive-data-quality/events.jsonl`; or pass `--run-dir`._"
        )
        parts.append("")
    for i, (rid, iid, text, _src) in enumerate(examples, 1):
        parts.append(f"### Example {i}: `{rid}` / `{iid}`")
        parts.append("")
        parts.append("```")
        parts.append(text.rstrip("\n"))
        parts.append("```")
        parts.append("")

    parts.append("## 1. Truncated / missing cells")
    parts.append("")
    if not missing:
        parts.append("_None._")
        parts.append("")
    else:
        by_item = Counter(m["item"] for m in missing)
        item_bits = [f"`{iid}`: {c}" for iid, c in by_item.most_common()]
        parts.append(
            f"**{n_missing} cells** recorded as missing "
            f"(finish_reason=length + graded incorrect), concentrated on a few items "
            f"rather than spread evenly across the item bank:"
        )
        parts.append("")
        parts.append(", ".join(item_bits))
        parts.append("")
        if trunc_hits:
            n_trunc = len(trunc_hits)
            n_kept = n_trunc - n_missing
            parts.append(
                f"Note: {n_trunc} responses hit the `max_tokens` cap; only {n_missing} "
                f"were discarded. The other {n_kept} graded correct (answer-then-ramble) "
                f"and were kept — the rule drops a cell only when it is both truncated "
                f"and wrong."
            )
            parts.append("")
        parts.append(
            "**Why:** hit `max_tokens` with an ungradeable/incorrect fragment → "
            "treated as model failure, not evidence the respondent got it wrong."
        )
        parts.append("")

    parts.append("## 2. Retries (timeout / 429 / connection)")
    parts.append("")
    if not retries:
        parts.append("_None in this run._")
        parts.append("")
    else:
        parts.append(
            f"{n_retries} retry log lines "
            f"({len({(r['respondent'], r['item'], r['kind']) for r in retries})} "
            "unique respondent×item×kind)."
        )
        parts.append("")
        parts.append("| Respondent | Item | Kind | Max attempt seen |")
        parts.append("|---|---|---|---|")
        seen: dict[tuple[str, str, str], int] = {}
        for r in retries:
            key = (r["respondent"], r["item"], r["kind"])
            seen[key] = max(seen.get(key, 0), r["attempt"])
        for (rid, iid, kind), att in sorted(seen.items()):
            parts.append(f"| `{rid}` | `{iid}` | {kind} | {att} |")
        parts.append("")

    parts.append("## 3. Skipped items (sheet / calibratability)")
    parts.append("")
    if not skips:
        parts.append(
            "_No skip snapshot for this run._ Pass `--evaluations-json` from the "
            "calibrate result (`evaluations` array), or use "
            "`~/prompthive-data-quality/latest_skips.json` after the next run "
            "(written automatically going forward)."
        )
        parts.append("")
    else:
        parts.append(
            f"{n_skips} non-kept evaluation rows (excluding companion problem rows)."
        )
        parts.append("")
        parts.append("| Item / problem | Reason | Answer key |")
        parts.append("|---|---|---|")
        for s in skips:
            label = s.get("item_id") or s.get("problem_name") or f"index={s.get('index')}"
            key = str(s.get("answer_key") or "").replace("|", "\\|")
            if len(key) > 60:
                key = key[:57] + "..."
            parts.append(f"| `{label}` | `{s.get('reason')}` | `{key}` |")
        parts.append("")

    report = "\n".join(parts).rstrip() + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    return report


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--error-log",
        type=Path,
        default=Path.home() / "prompthive-backend-error.log",
    )
    p.add_argument(
        "--access-log",
        type=Path,
        default=Path.home() / "prompthive-backend-access.log",
    )
    p.add_argument("--run-dir", type=Path, default=None)
    p.add_argument("--evaluations-json", type=Path, default=None)
    p.add_argument("--events-jsonl", type=Path, default=None)
    p.add_argument(
        "--fallback-examples-dir",
        type=Path,
        default=None,
        help="Kept run dir used only for verbatim gibberish examples if this run has none",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path.home() / "prompthive-data-quality" / "latest_run_summary.md",
    )
    args = p.parse_args()

    report = build_report(
        error_log=args.error_log,
        access_log=args.access_log if args.access_log.is_file() else None,
        run_dir=args.run_dir,
        evaluations_json=args.evaluations_json,
        events_jsonl=args.events_jsonl,
        out_path=args.out,
        fallback_examples_dir=args.fallback_examples_dir,
    )
    print(report)
    print(f"\nWrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
