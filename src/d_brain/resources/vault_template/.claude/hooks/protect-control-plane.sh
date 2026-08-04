#!/usr/bin/env bash
# Prevent runtime edits to repo-managed control-plane assets.

VALIDATION_ERROR="BLOCKED: unable to validate control-plane target."

deny() {
  echo "$VALIDATION_ERROR" >&2
  exit 2
}

command -v jq >/dev/null 2>&1 || deny

INPUT=$(cat) || deny
[[ -n "$INPUT" ]] || deny
EXTRACTED=$(printf '%s' "$INPUT" | jq -er '
  if type != "object" or (.tool_input | type) != "object" then error("tool_input")
  elif (.tool_input.file_path? | type) == "string" then
    if (.tool_input.file_path | (test("[\r\n\t]") or contains("\u0000"))) then error("unsafe path")
    else "file_path\t" + .tool_input.file_path end
  elif (.tool_input.command? | type) == "string" then
    if (.tool_input.command | contains("\u0000")) then error("unsafe command")
    else "command\t" + .tool_input.command end
  else error("target") end
' 2>/dev/null) || deny

KIND=${EXTRACTED%%$'\t'*}
TARGET=${EXTRACTED#*$'\t'}
[[ "$KIND" != "$EXTRACTED" && -n "$TARGET" ]] || deny

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR" || deny

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  CLI_BIN="$(command -v a-second-brain || true)"
  if [[ -n "$CLI_BIN" && -x "$(dirname "$CLI_BIN")/python" ]]; then
    PYTHON_BIN="$(dirname "$CLI_BIN")/python"
  else
    command -v uv >/dev/null 2>&1 || deny
    exec uv run --frozen --no-dev python -m d_brain.run_control_plane_protect_hook \
      --kind "$KIND" --target "$TARGET"
  fi
fi

exec "$PYTHON_BIN" -m d_brain.run_control_plane_protect_hook \
  --kind "$KIND" --target "$TARGET"
