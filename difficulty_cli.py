#!/usr/bin/env python3
"""Interactive CLI to generate OATutor questions with difficulty filtering."""

from __future__ import annotations

# PromptHive/email.py shadows stdlib "email" when this repo is on sys.path.
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0]).resolve() == _root:
    sys.path.pop(0)

import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

ROOT = _root
BACKEND_DIR = ROOT / "abscribe_backend"
BACKEND_PORT = 8080
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"

LEVELS = [
    (1, "Very Easy", -4.0, -1.0),
    (2, "Easy", -1.0, -0.3),
    (3, "Medium", -0.3, 0.3),
    (4, "Hard", 0.3, 1.0),
    (5, "Very Hard", 1.0, 4.0),
]

_we_started_backend = False
_backend_proc: subprocess.Popen | None = None
_server_log: Path | None = None


def print_mapping() -> None:
    print("=" * 70)
    print("Difficulty levels (used as minLevel / maxLevel in difficultyFilter)")
    print("=" * 70)
    for level, label, lo, hi in LEVELS:
        print(f"  {level} = {label:<12}  logit {lo:+.1f} to {hi:+.1f}")
    print("=" * 70)
    print()


def load_dotenv() -> None:
    for envfile in (BACKEND_DIR / ".env", BACKEND_DIR / "abscribe_backend" / ".env"):
        if not envfile.is_file():
            continue
        for line in envfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            os.environ.setdefault(key, value)


def preflight() -> None:
    load_dotenv()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        for envfile in (BACKEND_DIR / ".env", BACKEND_DIR / "abscribe_backend" / ".env"):
            if envfile.is_file() and re.search(
                r"^\s*OPENAI_API_KEY=\S+", envfile.read_text(encoding="utf-8"), re.M
            ):
                key = "present"
                break
    if not key:
        print("ERROR: OPENAI_API_KEY is not set.")
        print(f"  Add it to {BACKEND_DIR}/.env")
        sys.exit(1)
    if not shutil.which("Rscript"):
        print("ERROR: Rscript is not installed (required for Rasch calibration).")
        sys.exit(1)


def backend_responds() -> bool:
    try:
        with urllib.request.urlopen(f"{BACKEND_URL}/recipes/", timeout=2) as resp:
            return resp.status < 600
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def stop_backend_if_ours() -> None:
    global _backend_proc
    if _we_started_backend and _backend_proc is not None:
        print("\nStopping backend we started...")
        _backend_proc.terminate()
        try:
            _backend_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _backend_proc.kill()
        _backend_proc = None


