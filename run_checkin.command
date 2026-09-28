#!/bin/sh
# macOS Finder double-click entry point; the real launcher is run_checkin.sh.
exec "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/run_checkin.sh" "$@"
