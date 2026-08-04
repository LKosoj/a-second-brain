"""CLI entrypoint for unified daily processing."""

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any, cast

from d_brain.config import get_settings
from d_brain.services.processor import (
    INTERACTIVE_MODE,
    SCHEDULED_MODE,
    CliProcessor,
)
from d_brain.services.reflection_digest import ReflectionDigestService
from d_brain.services.telegram_delivery import send_telegram_text_sync

logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


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

    lines = [heading, ""]
    if daily_empty:
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

    if (
        not tasks_created
        and not thoughts_saved
        and not crm_updated
        and not periodic_cycles
        and not audit_tasks_created
        and not audit_task_candidates
    ):
        lines.extend(["", "По результатам сегодняшнего дня ничего нового."])
        return "\n".join(lines)

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
) -> None:
    """Always notify the owner after one scheduled daily processing attempt."""
    digest = _build_scheduled_digest(
        day,
        result,
        _load_execute_payload(vault_path),
        takeaways=takeaways,
    )
    send_telegram_text_sync(digest, rich="error" not in result)


def _run_processor_cycle(processor: Any, day: date, mode: str) -> dict[str, Any]:
    """Call the best available processor entrypoint for the requested mode."""
    if mode == SCHEDULED_MODE and hasattr(processor, "run_scheduled_cycle"):
        return cast(dict[str, Any], processor.run_scheduled_cycle(day))
    return cast(dict[str, Any], processor.process_daily(day, mode=mode))


def main() -> int:
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

    day = _parse_date(args.day) or date.today()
    settings = get_settings()
    processor = CliProcessor(
        settings.vault_path,
        settings.todoist_api_key,
        settings.ai_cli,
        settings.owner_full_name,
        settings.content_language,
        getattr(settings, "openai_api_key", ""),
        getattr(settings, "openai_base_url", ""),
        getattr(settings, "openai_model", ""),
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
        report = str(result.get("report", ""))
        if report:
            sys.stdout.write(report)
            if not report.endswith("\n"):
                sys.stdout.write("\n")

    if args.mode == SCHEDULED_MODE and not args.skip_notify:
        try:
            takeaways = _build_digest_takeaways(settings, day, result)
            _notify_scheduled_digest(
                day,
                settings.vault_path,
                result,
                takeaways=takeaways,
            )
        except Exception as exc:  # pragma: no cover - notification boundary
            logger.warning("Failed to send scheduled digest: %s", exc)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