def ensure_backend() -> None:
    global _we_started_backend, _backend_proc, _server_log
    if backend_responds():
        print(f"Backend already running on port {BACKEND_PORT}.")
        return

    print(f"Starting backend on port {BACKEND_PORT}...")
    _server_log = Path(os.environ.get("TMPDIR", "/tmp")) / f"difficulty_cli_server_{os.getpid()}.log"
    env = os.environ.copy()
    env["FLASK_APP"] = "abscribe_backend.app:app"
    env["FLASK_RUN_HOST"] = "0.0.0.0"
    env["FLASK_RUN_PORT"] = str(BACKEND_PORT)
    env["PYTHONPATH"] = str(BACKEND_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    if shutil.which("poetry") and (BACKEND_DIR / "pyproject.toml").is_file():
        cmd = ["poetry", "run", "flask", "run"]
    else:
        cmd = [sys.executable, "-m", "flask", "run"]

    log_f = _server_log.open("w", encoding="utf-8")
    _backend_proc = subprocess.Popen(
        cmd,
        cwd=BACKEND_DIR,
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    log_f.close()
    _we_started_backend = True
    print(f"  PID {_backend_proc.pid}  log → {_server_log}")

    for _ in range(60):
        if backend_responds():
            print("Backend ready.")
            return
        if _backend_proc.poll() is not None:
            tail = _server_log.read_text(encoding="utf-8")[-2000:] if _server_log.is_file() else ""
            print("ERROR: backend exited during startup.")
            if tail:
                print(tail)
            sys.exit(1)
        time.sleep(1)
    print("ERROR: backend did not respond within 60s.")
    sys.exit(1)


def parse_level_input(raw: str) -> tuple[int, int]:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty input")

    m = re.match(r"^(\d)\s*-\s*(\d)$", raw)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
    elif re.match(r"^(\d)\s+(\d)$", raw):
        parts = raw.split()
        lo, hi = int(parts[0]), int(parts[1])
    elif re.match(r"^\d$", raw):
        lo = hi = int(raw)
    else:
        raise ValueError("use a single level 1-5 or a range like 2-4")

    if not (1 <= lo <= 5 and 1 <= hi <= 5):
        raise ValueError("levels must be between 1 and 5")
    return min(lo, hi), max(lo, hi)


def prompt_levels() -> tuple[int, int]:
    while True:
        raw = input("Pick a difficulty level (1-5), or a range like 2-4: ").strip()
        try:
            return parse_level_input(raw)
        except ValueError as exc:
            print(f"  Invalid: {exc}. Try again.")


def prompt_max_count() -> int:
    while True:
        raw = input("How many questions to keep? (default 3): ").strip()
        if not raw:
            return 3
        try:
            n = int(raw)
            if n < 1:
                raise ValueError("must be at least 1")
            return n
        except ValueError:
            print("  Invalid: enter a positive integer or press Enter for default 3.")


def prompt_include_unscored() -> bool:
    while True:
        raw = input("Include unscored questions? (y/n, default y): ").strip().lower()
        if not raw or raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Invalid: enter y or n.")


def generate(
    min_level: int,
    max_level: int,
    max_count: int,
    include_unscored: bool,
) -> dict[str, Any]:
    payload = {
        "type": "oatutor_questions",
        "model": "gpt-4o-2024-08-06",
        "temperature": 0.7,
        "isOpenEnded": False,
        "messages": [
            {"role": "system", "content": "The output should always be valid JSON."},
            {
                "role": "system",
                "content": (
                    "Generate exactly one new algebra exercise as JSON with a problem row "
                    "and one step row. Step answerType must be numeric or algebra. Include "
                    "Problem Name, Row Type, Body Text, Answer, answerType fields."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Create one linear equation problem: solve 2x+3=11. "
                    f"Use problem name cli_diff_{min_level}_{max_level}."
                ),
            },
        ],
        "difficultyFilter": {
            "minLevel": min_level,
            "maxLevel": max_level,
            "maxCount": max_count,
            "includeUnscored": include_unscored,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BACKEND_URL}/chatGPT/json",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print("\nGenerating and calibrating (may take 1-3 minutes)...\n")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        print(f"ERROR: HTTP {exc.code}\n{err_body[:500]}")
        sys.exit(1)


def print_result(data: dict[str, Any], min_level: int, max_level: int) -> None:
    if err := data.get("difficultyFilterError"):
        print(f"ERROR: difficultyFilterError = {err}")
        return

    df = data.get("difficultyFilter") or {}
    questions = data.get("questions") or []
    min_logit = df.get("minLogit", df.get("min_logit"))
    max_logit = df.get("maxLogit", df.get("max_logit"))
    total_before = df.get("totalBefore", "?")
    total_after = df.get("totalAfter", "?")
    status = df.get("calibrationStatus", "?")

    print("-" * 70)
    print("RESULT")
    print("-" * 70)
    print(f"Filter:        levels {min_level}-{max_level}  →  logit [{min_logit}, {max_logit}]")
    print(f"Calibration:   {status}")
    print(f"Counts:        totalBefore={total_before}  totalAfter={total_after}")
    print()

    steps_kept = [q for q in questions if str(q.get("Row Type", "")).lower() == "step"]
    problems = [q for q in questions if str(q.get("Row Type", "")).lower() == "problem"]

    step_evals = [
        e for e in (df.get("evaluations") or []) if str(e.get("rowType", "")).lower() == "step"
    ]

    if steps_kept:
        for q in steps_kept:
            body = (q.get("Body Text") or q.get("bodyText") or "").strip()
            answer = q.get("Answer") or q.get("answer") or ""
            logit = q.get("difficultyLogit")
            label = q.get("difficultyLabel") or "?"
            scored = q.get("difficultyScored", logit is not None)
            print(f"Status:        KEPT")
            print(f"Difficulty:    {label}  (logit={logit})  scored={scored}")
            if problems:
                prob = (problems[0].get("Body Text") or problems[0].get("bodyText") or "").strip()
                print(f"Problem:       {prob}")
            print(f"Step:          {body}")
            if answer:
                print(f"Answer:        {answer}")
    elif step_evals:
        e = step_evals[0]
        logit = e.get("difficultyLogit")
        label = e.get("difficultyLabel") or "?"
        reason = e.get("reason", "?")
        kept = e.get("kept", False)
        print(f"Status:        FILTERED OUT")
        print(f"Difficulty:    {label}  (logit={logit})")
        print(f"Reason:        {reason}")
        if reason == "outside_logit_range":
            print("               → Generated question was outside your selected logit range.")
        elif reason == "insufficient_informative_items":
            print("               → Could not calibrate; excluded because includeUnscored=false.")
        # Try to show generated text from evaluations context - not always there
        print("(Question text not returned — filtered before response.)")
    else:
        print("No step data in response.")

    print("-" * 70)


def prompt_again() -> bool:
    while True:
        raw = input("\nRun another? (y/n): ").strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no", ""):
            return False
        print("  Invalid: enter y or n.")


def main() -> None:
    atexit.register(stop_backend_if_ours)

    def _sig(_signum, _frame) -> None:
        stop_backend_if_ours()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    print("\nPromptHive — difficulty filter CLI\n")
    preflight()
    print_mapping()

    ensure_backend()

    while True:
        min_level, max_level = prompt_levels()
        max_count = prompt_max_count()
        include_unscored = prompt_include_unscored()

        print(
            f"\n→ minLevel={min_level}, maxLevel={max_level}, "
            f"maxCount={max_count}, includeUnscored={include_unscored}"
        )

        data = generate(min_level, max_level, max_count, include_unscored)
        print_result(data, min_level, max_level)

        if not prompt_again():
            break

    print("Goodbye.")


if __name__ == "__main__":
    main()
