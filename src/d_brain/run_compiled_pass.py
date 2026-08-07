"""CLI entrypoint for a manual, one-off compile-enrich pass.

Named ``run_compiled_pass.py`` to follow this project's ``run_compiled_*.py``
convention for standalone compiled-layer CLIs (the six existing ones:
``run_compiled_maintenance.py``, ``run_compiled_daily_full_pass.py``,
``run_compiled_reprocess_days.py``, ``run_compiled_digest.py``,
``run_compiled_brief.py``, ``run_compiled_monthly_verify.py``) rather than a
more literal name -- an intentional naming choice, not a spec requirement.

Three modes:

- Regular pass (``--date``/``--source``, repeatable and combinable): enqueue
  each resolved source via ``CompiledBriefingService.enqueue_refresh`` and
  then run one ``run_nightly_maintenance()`` pass -- the exact same two
  calls ``processor.py``'s ``_run_compiled_nightly_maintenance`` makes for
  the scheduled ``maintenance.compiled-nightly`` workflow, so a manual pass
  behaves identically to a nightly one.
- ``--rollback PASS_ID``: a direct call to the already-implemented
  ``CompiledBriefingService.rollback_compile_enrich_pass``. A ``pass_id``
  with no snapshot manifest on disk at all (made-up id, typo, or a snapshot
  already cleaned up by retention) is a CLI error -- non-zero exit, an
  ``errors`` entry in the JSON output, no traceback -- distinct from a
  known pass that genuinely has nothing to restore, which stays exit 0.
- ``--dry-run`` (combined with ``--date``/``--source``): a **read-only
  preview only**, not a real no-write pass. ``compiled_briefings.py`` has no
  no-write mode for the actual compile step -- resolving which compiled
  pages a source affects (``_resolve_targets``) is itself a model call, and
  "run then roll back" is not a safe substitute: rollback only restores/
  removes page files, it does not undo side effects outside files (sent
  Telegram messages, the monthly enrichment-budget counters, or the queue/
  pass-journal history a real run would append). So ``--dry-run`` honestly
  limits itself to what is genuinely safe: which sources would be enqueued
  (after the same path normalization ``enqueue_refresh`` applies) and what
  is currently sitting in the refresh queue. It does not, and cannot safely,
  show which compiled pages the pass would touch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from d_brain.config import get_settings
from d_brain.services.compiled_briefings import (
    CompiledBriefingService,
    CompiledSourceStateError,
)
from d_brain.services.compiled_enrich_report import PASS_JOURNAL_RELATIVE_PATH


def _resolve_sources(dates: list[str], sources: list[str]) -> list[str]:
    """Turn ``--date``/``--source`` arguments into a de-duplicated,
    order-preserving list of vault-relative source paths."""
    resolved = [f"daily/{day}.md" for day in dates]
    resolved.extend(sources)
    seen: set[str] = set()
    unique: list[str] = []
    for item in resolved:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _read_source_excerpt(vault_path: Path, rel_path: str) -> str:
    try:
        # errors="replace", like CompiledBriefingService._source_excerpt:
        # one bad byte in a daily file used to raise UnicodeDecodeError,
        # which is not an OSError and so escaped the guard below and took
        # the whole pass down.
        return (vault_path / rel_path).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""


def _run_dry_run(
    service: CompiledBriefingService, vault_path: Path, rel_paths: list[str]
) -> dict[str, Any]:
    # ``_normalize_rel_path``/``_load_queue`` are private -- there is no
    # public read-only equivalent on ``CompiledBriefingService`` today, and
    # this CLI is not allowed to add one (out of scope for this task). Both
    # are pure/read-only (string normalization and a queue-file read), so
    # reusing them here previews the same thing ``enqueue_refresh`` would
    # actually do, instead of re-implementing that logic and risking drift.
    would_enqueue = []
    for rel_path in rel_paths:
        normalized = service._normalize_rel_path(rel_path)  # noqa: SLF001
        would_enqueue.append(
            {
                "source": rel_path,
                "normalized": normalized,
                "would_enqueue": bool(normalized)
                and not normalized.startswith("compiled/"),
            }
        )
    queue = service._load_queue()  # noqa: SLF001
    current_queue = [
        {
            "source_path": entry.get("source_path"),
            "state": entry.get("state"),
            "due_at": entry.get("due_at"),
            "attempts": entry.get("attempts"),
        }
        for entry in queue
    ]
    return {
        "dry_run": True,
        "would_enqueue": would_enqueue,
        "current_queue": current_queue,
        "note": (
            "Preview only: sources that would be queued and the queue's "
            "current contents. Does not run compilation and cannot show "
            "which compiled pages the pass would touch, since determining "
            "that (_resolve_targets) is itself a model call, not a "
            "read-only check -- see this module's docstring."
        ),
    }


def _run_rollback(service: CompiledBriefingService, pass_id: str) -> dict[str, Any]:
    result = service.rollback_compile_enrich_pass(pass_id)
    # ``manifest_found`` tells apart "nothing to roll back because this
    # pass_id has no snapshot at all" (made-up id, typo, already cleaned up
    # by retention) from "nothing to roll back because this pass genuinely
    # changed zero pages" -- both otherwise return the same empty
    # restored/skipped lists. Only the first is an error: surface it here as
    # a plain ``errors`` entry (picked up by ``main``'s existing exit-code
    # check below) instead of letting it look like a quiet success.
    if result.get("manifest_found") is False:
        result["errors"] = [
            f"no snapshot found for pass id {pass_id!r}: nothing to roll back "
            "(unknown id, a typo, or its snapshot was already cleaned up)"
        ]
    return {"pass_id": pass_id, **result}


def _run_pass(
    service: CompiledBriefingService, vault_path: Path, rel_paths: list[str]
) -> dict[str, Any]:
    enqueue_errors: list[str] = []
    for rel_path in rel_paths:
        excerpt = _read_source_excerpt(vault_path, rel_path)
        enqueue_result = service.enqueue_refresh(
            source_path=rel_path, source_excerpt=excerpt
        )
        enqueue_errors.extend(enqueue_result.get("errors", []))

    result = service.run_nightly_maintenance()
    result["enqueue_errors"] = enqueue_errors

    journal_path = vault_path / PASS_JOURNAL_RELATIVE_PATH
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        journal = {}
    if isinstance(journal, dict):
        result["pass_id"] = journal.get("pass_id")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run, preview, or roll back one manual compile-enrich pass"
    )
    parser.add_argument(
        "--date",
        action="append",
        default=[],
        dest="dates",
        metavar="YYYY-MM-DD",
        help="Enqueue daily/<date>.md as a source; may repeat",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        dest="sources",
        metavar="PATH",
        help="Enqueue an arbitrary vault-relative source path; may repeat",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview sources/queue only; do not enqueue or run maintenance",
    )
    parser.add_argument(
        "--rollback",
        metavar="PASS_ID",
        default=None,
        help="Roll back one previously run pass by id, and exit",
    )
    args = parser.parse_args()

    if args.rollback:
        if args.dates or args.sources or args.dry_run:
            parser.error("--rollback cannot be combined with --date/--source/--dry-run")
    elif not args.dates and not args.sources:
        parser.error(
            "at least one --date or --source is required unless --rollback is given"
        )

    settings = get_settings()
    vault_path = Path(settings.vault_path)
    service = CompiledBriefingService(
        vault_path,
        content_language=settings.content_language,
        ai_cli=settings.ai_cli,
    )

    try:
        if args.rollback:
            result = _run_rollback(service, args.rollback)
        else:
            rel_paths = _resolve_sources(args.dates, args.sources)
            if args.dry_run:
                result = _run_dry_run(service, vault_path, rel_paths)
            else:
                result = _run_pass(service, vault_path, rel_paths)
    except CompiledSourceStateError as exc:
        # Same handling as ``run_compiled_maintenance.py`` (its own "Code
        # review Finding 2"): a corrupt ``.compiled/source-state.json`` is a
        # real failure and must still exit non-zero, but this family of CLIs
        # promises a JSON ``{"errors": [...]}`` contract on stdout -- a raw
        # traceback would leave any caller parsing that contract with
        # nothing at all.
        result = {
            "errors": [f"повреждено состояние источников компиляции: {exc}"],
        }

    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    # ``enqueue_errors`` counts towards the exit code for the same reason a
    # ``--rollback`` with no snapshot does (see ``_run_rollback``): the
    # operator named a specific source on the command line, and a source that
    # never reached the queue was never part of the pass at all. Reporting
    # exit 0 there would read as "your source was processed" to anything
    # driving this CLI, which is exactly the quiet success this module's
    # docstring refuses to hand back.
    failed = result.get("errors") or result.get("enqueue_errors")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
