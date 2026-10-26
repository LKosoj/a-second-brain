"""CLI entry point for the read-only vault context pack."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import dotenv_values

from d_brain.clock import owner_today, set_owner_timezone
from d_brain.manifest import ManifestValidationError, load_manifest
from d_brain.services.context_pack import ContextPackBuilder


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the d-brain context pack")
    parser.add_argument("--hook-json", action="store_true")
    parser.add_argument("--date", dest="target_day")
    return parser.parse_args()


def main() -> int:
    """Print a plain pack or the SessionStart hook response."""
    args = _arguments()
    try:
        project_root = Path.cwd()
        # The SessionStart hook runs without the service EnvironmentFile and
        # never loads Settings, so it reads TIMEZONE from .env itself to land
        # on the same day as the bot.
        timezone_name = os.environ.get("TIMEZONE") or dotenv_values(
            project_root / ".env"
        ).get("TIMEZONE")
        if timezone_name:
            try:
                set_owner_timezone(timezone_name)
            except ValueError:
                # Same as clock.owner_tz: a bad name means the system zone,
                # the hook must still hand over the context.
                pass
        target_day = (
            date.fromisoformat(args.target_day) if args.target_day else owner_today()
        )
        manifest = load_manifest(project_root)
        vault_path = (project_root / manifest.memory_root).resolve()
        pack = ContextPackBuilder(vault_path, manifest).build(target_day)
    except ManifestValidationError as exc:
        message = str(exc)
        if message == "vault-manifest.json: is missing":
            message = "vault-manifest.json is missing"
        print(f"context-pack: {message}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"context-pack: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"context-pack: {exc}", file=sys.stderr)
        return 1

    if args.hook_json:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": pack.text,
                    },
                    "additional_context": pack.text,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(pack.text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
