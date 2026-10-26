"""CLI entrypoint for unified daily processing.

Scheduled catch-up: systemd's ``Persistent=true`` can fire this process late
(e.g. at 09:00 the next morning for a missed 21:00 run). Without ``--date``,
a ``--mode scheduled`` run observed before the scheduled hour is treated as a
delayed catch-up and processes yesterday instead of today. The scheduled
hour is read from the ``SCHEDULED_PROCESS_HOUR`` env var, else from the hour
of ``DAILY_PROCESS_TIME`` (the timer time), else
``DEFAULT_SCHEDULED_PROCESS_HOUR`` below; pass ``--date`` to override.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from d_brain.clock import owner_now
from d_brain.config import get_settings, parse_clock_time
from d_brain.services.frontmatter import ensure_run_identity
from d_brain.services.processor import (
    INTERACTIVE_MODE,
    SCHEDULED_MODE,
    CliProcessor,
)
from d_brain.services.reflection_digest import ReflectionDigestService
from d_brain.services.telegram_delivery import send_telegram_text_sync
from d_brain.services.transcription import DeepgramTranscriber
from d_brain.services.voice_retry import retry_unrecognized_voice

logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


DEFAULT_SCHEDULED_PROCESS_HOUR = 21
"""Fallback scheduled run hour (owner's time, 24h) used to detect a delayed
systemd ``Persistent=true`` catch-up run when ``SCHEDULED_PROCESS_HOUR`` is
unset."""


def _resolve_processing_day(mode: str) -> date:
    """Pick the day to process when ``--date`` is not given.

    A systemd timer with ``Persistent=true`` can fire late -- e.g. at 09:00
    the next morning for a missed 21:00 run. Today's date would then pick
    the new day and leave yesterday's entries unprocessed. For
    ``--mode scheduled`` runs, a run observed before the scheduled hour
    (``SCHEDULED_PROCESS_HOUR`` env var, else the hour of
    ``DAILY_PROCESS_TIME``, else ``DEFAULT_SCHEDULED_PROCESS_HOUR``) is
    treated as a delayed catch-up for yesterday.

    Limitation: with a cut-off hour of 0 (timer at 00:00-00:59) no run can
    be earlier than the scheduled hour, so a late run after midnight is
    never recognised as a catch-up and takes the new, empty day.
    """
    now = owner_now()
    if mode != SCHEDULED_MODE:
        return now.date()
    hour = _read_scheduled_process_hour()
    if now.hour < hour:
        return now.date() - timedelta(days=1)
    return now.date()


def _read_scheduled_process_hour() -> int:
    """Read ``SCHEDULED_PROCESS_HOUR``, falling back to the default on any
    unparseable or out-of-range value instead of crashing before the owner
    is notified."""
    raw_hour = os.environ.get("SCHEDULED_PROCESS_HOUR")
    if raw_hour is None:
        # The timer fires at DAILY_PROCESS_TIME; a cut-off left at 21 would
        # make every run of an earlier timer look like a late catch-up.
        raw_time = os.environ.get("DAILY_PROCESS_TIME")
        if raw_time is None:
            return DEFAULT_SCHEDULED_PROCESS_HOUR
        parsed = parse_clock_time(raw_time)
        if parsed is None:
            logger.warning(
                "Invalid DAILY_PROCESS_TIME=%r, using default hour %s",
                raw_time,
                DEFAULT_SCHEDULED_PROCESS_HOUR,
            )
            return DEFAULT_SCHEDULED_PROCESS_HOUR
        return parsed[0]
    try:
        hour = int(raw_hour)
    except ValueError:
        logger.warning(
            "Invalid SCHEDULED_PROCESS_HOUR=%r, using default %s",
            raw_hour,
            DEFAULT_SCHEDULED_PROCESS_HOUR,
        )
        return DEFAULT_SCHEDULED_PROCESS_HOUR
    if not 0 <= hour <= 23:
        logger.warning(
            "SCHEDULED_PROCESS_HOUR=%s out of range 0..23, using default %s",
            hour,
            DEFAULT_SCHEDULED_PROCESS_HOUR,
        )
        return DEFAULT_SCHEDULED_PROCESS_HOUR
    return hour


def _retry_unrecognized_voice(settings: Any, day: date) -> None:
    """Transcribe voice saved as unrecognized audio; never stops the run."""
    api_key = getattr(settings, "deepgram_api_key", "")
    if not api_key:
        logger.info("DEEPGRAM_API_KEY is not set, voice retry skipped")
        return
    try:
        result = retry_unrecognized_voice(
            settings.vault_path,
            (day - timedelta(days=1), day),
            # main() has no running event loop, so asyncio.run is safe here.
            # A new transcriber per file: its HTTP client binds to the event
            # loop of the first call, and asyncio.run closes that loop.
            lambda data: asyncio.run(
                DeepgramTranscriber(api_key, settings.content_language).transcribe(
                    data
                )
            ),
        )
    except Exception:
        logger.exception("Voice retry failed")
        return
    logger.info(
        "Voice retry: %d recognized, %d failed, %d skipped",
        result.recognized,
        result.failed,
        result.skipped,
    )


def _load_execute_payload(vault_path: Path) -> dict[str, Any]:
    """Best-effort execute payload for digest rendering."""
    path = vault_path / ".session" / "execute.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Invalid execute payload at %s", path)
        return {}
    return payload if isinstance(payload, dict) else {}


def _dict_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = payload.get(key)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _render_list_preview(items: list[str]) -> list[str]:
    preview: list[str] = []
    for item in items:
        value = " ".join(str(item or "").split())
        if not value:
            continue
        preview.append(f"- {value}")
    return preview


def _build_scheduled_digest(
    day: date,
    result: dict[str, Any],
    execute_payload: dict[str, Any],
    *,
    takeaways: list[str] | None = None,
    takeaways_error: str | None = None,
) -> str:
    """Build one short always-sent Telegram digest for scheduled processing."""
    heading = f"**🧠 D-Brain — {day.isoformat()}**"
    daily_empty = bool(result.get("empty_daily"))
    periodic_cycles = result.get("periodic_cycles")
    if not isinstance(periodic_cycles, list):
        periodic_cycles = []
    audit_tasks_created = [
        str(item.get("content") or "").strip()
        for item in result.get("audit_tasks_created", [])
        if isinstance(item, dict) and str(item.get("content") or "").strip()
    ]
    audit_task_candidates = [
        str(item).strip()
        for item in result.get("audit_task_candidates", [])
        if str(item).strip()
    ]

    if "error" in result:
        error_text = str(result["error"]).strip() or "unknown error"
        return "\n".join(
            [
                heading,
                "",
                "Ежедневная обработка завершилась с ошибкой.",
                f"`{error_text}`",
            ]
        )

    tasks_created = _dict_list(execute_payload, "tasks_created")
    thoughts_saved = _dict_list(execute_payload, "thoughts_saved")
    crm_updated = _dict_list(execute_payload, "crm_updated")
    daily_result = result.get("daily")
    if not isinstance(daily_result, dict):
        daily_result = result
    processed_entries = int(daily_result.get("processed_entries") or 0)

    daily_error = str(result.get("daily_error") or "").strip()
    lines = [heading, ""]
    if daily_error:
        # Only the daily part failed: say so, and still show the rest below.
        lines.extend(["**Ежедневная обработка: ошибка**", f"`{daily_error}`"])
        disabled_notice = str(result.get("disabled_steps_notice") or "").strip()
        if disabled_notice:
            lines.extend(["", disabled_notice])
    elif daily_empty:
        lines.append("Сегодня новых записей не было.")
    else:
        lines.extend(
            [
                "Ежедневная обработка завершена.",
                (
                    f"Записей обработано: **{processed_entries}** | "
                    f"задач: **{len(tasks_created)}** | "
                    f"мыслей: **{len(thoughts_saved)}** | "
                    f"CRM: **{len(crm_updated)}**"
                ),
            ]
        )

    # Tasks that never reached Todoist (or whose creation was not confirmed)
    # must stay visible: otherwise the owner believes they are tracked.
    tasks_pending = _dict_list(execute_payload, "tasks_pending")
    tasks_unconfirmed = [
        *_dict_list(execute_payload, "tasks_unconfirmed"),
        *[
            item
            for key in ("retry_tasks_unconfirmed", "audit_tasks_unconfirmed")
            for item in result.get(key, [])
            if isinstance(item, dict)
        ],
    ]
    if tasks_pending:
        lines.append(
            f"⚠️ Не отправлено в Todoist (ждёт повтора): **{len(tasks_pending)}**"
        )
    if tasks_unconfirmed:
        lines.append(
            f"⚠️ Не подтверждено, проверьте в Todoist: **{len(tasks_unconfirmed)}**"
        )

    if (
        not daily_error
        and not tasks_created
        and not tasks_pending
        and not tasks_unconfirmed
        and not thoughts_saved
        and not crm_updated
        and not periodic_cycles
        and not audit_tasks_created
        and not audit_task_candidates
    ):
        lines.extend(["", "По результатам сегодняшнего дня ничего нового."])
        return "\n".join(lines)

    if takeaways_error:
        lines.extend(["", f"⚠️ выводы не сформированы: {takeaways_error}"])

    takeaway_lines = [
        f"- {' '.join(item.split())}"
        for item in (takeaways or [])
        if str(item or "").strip()
    ]
    if takeaway_lines:
        lines.extend(["", "**Ключевые выводы**"])
        lines.extend(takeaway_lines)

    task_titles = [
        str(item.get("content") or "").strip()
        for item in tasks_created
        if str(item.get("content") or "").strip()
    ]
    thought_titles = [
        str(item.get("title") or item.get("path") or "").strip()
        for item in thoughts_saved
        if str(item.get("title") or item.get("path") or "").strip()
    ]
    crm_titles = [
        str(item.get("change") or item.get("path") or "").strip()
        for item in crm_updated
        if str(item.get("change") or item.get("path") or "").strip()
    ]

    if task_titles:
        lines.extend(["", "**Новые задачи**"])
        lines.extend(_render_list_preview(task_titles))
    if thought_titles and not takeaway_lines:
        lines.extend(["", "**Новые мысли**"])
        lines.extend(_render_list_preview(thought_titles))
    if crm_titles:
        lines.extend(["", "**CRM / контекст**"])
        lines.extend(_render_list_preview(crm_titles))
    if periodic_cycles:
        lines.extend(["", "**Периодические циклы**"])
        for cycle in periodic_cycles:
            if not isinstance(cycle, dict):
                continue
            label = str(cycle.get("label") or cycle.get("name") or "").strip()
            cycle_result = cycle.get("result")
            if not isinstance(cycle_result, dict):
                cycle_result = {}
            if "error" in cycle_result:
                status = f"- {label}: ошибка (`{str(cycle_result['error'])}`)"
            elif cycle_result.get("summary_path"):
                status = f"- {label}: `{str(cycle_result['summary_path'])}`"
            elif cycle_result.get("note_path"):
                status = f"- {label}: `{str(cycle_result['note_path'])}`"
            elif cycle_result.get("status") == "skipped-interval":
                status = (
                    f"- {label}: пропущен — срок следующего прохода ещё не наступил"
                )
            elif cycle_result.get("skipped"):
                status = f"- {label}: без новых сигналов"
            elif cycle_result.get("carry_forward_observations"):
                status = f"- {label}: наблюдения перенесены на следующий проход"
            else:
                status = f"- {label}: выполнен"
            lines.append(status)
    if audit_tasks_created:
        lines.extend(["", "**Задачи на доработку**"])
        lines.extend(_render_list_preview(audit_tasks_created))
    elif audit_task_candidates:
        lines.extend(["", "**Найдены проблемы**"])
        lines.extend(_render_list_preview(audit_task_candidates))
    return "\n".join(lines)


def _build_digest_takeaways(
    settings: Any,
    day: date,
    result: dict[str, Any],
) -> list[str]:
    """Best-effort LLM digest over already completed reflection results."""
    daily_result = result.get("daily")
    if not isinstance(daily_result, dict):
        daily_result = result
    if "error" in daily_result:
        return []
    execute_payload = _load_execute_payload(settings.vault_path)
    periodic_cycles = result.get("periodic_cycles")
    if not isinstance(periodic_cycles, list):
        periodic_cycles = []
    audit_tasks_created = result.get("audit_tasks_created")
    audit_task_candidates = result.get("audit_task_candidates")
    if not any(
        [
            _dict_list(execute_payload, "tasks_created"),
            _dict_list(execute_payload, "thoughts_saved"),
            _dict_list(execute_payload, "crm_updated"),
            periodic_cycles,
            audit_tasks_created,
            audit_task_candidates,
        ]
    ):
        return []

    report_markdown = str(result.get("report", "")).strip() or str(
        daily_result.get("report", "")
    ).strip()
    # Compiled changes are dated by processing time, including archived sources.
    for cycle in periodic_cycles:
        if cycle.get("name") != "maintenance.compiled-digest":
            continue
        cycle_report = str(cycle.get("result", {}).get("report", "")).strip()
        if cycle_report:
            report_markdown = "\n\n".join(
                part.strip()
                for part in report_markdown.split(cycle_report)
                if part.strip()
            )
    if not report_markdown:
        return []

    service = ReflectionDigestService(
        settings.vault_path,
        ai_cli=settings.ai_cli,
        content_language=settings.content_language,
    )
    return service.summarize(
        day=day,
        report_markdown=report_markdown,
        execute_payload=execute_payload,
    )



def _notify_scheduled_digest(
    day: date,
    vault_path: Path,
    result: dict[str, Any],
    *,
    takeaways: list[str] | None = None,
    takeaways_error: str | None = None,
) -> None:
    """Always notify the owner after one scheduled daily processing attempt."""
    digest = _build_scheduled_digest(
        day,
        result,
        _load_execute_payload(vault_path),
        takeaways=takeaways,
        takeaways_error=takeaways_error,
    )
    run_id = os.environ.get("D_BRAIN_RUN_ID")
    if run_id:
        digest = (
            f"{digest}\n\nrun_id: {run_id} "
            f"(откат: a-second-brain recover {run_id} --vault {vault_path})"
        )
    send_telegram_text_sync(digest, rich="error" not in result)


def _run_processor_cycle(processor: Any, day: date, mode: str) -> dict[str, Any]:
    """Call the best available processor entrypoint for the requested mode."""
    if mode == SCHEDULED_MODE and hasattr(processor, "run_scheduled_cycle"):
        return cast(dict[str, Any], processor.run_scheduled_cycle(day))
    return cast(dict[str, Any], processor.process_daily(day, mode=mode))


def main() -> int:
    ensure_run_identity("daily-process", "daily-process")
    parser = argparse.ArgumentParser(description="Run daily processing")
    parser.add_argument(
        "--mode",
        choices=[INTERACTIVE_MODE, SCHEDULED_MODE],
        default=SCHEDULED_MODE,
        help="interactive preview or full scheduled pipeline",
    )
    parser.add_argument(
        "--date",
        dest="day",
        help="Date to process in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--skip-notify",
        action="store_true",
        help="Do not send the scheduled Telegram digest from this process.",
    )
    args = parser.parse_args()

    # Settings first: loading them fixes the owner's timezone for the day below.
    settings = get_settings()
    day = _parse_date(args.day) or _resolve_processing_day(args.mode)
    if args.mode == SCHEDULED_MODE:
        # Before classification, so a recording recognized now is processed
        # with the rest of the day.
        _retry_unrecognized_voice(settings, day)
    processor = CliProcessor(
        settings.vault_path,
        settings.todoist_api_key,
        settings.ai_cli,
        settings.owner_full_name,
        settings.content_language,
        getattr(settings, "openai_api_key", ""),
        getattr(settings, "openai_base_url", ""),
        getattr(settings, "openai_model", ""),
        getattr(settings, "tavily_api_key", ""),
        settings.owner_telegram_id,
        getattr(settings, "recall_planner_disable_thinking", False),
    )
    try:
        result = _run_processor_cycle(processor, day, args.mode)
    except Exception as exc:  # pragma: no cover - CLI boundary
        logger.exception("Daily processing crashed")
        result = {"error": f"Processing task crashed unexpectedly: {exc}"}

    exit_code = 0

    if "error" in result:
        sys.stderr.write(str(result["error"]).rstrip() + "\n")
        exit_code = 1
    else:
        if result.get("daily_error"):
            sys.stderr.write(str(result["daily_error"]).rstrip() + "\n")
            exit_code = 1
        report = str(result.get("report", ""))
        if report:
            sys.stdout.write(report)
            if not report.endswith("\n"):
                sys.stdout.write("\n")

    if args.mode == SCHEDULED_MODE and not args.skip_notify:
        takeaways: list[str] = []
        takeaways_error: str | None = None
        try:
            takeaways = _build_digest_takeaways(settings, day, result)
        except Exception as exc:
            logger.warning("Failed to build digest takeaways: %s", exc)
            takeaways_error = str(exc)[:200]
        try:
            _notify_scheduled_digest(
                day,
                settings.vault_path,
                result,
                takeaways=takeaways,
                takeaways_error=takeaways_error,
            )
        except Exception as exc:  # pragma: no cover - notification boundary
            logger.warning("Failed to send scheduled digest: %s", exc)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
