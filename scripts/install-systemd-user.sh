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

project_value="$(escape_sed "$PROJECT_DIR")"
uv_value="$(escape_sed "$UV_BIN")"
mkdir -p "$UNIT_DIR"

for template in "$PROJECT_DIR"/deploy/*.in; do
  destination="$UNIT_DIR/$(basename "${template%.in}")"
  sed \
    -e "s|@PROJECT_DIR@|$project_value|g" \
    -e "s|@UV_BIN@|$uv_value|g" \
    "$template" >"$destination"
done

systemctl --user daemon-reload

if [[ "$ENABLE" -eq 1 ]]; then
  "$UV_BIN" run --directory "$PROJECT_DIR" --frozen --no-dev \
    a-second-brain doctor "$PROJECT_DIR"
  units=(
    a-second-brain.service
    a-second-brain-process.timer
  )
  if grep -Eq '^PLAUD_BEARER_TOKEN=.+$' "$PROJECT_DIR/.env"; then
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
