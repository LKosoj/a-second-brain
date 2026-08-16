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

# auto-export every assignment so the child sees every key without us
# having to enumerate them in the plist. dotenv-style files use # for
# comments and `export` is optional; the `set -a` switch handles both.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

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