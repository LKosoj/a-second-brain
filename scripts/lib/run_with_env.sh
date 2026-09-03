#!/usr/bin/env bash
# Source a project .env file then run the supplied command.
#
# Used by macOS LaunchAgent plists (see deploy/*.plist.in) so the bot and
# its scheduled jobs see the same TELEGRAM_BOT_TOKEN, DEEPGRAM_API_KEY and
# other settings as the systemd units on Linux. systemd has
# EnvironmentFile= which loads .env natively; launchd does not, so we
# shell out to a wrapper instead.
#
# Usage: run_with_env.sh <project_dir> <command> [args ...]
set -euo pipefail

if [[ "$#" -lt 2 ]]; then
    echo "Usage: $0 <project_dir> <command> [args ...]" >&2
    exit 64
fi

PROJECT_DIR="$1"
shift

ENV_FILE="$PROJECT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "$0: missing $ENV_FILE" >&2
    exit 66
fi

# Parse the .env file ourselves instead of sourcing it. `source` runs every
# line as bash: an unquoted space in a value (OWNER_FULL_NAME=John Smith)
# becomes "Smith: command not found", and characters like $$ or $HOME get
# shell-expanded -- unlike python-dotenv, which is what the rest of the
# project uses to read the same file and only expands explicit ${VAR}
# references. This loop copies python-dotenv's basic semantics instead:
#   - blank lines and lines starting with '#' (leading whitespace allowed)
#     are skipped
#   - a leading "export" followed by whitespace is accepted and stripped;
#     "export=1" is a plain key named "export", as in python-dotenv
#   - a line whose key is not a valid identifier is skipped with a warning
#     on stderr instead of aborting the wrapped command
#   - key/value are split on the first '='
#   - a value wrapped in matching single or double quotes has the quotes
#     stripped; anything else is taken literally, with no expansion
# Limitations (unlike python-dotenv): no inline comments, no line
# continuation, no escape sequences (\n, \t, ...) inside double-quoted
# values, and no multi-line values.
while IFS= read -r _line || [[ -n "$_line" ]]; do
    _trimmed="${_line#"${_line%%[![:space:]]*}"}"
    [[ -z "$_trimmed" || "$_trimmed" == \#* ]] && continue
    [[ "$_trimmed" != *=* ]] && continue

    _key="${_trimmed%%=*}"
    _value="${_trimmed#*=}"
    if [[ "$_key" == export[[:space:]]* ]]; then
        _key="${_key#export}"
    fi
    _key="${_key#"${_key%%[![:space:]]*}"}"
    _key="${_key%"${_key##*[![:space:]]}"}"
    if [[ ! "$_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "run_with_env: skipping malformed .env line: ${_trimmed%%=*}=..." >&2
        continue
    fi

    _value="${_value#"${_value%%[![:space:]]*}"}"
    _value="${_value%"${_value##*[![:space:]]}"}"
    if [[ ${#_value} -ge 2 ]]; then
        if [[ "$_value" == \"*\" && "$_value" == *\" ]]; then
            _value="${_value:1:${#_value}-2}"
        elif [[ "$_value" == \'*\' && "$_value" == *\' ]]; then
            _value="${_value:1:${#_value}-2}"
        fi
    fi
    export "$_key=$_value"
done <"$ENV_FILE"
unset _line _trimmed _key _value

# launchd runs jobs with a minimal PATH (only /usr/bin:/bin and a few
# Apple-internal locations). That is fine for ``uv`` because the plist
# calls it by absolute path, but the AI CLI backends the bot spawns at
# runtime (``opencode``, ``claude``, ``codex``, ``gwen`` ...) are
# looked up by name in CliRunner, so they need a PATH that includes the
# user's local bin directories. Prepend Homebrew locations (Apple
# Silicon default); honour EXTRA_PATH from .env for anything extra,
# typically ``EXTRA_PATH=$HOME/.local/bin:$HOME/.opencode/bin``.
_extra_path=(
    "/opt/homebrew/bin"
    "/opt/homebrew/sbin"
)
if [[ -n "${EXTRA_PATH:-}" ]]; then
    # shellcheck disable=SC2206
    _extra_path+=(${EXTRA_PATH//:/ })
fi
for _dir in "${_extra_path[@]}"; do
    if [[ -d "$_dir" && ":$PATH:" != *":$_dir:"* ]]; then
        PATH="$_dir:$PATH"
    fi
done
export PATH
unset _extra_path _dir

cd "$PROJECT_DIR"
exec "$@"