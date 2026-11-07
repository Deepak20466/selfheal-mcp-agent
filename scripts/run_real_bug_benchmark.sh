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
"$PYTHON" -m benchmarks.real_bugs.run --run-file "$FULL" --token-cap 40000 --cost-cap-usd 0 --timeout 240 --resume
"$PYTHON" -m benchmarks.real_bugs.run --run-file "$REPEAT_A" --token-cap 40000 --cost-cap-usd 0 --timeout 240 --limit 20 --resume
"$PYTHON" -m benchmarks.real_bugs.run --run-file "$REPEAT_B" --token-cap 40000 --cost-cap-usd 0 --timeout 240 --limit 20 --resume
"$PYTHON" -m benchmarks.real_bugs.score "$FULL" "$REPEAT_A" "$REPEAT_B" --output-dir "$RESULTS"
