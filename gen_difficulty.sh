#!/usr/bin/env bash
# Generate one algebra question via POST /chatGPT/json and apply difficultyFilter.
# Usage: ./gen_difficulty.sh MIN_LEVEL [MAX_LEVEL]
#   ./gen_difficulty.sh 4       # Hard only (level 4)
#   ./gen_difficulty.sh 2 4     # Easy through Hard
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="${ROOT}/abscribe_backend"
BACKEND_PORT=8080
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"

WE_STARTED_BACKEND=0
BACKEND_PID=""
RESPONSE_TMP=""

cleanup() {
  if [[ "$WE_STARTED_BACKEND" -eq 1 ]] && [[ -n "${BACKEND_PID:-}" ]]; then
    echo ""
    echo "Stopping backend we started (PID ${BACKEND_PID})..."
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
  [[ -n "${RESPONSE_TMP:-}" ]] && rm -f "$RESPONSE_TMP"
}
trap cleanup EXIT INT TERM

usage() {
  echo "Usage: $0 MIN_LEVEL [MAX_LEVEL]"
  echo "  MIN_LEVEL and MAX_LEVEL are integers 1-5 (default MAX = MIN)."
  exit 1
}

[[ $# -lt 1 || $# -gt 2 ]] && usage

MIN_LEVEL="$1"
MAX_LEVEL="${2:-$1}"

if ! [[ "$MIN_LEVEL" =~ ^[1-5]$ && "$MAX_LEVEL" =~ ^[1-5]$ ]]; then
  echo "ERROR: levels must be integers 1-5."
  usage
fi

# Swap if given out of order
if [[ "$MIN_LEVEL" -gt "$MAX_LEVEL" ]]; then
  tmp="$MIN_LEVEL"
  MIN_LEVEL="$MAX_LEVEL"
  MAX_LEVEL="$tmp"
fi

echo "======================================================================"
echo "Difficulty level mapping (minLevel / maxLevel → logit range)"
echo "======================================================================"
echo "  1 = Very Easy   :  -4.0 to -1.0"
echo "  2 = Easy        :  -1.0 to -0.3"
echo "  3 = Medium      :  -0.3 to +0.3"
echo "  4 = Hard        :  +0.3 to +1.0"
echo "  5 = Very Hard   :  +1.0 to +4.0"
echo ""
echo "Request filter: minLevel=${MIN_LEVEL}, maxLevel=${MAX_LEVEL}"
echo "======================================================================"
echo ""

# --- Load backend .env (do not print secrets) ---
OPENAI_PRESENT=0
for envfile in "${BACKEND_DIR}/.env" "${BACKEND_DIR}/abscribe_backend/.env"; do
  if [[ -f "$envfile" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$envfile"
    set +a
  fi
done
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
  echo "ERROR: OPENAI_API_KEY is not set."
  echo "  Add it to ${BACKEND_DIR}/.env before generating questions."
  exit 1
fi

if ! command -v Rscript >/dev/null 2>&1; then
  echo "ERROR: Rscript is not installed (required for Rasch calibration)."
  echo "  Install R, then re-run this script."
  exit 1
fi

# --- Backend: use existing or start one ---
backend_responds() {
  curl -sS --connect-timeout 2 "${BACKEND_URL}/recipes/" >/dev/null 2>&1
}

if backend_responds; then
  echo "Backend already running on port ${BACKEND_PORT} — using it."
else
  echo "Starting backend on port ${BACKEND_PORT}..."
  SERVER_LOG="$(mktemp "${TMPDIR:-/tmp}/gen_difficulty_server_XXXXXX")"
  export FLASK_APP=abscribe_backend.app:app
  export FLASK_RUN_HOST=0.0.0.0
  export FLASK_RUN_PORT=${BACKEND_PORT}
  export PYTHONPATH="${BACKEND_DIR}:${PYTHONPATH:-}"

  if command -v poetry >/dev/null 2>&1 && [[ -f "${BACKEND_DIR}/pyproject.toml" ]]; then
    FLASK_CMD=(poetry run flask run)
  else
    FLASK_CMD=(python3 -m flask run)
  fi

  (
    cd "$BACKEND_DIR"
    "${FLASK_CMD[@]}" >>"$SERVER_LOG" 2>&1
  ) &
  BACKEND_PID=$!
  WE_STARTED_BACKEND=1
  echo "  PID ${BACKEND_PID}  log → ${SERVER_LOG}"

  ready=0
  for _ in $(seq 1 60); do
    if backend_responds; then
      ready=1
      echo "Backend ready."
      break
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      echo "ERROR: backend exited during startup. Log tail:"
      tail -30 "$SERVER_LOG" || true
      exit 1
    fi
    sleep 1
  done
  if [[ "$ready" -eq 0 ]]; then
    echo "ERROR: backend did not respond within 60s."
    exit 1
  fi
fi

echo ""
echo "POST ${BACKEND_URL}/chatGPT/json  (type=oatutor_questions)"
echo ""

RESPONSE_TMP="$(mktemp "${TMPDIR:-/tmp}/gen_difficulty_resp_XXXXXX").json"

HTTP_CODE="$(
  curl -sS -w '%{http_code}' -o "$RESPONSE_TMP" \
    -X POST "${BACKEND_URL}/chatGPT/json" \
    -H "Content-Type: application/json" \
    -d "{
      \"type\": \"oatutor_questions\",
      \"model\": \"gpt-4o-2024-08-06\",
      \"temperature\": 0.7,
      \"isOpenEnded\": false,
      \"messages\": [
        {
          \"role\": \"system\",
          \"content\": \"The output should always be valid JSON.\"
        },
        {
          \"role\": \"system\",
          \"content\": \"Generate exactly one new algebra exercise as JSON with a problem row and one step row. Step answerType must be numeric or algebra. Include Problem Name, Row Type, Body Text, Answer, answerType fields.\"
        },
        {
          \"role\": \"user\",
          \"content\": \"Create one linear equation problem: solve 2x+3=11. Use problem name gen_diff_${MIN_LEVEL}_${MAX_LEVEL}.\"
        }
      ],
      \"difficultyFilter\": {
        \"minLevel\": ${MIN_LEVEL},
        \"maxLevel\": ${MAX_LEVEL},
        \"includeUnscored\": true
      }
    }"
)"

