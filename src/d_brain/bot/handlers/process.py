"""Process command handler."""

import asyncio
import logging
from datetime import date

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from d_brain.bot.formatters import format_empty_daily, format_process_report
from d_brain.bot.progress import wait_for_task_with_progress
from d_brain.bot.replies import (
    answer_rich_text,
    answer_text,
    edit_rich_text,
    edit_text,
)
from d_brain.config import get_settings
from d_brain.services.processor import (
    INTERACTIVE_MODE,
    SCHEDULED_MODE,
    CliProcessor,
)

router = Router(name="process")
logger = logging.getLogger(__name__)


def _build_processor() -> CliProcessor:
    """Build processor from runtime settings."""
    settings = get_settings()
    return CliProcessor(
        settings.vault_path,
        settings.todoist_api_key,
        settings.ai_cli,
        settings.owner_full_name,
        settings.content_language,
        getattr(settings, "openai_api_key", ""),
        getattr(settings, "openai_base_url", ""),
        getattr(settings, "openai_model", ""),
        getattr(settings, "recall_planner_disable_thinking", False),
    )


async def _run_process_command(
    message: Message,
    *,
    mode: str,
    initial_status: str,
    progress_prefix: str,
    actor_id: int | str | None = None,
) -> None:
    """Run a processing command with progress updates."""
    if actor_id is None:
        actor_id = message.from_user.id if message.from_user else "unknown"
    logger.info("Process command triggered by user %s in mode=%s", actor_id, mode)

    status_msg = await answer_text(message, initial_status)
    processor = _build_processor()

    async def process_with_progress() -> dict[str, object]:
        task = asyncio.create_task(
            asyncio.to_thread(
                processor.run_scheduled_cycle
                if mode == SCHEDULED_MODE
                else processor.process_daily,
                date.today(),
                **({} if mode == SCHEDULED_MODE else {"mode": mode}),
            )
        )

        async def update_progress(elapsed_seconds: float) -> None:
            elapsed = int(elapsed_seconds)
            try:
                await edit_text(
                    status_msg,
                    f"{progress_prefix} ({elapsed // 60}m {elapsed % 60}s)",
                )
            except Exception:
                pass  # Ignore edit errors

        return await wait_for_task_with_progress(
            task,
            interval_seconds=30,
            on_progress=update_progress,
        )

    try:
        report = await process_with_progress()
    except Exception:
        logger.exception("Processing task failed")
        report = {"error": "Processing task crashed unexpectedly"}

    # Format and send report
    formatted = (
        format_empty_daily()
        if report.get("empty_daily")
        else format_process_report(report)
    )
    # Both sends below are nested so their *fallbacks* are covered too (code
    # review). The run is over by the time either one is reached -- for
    # /process_full the vault and Todoist writes have already committed --
    # and nothing outside this function catches anything, so a fallback that
    # raised escaped into the dispatcher: the owner was left staring at the
    # "⏳" status forever, with a finished report that never arrived and not
    # even a log line saying delivery was what failed.
    if "error" in report:
        try:
            try:
                await edit_text(status_msg, formatted, parse_mode=None)
            except Exception:
                await answer_text(message, formatted, parse_mode=None)
        except Exception:
            logger.exception("Failed to deliver process error report")
        return

    try:
        try:
            await edit_rich_text(status_msg, formatted)
        except TelegramBadRequest:
            await answer_rich_text(message, formatted)
    except Exception:
        logger.exception("Failed to deliver rich process report")


async def run_interactive_process(
    message: Message, *, actor_id: int | str | None = None
) -> None:
    """Run the quick preview flow from command or menu."""
    await _run_process_command(
        message,
        mode=INTERACTIVE_MODE,
        initial_status="⏳ Быстрый разбор... (без записи в vault/Todoist)",
        progress_prefix="⏳ Быстрый разбор...",
        actor_id=actor_id,
    )


async def run_full_process(
    message: Message, *, actor_id: int | str | None = None
) -> None:
    """Run the full write-heavy flow from command or menu."""
    await _run_process_command(
        message,
        mode=SCHEDULED_MODE,
        initial_status="⏳ Полная обработка... (с записью в vault/Todoist)",
        progress_prefix="⏳ Полная обработка...",
        actor_id=actor_id,
    )


@router.message(Command("process"))
async def cmd_process(message: Message) -> None:
    """Handle /process command - run quick interactive preview."""
    await run_interactive_process(message)


@router.message(Command("process_full"))
async def cmd_process_full(message: Message) -> None:
    """Handle /process_full command - run the full write-heavy pipeline now."""
    await run_full_process(message)
