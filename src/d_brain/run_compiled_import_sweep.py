"""CLI entrypoint for the T4 "Импорты доводятся до конца" import sweep.

``sweep_unmarked_imports`` (``compiled_briefings.py``) is the code
``run_nightly_maintenance`` already calls, bounded to
``NIGHTLY_IMPORT_SWEEP_LIMIT`` new postings a night so an old backlog of
unmarked import notes cannot itself blow the nightly model-call budget.
This CLI is the owner-run, one-off counterpart for the opposite case: a
first pass over every pre-existing import note from before this feature
shipped, with no posting limit and no nightly model-call budget (it drains
the queue directly through ``drain_queue``, never through
``run_nightly_maintenance``, so ``CompiledBriefingService._active_pass``
stays ``None`` and the per-pass budgets in ``_run_model`` are simply never
checked -- exactly the same "unbudgeted manual refresh" path a single
``enqueue_refresh`` call already gets today).

Two modes:

- No flags: ``sweep_unmarked_imports(limit=NIGHTLY_IMPORT_SWEEP_LIMIT)``,
  then drain whatever that sweep (and anything else already queued) just
  posted -- a bounded, repeatable "catch up a little more" run.
- ``--no-limit``: ``sweep_unmarked_imports(limit=None)`` -- every
  unmarked, stale-enough import note gets posted, not just the first 20 --
  then the same drain loop.

The drain loop calls ``CompiledBriefingService.drain_queue`` directly,
repeatedly, until one iteration processes zero events -- ``force=True`` so
a fresh event's debounce wait never stalls the loop, which makes "zero
events processed" mean either the queue is genuinely empty or every
remaining event is stuck ``in_flight`` under another worker, not "not due
yet". ``drain_queue`` takes its own non-blocking worker lock per call
(see that method); if a background worker (``spawn_background_drain``) or
another instance of this CLI already holds it, the very first iteration
comes back with ``errors: ["worker-busy"]`` and the loop stops immediately
instead of silently competing with it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from d_brain.config import get_settings
from d_brain.services.compiled_briefings import (
    DEFAULT_QUEUE_BATCH_SIZE,
    NIGHTLY_IMPORT_SWEEP_LIMIT,
    CompiledBriefingService,
    CompiledSourceStateError,
)
from d_brain.services.frontmatter import ensure_run_identity

logger = logging.getLogger(__name__)


def _drain_to_completion(service: CompiledBriefingService) -> dict[str, Any]:
    """Drain the refresh queue in a loop until it is empty, another worker
    holds it, or one iteration makes no progress (the last a safety net,
    not an expected outcome with ``force=True``)."""
    drained = 0
    updated: list[str] = []
    consolidations: list[str] = []
    errors: list[str] = []
    queue_busy = False
    while True:
        result = service.drain_queue(force=True, max_events=DEFAULT_QUEUE_BATCH_SIZE)
        if result.get("errors") == ["worker-busy"]:
            queue_busy = True
            errors.extend(result["errors"])
            break
        drained_this_round = int(result.get("drained") or 0)
        drained += drained_this_round
        updated.extend(result.get("updated") or [])
        consolidations.extend(result.get("consolidations") or [])
        errors.extend(result.get("errors") or [])
        if drained_this_round <= 0:
            break
    return {
        "drained": drained,
        "updated": list(dict.fromkeys(updated)),
        "consolidations": consolidations,
        "errors": errors,
        "queue_busy": queue_busy,
    }


def _run_sweep(service: CompiledBriefingService, *, no_limit: bool) -> dict[str, Any]:
    limit = None if no_limit else NIGHTLY_IMPORT_SWEEP_LIMIT
    sweep = service.sweep_unmarked_imports(limit=limit)
    drain = _drain_to_completion(service)

    compiled_index_written = False
    try:
        from d_brain.services.compiled_index import refresh_compiled_index

        compiled_index_written = refresh_compiled_index(service.vault_path)
    except Exception as index_exc:  # noqa: BLE001 - best-effort, T3
        logger.warning("Compiled index catalog refresh failed: %s", index_exc)

    return {
        "sweep": sweep,
        "drain": drain,
        "compiled_index_written": compiled_index_written,
        "errors": list(drain["errors"]),
    }


def main() -> int:
    ensure_run_identity("compiled-import-sweep", "compiled-import-sweep")
    parser = argparse.ArgumentParser(
        description=(
            "Post-and-drain unmarked import notes (T4 compile_state catch-up)"
        )
    )
    parser.add_argument(
        "--no-limit",
        action="store_true",
        help="Post every unmarked, stale-enough import note, not just the first 20",
    )
    args = parser.parse_args()

    settings = get_settings()
    vault_path = settings.vault_path
    service = CompiledBriefingService(
        vault_path,
        content_language=settings.content_language,
        ai_cli=settings.ai_cli,
    )

    try:
        result = _run_sweep(service, no_limit=args.no_limit)
    except CompiledSourceStateError as exc:
        # Same handling as the rest of this CLI family (run_compiled_pass.py,
        # run_compiled_maintenance.py): a corrupt ``.compiled/source-state.json``
        # is a real failure, but this family promises a JSON ``{"errors": [...]}``
        # contract on stdout rather than a raw traceback.
        result = {
            "errors": [f"повреждено состояние источников компиляции: {exc}"],
        }

    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
