#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON="${PYTHON:-python}"
DATE="$("$PYTHON" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime("%Y-%m-%d"))')"
RESULTS="benchmarks/real_bugs/results"
RUNS="$RESULTS/runs"
mkdir -p "$RUNS"

if [[ -z "${REAL_BUG_PROVIDER:-}" ]]; then
  echo "Set REAL_BUG_PROVIDER to groq_api or openrouter_api and configure its free-tier key." >&2
  exit 2
fi
if [[ -z "${REAL_BUG_DATABASE_URL:-}" ]]; then
  echo "Set REAL_BUG_DATABASE_URL to a running, disposable local PostgreSQL database ending in _test." >&2
  exit 2
fi

"$PYTHON" -m benchmarks.real_bugs.mine
"$PYTHON" -m benchmarks.real_bugs.prepare
FULL="$RUNS/$DATE-full.jsonl"
REPEAT_A="$RUNS/$DATE-repeat-a.jsonl"
REPEAT_B="$RUNS/$DATE-repeat-b.jsonl"

score_partial_and_stop() {
  local inputs=()
  for path in "$FULL" "$REPEAT_A" "$REPEAT_B"; do
    [[ -f "$path" ]] && inputs+=("$path")
  done
  if ((${#inputs[@]})); then
    "$PYTHON" -m benchmarks.real_bugs.score "${inputs[@]}" --output-dir "$RESULTS"
  fi
  echo "Benchmark stopped on provider cooldown. Raw records and partial aggregates were saved." >&2
  exit 75
}

run_phase() {
  local run_file="$1"
  shift
  local status=0
  set +e
  "$PYTHON" -m benchmarks.real_bugs.run --run-file "$run_file" \
    --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume "$@"
  status=$?
  set -e
  if [[ "$status" -eq 75 ]]; then
    score_partial_and_stop
  elif [[ "$status" -ne 0 ]]; then
    echo "Benchmark run failed with exit code $status." >&2
    exit "$status"
  fi
}

run_phase "$FULL"
# Each case has two arms, so 20 attempts cover the first ten cases.
run_phase "$REPEAT_A" --limit 20
run_phase "$REPEAT_B" --limit 20
"$PYTHON" -m benchmarks.real_bugs.score "$FULL" "$REPEAT_A" "$REPEAT_B" --output-dir "$RESULTS"
