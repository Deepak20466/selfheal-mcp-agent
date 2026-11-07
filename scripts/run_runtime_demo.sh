#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON=".venv/bin/python"
SELFHEAL=".venv/bin/selfheal"
if [[ ! -x "$PYTHON" || ! -x "$SELFHEAL" ]]; then
  echo "Install the project into .venv first (see docs/operations.md)." >&2
  exit 2
fi

BUG="${1:-profile}"
if [[ ! "$BUG" =~ ^(profile|pagination|quantity)$ ]]; then
  echo "Usage: scripts/run_runtime_demo.sh [profile|pagination|quantity]" >&2
  exit 2
fi

"$SELFHEAL" up
SENTINEL_INGEST_TOKEN="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SENTINEL_INGEST_TOKEN
"$PYTHON" scripts/sync_monitored_apps.py
STARTED_AT="$("$PYTHON" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
"$PYTHON" -m uvicorn examples.runtime_demo.main:app --host 127.0.0.1 --port 8110 &
APP_PID=$!
cleanup() {
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$PYTHON" examples/runtime_demo/trigger.py "$BUG"
"$PYTHON" scripts/wait_runtime_demo_job.py --since "$STARTED_AT"
echo "Demo app stopped. Selfheal service processes remain under 'selfheal down' control."
