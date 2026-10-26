"""Video, location, contact and the stub for everything else the bot cannot read.

Registered last in ``bot/main.py``: text, voice, photo, documents and forwards
are claimed by their own routers first, so only the leftovers land here.
"""

import asyncio
import logging

from aiogram import Router
from aiogram.types import Message

from d_brain.bot.replies import answer_text
from d_brain.clock import to_owner
from d_brain.config import get_settings
from d_brain.services.secrets import scrub_secrets
from d_brain.services.session import SessionStore
from d_brain.services.source_links import build_telegram_source_info
from d_brain.services.storage import VaultStorage

router = Router(name="fallback")
logger = logging.getLogger(__name__)


def _video_entry(message: Message) -> str:
    video = message.video
    assert video is not None
    caption, _ = scrub_secrets(message.caption or "")
    size_mb = (video.file_size or 0) / (1024 * 1024)
    meta = (
        f"Видео: {video.duration} с, {video.file_name or 'без имени'}, "
        f"{size_mb:.1f} МБ. Файл не сохранён."
    )
    return f"{caption}\n\n{meta}" if caption else meta


def _location_entry(message: Message) -> str:
    location = message.location
    assert location is not None
    lat, lon = location.latitude, location.longitude
    lines = [
        f"Геолокация: {lat:.6f}, {lon:.6f}",
        f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}",
    ]
    if message.venue is not None:
        lines.append(f"Место: {message.venue.title}, {message.venue.address}")
    return "\n".join(lines)


def _contact_entry(message: Message) -> str:
    contact = message.contact
    assert contact is not None
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
    lines = [f"Контакт: {name}", f"Телефон: {contact.phone_number}"]
    if contact.user_id:
        lines.append(f"Telegram ID: {contact.user_id}")
    return "\n".join(lines)


async def _save_entry(message: Message, kind: str, text: str) -> None:
    """Write one entry to daily and the session log, then confirm."""
    if not message.from_user:
        return
    settings = get_settings()
    try:
        storage = VaultStorage(settings.vault_path, settings.content_language)
        source = build_telegram_source_info(
            message, language=settings.content_language
        )
        await asyncio.to_thread(
            storage.append_to_daily,
            text,
            to_owner(message.date),
            f"[{kind}]",
            source=source,
        )
        SessionStore(settings.vault_path).append(
            message.from_user.id,
            kind,
            text=text,
            msg_id=message.message_id,
            source_ref=source.ref,
            source_url=source.url,
        )
    except Exception:
        logger.exception("Error processing %s message", kind)
        try:
            await answer_text(
                message,
                "Не удалось сохранить сообщение, подробности в логе.",
                parse_mode=None,
            )
        except Exception:
            logger.exception("Failed to deliver %s failure report", kind)
        return

    # Separate from the save above: a failed confirmation is not a lost entry.
    try:
        await answer_text(message, "✓ Сохранено", parse_mode=None)
    except Exception:
        logger.exception("Failed to deliver %s confirmation", kind)


@router.message(lambda m: m.video is not None)
async def handle_video(message: Message) -> None:
    """Keep video metadata and caption; the file itself is not downloaded."""
    await _save_entry(message, "video", _video_entry(message))


@router.message(lambda m: m.location is not None)
async def handle_location(message: Message) -> None:
    """Keep coordinates (and the venue, which also carries a location)."""
    await _save_entry(message, "location", _location_entry(message))


@router.message(lambda m: m.contact is not None)
async def handle_contact(message: Message) -> None:
    await _save_entry(message, "contact", _contact_entry(message))


@router.message()
async def handle_unsupported(message: Message) -> None:
    """Say so out loud instead of leaving stickers, polls etc. unanswered."""
    try:
        await answer_text(
            message, "Этот тип сообщения не поддерживается", parse_mode=None
        )
    except Exception:
        logger.exception("Failed to deliver unsupported-type reply")
