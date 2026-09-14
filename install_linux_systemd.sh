#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RUNNER="$SCRIPT_DIR/run_checkin.sh"
UNIT_DIR=/etc/systemd/system
CHECKIN_SERVICE=autocheckin-checkin.service
CHECKIN_TIMER=autocheckin-checkin.timer
TELEGRAM_SERVICE=autocheckin-telegram.service
UNITS=("$CHECKIN_TIMER" "$CHECKIN_SERVICE" "$TELEGRAM_SERVICE")
CONFIG_PATH=${AGENTROUTER_CONFIG:-"$SCRIPT_DIR/config.json"}
ON_CALENDAR=${AUTOCHECKIN_ON_CALENDAR:-"*-*-* 08:00:00"}
RUN_AS=${SUDO_USER:-$(id -un)}
ACTION=install

usage() {
    printf '%s\n' \
        'Usage: sudo ./install_linux_systemd.sh [options]' \
        '' \
        'Install system-level AutoCheckin services and migrate the selected user’s legacy units.' \
        '' \
        '  --on-calendar VALUE  systemd calendar expression (default: *-*-* 08:00:00)' \
        '  --config PATH        Config file used by the services' \
        '  --run-as USER        Runtime / migration account (default: SUDO_USER or current user)' \
        '  --remove             Stop and remove system services only (requires root)' \
        '  --show               Show system service and timer status (read-only)' \
        '  -h, --help           Show this help'
}

fail() { printf '%s\n' "$*" >&2; exit 2; }

while (($#)); do
    case "$1" in
        --on-calendar|--config|--run-as)
            (($# >= 2)) || fail "$1 requires a value"
            case "$1" in
                --on-calendar) ON_CALENDAR=$2 ;;
                --config) CONFIG_PATH=$2 ;;
                --run-as) RUN_AS=$2 ;;
            esac
            shift 2
            ;;
        --remove) ACTION=remove; shift ;;
        --show) ACTION=show; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

command -v systemctl >/dev/null 2>&1 || fail 'systemctl is required on Linux.'
if [ "$ACTION" = show ]; then
    systemctl --no-pager status "${UNITS[@]}" || true
    exit 0
fi
[ "$(id -u)" -eq 0 ] || fail 'System service installation/removal requires root. Run this script with sudo.'

# Only replace/remove units matching this installer; custom overrides need review.
inspect_units() {
    local directory=$1 unit path
    for unit in "${UNITS[@]}"; do
        [ ! -e "$directory/$unit.d" ] && [ ! -L "$directory/$unit.d" ] \
            || fail "Custom overrides found: $directory/$unit.d. Review them before continuing."
        path="$directory/$unit"
        if [ -e "$path" ] || [ -L "$path" ]; then
            [ -f "$path" ] && [ ! -L "$path" ] || fail "Unrecognized unit: $path; refusing to remove or overwrite it."
            grep -q '^Description=.*AutoCheckin' "$path" || fail "Unrecognized unit: $path"
            if [ "$unit" = "$CHECKIN_TIMER" ]; then
                grep -qx "Unit=$CHECKIN_SERVICE" "$path" || fail "Unrecognized timer: $path"
            else
                grep -q '^ExecStart=.*run_checkin\.sh' "$path" || fail "Unrecognized service: $path"
            fi
        fi
    done
}

stop_system_units() {
    local unit state
    for unit in "${UNITS[@]}"; do
        state=$(systemctl show "$unit" --property=LoadState --value) || fail "Cannot inspect system unit $unit"
        if [ "$state" != not-found ]; then
            systemctl stop "$unit"
            systemctl disable "$unit"
        fi
    done
}

inspect_units "$UNIT_DIR"
if [ "$ACTION" = remove ]; then
    stop_system_units
    for unit in "${UNITS[@]}"; do rm -f -- "$UNIT_DIR/$unit"; done
    systemctl daemon-reload
    printf '%s\n' 'Removed AutoCheckin system services. Config, browser profile and user services were not removed.'
    exit 0
fi