echo "HTTP ${HTTP_CODE}"
echo ""
echo "--- Response ---"
if command -v python3 >/dev/null 2>&1; then
  python3 -m json.tool "$RESPONSE_TMP" 2>/dev/null || cat "$RESPONSE_TMP"
else
  cat "$RESPONSE_TMP"
fi
echo "--- End response ---"
echo ""

if [[ "$HTTP_CODE" != "200" ]]; then
  echo "ERROR: request failed with HTTP ${HTTP_CODE}"
  exit 1
fi

python3 - "$RESPONSE_TMP" "$MIN_LEVEL" "$MAX_LEVEL" << 'PY'
import json
import sys

path, min_level, max_level = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
data = json.loads(open(path, encoding="utf-8").read())

if err := data.get("difficultyFilterError"):
    print(f"SUMMARY: ERROR difficultyFilterError={err!r}")
    sys.exit(1)

df = data.get("difficultyFilter") or {}
questions = data.get("questions") or []
min_logit = df.get("minLogit", df.get("min_logit"))
max_logit = df.get("maxLogit", df.get("max_logit"))
total_before = df.get("totalBefore", "?")
total_after = df.get("totalAfter", "?")
status = df.get("calibrationStatus", "?")

steps = [q for q in questions if str(q.get("Row Type", "")).lower() == "step"]
evals = {e["index"]: e for e in df.get("evaluations") or [] if "index" in e}

print("--- Summary ---")
print(f"Filter: levels {min_level}-{max_level}  →  logit [{min_logit}, {max_logit}]")
print(f"calibrationStatus: {status}")
print(f"totalBefore: {total_before}  totalAfter: {total_after}")

if not steps:
    # Show why from evaluations if everything filtered out
    step_evals = [e for e in (df.get("evaluations") or []) if str(e.get("rowType", "")).lower() == "step"]
    if step_evals:
        e = step_evals[0]
        logit = e.get("difficultyLogit")
        label = e.get("difficultyLabel") or "?"
        kept = e.get("kept", False)
        reason = e.get("reason", "?")
        print(f"Generated step: {label} logit={logit}  kept={kept}  reason={reason}")
        if not kept and reason == "outside_logit_range":
            print("→ Question was FILTERED OUT (logit outside requested range).")
        elif not kept:
            print(f"→ Question was FILTERED OUT ({reason}).")
    else:
        print("No step rows in response or evaluations.")
else:
    for q in steps:
        body = (q.get("Body Text") or q.get("bodyText") or "")[:80]
        logit = q.get("difficultyLogit")
        label = q.get("difficultyLabel") or "?"
        scored = q.get("difficultyScored", logit is not None)
        st = q.get("difficultyStatus") or "?"
        print(f"KEPT step: {label}  logit={logit}  scored={scored}  status={st}")
        print(f"  Body: {body!r}...")
        print("→ Question was KEPT (passed filter).")

print("---")
PY
