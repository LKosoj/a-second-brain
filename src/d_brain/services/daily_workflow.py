"""Daily capture, preview, execute, and reflect orchestration."""

import logging
from collections.abc import Callable
from contextlib import nullcontext
from datetime import date
from typing import TYPE_CHECKING, Any

from d_brain.services.json_normalizer import extract_first_json_dict

if TYPE_CHECKING:
    from d_brain.services.processor import CliProcessor

logger = logging.getLogger(__name__)

INTERACTIVE_MODE = "interactive"
SCHEDULED_MODE = "scheduled"
VALID_PROCESS_MODES = {INTERACTIVE_MODE, SCHEDULED_MODE}


def _json_retry_suffix() -> str:
    """Tighten the contract when the first JSON response is malformed."""
    return (
        "\n\nCRITICAL OUTPUT CONTRACT:\n"
        "- Return exactly one JSON object.\n"
        "- No prose before or after JSON.\n"
        "- No markdown fences.\n"
        "- No explanations.\n"
    )


def run_json_phase(
    prompt: str,
    *,
    phase_name: str,
    retry_on_parse_error: bool,
    run_prompt: Callable[[str], str],
    raw_output_writer: Callable[[str, str], object] | None,
) -> dict[str, Any]:
    """Run one JSON phase with the requested parse-failure policy."""
    output = run_prompt(prompt)
    try:
        return extract_first_json_dict(output, error_context="CLI output")
    except ValueError:
        if raw_output_writer is not None:
            raw_output_writer(f"{phase_name}-raw-output.txt", output)
        if not retry_on_parse_error:
            raise

    retry_output = run_prompt(prompt + _json_retry_suffix())
    try:
        return extract_first_json_dict(retry_output, error_context="CLI output")
    except ValueError:
        if raw_output_writer is not None:
            raw_output_writer(f"{phase_name}-retry-raw-output.txt", retry_output)
        raise


class DailyWorkflow:
    """Coordinate the existing daily phases through a CliProcessor facade."""

    def __init__(self, host: "CliProcessor") -> None:
        self._host = host

    def run(
        self,
        day: date | None = None,
        *,
        mode: str = INTERACTIVE_MODE,
    ) -> dict[str, Any]:
        """Run the interactive preview or scheduled daily workflow."""
        target_day = day or date.today()
        if mode not in VALID_PROCESS_MODES:
            return {"error": f"Unknown process mode: {mode}", "processed_entries": 0}

        lock_context = (
            self._host._scheduled_process_lock()
            if mode == SCHEDULED_MODE
            and not self._host._scheduled_cycle_lock_held
            else nullcontext()
        )
        with lock_context:
            if mode == SCHEDULED_MODE:
                self._host._clear_session_phase_artifacts()

            daily_file = (
                self._host._ensure_daily_file(target_day)
                if mode == SCHEDULED_MODE
                else self._host._get_daily_file(target_day)
            )

            if mode == SCHEDULED_MODE:
                self._host._ensure_handoff_file()
                self._host._compact_handoff_file()
                self._host._log_graph_age_warning()

            if not daily_file.exists():
                logger.warning("No daily file for %s", target_day)
                return self._empty_result(mode)

            if not self._host._daily_has_processable_entries(daily_file):
                logger.info(
                    "Daily file for %s has no processable entries; skipping",
                    target_day,
                )
                if mode == SCHEDULED_MODE:
                    self._host._rebuild_graph()
                    self._host._refresh_qmd_index()
                return self._empty_result(mode)

            capture_data, processed_entries = self._capture(target_day, mode=mode)
            if mode == INTERACTIVE_MODE:
                return self._preview(
                    target_day,
                    capture_data,
                    processed_entries=processed_entries,
                )

            execute_data = self._execute(target_day, capture_data)
            return self._reflect(
                target_day,
                execute_data,
                processed_entries=processed_entries,
            )

    @staticmethod
    def _empty_result(mode: str) -> dict[str, Any]:
        return {
            "report": "",
            "processed_entries": 0,
            "empty_daily": True,
            "mode": mode,
        }

    def _capture(
        self,
        day: date,
        *,
        mode: str,
    ) -> tuple[dict[str, Any], int]:
        capture_data = self._host._run_json_phase(
            self._host._build_capture_prompt(day),
            phase_name="capture",
            persist_raw_output=mode == SCHEDULED_MODE,
        )
        self._host._apply_entry_status_guardrails(
            day=day,
            capture_data=capture_data,
        )
        return capture_data, self._host._count_processed_entries(capture_data)

    def _preview(
        self,
        day: date,
        capture_data: dict[str, Any],
        *,
        processed_entries: int,
    ) -> dict[str, Any]:
        report = self._host._run_vault_prompt(
            self._host._build_preview_prompt(day, capture_data)
        )
        return {
            "report": self._host._normalize_owner_report_markdown(report),
            "processed_entries": processed_entries,
            "mode": INTERACTIVE_MODE,
        }

    def _execute(
        self,
        day: date,
        capture_data: dict[str, Any],
    ) -> dict[str, Any]:
        self._host._write_session_json("capture.json", capture_data)
        execute_data = self._host._run_json_phase(
            self._host._build_execute_prompt(day),
            phase_name="execute",
            retry_on_parse_error=False,
        )
        self._host._write_session_json("execute.json", execute_data)
        self._host._normalize_saved_thoughts(execute_data, day=day)
        return execute_data

    def _reflect(
        self,
        day: date,
        execute_data: dict[str, Any],
        *,
        processed_entries: int,
    ) -> dict[str, Any]:
        self._host._run_memory_decay()
        self._host._capture_creative_recall()
        self._host._run_vault_health_maintenance()
        self._host._capture_memory_audit()
        report = self._host._run_vault_prompt(self._host._build_reflect_prompt(day))
        self._host._compact_handoff_file()
        self._host._write_reflect_daily_block(day, execute_data)
        self._host._refresh_qmd_index()
        return {
            "report": self._host._normalize_owner_report_markdown(report),
            "processed_entries": processed_entries,
            "mode": SCHEDULED_MODE,
        }
