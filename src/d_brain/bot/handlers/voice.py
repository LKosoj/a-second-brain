"""Voice message handler."""

import asyncio
import logging

from aiogram import Bot, Router
from aiogram.types import Message

from d_brain.bot.replies import answer_text
from d_brain.config import get_settings
from d_brain.services.qmd import QmdService
from d_brain.services.session import SessionStore
from d_brain.services.source_links import build_telegram_source_info
from d_brain.services.storage import VaultStorage
from d_brain.services.transcription import DeepgramTranscriber

router = Router(name="voice")
logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task[None]] = set()


def _track_background_task(task: asyncio.Task[None]) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _refresh_qmd_in_background(vault_path) -> None:  # type: ignore[no-untyped-def]
    try:
        await asyncio.to_thread(QmdService(vault_path).refresh_after_searchable_write)
    except Exception as exc:
        logger.warning("Voice qmd refresh skipped: %s", exc)


@router.message(lambda m: m.voice is not None)
async def handle_voice(message: Message, bot: Bot) -> None:
    """Handle voice messages."""
    if not message.voice or not message.from_user:
        return

    await message.chat.do(action="typing")

    settings = get_settings()
    storage = VaultStorage(settings.vault_path, settings.content_language)
    transcriber = DeepgramTranscriber(
        settings.deepgram_api_key,
        settings.content_language,
    )

    try:
        file = await asyncio.wait_for(
            bot.get_file(message.voice.file_id), timeout=60
        )
        if not file.file_path:
            await answer_text(message, "Не удалось скачать голосовое сообщение.")
            return

        file_bytes = await asyncio.wait_for(
            bot.download_file(file.file_path), timeout=120
        )
        if not file_bytes:
            await answer_text(message, "Не удалось скачать голосовое сообщение.")
            return

        audio_bytes = file_bytes.read()
        transcript = await transcriber.transcribe(audio_bytes)

        if not transcript:
            await answer_text(message, "Не удалось распознать аудио.")
            return

        timestamp = message.date.astimezone()
        source = build_telegram_source_info(message, language=settings.content_language)
        await asyncio.to_thread(
            storage.append_to_daily,
            transcript,
            timestamp,
            "[voice]",
            source=source,
            refresh_qmd=False,
        )

        # Log to session
        session = SessionStore(settings.vault_path)
        session.append(
            message.from_user.id,
            "voice",
            text=transcript,
            duration=message.voice.duration,
            msg_id=message.message_id,
            source_ref=source.ref,
            source_url=source.url,
        )

        await answer_text(
            message,
            f"🎤 {transcript}\n\n✓ Сохранено",
            parse_mode=None,
        )
        _track_background_task(
            asyncio.create_task(_refresh_qmd_in_background(settings.vault_path))
        )
        logger.info("Voice message saved: %d chars", len(transcript))

    except Exception as e:
        logger.exception("Error processing voice message")
        await answer_text(
            message,
            f"Ошибка: {e}",
            parse_mode=None,
        )
