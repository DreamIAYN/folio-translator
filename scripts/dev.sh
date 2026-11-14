#!/usr/bin/env bash
# Start Folio in the foreground. mini-harness must already be running on :8000.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m app
