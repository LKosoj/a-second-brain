#!/usr/bin/env bash
set -euo pipefail

if [[ "${D_BRAIN_CONTEXT_PACK_MODE:-}" == "runtime" ]]; then
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"
exec uv run python -m d_brain.run_context_pack --hook-json
