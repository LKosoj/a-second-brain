#!/usr/bin/env bash
# Validate user-owned vault Markdown through the shared frontmatter contract.

INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)

[[ -z "$FILE" ]] && exit 0

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR" || exit 2

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  CLI_BIN="$(command -v a-second-brain || true)"
  if [[ -n "$CLI_BIN" && -x "$(dirname "$CLI_BIN")/python" ]]; then
    PYTHON_BIN="$(dirname "$CLI_BIN")/python"
  else
    exec uv run --frozen --no-dev python -m d_brain.run_frontmatter_hook --file "$FILE"
  fi
fi

exec "$PYTHON_BIN" -m d_brain.run_frontmatter_hook --file "$FILE"
