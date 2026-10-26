#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ENABLE=0

if [[ "${1:-}" == "--enable" ]]; then
  ENABLE=1
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--enable]" >&2
  exit 2
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is required" >&2
  exit 1
fi

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  echo "uv is required" >&2
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/.env" || ! -d "$PROJECT_DIR/vault" ]]; then
  echo "Run ./install.sh before installing systemd units." >&2
  exit 1
fi

escape_sed() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//&/\\&}"
  value="${value//|/\\|}"
  printf '%s' "$value"
}

# Non-empty check for PLAUD_BEARER_TOKEN: a bare grep for
# '^PLAUD_BEARER_TOKEN=.+' also matches a quoted-empty value
# (PLAUD_BEARER_TOKEN="") or one that is only whitespace, which would wire
# up the PLAUD sync timer with nothing to authenticate with.
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
# the install: it ends up inside a unit, and "Europe/Moscw" would never fire.
# The character check keeps the value safe for the sed substitution below.
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

process_time="$(schedule_time "$PROJECT_DIR/.env" DAILY_PROCESS_TIME 21:00)"
brief_time="$(schedule_time "$PROJECT_DIR/.env" MORNING_BRIEF_TIME 08:00)"
owner_tz="$(owner_timezone "$PROJECT_DIR/.env")"
# systemd takes a zone at the end of OnCalendar, so "21:00" means the owner's
# 21:00 whatever the system zone is. Empty keeps the system zone, as before.
timezone_suffix=""
[[ -n "$owner_tz" ]] && timezone_suffix=" $owner_tz"
# The same zone goes to the services as TZ, so skill scripts, ops_log and
# technical timestamps count time like the Python code does. It is appended to
# Environment= (no extra line when empty). EnvironmentFile= overrides
# Environment= in systemd, so a TZ written in .env explicitly still wins.
tz_environment=""
[[ -n "$owner_tz" ]] && tz_environment=" TZ=$owner_tz"

project_value="$(escape_sed "$PROJECT_DIR")"
uv_value="$(escape_sed "$UV_BIN")"
mkdir -p "$UNIT_DIR"

# Only *.service.in and *.timer.in are systemd unit templates; *.plist.in
# under deploy/ is the macOS launchd counterpart and must not be rendered
# into a systemd user-unit directory.
for template in "$PROJECT_DIR"/deploy/*.service.in "$PROJECT_DIR"/deploy/*.timer.in; do
  [[ -f "$template" ]] || continue
  destination="$UNIT_DIR/$(basename "${template%.in}")"
  sed \
    -e "s|@PROJECT_DIR@|$project_value|g" \
    -e "s|@UV_BIN@|$uv_value|g" \
    -e "s|@DAILY_PROCESS_TIME@|$process_time|g" \
    -e "s|@MORNING_BRIEF_TIME@|$brief_time|g" \
    -e "s|@TIMEZONE_SUFFIX@|$timezone_suffix|g" \
    -e "s|@TZ_ENVIRONMENT@|$tz_environment|g" \
    "$template" >"$destination"
done

systemctl --user daemon-reload

if [[ "$ENABLE" -eq 1 ]]; then
  "$UV_BIN" run --directory "$PROJECT_DIR" --frozen --no-dev \
    a-second-brain doctor "$PROJECT_DIR"
  units=(
    a-second-brain.service
    a-second-brain-process.timer
    a-second-brain-morning-brief.timer
    a-second-brain-backup-verify.timer
  )
  if has_plaud_token "$PROJECT_DIR/.env"; then
    units+=(a-second-brain-plaud-sync.timer)
  fi
  if command -v qmd >/dev/null 2>&1; then
    units+=(a-second-brain-qmd-maintenance.timer)
  fi
  systemctl --user enable --now "${units[@]}"
fi

echo "User units installed in $UNIT_DIR"
if [[ "$ENABLE" -eq 0 ]]; then
  echo "Review them, then rerun with --enable."
fi
