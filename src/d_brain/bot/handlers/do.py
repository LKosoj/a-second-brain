"""Handler for /do command - arbitrary AI requests."""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from d_brain.bot.formatters import format_process_report, inline_artifact_image_paths
from d_brain.bot.progress import wait_for_task_with_progress
from d_brain.bot.replies import answer_files, answer_rich_text, answer_text, edit_text
from d_brain.bot.states import DoCommandState
from d_brain.config import get_settings
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
        getattr(settings, "tavily_api_key", ""),
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

    final_sender = answer_text if "error" in report else answer_rich_text
    try:
        await final_sender(message, formatted)
    except Exception:
        logger.exception("Failed to send /do final reply")
    artifact_paths = report.get("artifact_paths")
    if isinstance(artifact_paths, list):
        paths = [str(path) for path in artifact_paths]
        inline_paths = inline_artifact_image_paths(formatted, paths)
        try:
            await answer_files(
                message, [path for path in paths if path not in inline_paths]
            )
        except Exception:
            logger.exception("Failed to send /do artifacts")
