#!/usr/bin/env bash
# Rapid local test runner: sets up an isolated venv (if missing), installs
# dev+runtime deps, and runs the full pytest suite headlessly.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV_DIR=".venv-test"

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install -q --upgrade pip
    "$VENV_DIR/bin/pip" install -q -r requirements-dev.txt
fi

export QT_QPA_PLATFORM=offscreen
exec "$VENV_DIR/bin/python" -m pytest "$@"
