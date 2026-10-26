"""Handler for "напомни ..." reminders and the ``/reminders`` list (аудит, пункт 13).

Parsing and storage live in ``services/reminders.py`` (no aiogram
dependency there, mirroring the rest of the codebase's bot -> services
direction). This module owns the Telegram-facing pieces: the text trigger,
the ``/reminders`` listing, and the periodic ticker that sends due
reminders (started from ``bot/main.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from d_brain.bot.conversations import is_reply_to_bot
from d_brain.bot.replies import answer_text, edit_text, send_text
from d_brain.clock import owner_now, to_owner
from d_brain.config import get_settings
from d_brain.services.reminders import (
    ReminderRecord,
    add_reminder,
    cancel_reminder,
    is_pending_at,
    is_reminder_request,
    list_pending,
    mark_sent,
    parse_reminder,
    postpone_reminder,
)

router = Router(name="reminders")
logger = logging.getLogger(__name__)


# Telegram caps one keyboard at 100 buttons and the list at one message; two
# buttons per reminder keeps both comfortably inside.
_MAX_LISTED = 20
_STALE = "Список устарел, откройте /reminders заново"


def format_reminder_confirmation(record: ReminderRecord) -> str:
    due = record.due_at
    return f"⏰ Напомню {due:%d.%m} в {due:%H:%M}: {record.text}"


async def create_reminder_from_text(
    message: Message, text: str
) -> ReminderRecord | None:
    """Parse and save a reminder; None when no time expression was understood.

    Raises when saving fails -- each caller words that failure itself.
    """
    settings = get_settings()
    now = to_owner(message.date)
    reminder = parse_reminder(text, now)
    if reminder is None:
        return None
    return await asyncio.to_thread(
        add_reminder, settings.vault_path, message.chat.id, reminder, now=now
    )


# No StateFilter(None): after a why/brief/find wait expires and raises
# SkipHandler, this router still sees the stale state from the middleware and
# would drop the reminder. Wait handlers sit earlier in bot/main.py and take
# their own text first; a reply to the bot continues the dialog, as in voice.py.
@router.message(
    lambda m: m.text is not None
    and is_reminder_request(m.text)
    and not is_reply_to_bot(m),
)
async def handle_reminder_text(message: Message) -> None:
    """Parse a "напомни ..." message and save it, or explain why it failed."""
    if not message.text or not message.from_user:
        return

    # Same shape as text.py: an exception that escapes into aiogram's
    # dispatcher is only logged there and the user gets no reply at all.
    try:
        record = await create_reminder_from_text(message, message.text)
    except Exception:
        logger.exception("Failed to save reminder")
        try:
            await answer_text(
                message,
                "Не удалось сохранить напоминание, подробности в логе.",
                parse_mode=None,
            )
        except Exception:
            logger.exception("Failed to deliver reminder failure report")
        return
    if record is None:
        await answer_text(
            message,
            "Не понял время, пример: напомни завтра в 9 позвонить Ивану",
            parse_mode=None,
        )
        return
    await answer_text(message, format_reminder_confirmation(record), parse_mode=None)


def _fingerprint(record: ReminderRecord) -> str:
    """Short digest of the fields a button acts on.

    Postponing changes ``due_at``, so buttons from before the change stop
    matching and cannot hit a record that has since moved.
    """
    raw = f"{record.id}|{record.due_at.isoformat()}|{record.text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _render_list(
    pending: list[ReminderRecord],
) -> tuple[str, InlineKeyboardMarkup | None]:
    if not pending:
        return "Нет активных напоминаний", None

    ordered = sorted(pending, key=lambda r: r.due_at)
    shown = ordered[:_MAX_LISTED]
    lines = ["Ожидающие напоминания:"]
    rows: list[list[InlineKeyboardButton]] = []
    for index, record in enumerate(shown, 1):
        lines.append(f"{index}. {record.due_at:%d.%m %H:%M}: {record.text}")
        # callback_data is capped at 64 bytes: "rem:c:" + uuid (36) + ":" + 8.
        fingerprint = _fingerprint(record)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✖ Отменить {index}",
                    callback_data=f"rem:c:{record.id}:{fingerprint}",
                ),
                InlineKeyboardButton(
                    text=f"⏩ На завтра {index}",
                    callback_data=f"rem:p:{record.id}:{fingerprint}",
                ),
            ]
        )
    if len(ordered) > len(shown):
        lines.append(f"…и ещё {len(ordered) - len(shown)}")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _pending_for_chat(chat_id: int) -> list[ReminderRecord]:
    settings = get_settings()
    return [
        record
        for record in await asyncio.to_thread(list_pending, settings.vault_path)
        if record.chat_id == chat_id
    ]


@router.message(Command("reminders"))
async def handle_reminders_list(message: Message) -> None:
    """List every reminder still pending for this chat."""
    try:
        pending = await _pending_for_chat(message.chat.id)
    except Exception:
        logger.exception("Failed to read reminders")
        try:
            await answer_text(
                message,
                "Не удалось прочитать напоминания, подробности в логе.",
                parse_mode=None,
            )
        except Exception:
            logger.exception("Failed to deliver reminders failure report")
        return
    text, keyboard = _render_list(pending)
    await answer_text(message, text, parse_mode=None, reply_markup=keyboard)


@router.callback_query(F.data.startswith("rem:"))
async def handle_reminder_action(query: CallbackQuery) -> None:
    """Cancel or postpone one reminder from the /reminders list."""
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[1] not in ("c", "p"):
        await query.answer("Некорректная кнопка")
        return
    _, action, reminder_id, fingerprint = parts
    chat = query.message.chat if query.message is not None else None
    if chat is None:
        await query.answer(_STALE)
        return

    settings = get_settings()
    try:
        pending = await _pending_for_chat(chat.id)
        record = next((r for r in pending if r.id == reminder_id), None)
        if record is None or _fingerprint(record) != fingerprint:
            await query.answer(_STALE)
            return

        if action == "c":
            done = await asyncio.to_thread(
                cancel_reminder, settings.vault_path, reminder_id
            )
            result = "Отменено" if done else _STALE
        else:
            moved = await asyncio.to_thread(
                lambda: postpone_reminder(
                    settings.vault_path, reminder_id, now=_now()
                )
            )
            result = (
                f"Перенесено на {moved.due_at:%d.%m %H:%M}"
                if moved is not None
                else _STALE
            )
        pending = await _pending_for_chat(chat.id)
    except Exception:
        logger.exception("Failed to apply reminder action %s", action)
        await query.answer("Не удалось выполнить действие, подробности в логе.")
        return

    await query.answer(result)
    # The action is already done; a failed redraw is only worth a log line.
    try:
        text, keyboard = _render_list(pending)
        await edit_text(query.message, text, parse_mode=None, reply_markup=keyboard)  # type: ignore[arg-type]
    except Exception:
        logger.exception("Failed to redraw reminders list")


def _now() -> datetime:
    return owner_now()


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
                # The list was read a moment ago; the owner may have cancelled
                # or postponed this one since, and that must win over sending.
                if not await asyncio.to_thread(
                    is_pending_at, vault_path, record.id, record.due_at
                ):
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
                await asyncio.to_thread(
                    mark_sent, vault_path, record.id, now=now, due_at=record.due_at
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder ticker tick failed")

        if once:
            return
        await asyncio.sleep(interval)
