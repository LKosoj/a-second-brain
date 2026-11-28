#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=scripts/lib/env_reader.sh
source "$PROJECT_DIR/scripts/lib/env_reader.sh"

systemctl --user restart a-second-brain.service

if has_network_api_token "$PROJECT_DIR/.env"; then
  systemctl --user restart a-second-brain-network.service
else
  echo "Skipping a-second-brain-network.service: NETWORK_API_TOKEN is empty"
fi
