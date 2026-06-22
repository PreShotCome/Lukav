#!/usr/bin/env bash
# scripts/e2e.sh — one-command, zero-input end-to-end check for Lukav.
#
# Inspired by Soteria's scripts/e2e.sh. Pattern: install deps in the
# active venv, run unit tests, boot uvicorn on a free port, hit
# /healthz, then tear down. Exits non-zero if any stage fails.
#
# Phase-by-phase this script grows:
#   Phase 0: pytest + healthz boot probe.
#   Phase 1: also walks /link and /accounts against Plaid sandbox.
#   Phase 2: also runs scan against a seeded sandbox card.
#   Phase 3: also generates a sample dispute PDF and asserts it exists.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${LUKAV_E2E_PORT:-8766}"
BASE_URL="http://127.0.0.1:${PORT}"

echo "==> ensuring deps"
python -m pip install --quiet -e ".[dev]"

echo "==> pytest"
python -m pytest

echo "==> smoke construct"
python -m lukav --check

echo "==> end-to-end smoke (Plaid+scan+letter)"
LUKAV_DB="$(mktemp -d)/smoke.db" python scripts/smoke.py

echo "==> booting server on ${PORT}"
LOG="$(mktemp)"
python -m lukav --no-open --host 127.0.0.1 --port "${PORT}" >"${LOG}" 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "${SERVER_PID}" 2>/dev/null || true
  wait "${SERVER_PID}" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for readiness.
ready=0
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "${BASE_URL}/healthz" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 0.5
done
if [[ "${ready}" -ne 1 ]]; then
  echo "!! server failed to come up; last log lines:" >&2
  tail -30 "${LOG}" >&2
  exit 1
fi
echo "==> healthz green"

# Index page renders.
if ! curl -fsS "${BASE_URL}/" | grep -q "Lukav"; then
  echo "!! index page missing 'Lukav'" >&2
  exit 1
fi
echo "==> index green"

echo ""
echo "  E2E GREEN — pytest + boot + healthz + index all pass."