command -v python3 >/dev/null 2>&1 || fail 'python3 is required.'
command -v systemd-analyze >/dev/null 2>&1 || fail 'systemd-analyze is required to validate the schedule.'
command -v runuser >/dev/null 2>&1 || fail 'runuser is required to check the runtime account.'
[[ "$RUN_AS" != -* && -n "$RUN_AS" ]] || fail 'Invalid --run-as account.'
ACCOUNT=$(getent passwd "$RUN_AS") || fail "Unknown runtime account: $RUN_AS"
IFS=: read -r RUN_AS _ RUN_UID RUN_GID _ RUN_HOME _ <<< "$ACCOUNT"
[[ "$RUN_HOME" = /* && "$RUN_HOME" != / ]] || fail "Invalid home directory for $RUN_AS"
[ "$RUN_UID" -ne 0 ] || printf '%s\n' 'Warning: running browsers as root is not recommended; use --run-as with the original non-root account.' >&2
CONFIG_PATH=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CONFIG_PATH")
for value in "$SCRIPT_DIR" "$CONFIG_PATH" "$RUN_HOME" "$ON_CALENDAR"; do
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *$'\t'* ]] \
        || fail 'Paths and calendar must not contain line breaks or tabs.'
done
[ -f "$CONFIG_PATH" ] || fail "Config file not found: $CONFIG_PATH. Create it first with $RUNNER --init-config"
runuser -u "$RUN_AS" -- test -r "$CONFIG_PATH" || fail "$RUN_AS cannot read $CONFIG_PATH"
runuser -u "$RUN_AS" -- test -x "$RUNNER" || fail "$RUN_AS cannot execute $RUNNER"
runuser -u "$RUN_AS" -- test -w "$(dirname -- "$CONFIG_PATH")" || fail "$RUN_AS cannot write Telegram state beside $CONFIG_PATH"

CONFIG_SETTINGS=$(python3 - "$CONFIG_PATH" <<'PY'
import json
import sys
from zoneinfo import ZoneInfo

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        config = json.load(stream)
    timezone = config.get("timezone", "Asia/Taipei")
    ZoneInfo(timezone)
    telegram = config.get("telegram", {})
    enabled = bool(telegram.get("bot_token") and telegram.get("chat_id"))
    print(timezone)
    print(int(enabled))
except (OSError, ValueError, TypeError, KeyError, AttributeError):
    raise SystemExit("Invalid config: check the JSON, timezone and telegram object.")
PY
) || fail 'Configuration validation failed; existing services were not changed.'
TIMEZONE=${CONFIG_SETTINGS%$'\n'*}
TELEGRAM_ENABLED=${CONFIG_SETTINGS##*$'\n'}
systemd-analyze calendar "$ON_CALENDAR $TIMEZONE" >/dev/null || fail 'Invalid calendar; existing services were not changed.'

LEGACY_DIR="$RUN_HOME/.config/systemd/user"
inspect_units "$LEGACY_DIR"
LEGACY_FILES=()
shopt -s nullglob
for unit in "${UNITS[@]}"; do
    if [ -e "$LEGACY_DIR/$unit" ]; then LEGACY_FILES+=("$LEGACY_DIR/$unit"); fi
    for link in "$LEGACY_DIR"/*.wants/"$unit" "$LEGACY_DIR"/*.requires/"$unit"; do
        [ -L "$link" ] && [ "$(readlink -m -- "$link")" = "$LEGACY_DIR/$unit" ] \
            || fail "Unrecognized legacy enablement entry: $link"
        LEGACY_FILES+=("$link")
    done
done

USER_MANAGER_STATE=$(systemctl show "user@$RUN_UID.service" --property=ActiveState --value) \
    || fail 'Cannot determine whether the legacy user manager is running.'
USER_CONNECTED=false
LEGACY_LOADED=()
userctl() {
    if [ "$USER_TRANSPORT" = machine ]; then
        systemctl --machine="$RUN_AS@.host" --user "$@"
    else
        runuser -u "$RUN_AS" -- env XDG_RUNTIME_DIR="/run/user/$RUN_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$RUN_UID/bus" systemctl --user "$@"
    fi
}
case "$USER_MANAGER_STATE" in
    inactive|failed) ;;
    active)
        USER_TRANSPORT=machine
        if userctl show-environment >/dev/null 2>&1; then
            USER_CONNECTED=true
        else
            USER_TRANSPORT=bus
            userctl show-environment >/dev/null 2>&1 \
                || fail "Cannot inspect running user services for $RUN_AS; migration stopped to avoid duplicate jobs."
            USER_CONNECTED=true
        fi
        for unit in "${UNITS[@]}"; do
            state=$(userctl show "$unit" --property=LoadState --value) || fail "Cannot inspect legacy $unit"
            if [ "$state" != not-found ]; then
                fragment=$(userctl show "$unit" --property=FragmentPath --value) || fail "Cannot inspect legacy path for $unit"
                [[ -z "$fragment" || "$fragment" = "$LEGACY_DIR/$unit" ]] \
                    || fail "Legacy $unit is loaded from $fragment; review it manually before migration."
                dropins=$(userctl show "$unit" --property=DropInPaths --value) || fail "Cannot inspect legacy overrides for $unit"
                [ -z "$dropins" ] || fail "Legacy $unit has custom overrides; review them before migration."
                LEGACY_LOADED+=("$unit")
            fi
        done
        ;;
    *) fail "User manager is in state '$USER_MANAGER_STATE'; retry after it settles." ;;
esac

systemd_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    value=${value//$'\t'/\\t}
    printf '"%s"' "$value"
}
exec_quote() {
    local value=$1
    value=${value//\$/\$\$}
    systemd_quote "$value"
}

STAGING=$(mktemp -d)
trap 'rm -rf -- "$STAGING"' EXIT
quoted_runner=$(exec_quote "$RUNNER")
quoted_config=$(exec_quote "$CONFIG_PATH")
working_directory=${SCRIPT_DIR//%/%%}
quoted_home=$(systemd_quote "HOME=$RUN_HOME")

printf '%s\n' \
    '[Unit]' 'Description=AutoCheckin AgentRouter daily sign-in' \
    'After=network-online.target' 'Wants=network-online.target' '' \
    '[Service]' 'Type=oneshot' "User=$RUN_UID" "Group=$RUN_GID" \
    "Environment=$quoted_home" "WorkingDirectory=$working_directory" \
    "ExecStart=$quoted_runner --headless --no-telegram-poll --config $quoted_config" \
    > "$STAGING/$CHECKIN_SERVICE"
printf '%s\n' \
    '[Unit]' 'Description=Run AutoCheckin AgentRouter sign-in on schedule' '' \
    '[Timer]' "OnCalendar=$ON_CALENDAR $TIMEZONE" 'Persistent=true' "Unit=$CHECKIN_SERVICE" '' \
    '[Install]' 'WantedBy=timers.target' > "$STAGING/$CHECKIN_TIMER"
printf '%s\n' \
    '[Unit]' 'Description=AutoCheckin Telegram command listener' \
    'After=network-online.target' 'Wants=network-online.target' '' \
    '[Service]' "User=$RUN_UID" "Group=$RUN_GID" "Environment=$quoted_home" \
    "WorkingDirectory=$working_directory" \
    "ExecStart=$quoted_runner --telegram-listen --config $quoted_config" \
    'Restart=always' 'RestartSec=10' '' '[Install]' 'WantedBy=multi-user.target' \
    > "$STAGING/$TELEGRAM_SERVICE"

if ((${#LEGACY_FILES[@]} || ${#LEGACY_LOADED[@]})); then
    printf 'Detected legacy --user AutoCheckin services for %s. Stopping and removing them to upgrade to system services.\n' "$RUN_AS"
    if [ "$USER_CONNECTED" = true ] && ((${#LEGACY_LOADED[@]})); then
        for unit in "${LEGACY_LOADED[@]}"; do
            userctl stop "$unit" || fail "Could not stop legacy $unit; system services were not started."
            state=$(userctl show "$unit" --property=ActiveState --value) || fail "Cannot verify legacy $unit stopped."
            [[ "$state" = inactive || "$state" = failed ]] || fail "Legacy $unit is still $state; migration stopped."
        done
    fi
    # Remove only pre-inspected files and links; keep other user units and lingering unchanged.
    if ((${#LEGACY_FILES[@]})); then
        for path in "${LEGACY_FILES[@]}"; do rm -f -- "$path"; done
    fi
    if [ "$USER_CONNECTED" = true ]; then userctl daemon-reload; fi
    printf '%s\n' 'Legacy AutoCheckin user units removed; config, profile and Telegram state preserved.'
fi

stop_system_units
mkdir -p -- "$UNIT_DIR"
for unit in "${UNITS[@]}"; do install -m 644 "$STAGING/$unit" "$UNIT_DIR/$unit"; done
systemctl daemon-reload
systemctl enable --now "$CHECKIN_TIMER"
if [ "$TELEGRAM_ENABLED" = 1 ]; then
    systemctl enable --now "$TELEGRAM_SERVICE"
else
    printf '%s\n' 'Telegram service installed but not started: configure telegram.bot_token and telegram.chat_id first.' >&2
fi
printf 'Installed system AutoCheckin timer: %s %s (runtime account: %s).\n' "$ON_CALENDAR" "$TIMEZONE" "$RUN_AS"
printf 'Check status with: %s --show\n' "$0"
