"""Handler for /do command - arbitrary AI requests."""

import asyncio
import logging
from functools import partial
from typing import cast

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from d_brain.bot.conversations import reply_context, save_answer
from d_brain.bot.dashboard import render_home, render_input
from d_brain.bot.formatters import format_process_report, inline_artifact_image_paths
from d_brain.bot.handlers.photo import (
    PhotoEntry,
    save_album_for_request,
    save_single_photo,
)
from d_brain.bot.progress import wait_for_task_with_progress
from d_brain.bot.replies import answer_files, answer_rich_text, answer_text, edit_text
from d_brain.bot.states import DoCommandState
from d_brain.bot.voice_input import (
    VoiceInputError,
    download_voice,
    pick_voice_media,
    transcribe_audio,
)
from d_brain.config import get_settings
from d_brain.services.processor import CliProcessor

router = Router(name="do")
logger = logging.getLogger(__name__)

# Commands actually registered via Command(...) across bot/handlers/*.py
# (code review): matching any "/word" shape also caught things like
# "/brief" and "/cancel", which are not real commands, so they were dropped
# here with nowhere else to go -- the user was left stuck in /do. Only the
# commands listed here should fall through to their own router instead of
# being treated as a /do prompt.
_KNOWN_COMMANDS = (
    "start",
    "help",
    "stats",
    "status",
    "do",
    "menu",
    "files",
    "process",
    "process_full",
    "why",
    "reminders",
)
# "/" + one of the known commands, optionally "@botname", then end-of-text
# or whitespace -- e.g. "/why", "/why@bot", "/why foo" all match. A prompt
# that merely starts with "/" without being one of these, e.g. "/etc/hosts"
# or "/brief", does not match and still reaches handle_do_input below (no
# other router claims it either).
_BOT_COMMAND_RE = rf"^/(?:{'|'.join(_KNOWN_COMMANDS)})(@[A-Za-z0-9_]+)?(\s|$)"

# Request for a photo or document sent to /do without the owner's own caption.
_PHOTO_REQUEST = "Разбери это изображение."
_ALBUM_REQUEST = "Разбери эти изображения."
_DOCUMENT_REQUEST = "Разбери этот документ."


async def start_do_flow(
    message: Message, state: FSMContext, *, menu_message_id: int | None = None
) -> None:
    """Prompt the user for the next /do input."""
    await state.set_state(DoCommandState.waiting_for_input)
    if menu_message_id is not None:
        await state.update_data(menu_message_id=menu_message_id)
        await render_input(
            cast(Bot, message.bot), chat_id=message.chat.id, message_id=menu_message_id,
            screen="do_input",
            text=(
                "**Что сделать?**\n\n"
                "Отправь вопрос или поручение голосом или текстом, "
                "или фото/файл с подписью.\n"
                "Для отмены нажми «Назад» или отправь `-`."
            ),
        )
        return
    await answer_text(
        message,
        "🎯 **Что сделать?**\n\n"
        "Отправь голосовое или текстовое сообщение с запросом "
        "или фото/файл с подписью.\n"
        "Повтори `/do` или отправь `-`, чтобы отменить."
    )


@router.message(Command("do"))
async def cmd_do(message: Message, command: CommandObject, state: FSMContext) -> None:
    """Handle /do command."""
    user_id = message.from_user.id if message.from_user else 0
    current_state = await state.get_state()

    if (
        not command.args
        and current_state == DoCommandState.waiting_for_input.state
    ):
        await state.clear()
        await answer_text(message, "❌ **/do отменён**")
        return

    # Check for inline text: /do move overdue tasks
    if command.args:
        await state.clear()
        await process_request(message, command.args, user_id)
        return

    # Otherwise, wait for next message
    await start_do_flow(message, state)


@router.message(DoCommandState.waiting_for_input, ~F.text.regexp(_BOT_COMMAND_RE))
async def handle_do_input(message: Message, bot: Bot, state: FSMContext) -> None:
    """Handle voice, text, photo or document input after /do command.

    Excludes text shaped like one of the actually registered commands
    (_BOT_COMMAND_RE / _KNOWN_COMMANDS) so a command typed while waiting for
    /do input -- e.g. ``/why``, ``/why@bot``, ``/why foo`` -- is not
    swallowed as a literal AI prompt and instead falls through to its own
    router (why.py is registered after do.py in bot/main.py). A prompt that
    is not one of those commands, even if it starts with "/", e.g.
    ``/etc/hosts`` or ``/brief`` (not a registered command), does not match
    and still reaches this handler -- no other router would claim it either,
    so matching on command *shape* alone (any "/word") silently dropped it
    instead. Voice messages have ``text is None``, which the magic filter
    resolves to ``False`` here, so they still reach this handler unaffected.
    """
    data = await state.get_data()
    await state.clear()  # Clear state immediately
    menu_message_id = data.get("menu_message_id")
    if isinstance(menu_message_id, int):
        await render_home(
            bot, chat_id=message.chat.id, vault_path=get_settings().vault_path,
            user_id=message.from_user.id if message.from_user else message.chat.id,
            preferred_message_id=menu_message_id,
        )

    prompt = None

    # Handle voice input (voice, audio file, video note)
    picked = pick_voice_media(message)
    if picked is not None:
        _kind, media = picked
        await message.chat.do(action="typing")
        settings = get_settings()

        try:
            _path, audio_bytes = await download_voice(bot, media)
            prompt = await transcribe_audio(settings, audio_bytes)
        except VoiceInputError as exc:
            await answer_text(message, f"❌ {exc}", parse_mode=None)
            return
        except Exception:
            logger.exception("Failed to transcribe voice for /do")
            await answer_text(
                message,
                "❌ Не удалось транскрибировать голосовое, подробности в логе.",
                parse_mode=None,
            )
            return

        if not prompt:
            await answer_text(
                message,
                "❌ Не удалось распознать речь"
                if settings.deepgram_api_key
                else "❌ Распознавание голоса не настроено (нет DEEPGRAM_API_KEY)",
                parse_mode=None,
            )
            return

        # Echo transcription to user
        await answer_text(
            message,
            f"🎤 {prompt}",
            parse_mode=None,
        )

    # Handle text input
    elif message.text:
        prompt = message.text.strip()
        if prompt == "-":
            await answer_text(message, "❌ **/do отменён**")
            return

    elif message.photo or message.document:
        # A forwarded file's caption is someone else's words, not the owner's
        # request (the same rule as document captions outside /do).
        caption = "" if message.forward_origin else (message.caption or "").strip()
        if message.photo and message.media_group_id:
            await save_album_for_request(
                message, bot, partial(_run_album_request, message)
            )
        elif message.photo:
            await _process_photo_request(message, bot, caption or _PHOTO_REQUEST)
        else:
            # Imported here: document.py imports process_request from this module.
            from d_brain.bot.handlers.document import receive_document

            await receive_document(
                message, bot, request=caption or _DOCUMENT_REQUEST
            )
        return

    else:
        await answer_text(
            message, "❌ Отправь текст, голосовое, фото или документ"
        )
        return

    user_id = message.from_user.id if message.from_user else 0
    await process_request(message, prompt, user_id)


