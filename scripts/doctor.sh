#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec uv run --frozen --no-dev a-second-brain doctor "$PROJECT_DIR" "$@"
