"""Editing a saved message (``edited_message``) and ``/undo`` for the last entry."""

import asyncio
import logging
from datetime import date, datetime
from functools import partial
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from d_brain.bot.replies import answer_text, edit_text
from d_brain.clock import owner_now, to_owner
from d_brain.config import get_settings
from d_brain.services.secrets import scrub_secrets
from d_brain.services.session import SessionStore
from d_brain.services.source_links import SourceInfo, build_telegram_source_info
from d_brain.services.storage import VaultStorage

router = Router(name="edit")
logger = logging.getLogger(__name__)

_EDITABLE_TYPES = {"text", "photo", "edit"}
_STALE = "Кнопка устарела"
# The handlers write daily first and the session record a moment later (text
# only after link enrichment), so a quick edit can outrun the record. Wait for
# it a few seconds; polling by attempts keeps the total bounded and testable.
_WAIT_ATTEMPTS = 10
_WAIT_STEP = 0.5


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _today() -> datetime:
    return owner_now()


def _is_editable(message: Message) -> bool:
    """Plain text or a photo caption; "/..." edits are not entries."""
    if message.text:
        return not message.text.startswith("/")
    return bool(message.photo and message.caption)


async def _reply(message: Message, text: str) -> None:
    """Confirmation after the work is done: a delivery failure is only logged."""
    try:
        await answer_text(message, text, parse_mode=None)
    except Exception:
        logger.exception("Failed to deliver edit report")


def _apply_correction(
    storage: VaultStorage,
    original: datetime | None,
    source_ref: str,
    content: str,
    *,
    is_caption: bool,
    caption_added: bool = False,
) -> None:
    """Add (or refresh) a separate correction entry for an entry we must not touch.

    The type stays "[text]" on purpose: the trust rating of compiled pages only
    knows the usual markers, and a correction is still the owner's own word.
    """
    if original is not None:
        intro = f"Исправление записи от {original:%d.%m %H:%M}:"
    else:
        intro = "Исправление записи (исходная запись не найдена):"
    label = ""
    if is_caption:
        label = "Подпись добавлена: " if caption_added else "Подпись изменена: "
    body = f"{intro}\n\n{label}{content}"
    correction_ref = f"{source_ref}:edit"
    now = _today()
    # A second edit refreshes the correction in place while the nightly pass
    # has not read it yet, instead of piling up one entry per edit.
    status = storage.edit_daily_entry(now.date(), correction_ref, body)
    if status in ("processed", "missing"):
        storage.append_to_daily(
            body,
            now,
            "[text]",
            source=SourceInfo(kind="telegram", ref=correction_ref),
        )


def _lookup(
    session: SessionStore, user_id: int, msg_id: int, source_ref: str, day: date
) -> tuple[dict[str, Any] | None, bool]:
    """Saved record of the message and whether /undo already removed its entry."""
    return (
        session.find_by_msg_id(user_id, msg_id, day),
        session.was_undone(user_id, source_ref, day),
    )


@router.edited_message(_is_editable)
async def handle_edited(message: Message) -> None:
    """Apply an edit of a message the bot has already saved to daily."""
    if not message.from_user:
        return
    settings = get_settings()
    user_id = message.from_user.id
    reply = ""
    try:
        session = await asyncio.to_thread(SessionStore, settings.vault_path)
        source_ref = build_telegram_source_info(message).ref
        day = to_owner(message.date).date()
        record, undone = await asyncio.to_thread(
            _lookup, session, user_id, message.message_id, source_ref, day
        )
        for _ in range(_WAIT_ATTEMPTS if record is None else 0):
            await _sleep(_WAIT_STEP)
            record, undone = await asyncio.to_thread(
                _lookup, session, user_id, message.message_id, source_ref, day
            )
            if record is not None:
                break
        if record is None:
            storage = VaultStorage(settings.vault_path, settings.content_language)
            daily = await asyncio.to_thread(storage.read_daily, day)
            if f"`{source_ref}`" in daily:
                # Saved to daily, but the session note never showed up: keep
                # the owner's fix as a separate correction, the entry itself
                # may be mid-write or unreadable for us.
                raw = (message.caption if message.photo else message.text) or ""
                content, _ = scrub_secrets(raw)
                await asyncio.to_thread(
                    partial(
                        _apply_correction,
                        storage,
                        to_owner(message.date),
                        source_ref,
                        content,
                        is_caption=bool(message.photo),
                    )
                )
                await _reply(
                    message,
                    "✓ Запись ещё сохранялась, "
                    "исправление добавлено отдельной записью",
                )
                return
            # A question to the bot, /do, a reminder: nothing was saved, and
            # answering every such edit would only be noise. An edit that
            # outruns even the daily write (slow link enrichment) cannot be
            # told apart from these, so it stays dropped; the owner can resend.
            logger.debug("Edit of message %s has no saved entry", message.message_id)
            return
        if undone:
            # Applying it would bring back, as a "correction", the text the
            # owner has just taken back.
            await _reply(message, "Запись удалена через /undo, правку не применяю")
            return
        if record["type"] not in _EDITABLE_TYPES:
            await _reply(message, "Правку этого типа записи не применяю")
            return

        is_caption = bool(message.photo)
        raw = (message.caption if is_caption else message.text) or ""
        content, _ = scrub_secrets(raw)
        storage = VaultStorage(settings.vault_path, settings.content_language)
        status = await asyncio.to_thread(
            storage.edit_daily_entry,
            day,
            source_ref,
            content,
            old_caption=(record.get("caption") or "") if is_caption else None,
        )
        if status == "unchanged":
            return
        if status == "replaced":
            reply = "✓ Запись обновлена"
        else:
            original = (
                to_owner(message.date)
                if status in ("processed", "no_caption")
                else None
            )
            await asyncio.to_thread(
                partial(
                    _apply_correction,
                    storage,
                    original,
                    source_ref,
                    content,
                    is_caption=is_caption,
                    caption_added=status == "no_caption",
                )
            )
            reply = {
                "processed": "✓ Запись уже обработана, "
                "исправление добавлено отдельной записью",
                "no_caption": "✓ Подпись добавлена отдельной записью",
            }.get(
                status,
                "✓ Исправление добавлено отдельной записью (исходной записи не нашёл)",
            )
    except Exception:
        logger.exception("Error applying message edit")
        await _reply(message, "Не удалось применить правку, подробности в логе.")
        return
    # The edit is already in daily: a failed session note must not turn into
    # "could not apply" for the owner.
    try:
        await asyncio.to_thread(
            session.append,
            user_id,
            "edit",
            msg_id=message.message_id,
            source_ref=source_ref,
            **({"caption": content} if is_caption else {"text": content}),
        )
    except Exception:
        logger.exception("Failed to log the edit to the session")
    await _reply(message, reply)


