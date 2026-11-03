#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

has_network_api_token() {
  local line value
  line="$(grep -E '^(export[[:space:]]+)?NETWORK_API_TOKEN=' "$PROJECT_DIR/.env" | tail -n1)" || true
  [[ -z "$line" ]] && return 1
  value="${line#*=}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ ${#value} -ge 2 ]]; then
    if [[ "$value" == \"* && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'* && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  [[ -n "$value" ]]
}

systemctl --user restart a-second-brain.service

if has_network_api_token; then
  systemctl --user restart a-second-brain-network.service
else
  echo "Skipping a-second-brain-network.service: NETWORK_API_TOKEN is empty"
fi
