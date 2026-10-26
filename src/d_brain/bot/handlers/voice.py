"""Voice message handler."""

import asyncio
import logging
import re

from aiogram import Bot, Router
from aiogram.types import Message

from d_brain.bot.conversations import is_reply_to_bot, save_answer
from d_brain.bot.handlers.do import process_request
from d_brain.bot.handlers.reminders import (
    create_reminder_from_text,
    format_reminder_confirmation,
)
from d_brain.bot.replies import answer_text
from d_brain.bot.voice_input import (
    VoiceInputError,
    check_size,
    download_voice,
    pick_voice_media,
    transcribe_audio,
)
from d_brain.clock import to_owner
from d_brain.config import Settings, get_settings
from d_brain.services.qmd import QmdService
from d_brain.services.reminders import is_reminder_request
from d_brain.services.session import SessionStore
from d_brain.services.source_links import (
    build_telegram_source_info,
    forward_source_name,
)
from d_brain.services.storage import VaultStorage
from d_brain.services.voice_retry import (
    VOICE_UNRECOGNIZED_STATUS,
    build_unrecognized_body,
)

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


def _entry_type(message: Message) -> str:
    # A forwarded voice message is someone else's recording, not the
    # owner's own capture -- see TRUST_RANK / CONSEQUENTIAL_ACTION_TRUST_LEVELS
    # in compiled_briefings.py. voice.router is registered before
    # forward.router (bot/main.py), so it must make this check itself or
    # a forwarded voice note would inherit the "[voice]" marker and be
    # rated "own" trust by _source_trust_level. Same fix already applied
    # to document.py; see forward_source_name for the shared helper.
    if message.forward_origin is not None:
        return f"[forward from: {forward_source_name(message)}]"
    return "[voice]"


async def _save_unrecognized(
    message: Message,
    settings: Settings,
    storage: VaultStorage,
    file_path: str,
    audio_bytes: bytes,
) -> None:
    """Keep the recording and a marked daily entry when it cannot be read.

    The nightly run retries the transcription (services/voice_retry.py), so
    the audio must not be lost just because Deepgram failed or has no key.
    """
    extension = file_path.rsplit(".", 1)[-1].lower()
    if not re.fullmatch(r"[a-z0-9]{1,5}", extension):
        extension = "ogg"
    timestamp = to_owner(message.date)
    relative = await asyncio.to_thread(
        storage.save_attachment,
        audio_bytes,
        timestamp.date(),
        timestamp,
        extension,
        name_hint=f"voice-{message.message_id}",
    )
    source = build_telegram_source_info(message, language=settings.content_language)
    await asyncio.to_thread(
        storage.append_to_daily,
        build_unrecognized_body(relative),
        timestamp,
        _entry_type(message),
        entry_statuses=[VOICE_UNRECOGNIZED_STATUS],
        source=source,
        refresh_qmd=False,
    )
    logger.warning("Voice message saved as unrecognized audio: %s", relative)
    reply = (
        "Распознавание голоса не настроено (нет DEEPGRAM_API_KEY). "
        "Аудио сохранено в daily, распознаю после настройки."
        if not settings.deepgram_api_key
        else "Не удалось распознать аудио. "
        "Сохранил запись в daily, повторю распознавание ночью."
    )
    # The entry is already on disk: a failed reply must not read as "lost".
    try:
        await answer_text(message, reply, parse_mode=None)
    except Exception:
        logger.exception("Failed to confirm unrecognized voice message")


@router.message(
    lambda m: m.voice is not None
    or m.audio is not None
    or m.video_note is not None
)
async def handle_voice(message: Message, bot: Bot) -> None:
    """Handle voice messages, audio files and video notes (all transcribed alike)."""
    picked = pick_voice_media(message)
    if picked is None or not message.from_user:
        return
    kind, media = picked

    try:
        check_size(media)
    except VoiceInputError as exc:
        await answer_text(message, str(exc), parse_mode=None)
        return

    await message.chat.do(action="typing")

    settings = get_settings()
    storage = VaultStorage(settings.vault_path, settings.content_language)

    try:
        try:
            file_path, audio_bytes = await download_voice(bot, media)
        except VoiceInputError as exc:
            await answer_text(message, str(exc))
            return

        transcript = await transcribe_audio(settings, audio_bytes)

        if not transcript:
            await _save_unrecognized(
                message, settings, storage, file_path, audio_bytes
            )
            return

        if is_reply_to_bot(message):
            await process_request(message, transcript, message.from_user.id)
            return

        reminder_note = ""
        # A forwarded recording is someone else's, so never a reminder.
        if message.forward_origin is None and is_reminder_request(transcript):
            try:
                record = await create_reminder_from_text(message, transcript)
            except Exception:
                # The recording cannot be re-sent, so a failed reminder must
                # not cost the transcript: fall through to the daily entry.
                logger.exception("Failed to save reminder from voice message")
                record = None
                reminder_note = (
                    "\n\nНапоминание не создано: не удалось сохранить, "
                    "подробности в логе"
                )
            else:
                if record is None:
                    # Unlike typed text the recording cannot be re-sent with a
                    # fixed phrase, so keep it in daily and only add the hint.
                    reminder_note = (
                        "\n\nНапоминание не создано: не понял время, "
                        "пример: напомни завтра в 9 позвонить Ивану"
                    )
            if record is not None:
                # The reminder is already saved, so a failed reply or session
                # record is not a lost recording -- log it like the
                # confirmation below instead of reaching the outer handler.
                try:
                    reply = (
                        f"🎤 {transcript}\n\n{format_reminder_confirmation(record)}"
                    )
                    await answer_text(message, reply, parse_mode=None)
                    SessionStore(settings.vault_path).append(
                        message.from_user.id,
                        "voice",
                        text=transcript,
                        kind=kind,
                        duration=media.duration,
                        msg_id=message.message_id,
                        reminder_id=record.id,
                    )
                except Exception:
                    logger.exception("Failed to confirm voice reminder")
                return

        timestamp = to_owner(message.date)
        source = build_telegram_source_info(message, language=settings.content_language)
        # Forwarded voice is not "own" trust; see _entry_type.
        forwarded = message.forward_origin is not None
        entry_type = _entry_type(message)
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
            kind=kind,
            duration=media.duration,
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
            sent = await answer_text(
                message,
                f"🎤 {transcript}\n\n✓ Сохранено{reminder_note}",
                parse_mode=None,
            )
            save_answer(
                message,
                sent,
                settings.vault_path,
                message.from_user.id,
                transcript,
                f"🎤 {transcript}\n\n✓ Сохранено{reminder_note}",
            )
        except Exception:
            logger.exception("Failed to deliver voice transcript confirmation")
        _track_background_task(
            asyncio.create_task(_refresh_qmd_in_background(settings.vault_path))
        )
        logger.info("Voice message saved: %d chars", len(transcript))

    except Exception:
        logger.exception("Error processing voice message")
        await answer_text(
            message,
            "Не удалось сохранить голосовое сообщение, подробности в логе.",
            parse_mode=None,
        )
