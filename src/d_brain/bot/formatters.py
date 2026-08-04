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
    if raw_report:
        return normalize_markdown_input(raw_report)

    return "✅ **Обработка завершена**"


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
