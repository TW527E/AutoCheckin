#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR=${AUTOCHECKIN_VENV_DIR:-"$SCRIPT_DIR/.venv"}

cd "$SCRIPT_DIR"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"
    "$VENV_DIR/bin/python" -m playwright install chromium
fi

exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/agentrouter_checkin.py" "$@"
