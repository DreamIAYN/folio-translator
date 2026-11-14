#!/usr/bin/env bash
# Create a dedicated mini-harness user for Folio and print its one-time API key.
# Usage:
#   HARNESS_URL=http://127.0.0.1:8000 \
#   HARNESS_ADMIN_TOKEN=... scripts/create_harness_user.sh [email]
set -euo pipefail

HARNESS_URL="${HARNESS_URL:-http://127.0.0.1:8000}"
EMAIL="${1:-booktrans@local}"

if [[ -z "${HARNESS_ADMIN_TOKEN:-}" ]]; then
  PROJ05="${PROJ05:-$(cd "$(dirname "$0")/../.." && pwd)/miniharness}"
  if [[ -f "$PROJ05/.env" ]]; then
    HARNESS_ADMIN_TOKEN="$(grep '^HARNESS_ADMIN_TOKEN=' "$PROJ05/.env" | cut -d= -f2-)"
  fi
fi
if [[ -z "${HARNESS_ADMIN_TOKEN:-}" ]]; then
  echo "Set HARNESS_ADMIN_TOKEN or PROJ05 to the mini-harness checkout" >&2
  exit 1
fi

curl -fsS -X POST "$HARNESS_URL/admin/users" \
  -H "X-Admin-Token: $HARNESS_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"name\":\"Folio Platform\",\"quota_tokens\":-1,\"issue_key\":true,\"key_name\":\"folio-platform\"}"
echo
