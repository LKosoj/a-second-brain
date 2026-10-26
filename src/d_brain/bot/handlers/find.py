"""Handler for /find - keyword search over vault notes (G6).

No model is called: the search is ``qmd search`` (BM25) and the answer lists
up to five hits with a path, a snippet and a button that sends the file.
Modeled on ``bot/handlers/why.py`` (wait state, text-only input).
"""

import asyncio
import hashlib
import logging
import re
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from d_brain.bot.replies import answer_callback_once, answer_text
from d_brain.bot.states import FindCommandState, wait_expired, wait_started_at
from d_brain.config import get_settings
from d_brain.services.file_browser import TELEGRAM_MAX_DOCUMENT_BYTES
from d_brain.services.vault_find import (
    FIND_QUERY_MAX_CHARS,
    FindHit,
    FindUnavailable,
    find_in_vault,
    normalize_query,
    resolve_findable_file,
)

router = Router(name="find")
logger = logging.getLogger(__name__)

_UNAVAILABLE_TEXT = "❌ Поиск сейчас недоступен (qmd не отвечает)."
# Characters that would turn a note's text into Markdown structure in a title.
_TITLE_MARKUP_RE = re.compile(r"[*_\[\]()~|#!]")


async def start_find_flow(message: Message, state: FSMContext) -> None:
    """Prompt the user for the next /find query."""
    await state.update_data(find_entered_at=wait_started_at())
    await state.set_state(FindCommandState.waiting_for_input)
    await answer_text(
        message,
        "🔍 **Что искать в заметках?**\n\n"
        "Отправь текстом слова для поиска.\n"
        "Повтори `/find` или отправь `-`, чтобы отменить.",
    )


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, state: FSMContext) -> None:
    """Handle /find command."""
    current_state = await state.get_state()
    data = await state.get_data()

    # An expired wait counts as absent (same rule as /why).
    if (
        not command.args
        and current_state == FindCommandState.waiting_for_input.state
        and not wait_expired(data.get("find_entered_at"))
    ):
        await state.clear()
        await answer_text(message, "❌ **/find отменён**")
        return

    if command.args:
        await state.clear()
        await process_find_request(message, command.args, state)
        return

    await start_find_flow(message, state)


@router.message(FindCommandState.waiting_for_input)
async def handle_find_input(message: Message, state: FSMContext) -> None:
    """Handle the query after a bare /find -- plain text only."""
    data = await state.get_data()
    expired = wait_expired(data.get("find_entered_at"))
    text = (message.text or "").strip()

    # Anything but plain text (voice, photo, document, forward), late text and
    # another command leave the wait and go on to the next routers, so they are
    # handled or saved as usual instead of being used as a search query.
    if message.text is None or message.forward_origin is not None:
        await state.clear()
        raise SkipHandler
    if expired and text != "-":
        await state.clear()
        raise SkipHandler
    if text.startswith("/"):
        await state.clear()
        raise SkipHandler

    if text == "-":
        await state.clear()
        await answer_text(message, "❌ **/find отменён**")
        return

    await state.clear()
    await process_find_request(message, text, state)


def _results_token(rel_paths: list[str]) -> str:
    """Short fingerprint of one result list for ``callback_data``.

    FSM data holds a single result list; the token makes a button from an
    older answer fail as "stale" instead of opening a file of the newer one.
    """
    return hashlib.sha256("\n".join(rel_paths).encode("utf-8")).hexdigest()[:8]


def _safe_text(value: str) -> str:
    """Neutralize untrusted vault text before it goes into a reply.

    ``<``/``>`` would make ``answer_text`` read the whole reply as HTML, and
    backticks or backslashes would break the code spans around the value, and
    ``![x](attachments/...)`` would turn the reply into an HTML document with
    the image inlined.
    """
    cleaned = (
        value.replace("<", "‹")
        .replace(">", "›")
        .replace("`", "'")
        .replace("\\", "")
        .replace("![", "[")
    )
    return " ".join(cleaned.split())


