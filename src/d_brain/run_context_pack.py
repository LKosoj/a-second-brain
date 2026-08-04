"""CLI entry point for the read-only vault context pack."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

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
        target_day = (
            date.fromisoformat(args.target_day) if args.target_day else date.today()
        )
        project_root = Path.cwd()
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
