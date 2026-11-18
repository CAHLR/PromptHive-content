#!/usr/bin/env bash
# End-to-end test harness for the difficulty-filter feature (PromptHive).
# Safe to re-run: cleans up prior backend processes on port 8080 before starting.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="${ROOT}/abscribe_backend"
FRONTEND_DIR="${ROOT}/frontend"
QD_DIR="${BACKEND_DIR}/abscribe_backend/services/question_difficulty"
ANCHORS_DIR="${QD_DIR}/anchors"
HUMAN_PARAMS="${QD_DIR}/reference/human_item_params.csv"
ANCHOR_PARAMS="${ANCHORS_DIR}/anchor_params.csv"

BACKEND_PORT=8080
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
BACKEND_PID=""
SERVER_LOG=""

# Step pass/fail tracking: 1=pass, 0=fail, empty=skipped
STEP_ENV=""
STEP_UNIT=""
STEP_CORRELATION=""
STEP_SERVER=""
STEP_API=""

FAILURES=()

set_step() {
  local step="$1"
  local val="$2"
  case "$step" in
    env) STEP_ENV="$val" ;;
    unit) STEP_UNIT="$val" ;;
    correlation) STEP_CORRELATION="$val" ;;
    server) STEP_SERVER="$val" ;;
    api) STEP_API="$val" ;;
  esac
}

get_step() {
  case "$1" in
    env) echo "$STEP_ENV" ;;
    unit) echo "$STEP_UNIT" ;;
    correlation) echo "$STEP_CORRELATION" ;;
    server) echo "$STEP_SERVER" ;;
    api) echo "$STEP_API" ;;
  esac
}

section() {
  echo ""
  echo "======================================================================"
  echo "$1"
  echo "======================================================================"
}

record_fail() {
  local step="$1"
  local msg="$2"
  set_step "$step" "0"
  FAILURES+=("$msg")
  echo "FAIL: $msg"
}

record_pass() {
  local step="$1"
  set_step "$step" "1"
  echo "PASS: $step"
}

cleanup_server() {
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "Stopping backend (PID ${BACKEND_PID})..."
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
  # Also free the port if something else is bound (e.g. prior crashed run).
  if command -v lsof >/dev/null 2>&1; then
    local stale
    stale="$(lsof -ti ":${BACKEND_PORT}" 2>/dev/null || true)"
    if [[ -n "$stale" ]]; then
      echo "Killing stale process(es) on port ${BACKEND_PORT}: ${stale}"
      kill -9 $stale 2>/dev/null || true
    fi
  fi
}

trap cleanup_server EXIT INT TERM

run_py() {
  if [[ -n "${PY_RUNNER:-}" ]]; then
    (cd "$BACKEND_DIR" && eval "$PY_RUNNER" "$@")
  else
    (cd "$BACKEND_DIR" && PYTHONPATH="${BACKEND_DIR}:${PYTHONPATH:-}" python3 "$@")
  fi
}

run_py_script() {
  # Run inline Python from stdin. Usage: run_py_script [-- arg ...]
  local args=("-")
  if [[ "${1:-}" == "--" ]]; then
    shift
    args=("-" "$@")
  fi
  if [[ -n "${PY_RUNNER:-}" ]]; then
    (cd "$BACKEND_DIR" && eval "$PY_RUNNER" "${args[@]}")
  else
    (cd "$BACKEND_DIR" && PYTHONPATH="${BACKEND_DIR}:${PYTHONPATH:-}" python3 "${args[@]}")
  fi
}

# ---------------------------------------------------------------------------
section "Step 1 — Environment setup"
# ---------------------------------------------------------------------------

if [[ ! -d "$BACKEND_DIR" ]]; then
  record_fail env "Backend directory not found: ${BACKEND_DIR}"
  exit 1
fi

# Prefer poetry (documented in abscribe_backend/README.md), else fall back to python3 + PYTHONPATH.
if command -v poetry >/dev/null 2>&1 && [[ -f "${BACKEND_DIR}/pyproject.toml" ]]; then
  PY_RUNNER="poetry run python"
  PYTEST_CMD=(poetry run pytest)
  FLASK_CMD=(poetry run flask run)
  echo "Using Poetry environment (poetry run …)."
