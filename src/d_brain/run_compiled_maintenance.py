"""CLI entrypoint for compiled briefing queue drain and nightly maintenance."""

from __future__ import annotations

import argparse
import json
import sys

from d_brain.config import get_settings
from d_brain.services.compiled_briefings import (
    CompiledBriefingService,
    CompiledSourceStateError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run compiled briefing maintenance")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--queue-only",
        action="store_true",
        help="Drain queued compiled refresh events only (default)",
    )
    mode_group.add_argument(
        "--nightly",
        action="store_true",
        help="Run queue drain plus lint/source-aware backfill",
    )
    mode_group.add_argument(
        "--initialize-source-state",
        action="store_true",
        help="Create a source-aware freshness baseline without rewriting notes",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force drain even when debounce windows are still active",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=8,
        help="Maximum queued events to process in one run",
    )
    args = parser.parse_args()

    settings = get_settings()
    service = CompiledBriefingService(
        settings.vault_path,
        content_language=settings.content_language,
        ai_cli=settings.ai_cli,
    )
    try:
        if args.initialize_source_state:
            result = service.initialize_source_state()
            result["lint_issues"] = service.lint_notes()
            result["freshness_issues"] = service.freshness_issues()
            if result["lint_issues"] or result["freshness_issues"]:
                result["errors"] = ["source-state-validation-failed"]
        elif args.nightly:
            result = service.run_nightly_maintenance()
        else:
            result = service.run_queue_worker(
                force=args.force,
                max_events=args.max_events,
            )
    except CompiledSourceStateError as exc:
        # Code review Finding 2: this is a real failure (unlike the
        # queue-only drain, which now degrades instead of raising -- see
        # ``CompiledBriefingService._source_tier_ranks``), so it must still
        # end the run non-zero. It just deserves a message the owner can
        # read instead of a raw traceback -- ``run_nightly_maintenance``
        # already recorded this as a failed pass in its journal before
        # re-raising, so nothing about that strictness changes here.
        result = {
            "errors": [f"повреждено состояние источников компиляции: {exc}"],
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
