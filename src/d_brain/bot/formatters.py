"""Report formatters for Telegram messages."""

from typing import Any

from d_brain.services.telegram_markup import (
    TELEGRAM_TEXT_LIMIT,
    contains_legacy_telegram_html,
    html_to_markdown,
    markdown_to_plain_text,
    normalize_markdown_input,
    truncate_plain_text_for_edit,
)


def format_process_report(report: dict[str, Any]) -> str:
    """Normalize one runtime report to owner-facing markdown."""
    if "error" in report:
        return f"❌ **Ошибка:** {str(report['error']).strip()}"

    raw_report = str(report.get("report") or "").strip()
    formatted = (
        normalize_markdown_input(raw_report)
        if raw_report
        else "✅ **Обработка завершена**"
    )
    artifact_paths = [
        str(path).strip()
        for path in report.get("artifact_paths", [])
        if str(path).strip()
    ]
    if artifact_paths:
        formatted += "\n\n**Файлы:**\n" + "\n".join(
            f"- `{path}`" for path in artifact_paths
        )
    return formatted


def format_error(error: str) -> str:
    """Format an error message as markdown."""
    return f"❌ **Ошибка:** {str(error).strip()}"


def format_empty_daily() -> str:
    """Format the empty-daily owner message as markdown."""
    return (
        "📭 **Нет записей для обработки**\n\n"
        "_Добавьте голосовые сообщения или текст в течение дня_"
    )


__all__ = [
    "TELEGRAM_TEXT_LIMIT",
    "contains_legacy_telegram_html",
    "format_empty_daily",
    "format_error",
    "format_process_report",
    "html_to_markdown",
    "markdown_to_plain_text",
    "normalize_markdown_input",
    "truncate_plain_text_for_edit",
]