else
  PY_RUNNER=""
  PYTEST_CMD=(python3 -m pytest)
  FLASK_CMD=(python3 -m flask run)
  echo "Poetry not found — using python3 with PYTHONPATH=${BACKEND_DIR}."
fi

# Load .env files (same locations the backend uses).
ENV_LOADED=0
for envfile in \
  "${BACKEND_DIR}/.env" \
  "${BACKEND_DIR}/abscribe_backend/.env"
do
  if [[ -f "$envfile" ]]; then
    echo "Loading ${envfile}"
    set -a
    # shellcheck disable=SC1090
    source "$envfile"
    set +a
    ENV_LOADED=1
  fi
done

OPENAI_PRESENT=0
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  OPENAI_PRESENT=1
else
  for envfile in "${BACKEND_DIR}/.env" "${BACKEND_DIR}/abscribe_backend/.env"; do
    if [[ -f "$envfile" ]] && grep -qE '^[[:space:]]*OPENAI_API_KEY=[^[:space:]]+' "$envfile"; then
      OPENAI_PRESENT=1
      break
    fi
  done
fi

if [[ "$OPENAI_PRESENT" -eq 0 ]]; then
  echo ""
  echo "**************************************************************************"
  echo "WARNING: OPENAI_API_KEY is missing or empty."
  echo "  Set it in ${BACKEND_DIR}/.env (recommended) or"
  echo "  ${BACKEND_DIR}/abscribe_backend/.env before running the live API test."
  echo "  Unit tests and anchor correlation will still run; Step 6 will be skipped."
  echo "**************************************************************************"
  echo ""
fi

if [[ ! -f "$ANCHOR_PARAMS" ]] || [[ ! -f "$HUMAN_PARAMS" ]]; then
  record_fail env "Canonical anchor bundle or human params missing."
  echo "  Expected: ${ANCHOR_PARAMS}"
  echo "  Expected: ${HUMAN_PARAMS}"
  exit 1
fi

export FLASK_APP=abscribe_backend.app:app
export FLASK_RUN_HOST=0.0.0.0
export FLASK_RUN_PORT=${BACKEND_PORT}

record_pass env

# ---------------------------------------------------------------------------
section "Step 2 — Unit tests (tests/test_question_difficulty.py)"
# ---------------------------------------------------------------------------

if (cd "$BACKEND_DIR" && "${PYTEST_CMD[@]}" tests/test_question_difficulty.py -q); then
  record_pass unit
else
  record_fail unit "pytest tests/test_question_difficulty.py failed"
fi

# ---------------------------------------------------------------------------
section "Step 3 — Anchor bundle vs human_item_params.csv correlation"
# ---------------------------------------------------------------------------
# Uses human_anchors.human_difficulty_by_anchor_id() + anchors/anchor_params.csv
# (same alignment as calibration research: ANCHOR_00N ↔ QN).
# ---------------------------------------------------------------------------

CORR_OUTPUT="$(run_py_script << 'PY'
from pathlib import Path
import sys

import pandas as pd
from scipy.stats import pearsonr, spearmanr

from abscribe_backend.services.question_difficulty.human_anchors import (
    HUMAN_ITEM_PARAMS_PATH,
    human_difficulty_by_anchor_id,
)

anchors_dir = Path("abscribe_backend/services/question_difficulty/anchors")
anchor_params = pd.read_csv(anchors_dir / "anchor_params.csv")
human = human_difficulty_by_anchor_id(HUMAN_ITEM_PARAMS_PATH)

pairs = []
for _, row in anchor_params.iterrows():
    aid = str(row["item_id"])
    if aid in human:
        pairs.append((human[aid], float(row["difficulty_logit"])))

if len(pairs) < 3:
    print("ERROR: fewer than 3 matched anchor/human pairs")
    sys.exit(1)

xs, ys = zip(*pairs)
pr = pearsonr(xs, ys).statistic
sr = spearmanr(xs, ys).statistic
print(f"n_items={len(pairs)}")
print(f"pearson={pr:.4f}")
print(f"spearman={sr:.4f}")
# Target from locked-in calibration: Spearman ~0.90 (paper target ~0.87).
if sr < 0.87:
    print("BELOW_THRESHOLD=1")
    sys.exit(1)
