#!/usr/bin/env bash
# macOS counterpart to scripts/install-systemd-user.sh.
#
# systemd is unavailable on macOS, so this script renders the same
# schedules as LaunchAgent plists into ~/Library/LaunchAgents/ and loads
# them with launchctl. Plists use run_with_env.sh to pick up .env the
# same way the systemd EnvironmentFile directive does.
#
# Usage:
#   bash scripts/install-launchd-user.sh            # render only
#   bash scripts/install-launchd-user.sh --enable   # render + launchctl load
#   bash scripts/install-launchd-user.sh --uninstall
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/com.second-brain"

ENABLE=0
UNINSTALL=0

case "${1:-}" in
    --enable) ENABLE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    "") ;;
    *) echo "Usage: $0 [--enable|--uninstall]" >&2; exit 64 ;;
esac

if ! command -v launchctl >/dev/null 2>&1; then
    echo "launchctl is required (this script targets macOS)" >&2
    exit 1
fi

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
    echo "uv is required (install via 'brew install uv' or https://astral.sh/uv)" >&2
    exit 1
fi

WRAPPER="$PROJECT_DIR/scripts/lib/run_with_env.sh"
if [[ ! -x "$WRAPPER" ]]; then
    echo "Wrapper $WRAPPER is missing or not executable" >&2
    exit 1
fi

# The complete set of labels this script can ever install, independent of
# the *current* .env/qmd state. Uninstall must sweep all of them: a plist
# rendered during a previous run (e.g. PLAUD_BEARER_TOKEN was set then, or
# qmd was on PATH then) would otherwise be left orphaned in $AGENT_DIR if
# that condition no longer holds at uninstall time.
ALL_LABELS=(
    "com.second-brain.bot"
    "com.second-brain.process"
    "com.second-brain.morning-brief"
    "com.second-brain.plaud-sync"
    "com.second-brain.qmd-maintenance"
    "com.second-brain.backup-verify"
)

uninstall_all() {
    for label in "${ALL_LABELS[@]}"; do
        if launchctl list | grep -q "$label"; then
            launchctl unload "$AGENT_DIR/$label.plist" 2>/dev/null || true
        fi
        rm -f "$AGENT_DIR/$label.plist"
    done
    echo "Uninstalled LaunchAgents for ${ALL_LABELS[*]}"
}

if [[ "$UNINSTALL" -eq 1 ]]; then
    mkdir -p "$AGENT_DIR"
    uninstall_all
    exit 0
fi

