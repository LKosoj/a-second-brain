#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$EUID" -eq 0 ]]; then
  echo "Do not install as root. Use a dedicated unprivileged account." >&2
  exit 1
fi

for command in uv jq; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required" >&2
    exit 1
  fi
done

cd "$PROJECT_DIR"
uv sync --frozen --no-dev

if [[ ! -d vault ]]; then
  uv run --frozen --no-dev a-second-brain init "$PROJECT_DIR"
else
  echo "Keeping existing private vault at $PROJECT_DIR/vault"
fi

chmod 600 "$PROJECT_DIR/.env"

echo
echo "Installation prepared."
echo "1. Edit $PROJECT_DIR/.env"
echo "2. Run: cd $PROJECT_DIR && uv run --frozen --no-dev a-second-brain doctor"
echo "3. Start with: cd $PROJECT_DIR && uv run --frozen --no-dev a-second-brain run"
echo "4. Optional systemd: $PROJECT_DIR/scripts/install-systemd-user.sh"