print("BELOW_THRESHOLD=0")
PY
)" || CORR_RC=$?

CORR_RC="${CORR_RC:-0}"
echo "$CORR_OUTPUT"

if [[ "$CORR_RC" -eq 0 ]] && ! grep -q "BELOW_THRESHOLD=1" <<< "$CORR_OUTPUT"; then
  record_pass correlation
  echo "Human correlation OK (expect Spearman ~0.90, threshold >= 0.87)."
else
  record_fail correlation "Anchor/human Spearman below 0.87 or correlation script failed"
fi

# ---------------------------------------------------------------------------
section "Step 4 — Start backend server (flask run on port ${BACKEND_PORT})"
# ---------------------------------------------------------------------------

cleanup_server

SERVER_LOG="$(mktemp "${TMPDIR:-/tmp}/prompthive_server_XXXXXX")"
echo "Server log: ${SERVER_LOG}"

(
  cd "$BACKEND_DIR"
  export FLASK_APP FLASK_RUN_HOST FLASK_RUN_PORT
  "${FLASK_CMD[@]}" >>"$SERVER_LOG" 2>&1
) &
BACKEND_PID=$!
echo "Backend PID: ${BACKEND_PID}"

READY=0
for i in $(seq 1 60); do
  if curl -sS --connect-timeout 2 "${BACKEND_URL}/recipes/" >/dev/null 2>&1; then
    READY=1
    echo "Backend responding on ${BACKEND_URL}/recipes/ (attempt ${i})"
    break
  fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "Backend process exited early. Last 40 log lines:"
    tail -40 "$SERVER_LOG" || true
    break
  fi
  sleep 1
done

if [[ "$READY" -eq 1 ]]; then
  record_pass server
else
  record_fail server "Backend did not become ready on port ${BACKEND_PORT} within 60s"
  echo "See ${SERVER_LOG} for details."
fi

# ---------------------------------------------------------------------------
section "Step 5 — Live API: POST /chatGPT/json with difficultyFilter"
# ---------------------------------------------------------------------------
# Route and payload match app.py + PromptEditor.jsx:
#   POST /chatGPT/json
#   { messages, type, model, temperature, isOpenEnded, difficultyFilter }
#   difficultyFilter: { minLevel, maxLevel, maxCount?, includeUnscored? }
# ---------------------------------------------------------------------------

API_TMP="$(mktemp "${TMPDIR:-/tmp}/prompthive_api_XXXXXX").json"

if [[ "$OPENAI_PRESENT" -eq 0 ]]; then
  echo "SKIP: No OPENAI_API_KEY — live generate-questions test not run."
  set_step api ""
elif [[ "$READY" -ne 1 ]]; then
  echo "SKIP: Backend not ready — live API test not run."
  set_step api ""
else
  echo "Sending request (Easy–Hard, levels 2–4 → logits -1.0..1.0, maxCount=3)..."
  HTTP_CODE="$(
    curl -sS -w '%{http_code}' -o "$API_TMP" \
      -X POST "${BACKEND_URL}/chatGPT/json" \
      -H "Content-Type: application/json" \
      -d '{
        "type": "oatutor_questions",
        "model": "gpt-4o-2024-08-06",
        "temperature": 0.7,
        "isOpenEnded": false,
        "messages": [
          {
            "role": "system",
            "content": "The output should always be valid JSON."
          },
          {
            "role": "system",
            "content": "Generate exactly one new algebra exercise as JSON with a problem row and one step row. Step answerType must be numeric or algebra. Include Problem Name, Row Type, Body Text, Answer, answerType fields."
          },
          {
            "role": "user",
            "content": "Create one linear equation problem: solve 2x+3=11. Use problem name test_diff_filter_e2e."
          }
        ],
        "difficultyFilter": {
          "minLevel": 2,
          "maxLevel": 4,
          "maxCount": 3,
          "includeUnscored": true
        }
      }'
  )"

  echo "HTTP status: ${HTTP_CODE}"
  echo "--- Response (pretty-printed) ---"
  if command -v python3 >/dev/null 2>&1; then
    python3 -m json.tool "$API_TMP" 2>/dev/null || cat "$API_TMP"
  else
    cat "$API_TMP"
  fi
  echo "--- End response ---"

  API_VALIDATE_RC=0
  run_py_script -- "$API_TMP" << 'PY' || API_VALIDATE_RC=$?
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
raw = path.read_text()
try:
    data = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"ERROR: response is not valid JSON: {e}")
    sys.exit(1)

