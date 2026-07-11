#!/usr/bin/env bash
# github-copilot-usage — one-command start (macOS / Linux)
# Creates .venv on first run, installs dependencies, starts the dashboard and
# opens it in your browser. Re-run any time; setup is skipped when done.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "[setup] creating virtual environment..."
    python3 -m venv .venv
    echo "[setup] installing dependencies..."
    ./.venv/bin/python -m pip install --quiet --upgrade pip
    ./.venv/bin/python -m pip install --quiet -r requirements.txt
fi

exec ./.venv/bin/python -m app --open "$@"
