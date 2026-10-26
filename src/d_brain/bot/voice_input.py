"""Shared voice input: pick the media, check its size, download, transcribe.

Used by the plain voice handler and by /do, so both accept the same media
kinds and refuse the same files.
"""

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import Message

from d_brain.config import Settings
from d_brain.services.transcription import DeepgramTranscriber

logger = logging.getLogger(__name__)

# Bot API refuses to hand files above this size to a bot ("file is too big").
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class VoiceInputError(Exception):
    """A voice file that cannot be used; the text is meant for the user."""


def pick_voice_media(message: Message) -> tuple[str, Any] | None:
    """Return (kind, media) for a voice, audio file or video note, else None."""
    # getattr: not every message-like object carries all three fields.
    for kind in ("voice", "audio", "video_note"):
        media = getattr(message, kind, None)
        if media:
            return kind, media
    return None


def check_size(media: Any) -> None:
    """Refuse files the Bot API would not let us download anyway."""
    file_size = getattr(media, "file_size", None)
    if file_size is not None and file_size > MAX_DOWNLOAD_BYTES:
        raise VoiceInputError(
            "Файл больше 20 МБ: Telegram не позволяет боту его скачать."
        )


async def download_voice(bot: Bot, media: Any) -> tuple[str, bytes]:
    """Download the file; return (telegram file path, bytes).

    Timeouts and Telegram errors propagate to the caller.
    """
    check_size(media)
    file = await asyncio.wait_for(bot.get_file(media.file_id), timeout=60)
    if not file.file_path:
        raise VoiceInputError("Не удалось скачать голосовое сообщение.")
    file_bytes = await asyncio.wait_for(
        bot.download_file(file.file_path), timeout=120
    )
    if not file_bytes:
        raise VoiceInputError("Не удалось скачать голосовое сообщение.")
    return file.file_path, file_bytes.read()


async def transcribe_audio(settings: Settings, audio_bytes: bytes) -> str:
    """Return the transcript, or "" when there is no key or Deepgram failed."""
    if not settings.deepgram_api_key:
        return ""
    try:
        transcriber = DeepgramTranscriber(
            settings.deepgram_api_key,
            settings.content_language,
        )
        return (await transcriber.transcribe(audio_bytes) or "").strip()
    except Exception:
        logger.exception("Voice transcription failed")
        return ""
