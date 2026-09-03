"""Handler for /do command - arbitrary AI requests."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import cast

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from d_brain.bot.formatters import format_process_report
from d_brain.bot.progress import wait_for_task_with_progress
from d_brain.bot.replies import answer_rich_text, answer_text, edit_text
from d_brain.bot.states import DoCommandState
from d_brain.config import get_settings
from d_brain.services.answers import (
    AnswerPayload,
    append_log,
    pop_pending_answer,
    register_pending_answer,
    save_answer,
)
from d_brain.services.processor import CliProcessor
from d_brain.services.transcription import DeepgramTranscriber

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


async def start_do_flow(message: Message, state: FSMContext) -> None:
    """Prompt the user for the next /do input."""
    await state.set_state(DoCommandState.waiting_for_input)
    await answer_text(
        message,
        "🎯 **Что сделать?**\n\n"
        "Отправь голосовое или текстовое сообщение с запросом.\n"
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
    """Handle voice/text input after /do command.

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
    await state.clear()  # Clear state immediately

    prompt = None

    # Handle voice input
    if message.voice:
        await message.chat.do(action="typing")
        settings = get_settings()
        transcriber = DeepgramTranscriber(
            settings.deepgram_api_key,
            settings.content_language,
        )

        try:
            file = await asyncio.wait_for(
                bot.get_file(message.voice.file_id), timeout=60
            )
            if not file.file_path:
                await answer_text(message, "❌ Не удалось скачать голосовое")
                return

            file_bytes = await asyncio.wait_for(
                bot.download_file(file.file_path), timeout=120
            )
            if not file_bytes:
                await answer_text(message, "❌ Не удалось скачать голосовое")
                return

            audio_bytes = file_bytes.read()
            prompt = await transcriber.transcribe(audio_bytes)
        except Exception as e:
            logger.exception("Failed to transcribe voice for /do")
            await answer_text(
                message,
                f"❌ Не удалось транскрибировать: {e}",
                parse_mode=None,
            )
            return

        if not prompt:
            await answer_text(message, "❌ Не удалось распознать речь")
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

    else:
        await answer_text(message, "❌ Отправь текст или голосовое сообщение")
        return

    user_id = message.from_user.id if message.from_user else 0
    await process_request(message, prompt, user_id)


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
    )

    async def run_with_progress() -> dict[str, object]:
        task = asyncio.create_task(
            asyncio.to_thread(processor.execute_prompt, prompt, user_id)
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
    except Exception:
        logger.exception("Execute prompt task failed")
        report = {"error": "Execution task crashed unexpectedly"}

    formatted = format_process_report(report)
    try:
        await status_msg.delete()
    except Exception:
        logger.exception("Failed to delete /do status message before final reply")

    # A save button only makes sense for a real answer -- an error message
    # has nothing worth filing (аудит 2026-09-03, п.24).
    send_kwargs: dict[str, object] = {}
    if "error" not in report:
        answer_id = register_pending_answer(
            AnswerPayload(
                question=prompt,
                answer_markdown=formatted,
                # /do's report carries no structured source list -- inventing
                # one here would misattribute claims that were never sourced.
                sources=(),
                kind="do",
            )
        )
        send_kwargs["reply_markup"] = build_save_answer_keyboard(answer_id)

    final_sender = answer_text if "error" in report else answer_rich_text
    try:
        await final_sender(message, formatted, **send_kwargs)
    except Exception:
        logger.exception("Failed to send /do final reply")


def build_save_answer_keyboard(answer_id: str) -> InlineKeyboardMarkup:
    """One "Сохранить" button for a /do or /why answer (аудит 2026-09-03,
    п.24). Both flows use this same builder and ``callback_data`` shape --
    ``why.py`` registers ``handle_answer_save`` on its own router too, since
    the callback carries no ``do``/``why`` prefix of its own."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👍 Сохранить", callback_data=f"answer:save:{answer_id}"
                )
            ]
        ]
    )


@router.callback_query(F.data.startswith("answer:save:"))
async def handle_answer_save(query: CallbackQuery) -> None:
    """File an owner-approved /do or /why answer under ``vault/answers/``
    (аудит 2026-09-03, п.24). Registered on both ``do.router`` and
    ``why.router`` (see ``why.py``) since a single shared in-memory store in
    ``services/answers.py`` -- keyed by ``answer_id``, not by which command
    produced the answer -- backs both buttons.
    """
    if query.message is None:
        await query.answer()
        return
    message = cast(Message, query.message)

    answer_id = (query.data or "").removeprefix("answer:save:")
    payload = pop_pending_answer(answer_id)
    if payload is None:
        await query.answer("Ответ устарел, задай вопрос заново.", show_alert=True)
        return

    # Acknowledged before the write, not after (same reasoning as
    # why.py's handle_why_choice): the write below can block or blow up,
    # and until this call returns the button keeps its spinner.
    await query.answer()

    settings = get_settings()
    vault_path = Path(settings.vault_path)
    now = datetime.now()
    try:
        saved_path = await asyncio.to_thread(
            save_answer,
            vault_path,
            question=payload.question,
            answer_markdown=payload.answer_markdown,
            sources=list(payload.sources),
            kind=payload.kind,
            now=now,
        )
        await asyncio.to_thread(
            append_log, vault_path, payload.kind, payload.question, now
        )
    except Exception:
        logger.exception("Failed to save /%s answer", payload.kind)
        await answer_text(message, "❌ Не удалось сохранить ответ.")
        return

    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("Failed to remove save button after saving answer")

    rel_path = saved_path.relative_to(vault_path).with_suffix("").as_posix()
    await answer_text(message, f"Сохранено: [[{rel_path}]]")