def build_find_text(query: str, hits: list[FindHit]) -> str:
    lines = [f"🔍 **Нашёл по запросу** `{_safe_text(query)}`: {len(hits)}"]
    for number, hit in enumerate(hits, start=1):
        title = " ".join(_TITLE_MARKUP_RE.sub("", _safe_text(hit.title)).split())
        lines.append("")
        lines.append(f"**{number}. {title or 'Без названия'}**")
        lines.append(f"`{_safe_text(hit.rel_path)}`")
        snippet = _safe_text(hit.snippet)
        if snippet:
            lines.append(f"```\n{snippet}\n```")
    return "\n".join(lines)


def build_find_keyboard(count: int, token: str) -> InlineKeyboardMarkup:
    """One numbered button per hit; numbers match the list in the text."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📄 {index + 1}",
                    callback_data=f"find:open:{token}:{index}",
                )
                for index in range(count)
            ]
        ]
    )


async def process_find_request(
    message: Message, raw_query: str, state: FSMContext
) -> None:
    """Validate one query, search, and reply."""
    query = normalize_query(raw_query)
    if not query:
        await answer_text(message, "Напиши, что искать: `/find слова для поиска`.")
        return
    if len(query) > FIND_QUERY_MAX_CHARS:
        await answer_text(
            message,
            f"Запрос слишком длинный (макс. {FIND_QUERY_MAX_CHARS} символов).",
        )
        return

    vault_path = Path(get_settings().vault_path)
    try:
        # qmd runs as a blocking subprocess; keep the event loop free.
        hits = await asyncio.to_thread(find_in_vault, vault_path, query)
    except FindUnavailable as error:
        logger.warning("/find unavailable for %r: %s", query, error)
        await answer_text(message, _UNAVAILABLE_TEXT)
        return
    except Exception:
        # The owner always gets an answer, as in the other command flows.
        logger.exception("/find failed for %r", query)
        await answer_text(message, _UNAVAILABLE_TEXT)
        return

    if not hits:
        await answer_text(
            message, f"🤷 По запросу «{_safe_text(query)}» ничего не нашёл."
        )
        return

    rel_paths = [hit.rel_path for hit in hits]
    token = _results_token(rel_paths)
    await state.update_data(find_results=rel_paths, find_token=token)
    await answer_text(
        message,
        build_find_text(query, hits),
        reply_markup=build_find_keyboard(len(hits), token),
    )


@router.callback_query(F.data.startswith("find:open:"))
async def handle_find_open(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Send the file behind one numbered /find button."""
    message = query.message
    if message is None:
        await answer_callback_once(query)
        return

    parts = (query.data or "").split(":", 3)
    if len(parts) != 4:
        await answer_callback_once(
            query, "Список устарел, повтори /find.", show_alert=True
        )
        return
    _, _, token, raw_index = parts
    data = await state.get_data()
    rel_paths = data.get("find_results")
    try:
        index = int(raw_index)
    except ValueError:
        index = -1
    if (
        not isinstance(rel_paths, list)
        or token != data.get("find_token")
        or not 0 <= index < len(rel_paths)
    ):
        await answer_callback_once(
            query, "Список устарел, повтори /find.", show_alert=True
        )
        return

    # Re-checked at open time: the file may be gone or swapped for a symlink
    # since the search.
    path = resolve_findable_file(get_settings().vault_path, str(rel_paths[index]))
    if path is None:
        await answer_callback_once(query, "Файл больше не доступен.", show_alert=True)
        return
    try:
        too_large = path.stat().st_size > TELEGRAM_MAX_DOCUMENT_BYTES
    except OSError:
        await answer_callback_once(query, "Файл больше не доступен.", show_alert=True)
        return
    if too_large:
        await answer_callback_once(
            query, "Файл слишком большой для отправки через Telegram.", show_alert=True
        )
        return

    with suppress(Exception):
        await bot.send_chat_action(message.chat.id, "upload_document")
    try:
        await bot.send_document(
            chat_id=message.chat.id,
            document=FSInputFile(path, filename=path.name),
        )
    except Exception:
        # Telegram errors are not OSError; the button must not hang.
        logger.exception("Failed to send /find file")
        await answer_callback_once(
            query, "Не удалось отправить файл.", show_alert=True
        )
        return
    await answer_callback_once(query)
