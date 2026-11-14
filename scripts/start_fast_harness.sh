#!/usr/bin/env bash
# Start a second mini-harness on :8001 with thinking disabled.
# It shares the same harness DB and API key as the main instance, so the same
# platform key works; the platform uses it only for jobs with thinking=false.
set -euo pipefail
PROJ20="${PROJ20:-$(cd "$(dirname "$0")/.." && pwd)}"
PROJ05="${PROJ05:-$(cd "$PROJ20/.." && pwd)/miniharness}"
if [[ ! -f "$PROJ05/.env" ]]; then
  echo "Set PROJ05 to the mini-harness checkout (default: $PROJ05)" >&2
  exit 1
fi

cd "$PROJ20"
set -a
. "$PROJ05/.env"
set +a
export LLM_THINKING=false
export HARNESS_PORT="${FAST_HARNESS_PORT:-8001}"
export HARNESS_DB_PATH="${HARNESS_DB_PATH:-$PROJ20/data/harness.db}"
export HARNESS_WORKSPACE_ROOT="${HARNESS_WORKSPACE_ROOT:-$PROJ20/data/workspaces}"
exec "$PROJ05/.venv/bin/python" -m harness
