"""Manually reprocess selected daily notes through compiled briefings."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
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


def _has_terminal_backend_error(errors: list[str]) -> bool:
    return any(detect_terminal_backend_message(error) for error in errors)


def _systemd_run_main_pid(unit_name: str) -> int:
    for _ in range(10):
        result = subprocess.run(  # noqa: S603
            ["systemctl", "show", unit_name, "--property=MainPID", "--value"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            value = (result.stdout or "").strip()
            if value.isdigit():
                pid = int(value)
                if pid > 0:
                    return pid
        time.sleep(0.1)
    return 0


def _spawn_full_pass(
    vault: Path,
    *,
    start_from: str,
    log_path: Path,
) -> tuple[int, str | None]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    project_root = vault.parent.resolve()
    python_bin = (project_root / ".venv" / "bin" / "python").resolve()

    if shutil.which("systemd-run"):
        unit_name = (
            "dbrain-compiled-full-pass-"
            f"{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}"
        )
        shell_command = (
            f"cd {shlex.quote(str(project_root))} && "
            f"exec {shlex.quote(str(python_bin))} -m "
            "d_brain.run_compiled_daily_full_pass "
            f"--resume --start-from {shlex.quote(start_from)} "
            f">> {shlex.quote(str(log_path))} 2>&1"
        )
        command = [
            "systemd-run",
            "--quiet",
            "--collect",
            "--no-block",
            f"--unit={unit_name}",
            "--description=d-brain compiled full pass resume",
            f"--working-directory={project_root}",
            "--setenv=TERM=dumb",
            "--setenv=NO_COLOR=1",
            "/bin/bash",
            "-lc",
            shell_command,
        ]
        result = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return _systemd_run_main_pid(unit_name), unit_name

    with log_path.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(  # noqa: S603
            [
                str(python_bin),
                "-m",
                "d_brain.run_compiled_daily_full_pass",
                "--resume",
                "--start-from",
                start_from,
            ],
            cwd=project_root,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
        )
    return int(process.pid), None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manually reprocess selected daily files for compiled briefings",
    )
    parser.add_argument(
        "days",
        nargs="+",
        help="Daily filenames like 2025-04-30.md",
    )
    parser.add_argument(
        "--resume-full-pass-from",
        default="",
        help="Restart detached historical full pass after manual reprocess finishes",
    )
    args = parser.parse_args()

    settings = get_settings()
    vault = settings.vault_path.resolve()
    progress_path = vault / ".compiled" / "manual-reprocess-progress.json"
    history_path = vault / ".compiled" / "manual-reprocess-history.jsonl"
    report_path = (
        vault
        / ".compiled"
        / (
            "manual-reprocess-"
            f"{datetime.now().astimezone().strftime('%Y-%m-%d-%H%M%S')}.json"
        )
    )

    state: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "mode": "manual-reprocess-days",
        "pid": os.getpid(),
        "days": list(args.days),
        "total_days": len(args.days),
        "current_file": None,
        "current_chunk": 0,
        "current_chunk_total": 0,
        "processed_days": 0,
        "updated_notes": 0,
        "errors": [],
        "finished": False,
        "resume_full_pass_from": args.resume_full_pass_from or None,
        "report_path": report_path.relative_to(vault).as_posix(),
    }
    _write_json(progress_path, state)

    service = CompiledBriefingService(
        vault,
        content_language=settings.content_language,
        ai_cli=settings.ai_cli,
    )
    report: dict[str, Any] = {
        "started_at": state["started_at"],
        "days": [],
        "updated_total": [],
        "had_updates": False,
    }
    updated_total: list[str] = []
    terminal_backend_error = False

    for index, day in enumerate(args.days, start=1):
        rel_path = f"daily/{day}"
        state["current_file"] = day
        state["current_chunk"] = 0
        state["current_chunk_total"] = 0
        state["last_activity_at"] = datetime.now().astimezone().isoformat()
        _write_json(progress_path, state)
        sys.stdout.write(f"==> {rel_path}\n")
        sys.stdout.flush()

        current_day = day

        def on_chunk(
            event: dict[str, Any],
            *,
            file_name: str = current_day,
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
            dict.fromkeys(str(item) for item in result.get("updated", []) if str(item))
        )
        errors = [str(item) for item in result.get("errors", []) if str(item)]
        for item in updated:
            if item not in updated_total:
                updated_total.append(item)

        entry = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "file": day,
            "rel_path": rel_path,
            "chunks": int(result.get("chunks") or 0),
            "processed_chunks": int(result.get("processed_chunks") or 0),
            "updated": updated,
            "errors": errors,
        }
        report["days"].append(entry)
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

        state["processed_days"] = index
        state["updated_notes"] = len(updated_total)
        state["current_file"] = None
        state["current_chunk"] = 0
        state["current_chunk_total"] = 0
        state["last_completed"] = day
        state["last_activity_at"] = datetime.now().astimezone().isoformat()
        if errors:
            state["errors"].append({"file": day, "errors": errors})
        _write_json(progress_path, state)

        sys.stdout.write(json.dumps(entry, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if _has_terminal_backend_error(errors):
            state["stopped_reason"] = "terminal-backend-error"
            terminal_backend_error = True
            _write_json(progress_path, state)
            break

    service._refresh_qmd_index()
    report["finished_at"] = datetime.now().astimezone().isoformat()
    report["updated_total"] = updated_total
    report["had_updates"] = bool(updated_total)
    _write_json(report_path, report)

    state["finished"] = not terminal_backend_error
    state["finished_at"] = report["finished_at"]
    state["last_activity_at"] = report["finished_at"]
    state["updated_notes"] = len(updated_total)

    if args.resume_full_pass_from and not terminal_backend_error:
        ts = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
        log_path = Path(f"/tmp/compiled-daily-full-pass-{ts}-resume.log")
        state["resumed_full_pass_log"] = str(log_path)
        resumed_pid, resumed_unit = _spawn_full_pass(
            vault,
            start_from=args.resume_full_pass_from,
            log_path=log_path,
        )
        state["resumed_full_pass_pid"] = resumed_pid
        if resumed_unit:
            state["resumed_full_pass_unit"] = resumed_unit

    _write_json(progress_path, state)
    sys.stdout.write(f"REPORT_PATH={report_path}\n")
    if args.resume_full_pass_from and not terminal_backend_error:
        sys.stdout.write(f"RESUMED_FULL_PASS_FROM={args.resume_full_pass_from}\n")
    return 1 if terminal_backend_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
