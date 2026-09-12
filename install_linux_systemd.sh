#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RUNNER="$SCRIPT_DIR/run_checkin.sh"
UNIT_DIR="$HOME/.config/systemd/user"
CHECKIN_SERVICE="autocheckin-checkin.service"
CHECKIN_TIMER="autocheckin-checkin.timer"
TELEGRAM_SERVICE="autocheckin-telegram.service"
CONFIG_PATH=${AGENTROUTER_CONFIG:-"$SCRIPT_DIR/config.json"}
ON_CALENDAR=${AUTOCHECKIN_ON_CALENDAR:-"*-*-* 08:00:00"}
ACTION=install

usage() {
    cat <<'EOF'
Usage: ./install_linux_systemd.sh [options]

Install or remove the current user's systemd check-in timer and Telegram listener.

Options:
  --on-calendar VALUE  systemd calendar expression (default: *-*-* 08:00:00)
  --config PATH        Config file used by the services
  --remove             Stop and remove the services
  --show               Show service and timer status
  -h, --help           Show this help
EOF
}

while (($#)); do
    case "$1" in
        --on-calendar)
            (($# >= 2)) || { echo "--on-calendar requires a value" >&2; exit 2; }
            ON_CALENDAR=$2
            shift 2
            ;;
        --config)
            (($# >= 2)) || { echo "--config requires a path" >&2; exit 2; }
            CONFIG_PATH=$2
            shift 2
            ;;
        --remove)
            ACTION=remove
            shift
            ;;
        --show)
            ACTION=show
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is required on Linux." >&2
    exit 2
fi

# User services normally use the session bus. SSH and non-login shells often
# omit its environment, so provide the standard runtime path or use the user
# manager through systemd's machine transport.
USER_ID=$(id -u)
USER_NAME=${USER:-$(id -un)}
RUNTIME_DIR=${XDG_RUNTIME_DIR:-"/run/user/$USER_ID"}
if [ -d "$RUNTIME_DIR" ]; then
    export XDG_RUNTIME_DIR="$RUNTIME_DIR"
fi
if [ -S "$RUNTIME_DIR/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME_DIR/bus"
fi

if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$USER_NAME" 2>/dev/null || echo "Warning: could not enable user lingering; services may stop when you log out." >&2
fi

# Lingering makes the user manager eligible to run, but on a fresh SSH shell
# it may not have been started yet. Starting it through the system manager
# creates the runtime directory and user bus without requiring manual exports.
if [ "$(id -u)" -eq 0 ]; then
    systemctl start "user@${USER_ID}.service" 2>/dev/null || true
fi

# The user manager may have created /run/user/<UID> only after it started.
if [ -d "$RUNTIME_DIR" ]; then
    export XDG_RUNTIME_DIR="$RUNTIME_DIR"
fi
if [ -S "$RUNTIME_DIR/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME_DIR/bus"
fi

if [ -S "$RUNTIME_DIR/bus" ] && systemctl --user show-environment >/dev/null 2>&1; then
    SYSTEMCTL_USER_ARGS=(--user)
elif systemctl --machine="$USER_NAME@.host" --user show-environment >/dev/null 2>&1; then
    SYSTEMCTL_USER_ARGS=(--machine="$USER_NAME@.host" --user)
else
    echo "Cannot connect to the systemd user bus for $USER_NAME." >&2
    echo "Run this from a logged-in user session, or enable lingering and retry:" >&2
    echo "  loginctl enable-linger $USER_NAME" >&2
    echo "  export XDG_RUNTIME_DIR=/run/user/$(id -u)" >&2
    echo "  export DBUS_SESSION_BUS_ADDRESS=unix:path=\$XDG_RUNTIME_DIR/bus" >&2
    exit 2
fi

systemctl_user() {
    systemctl "${SYSTEMCTL_USER_ARGS[@]}" "$@"
}

case "$ACTION" in
    show)
        systemctl_user --no-pager status "$CHECKIN_TIMER" "$TELEGRAM_SERVICE" || true
        exit 0
        ;;
    remove)
        systemctl_user disable --now "$CHECKIN_TIMER" "$TELEGRAM_SERVICE" 2>/dev/null || true
        rm -f "$UNIT_DIR/$CHECKIN_SERVICE" "$UNIT_DIR/$CHECKIN_TIMER" "$UNIT_DIR/$TELEGRAM_SERVICE"
        systemctl_user daemon-reload
        echo "Removed AutoCheckin systemd services."
        exit 0
        ;;
esac

if [ ! -f "$CONFIG_PATH" ]; then
    echo "Config file not found: $CONFIG_PATH" >&2
    echo "Create it first with: $RUNNER --init-config" >&2
    exit 2
fi

TIMEZONE=$(python3 - "$CONFIG_PATH" <<'PY'
import json
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        config = json.load(stream)
    timezone = config.get("timezone", "Asia/Taipei")
    ZoneInfo(timezone)
    print(timezone)
except (OSError, ValueError, TypeError, ZoneInfoNotFoundError):
    raise SystemExit(1)
PY
) || {
    echo "Invalid timezone in config. Use an IANA timezone such as Asia/Taipei." >&2
    exit 2
}

systemd_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//"/\\"}
    value=${value//%/%%}
    printf '"%s"' "$value"
}

mkdir -p "$UNIT_DIR"
quoted_runner=$(systemd_quote "$RUNNER")
quoted_config=$(systemd_quote "$CONFIG_PATH")

cat > "$UNIT_DIR/$CHECKIN_SERVICE" <<EOF
[Unit]
Description=AutoCheckin AgentRouter daily sign-in

[Service]
Type=oneshot
WorkingDirectory=$SCRIPT_DIR
ExecStart=$quoted_runner --headless --no-telegram-poll --config $quoted_config
EOF

cat > "$UNIT_DIR/$CHECKIN_TIMER" <<EOF
[Unit]
Description=Run AutoCheckin AgentRouter sign-in on schedule

[Timer]
OnCalendar=$ON_CALENDAR $TIMEZONE
Persistent=true
Unit=$CHECKIN_SERVICE

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/$TELEGRAM_SERVICE" <<EOF
[Unit]
Description=AutoCheckin Telegram command listener
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$SCRIPT_DIR
ExecStart=$quoted_runner --telegram-listen --config $quoted_config
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl_user daemon-reload
systemctl_user enable --now "$CHECKIN_TIMER"

if python3 - "$CONFIG_PATH" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        config = json.load(stream)
    telegram = config.get("telegram", {})
    raise SystemExit(0 if telegram.get("bot_token") and telegram.get("chat_id") else 1)
except (OSError, ValueError, TypeError):
    raise SystemExit(1)
PY
then
    systemctl_user enable --now "$TELEGRAM_SERVICE"
else
    systemctl_user disable --now "$TELEGRAM_SERVICE" 2>/dev/null || true
    echo "Telegram service was installed but not started: configure telegram.bot_token and telegram.chat_id first." >&2
fi

echo "Installed AutoCheckin systemd timer: $ON_CALENDAR"
echo "Check status with: $0 --show"