async def _process_photo_request(message: Message, bot: Bot, request: str) -> None:
    """Save the photo as a usual capture, then run the request on that file."""
    await message.chat.do(action="typing")
    try:
        entry = await save_single_photo(message, bot)
    except Exception:
        logger.exception("Failed to save photo for /do")
        await answer_text(
            message,
            "❌ Не удалось сохранить фото, подробности в логе.",
            parse_mode=None,
        )
        return
    settings = get_settings()
    prompt = f"{request}\n\nImage: {settings.vault_path / entry.relative_path}\n"
    user_id = message.from_user.id if message.from_user else 0
    await process_request(message, prompt, user_id)


async def _run_album_request(message: Message, entries: list[PhotoEntry]) -> None:
    """Run the album caption as one request over every saved photo."""
    # Fail closed like the album's daily entry: one forwarded photo makes the
    # caption someone else's words, not the owner's request.
    captions = [(entry.raw_caption or "").strip() for entry in entries]
    if any(entry.forwarded for entry in entries):
        captions = []
    request = next((text for text in captions if text), _ALBUM_REQUEST)
    vault_path = get_settings().vault_path
    images = "\n".join(f"- {vault_path / entry.relative_path}" for entry in entries)
    user_id = message.from_user.id if message.from_user else 0
    await process_request(message, f"{request}\n\nImages:\n{images}\n", user_id)


async def process_request(message: Message, prompt: str, user_id: int = 0) -> None:
    """Process the user's request with the configured CLI."""
    status_msg = await answer_text(message, "⏳ Выполняю...")

    settings = get_settings()
    processor = CliProcessor(
        settings.vault_path,
        settings.todoist_api_key,
        settings.ai_cli,
        settings.owner_full_name,
        settings.content_language,
        getattr(settings, "openai_api_key", ""),
        getattr(settings, "openai_base_url", ""),
        getattr(settings, "openai_model", ""),
        getattr(settings, "tavily_api_key", ""),
        0,
        getattr(settings, "recall_planner_disable_thinking", False),
    )

    async def run_with_progress() -> dict[str, object]:
        context = await reply_context(message, settings.vault_path, user_id)
        kwargs = {} if context is None else {"conversation_context": context}
        task = asyncio.create_task(
            asyncio.to_thread(processor.execute_prompt, prompt, user_id, **kwargs)
        )

        async def update_progress(elapsed_seconds: float) -> None:
            elapsed = int(elapsed_seconds)
            try:
                await edit_text(
                    status_msg,
                    f"⏳ Выполняю... ({elapsed // 60}m {elapsed % 60}s)",
                )
            except Exception:
                pass

        return await wait_for_task_with_progress(
            task,
            interval_seconds=30,
            on_progress=update_progress,
        )

    try:
        report = await run_with_progress()
    except ValueError as exc:
        report = {"error": str(exc)}
    except Exception:
        logger.exception("Execute prompt task failed")
        report = {"error": "Execution task crashed unexpectedly"}

    formatted = format_process_report(report)
    try:
        await status_msg.delete()
    except Exception:
        logger.exception("Failed to delete /do status message before final reply")

    final_sender = answer_text if "error" in report else answer_rich_text
    try:
        sent = await final_sender(message, formatted)
        if "error" not in report:
            save_answer(message, sent, settings.vault_path, user_id, prompt, formatted)
    except Exception:
        logger.exception("Failed to send /do final reply")
    artifact_paths = report.get("artifact_paths")
    if isinstance(artifact_paths, list):
        paths = [str(path) for path in artifact_paths]
        inline_paths = inline_artifact_image_paths(formatted, paths)
        try:
            files = await answer_files(
                message, [path for path in paths if path not in inline_paths]
            )
            if "error" not in report:
                for sent_file in files:
                    save_answer(
                        message,
                        sent_file,
                        settings.vault_path,
                        user_id,
                        prompt,
                        formatted,
                    )
        except Exception:
            logger.exception("Failed to send /do artifacts")