if "difficultyFilterError" in data:
    print(f"ERROR: difficultyFilterError={data['difficultyFilterError']!r}")
    sys.exit(1)

df = data.get("difficultyFilter") or {}
questions = data.get("questions") or []
min_logit = float(df.get("minLogit", df.get("min_logit", -999)))
max_logit = float(df.get("maxLogit", df.get("max_logit", 999)))

print(f"calibrationStatus={df.get('calibrationStatus')!r}")
print(f"filter bounds: minLogit={min_logit}, maxLogit={max_logit}")
print(f"totalBefore={df.get('totalBefore')} totalAfter={df.get('totalAfter')}")
print(f"keptInRange={df.get('keptInRange')} keptUnscored={df.get('keptUnscored')}")

steps = [q for q in questions if str(q.get("Row Type", "")).lower() == "step"]
print(f"returned step rows: {len(steps)}")

violations = []
for q in steps:
    name = q.get("Problem Name")
    logit = q.get("difficultyLogit")
    scored = q.get("difficultyScored", logit is not None)
    label = q.get("difficultyLabel")
    status = q.get("difficultyStatus")
    if scored and logit is not None:
        if not (min_logit <= float(logit) <= max_logit):
            violations.append(f"{name}: logit {logit} outside [{min_logit}, {max_logit}]")
        print(f"  step {name!r}: {label} logit={float(logit):.3f} status={status!r}")
    else:
        print(f"  step {name!r}: UNSCORED status={status!r}")

if violations:
    for v in violations:
        print(f"ERROR: {v}")
    sys.exit(1)

if not questions and df.get("totalAfter", 0) == 0 and df.get("totalBefore", 0) > 0:
    print("NOTE: all generated steps were filtered out (expected when none fall in range).")
elif not questions:
    print("ERROR: no questions returned and totalBefore was 0")
    sys.exit(1)

print("API filter validation OK.")
PY

  if [[ "$HTTP_CODE" == "200" ]] && [[ "$API_VALIDATE_RC" -eq 0 ]]; then
    record_pass api
  else
    record_fail api "Live /chatGPT/json test failed (HTTP ${HTTP_CODE}, validation rc=${API_VALIDATE_RC})"
  fi
fi

# ---------------------------------------------------------------------------
section "Step 6 — Summary"
# ---------------------------------------------------------------------------

cleanup_server
BACKEND_PID=""

OVERALL=0
for key in env unit correlation server api; do
  val="$(get_step "$key")"
  label=""
  case "$key" in
    env) label="Environment setup" ;;
    unit) label="Unit tests (question_difficulty)" ;;
    correlation) label="Anchor vs human correlation" ;;
    server) label="Backend startup" ;;
    api) label="Live API difficulty filter" ;;
  esac
  if [[ -z "$val" ]]; then
    status="SKIPPED"
  elif [[ "$val" == "1" ]]; then
    status="PASS"
  else
    status="FAIL"
    OVERALL=1
  fi
  printf "  %-36s %s\n" "$label" "$status"
done

if [[ ${#FAILURES[@]} -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for f in "${FAILURES[@]}"; do
    echo "  - $f"
  done
fi

echo ""
echo "To test the difficulty slider UI in your browser, start the frontend separately:"
echo "  cd ${FRONTEND_DIR} && npm install && npm start"
echo "  (Vite dev server — typically http://localhost:5173; API proxy via .env-dev → port 8080)"
echo ""

if [[ "$OVERALL" -eq 0 ]]; then
  echo "All executed steps passed."
  exit 0
else
  echo "One or more steps failed."
  exit 1
fi
