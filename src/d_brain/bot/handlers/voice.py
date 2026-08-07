"""Voice message handler."""

import asyncio
import logging

from aiogram import Bot, Router
from aiogram.types import Message

from d_brain.bot.replies import answer_text
from d_brain.config import get_settings
from d_brain.services.qmd import QmdService
from d_brain.services.session import SessionStore
from d_brain.services.source_links import (
    build_telegram_source_info,
    forward_source_name,
)
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
        # A forwarded voice message is someone else's recording, not the
        # owner's own capture -- see TRUST_RANK / CONSEQUENTIAL_ACTION_TRUST_LEVELS
        # in compiled_briefings.py. voice.router is registered before
        # forward.router (bot/main.py), so it must make this check itself or
        # a forwarded voice note would inherit the "[voice]" marker and be
        # rated "own" trust by _source_trust_level. Same fix already applied
        # to document.py; see forward_source_name for the shared helper.
        forwarded = message.forward_origin is not None
        entry_type = (
            f"[forward from: {forward_source_name(message)}]"
            if forwarded
            else "[voice]"
        )
        await asyncio.to_thread(
            storage.append_to_daily,
            transcript,
            timestamp,
            entry_type,
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
            forwarded=forwarded,
        )

        # Guarded on its own so it cannot reach the handler below (code
        # review): the daily entry and the session record are already on
        # disk, so a failure to *show* the transcript is not a failure to
        # save it -- reported as "Ошибка: ...", it told the owner their voice
        # note was lost when it was not, which is the one wrong answer here
        # that invites re-recording over an entry that already exists.
        try:
            await answer_text(
                message,
                f"🎤 {transcript}\n\n✓ Сохранено",
                parse_mode=None,
            )
        except Exception:
            logger.exception("Failed to deliver voice transcript confirmation")
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
