"""Run historical full daily bootstrap for compiled briefings."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from d_brain.config import get_settings
from d_brain.services.cli_runner import detect_terminal_backend_message
from d_brain.services.compiled_briefings import CompiledBriefingService


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _append_history(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _has_terminal_backend_error(errors: list[str]) -> bool:
    return any(detect_terminal_backend_message(error) for error in errors)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run chunk-aware historical full pass over vault/daily",
    )
    parser.add_argument(
        "--start-from",
        default="2025-04-30.md",
        help="Daily filename to start from (inclusive)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from progress file next_file/current_file when available",
    )
    parser.add_argument(
        "--refresh-every",
        type=int,
        default=5,
        help="Refresh qmd after this many days with compiled updates",
    )
    args = parser.parse_args()

    settings = get_settings()
    vault = settings.vault_path
    progress_path = vault / ".compiled" / "daily-full-pass-progress.json"
    history_path = vault / ".compiled" / "daily-full-pass-history.jsonl"

    all_days = sorted((vault / "daily").glob("*.md"))
    if not all_days:
        sys.stdout.write(
            json.dumps({"error": "no-daily-files"}, ensure_ascii=False) + "\n"
        )
        return 1

    start_name = args.start_from
    if args.resume and progress_path.exists():
        try:
            previous = json.loads(progress_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        for key in ("current_file", "next_file", "start_from"):
            candidate = str(previous.get(key) or "").strip()
            if candidate:
                start_name = candidate
                break

    start_index = 0
    for index, path in enumerate(all_days):
        if path.name >= start_name:
            start_index = index
            break
    else:
        start_index = len(all_days)

    state: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "mode": "full-daily-bootstrap",
        "pid": os.getpid(),
        "start_from": start_name,
        "total_files": len(all_days),
        "remaining_from_start": len(all_days[start_index:]),
        "current_file": None,
        "current_chunk": 0,
        "current_chunk_total": 0,
        "last_activity_at": None,
        "last_completed": None,
        "next_file": (
            all_days[start_index].name if start_index < len(all_days) else None
        ),
        "processed_from_start": 0,
        "updated_notes": 0,
        "errors": [],
        "finished": False,
    }
    _write_json(progress_path, state)

    service = CompiledBriefingService(
        vault,
        content_language=settings.content_language,
        ai_cli=settings.ai_cli,
    )
    updates_since_refresh = 0

    try:
        for offset, path in enumerate(all_days[start_index:], start=1):
            rel_path = path.relative_to(vault).as_posix()
            state["current_file"] = path.name
            state["current_chunk"] = 0
            state["current_chunk_total"] = 0
            state["last_activity_at"] = datetime.now().astimezone().isoformat()
            _write_json(progress_path, state)
            sys.stdout.write(f"==> {rel_path}\n")
            sys.stdout.flush()

            current_file = path.name

            def on_chunk(
                event: dict[str, Any],
                *,
                file_name: str = current_file,
            ) -> None:
                state["current_file"] = file_name
                state["current_chunk"] = int(event.get("index") or 0)
                state["current_chunk_total"] = int(event.get("total") or 0)
                state["last_chunk_status"] = str(event.get("status") or "")
                state["last_chunk_updated"] = list(event.get("updated", []))
                state["last_chunk_errors"] = list(event.get("errors", []))
                state["last_activity_at"] = datetime.now().astimezone().isoformat()
                _write_json(progress_path, state)

            result = service.refresh_daily_fully(
                source_path=rel_path,
                refresh_qmd=False,
                on_chunk=on_chunk,
            )
            updated = list(
                dict.fromkeys(str(item) for item in result.get("updated", []))
            )
            errors = [str(item) for item in result.get("errors", []) if str(item)]
            entry = {
                "timestamp": datetime.now().astimezone().isoformat(),
                "file": path.name,
                "rel_path": rel_path,
                "chunks": int(result.get("chunks") or 0),
                "processed_chunks": int(result.get("processed_chunks") or 0),
                "updated": updated,
                "errors": errors,
            }
            _append_history(history_path, entry)

            state["last_completed"] = path.name
            state["current_file"] = None
            state["current_chunk"] = 0
            state["current_chunk_total"] = 0
            state["processed_from_start"] = offset
            state["next_file"] = (
                all_days[start_index + offset].name
                if start_index + offset < len(all_days)
                else None
            )
            state["updated_notes"] += len(updated)
            state["last_activity_at"] = datetime.now().astimezone().isoformat()
            if errors:
                state["errors"].append({"file": path.name, "errors": errors})
            _write_json(progress_path, state)

            sys.stdout.write(json.dumps(entry, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            if updated:
                updates_since_refresh += 1
            if _has_terminal_backend_error(errors):
                state["stopped_reason"] = "terminal-backend-error"
                state["finished"] = False
                state["last_activity_at"] = datetime.now().astimezone().isoformat()
                _write_json(progress_path, state)
                return 1
            if updates_since_refresh >= max(1, args.refresh_every):
                service._refresh_qmd_index()
                updates_since_refresh = 0
                sys.stdout.write("QMD_REFRESH_DONE\n")
                sys.stdout.flush()

        service._refresh_qmd_index()
        state["finished"] = True
        state["finished_at"] = datetime.now().astimezone().isoformat()
        state["last_activity_at"] = state["finished_at"]
        _write_json(progress_path, state)
        sys.stdout.write("FULL_PASS_DONE\n")
        return 0
    except Exception as exc:
        state["crashed_at"] = datetime.now().astimezone().isoformat()
        state["crash"] = {"type": type(exc).__name__, "message": str(exc)}
        state["last_activity_at"] = state["crashed_at"]
        _write_json(progress_path, state)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
