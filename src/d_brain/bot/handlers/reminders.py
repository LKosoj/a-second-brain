"""Handler for "напомни ..." reminders and the ``/reminders`` list (аудит, пункт 13).

Parsing and storage live in ``services/reminders.py`` (no aiogram
dependency there, mirroring the rest of the codebase's bot -> services
direction). This module owns the Telegram-facing pieces: the text trigger,
the ``/reminders`` listing, and the periodic ticker that sends due
reminders (started from ``bot/main.py``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import Message

from d_brain.bot.replies import answer_text, send_text
from d_brain.config import get_settings
from d_brain.services.reminders import (
    ReminderRecord,
    add_reminder,
    is_reminder_request,
    list_pending,
    mark_sent,
    parse_reminder,
)

router = Router(name="reminders")
logger = logging.getLogger(__name__)


def _format_confirmation(record: ReminderRecord) -> str:
    due = record.due_at
    return f"⏰ Напомню {due:%d.%m} в {due:%H:%M}: {record.text}"


@router.message(
    lambda m: m.text is not None and is_reminder_request(m.text),
    StateFilter(None),
)
async def handle_reminder_text(message: Message) -> None:
    """Parse a "напомни ..." message and save it, or explain why it failed."""
    if not message.text or not message.from_user:
        return

    settings = get_settings()
    now = message.date.astimezone()
    reminder = parse_reminder(message.text, now)
    if reminder is None:
        await answer_text(
            message,
            "Не понял время, пример: напомни завтра в 9 позвонить Ивану",
            parse_mode=None,
        )
        return

    # Same shape as text.py: an exception that escapes into aiogram's
    # dispatcher is only logged there and the user gets no reply at all.
    try:
        record = await asyncio.to_thread(
            add_reminder, settings.vault_path, message.chat.id, reminder, now=now
        )
    except Exception as exc:
        logger.exception("Failed to save reminder")
        try:
            await answer_text(message, f"Ошибка: {exc}", parse_mode=None)
        except Exception:
            logger.exception("Failed to deliver reminder failure report")
        return
    await answer_text(message, _format_confirmation(record), parse_mode=None)


@router.message(Command("reminders"))
async def handle_reminders_list(message: Message) -> None:
    """List every reminder still pending for this chat."""
    settings = get_settings()
    try:
        pending = [
            record
            for record in await asyncio.to_thread(list_pending, settings.vault_path)
            if record.chat_id == message.chat.id
        ]
    except Exception as exc:
        logger.exception("Failed to read reminders")
        try:
            await answer_text(message, f"Ошибка: {exc}", parse_mode=None)
        except Exception:
            logger.exception("Failed to deliver reminders failure report")
        return
    if not pending:
        await answer_text(message, "Нет активных напоминаний", parse_mode=None)
        return

    lines = ["Ожидающие напоминания:"]
    for record in sorted(pending, key=lambda r: r.due_at):
        lines.append(f"- {record.due_at:%d.%m %H:%M}: {record.text}")
    await answer_text(message, "\n".join(lines), parse_mode=None)


def _now() -> datetime:
    return datetime.now().astimezone()


async def run_reminder_ticker(
    bot: Bot,
    vault_path: Path | str,
    *,
    now_fn: Callable[[], datetime] = _now,
    interval: float = 60.0,
    once: bool = False,
) -> None:
    """Send every due reminder once per tick and mark it sent.

    A failed send is logged and left pending: ``mark_sent`` only runs after
    ``send_text`` succeeds, so the next tick retries it instead of the
    ticker itself dying. The whole tick body is also guarded: a corrupted
    ``reminders.jsonl`` (e.g. a ``UnicodeDecodeError`` from ``_read_all``'s
    unguarded ``read_text``) must not kill this background task forever --
    it is logged and retried on the next tick too. ``asyncio.CancelledError``
    is deliberately not caught here, so shutdown (``task.cancel()`` in
    ``bot/main.py``) still stops the loop.
    """
    while True:
        try:
            now = now_fn()
            for record in await asyncio.to_thread(list_pending, vault_path):
                if record.due_at > now:
                    continue
                try:
                    await send_text(
                        bot,
                        chat_id=record.chat_id,
                        text=f"⏰ Напоминание: {record.text}",
                        parse_mode=None,
                    )
                except Exception:
                    logger.exception("Failed to send reminder %s", record.id)
                    continue
                await asyncio.to_thread(mark_sent, vault_path, record.id, now=now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder ticker tick failed")

        if once:
            return
        await asyncio.sleep(interval)