# Non-empty check for PLAUD_BEARER_TOKEN: a bare grep for
# '^PLAUD_BEARER_TOKEN=.+' also matches a quoted-empty value
# (PLAUD_BEARER_TOKEN="") or one that is only whitespace, which would wire
# up the PLAUD sync agent with nothing to authenticate with.
has_plaud_token() {
    local env_file="$1" line value
    line="$(grep -E '^(export[[:space:]]+)?PLAUD_BEARER_TOKEN=' "$env_file" | tail -n1)" || true
    [[ -z "$line" ]] && return 1
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ ${#value} -ge 2 ]]; then
        if [[ "$value" == \"*\" && "$value" == *\" ]]; then
            value="${value:1:${#value}-2}"
        elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
            value="${value:1:${#value}-2}"
        fi
    fi
    [[ -n "$value" ]]
}

LABELS=(
    "com.second-brain.bot"
    "com.second-brain.process"
    "com.second-brain.morning-brief"
    "com.second-brain.backup-verify"
)
PLIST_BASES=(
    "com.second-brain.bot"
    "com.second-brain.process"
    "com.second-brain.morning-brief"
    "com.second-brain.backup-verify"
)

if [[ -f "$PROJECT_DIR/.env" ]] && has_plaud_token "$PROJECT_DIR/.env"; then
    LABELS+=("com.second-brain.plaud-sync")
    PLIST_BASES+=("com.second-brain.plaud-sync")
fi

if command -v qmd >/dev/null 2>&1; then
    LABELS+=("com.second-brain.qmd-maintenance")
    PLIST_BASES+=("com.second-brain.qmd-maintenance")
fi

escape_sed() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//&/\\&}"
    value="${value//|/\\|}"
    printf '%s' "$value"
}

project_value="$(escape_sed "$PROJECT_DIR")"
uv_value="$(escape_sed "$UV_BIN")"
wrapper_value="$(escape_sed "$WRAPPER")"
log_value="$(escape_sed "$LOG_DIR")"

if [[ ! -f "$PROJECT_DIR/.env" || ! -d "$PROJECT_DIR/vault" ]]; then
    echo "Run ./install.sh before installing LaunchAgents." >&2
    exit 1
fi

# Last KEY=value from the .env file (empty when absent), without surrounding
# whitespace or one pair of quotes. Read from the file, not the shell
# environment: the units load .env themselves.
env_value() {
    local env_file="$1" key="$2" line value
    line="$(grep -E "^(export[[:space:]]+)?${key}[[:space:]]*=" "$env_file" | tail -n1)" || true
    [[ -z "$line" ]] && return 0
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ ${#value} -ge 2 ]]; then
        if [[ "$value" == \"*\" && "$value" == *\" ]]; then
            value="${value#?}"
            value="${value%?}"
        elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
            value="${value#?}"
            value="${value%?}"
        fi
    fi
    printf '%s' "$value"
}

# HH:MM from .env, DEFAULT when unset; anything else stops the install, since
# a timer rendered from a typo would silently never fire at the intended time.
schedule_time() {
    local env_file="$1" key="$2" default="$3" value
    value="$(env_value "$env_file" "$key")"
    [[ -z "$value" ]] && value="$default"
    if [[ ! "$value" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])$ ]]; then
        echo "$key must be HH:MM (00:00-23:59), got '$value'" >&2
        exit 1
    fi
    printf '%s' "$value"
}

# IANA zone of the owner's day from .env (empty when unset). A bad name stops
# the install: the schedule below is converted from it, and "Europe/Moscw"
# would silently shift every run.
owner_timezone() {
    local value
    value="$(env_value "$1" TIMEZONE)"
    [[ -z "$value" ]] && return 0
    if [[ ! "$value" =~ ^[A-Za-z0-9_+/-]+$ ]] ||
        ! python3 -c 'import sys, zoneinfo; zoneinfo.ZoneInfo(sys.argv[1])' "$value" 2>/dev/null; then
        echo "TIMEZONE must be an IANA name such as Europe/Moscow, got '$value'" >&2
        exit 1
    fi
    printf '%s' "$value"
}

# launchd cannot take a zone: StartCalendarInterval follows the system clock.
# Print "HOUR MINUTE" of the owner's HH:MM ($2, $3 in zone $1) on the system
# clock as of today. Daylight saving later moves the owner's time against the
# system's, so the installer has to be rerun after a clock change.
to_system_clock() {
    python3 - "$1" "$2" "$3" <<'PY'
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

zone, hour, minute = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
at = datetime.now(ZoneInfo(zone)).replace(
    hour=hour, minute=minute, second=0, microsecond=0
)
local = at.astimezone()
print(local.hour, local.minute)
PY
}

# 10# so "08" is not read as invalid octal and plist gets "8", not "08".
process_time="$(schedule_time "$PROJECT_DIR/.env" DAILY_PROCESS_TIME 21:00)"
brief_time="$(schedule_time "$PROJECT_DIR/.env" MORNING_BRIEF_TIME 08:00)"
process_hour=$((10#${process_time%%:*}))
process_minute=$((10#${process_time##*:}))
brief_hour=$((10#${brief_time%%:*}))
brief_minute=$((10#${brief_time##*:}))

owner_tz="$(owner_timezone "$PROJECT_DIR/.env")"
if [[ -n "$owner_tz" ]]; then
    owner_clock="$process_hour:$process_minute $brief_hour:$brief_minute"
    converted="$(to_system_clock "$owner_tz" "$process_hour" "$process_minute")"
    read -r process_hour process_minute <<<"$converted"
    converted="$(to_system_clock "$owner_tz" "$brief_hour" "$brief_minute")"
    read -r brief_hour brief_minute <<<"$converted"
    if [[ "$owner_clock" != "$process_hour:$process_minute $brief_hour:$brief_minute" ]]; then
        echo "TIMEZONE=$owner_tz differs from the system zone: launchd runs by the" \
            "system clock, so the schedule was converted to" \
            "$process_hour:$(printf '%02d' "$process_minute") (processing) and" \
            "$brief_hour:$(printf '%02d' "$brief_minute") (brief) system time." \
            "Rerun this installer after a daylight-saving change." >&2
    fi
fi

# TZ for the jobs themselves, so skill scripts, ops_log and technical
# timestamps count time in the owner's zone. It does not touch the conversion
# above: that runs in this installer's process, and launchd's calendar follows
# the system clock whatever TZ the job has. run_with_env.sh loads .env after
# launchd sets the environment, so a TZ written in .env explicitly still wins.
tz_entry=""
if [[ -n "$owner_tz" ]]; then
    tz_entry="<key>TZ</key><string>$owner_tz</string>"
fi

mkdir -p "$AGENT_DIR" "$LOG_DIR"

render_plist() {
    local base="$1"
    local template="$PROJECT_DIR/deploy/$base.plist.in"
    local destination="$AGENT_DIR/$base.plist"
    if [[ ! -f "$template" ]]; then
        echo "Missing template: $template" >&2
        exit 1
    fi
    sed \
        -e "s|@PROJECT_DIR@|$project_value|g" \
        -e "s|@UV_BIN@|$uv_value|g" \
        -e "s|@WRAPPER@|$wrapper_value|g" \
        -e "s|@LOG_DIR@|$log_value|g" \
        -e "s|@DAILY_PROCESS_HOUR@|$process_hour|g" \
        -e "s|@DAILY_PROCESS_MINUTE@|$process_minute|g" \
        -e "s|@MORNING_BRIEF_HOUR@|$brief_hour|g" \
        -e "s|@MORNING_BRIEF_MINUTE@|$brief_minute|g" \
        -e "s|@TZ_PLIST_ENTRY@|$tz_entry|g" \
        "$template" >"$destination"
    echo "Rendered: $destination"
}

for base in "${PLIST_BASES[@]}"; do
    render_plist "$base"
done

if [[ "$ENABLE" -eq 1 ]]; then
    "$UV_BIN" run --directory "$PROJECT_DIR" --frozen --no-dev \
        a-second-brain doctor "$PROJECT_DIR"
    for label in "${LABELS[@]}"; do
        launchctl unload "$AGENT_DIR/$label.plist" 2>/dev/null || true
        launchctl load -w "$AGENT_DIR/$label.plist"
    done
    echo
    echo "LaunchAgents loaded: ${LABELS[*]}"
    echo "Logs: $LOG_DIR"
    echo "Inspect: launchctl list | grep second-brain"
else
    echo
    echo "Plists rendered in $AGENT_DIR but not loaded."
    echo "Review them, then rerun with --enable."
fi