def _undo_day(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


@router.message(Command("undo"))
async def cmd_undo(message: Message) -> None:
    """Offer to remove the newest entry saved today, after a confirmation."""
    settings = get_settings()
    storage = VaultStorage(settings.vault_path, settings.content_language)
    today = _today().date()
    try:
        summary = await asyncio.to_thread(storage.last_undoable_entry, today)
    except Exception:
        logger.exception("Failed to read the last entry for /undo")
        await answer_text(
            message, "Не удалось прочитать записи, подробности в логе.", parse_mode=None
        )
        return
    if summary is None:
        await answer_text(message, "Сегодня нечего отменять", parse_mode=None)
        return
    if summary.processed:
        await answer_text(
            message,
            "Эта запись уже обработана ночным проходом, отменить её нельзя",
            parse_mode=None,
        )
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Удалить",
                    callback_data=f"undo:y:{today:%Y%m%d}:{summary.digest}",
                ),
                InlineKeyboardButton(text="Оставить", callback_data="undo:n"),
            ]
        ]
    )
    await answer_text(
        message,
        f"Удалить запись {summary.time} [{summary.entry_type}]?\n\n"
        f"{summary.preview}\n\nФайлы вложений останутся.",
        parse_mode=None,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("undo:"))
async def cb_undo(callback: CallbackQuery) -> None:
    """Confirm or cancel the removal offered by /undo."""
    parts = (callback.data or "").split(":")
    if parts[1:2] == ["n"] and len(parts) == 2:
        result = "Оставил запись"
    else:
        day = _undo_day(parts[2]) if len(parts) == 4 and parts[1] == "y" else None
        if day is None:
            await callback.answer(_STALE)
            return
        settings = get_settings()
        digest = parts[3]
        try:
            storage = VaultStorage(settings.vault_path, settings.content_language)
            # Read before the delete, while the entry is still there; /undo
            # remembers the ref so a later edit of that message is ignored.
            source_ref = await asyncio.to_thread(
                storage.entry_ref_by_digest, day, digest
            )
            status = await asyncio.to_thread(storage.delete_daily_entry, day, digest)
            if status == "deleted":
                session = await asyncio.to_thread(SessionStore, settings.vault_path)
                await asyncio.to_thread(
                    session.append,
                    callback.from_user.id,
                    "undo",
                    day=day.isoformat(),
                    digest=digest,
                    source_ref=source_ref,
                )
        except Exception:
            logger.exception("Failed to undo the last entry")
            await callback.answer("Не удалось удалить, подробности в логе.")
            return
        result = {
            "deleted": "✓ Запись удалена (файлы вложений остались)",
            "processed": "Запись уже обработана ночным проходом, удалять не стал",
        }.get(status, "Запись уже изменилась или удалена")
    await callback.answer()
    # The decision is already applied; a failed redraw is only worth a log line.
    try:
        if callback.message is not None:
            await edit_text(callback.message, result, parse_mode=None)  # type: ignore[arg-type]
    except Exception:
        logger.exception("Failed to redraw the /undo prompt")
