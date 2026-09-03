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
)
PLIST_BASES=(
    "com.second-brain.bot"
    "com.second-brain.process"
    "com.second-brain.morning-brief"
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