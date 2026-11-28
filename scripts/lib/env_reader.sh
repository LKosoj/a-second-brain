#!/usr/bin/env bash
# Shared .env reader helpers for installer and service management scripts.
# Sourced by install-systemd-user.sh, install-launchd-user.sh, and restart-service.sh.
# Keeps Bash 3.2 compatibility (no negative substring lengths).

# Last KEY=value from the .env file (empty when absent), without surrounding
# whitespace or one pair of quotes. Read from the file, not the shell
# environment: the units load .env themselves.
env_value() {
  local env_file="$1" key="$2" line value
  [[ -f "$env_file" ]] || return 0
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

# Ignore empty or quoted-empty PLAUD settings.
has_plaud_setting() {
  local env_file="$1" setting="$2" value
  value="$(env_value "$env_file" "$setting")"
  [[ -n "$value" ]]
}

# Ignore empty or quoted-empty NETWORK_API_TOKEN.
has_network_api_token() {
  local env_file="$1" value
  value="$(env_value "$env_file" NETWORK_API_TOKEN)"
  [[ -n "$value" ]]
}
