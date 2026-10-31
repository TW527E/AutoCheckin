#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR=${AUTOCHECKIN_VENV_DIR:-"$SCRIPT_DIR/.venv"}
# Written only after every install step succeeds, so an interrupted or failed
# install (no network, pip error) is retried on the next run instead of leaving
# a venv without Playwright.
READY_MARKER="$VENV_DIR/.autocheckin-ready"

cd "$SCRIPT_DIR"

if [ ! -f "$READY_MARKER" ]; then
    [ -x "$VENV_DIR/bin/python" ] || python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"
    "$VENV_DIR/bin/python" -m playwright install chromium
    : > "$READY_MARKER"
fi

exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/agentrouter_checkin.py" "$@"
