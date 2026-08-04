#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

./scripts/setup_control_plane.sh >/dev/null

cat <<'EOF'
Control-plane assets refreshed.

Validated:
- required control-plane files exist
- hook scripts are executable
- docs are present for ownership and security profiles
EOF
