#!/usr/bin/env bash
# Lukav.command — double-click launcher for macOS / Linux.
#
# First run: creates a venv at .venv/, installs deps, starts Lukav.
# Later runs: just starts.
#
# After cloning the repo:  chmod +x scripts/Lukav.command
# Then double-click it in Finder (macOS) or via your file manager.

set -e
cd "$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -x ".venv/bin/python" ]; then
    echo "[lukav] first-time setup — creating venv..."
    PYBIN="${PYTHON:-python3}"
    if ! command -v "$PYBIN" >/dev/null 2>&1; then
        echo "[lukav] Python 3.10+ not found. Install it (brew install python or python.org) and re-run."
        read -rsp "Press Enter to close..."
        exit 1
    fi
    "$PYBIN" -m venv .venv
    # shellcheck source=/dev/null
    source .venv/bin/activate
    echo "[lukav] installing dependencies (this only happens once)..."
    pip install --quiet --upgrade pip
    pip install --quiet -e ".[plaid,secrets,desktop]"
else
    # shellcheck source=/dev/null
    source .venv/bin/activate
fi

echo "[lukav] starting Lukav in a native window..."
python -m lukav --window
