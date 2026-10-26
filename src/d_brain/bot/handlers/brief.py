"""Handler for the dashboard's "Собрать бриф" button sub-flow (задача L, ТЗ 7.6).

The type buttons themselves (decision/topic/project) live in
``bot/handlers/menu.py`` alongside the rest of the dashboard callbacks (same
pattern as ``menu:do`` there importing ``start_do_flow``); this module only
owns the FSM step that follows a type choice: waiting for the free-text
query, then building, filing, and delivering the brief the same way
``run_compiled_brief.py``'s CLI already does.
"""

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from aiogram import Bot, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from d_brain.bot.dashboard import render_input, render_section
from d_brain.bot.replies import answer_rich_text, answer_text
from d_brain.bot.states import BriefCommandState, wait_expired, wait_started_at
from d_brain.bot.vault_busy import VaultBusyError, run_with_vault_lock
from d_brain.clock import owner_today
from d_brain.config import get_settings
from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_briefs import (
    BRIEF_TYPES,
    brief_path,
    build_brief,
    render_brief_note,
)
from d_brain.services.frontmatter import write_validated_vault_markdown
from d_brain.services.qmd import QmdService
from d_brain.services.vault_lock import vault_write_lock

router = Router(name="brief")
logger = logging.getLogger(__name__)

_BRIEF_TYPE_LABELS = {"decision": "решению", "topic": "теме", "project": "проекту"}


async def start_brief_flow(
    message: Message, state: FSMContext, brief_type: str,
    *, menu_message_id: int | None = None,
) -> None:
    """Prompt for the brief's target query after a type button was tapped."""
    if brief_type not in BRIEF_TYPES:
        raise ValueError(f"unknown brief type: {brief_type!r}")
    await state.update_data(brief_type=brief_type, brief_entered_at=wait_started_at())
    await state.set_state(BriefCommandState.waiting_for_query)
    label = _BRIEF_TYPE_LABELS[brief_type]
    if menu_message_id is not None:
        await state.update_data(menu_message_id=menu_message_id)
        await render_input(
            cast(Bot, message.bot), chat_id=message.chat.id, message_id=menu_message_id,
            screen="brief_input", back="menu:brief",
            text=(
                f"**Справка по {label}**\n\n"
                "Отправь текстом название или вопрос.\n"
                "Для отмены нажми «Назад» или отправь `-`."
            ),
        )
        return
    await answer_text(
        message,
        f"📝 **Справка по {label}**\n\n"
        "Отправь текстом название или запрос, чтобы найти страницу.\n"
        "Отправь `-`, чтобы отменить.",
    )


@router.message(BriefCommandState.waiting_for_query)
async def handle_brief_query(message: Message, state: FSMContext) -> None:
    """Handle the query text after a brief type was chosen -- text only."""
    data = await state.get_data()
    menu_message_id = data.get("menu_message_id")
    query = (message.text or "").strip()
    expired = wait_expired(data.get("brief_entered_at"))

    # Non-text messages, forwards and text after the wait expired leave the
    # flow and continue to the regular capture handlers (see why.py).
    if (
        message.text is None
        or message.forward_origin is not None
        or (expired and query != "-")
    ):
        await state.clear()
        # A failed menu repaint must not stop the message from reaching the
        # regular capture handlers.
        try:
            await _restore_menu(message, menu_message_id)
        except Exception:
            logger.exception("Failed to restore the dashboard menu")
        raise SkipHandler

    # A command that a later router handles ("/reminders", unknown "/foo")
    # must not be taken for the query (same rule as /find).
    if query.startswith("/"):
        await state.clear()
        try:
            await _restore_menu(message, menu_message_id)
        except Exception:
            logger.exception("Failed to restore the dashboard menu")
        raise SkipHandler

    brief_type = data.get("brief_type")
    await state.clear()
    await _restore_menu(message, menu_message_id)

    if query == "-" or not isinstance(brief_type, str) or brief_type not in BRIEF_TYPES:
        await answer_text(message, "Сбор справки отменён. Открыть меню: /menu")
        return

    await process_brief_request(message, brief_type, query)


async def _restore_menu(message: Message, menu_message_id: object) -> None:
    """Put the dashboard back over the "send the query" prompt."""
    if isinstance(menu_message_id, int):
        await render_section(
            cast(Bot, message.bot), chat_id=message.chat.id, section="summaries",
            preferred_message_id=menu_message_id,
        )


async def process_brief_request(message: Message, brief_type: str, query: str) -> None:
    """Build, file, and deliver one brief -- mirrors ``run_compiled_brief.py``'s
    ``main()`` write path exactly (задача L, ТЗ 7.6)."""
    settings = get_settings()
    vault_path = Path(settings.vault_path)

    try:
        result = await asyncio.to_thread(
            build_brief, vault_path, brief_type=brief_type, query=query
        )
    except Exception:
        # Same guard, and for the same reason, as ``why.py``'s around
        # ``build_why`` (code review): both re-read every ``compiled/**``
        # page, which the nightly pass can be rewriting or archiving
        # underneath them. The FSM state was already cleared by the caller,
        # so letting this escape leaves the owner with no reply at all and
        # no flow left to retry in -- just a message that vanished.
        logger.exception("Failed to build brief for %r", query)
        await answer_text(message, "❌ Не удалось собрать справку.")
        return
    if result is None:
        await answer_text(
            message, f"🤷 Не нашёл страницу «{brief_type}» по запросу «{query}»."
        )
        return

    today = owner_today()
    try:
        manifest = load_manifest_for_vault(vault_path)

        # Path selection under the vault write lock -- see
        # run_compiled_brief.py's main() for why (two near-simultaneous
        # callers must not both see the same counter suffix as free).
        def write_brief(lock: Any) -> Path:
            brief_file = brief_path(vault_path, result, today)
            write_validated_vault_markdown(
                vault_path,
                brief_file,
                render_brief_note(result, today),
                manifest=manifest,
                existing_lock=lock,
            )
            return brief_file

        path = await run_with_vault_lock(
            vault_path, write_brief, lock_factory=vault_write_lock
        )
    except VaultBusyError:
        await answer_text(message, "⏳ Идёт обработка, попробуйте позже.")
        return
    except Exception:
        logger.exception("Failed to write brief")
        await answer_text(message, "❌ Не удалось сохранить справку.")
        return

    # ТЗ 6.2: a page that reaches a brief counts as "used" -- best-effort,
    # a touch failure must not take down the brief reply itself.
    try:
        await asyncio.to_thread(
            QmdService(vault_path).touch_notes, [result.source_rel_path]
        )
    except Exception as exc:  # pragma: no cover - best-effort touch
        logger.warning(
            "Failed to touch brief source %s: %s", result.source_rel_path, exc
        )

    try:
        await answer_rich_text(message, result.markdown)
    except Exception:
        # The brief is already filed in the vault by this point, so a failed
        # delivery must not pass in silence: the owner would be left with a
        # new file they never learn about. Same principle as
        # ``menu.py::_deliver_daily_digest``'s last-resort edit, but this one
        # spells the path out: a digest lands on a predictable
        # ``summaries/compile/<date>.md``, while ``brief_path`` appends a
        # counter suffix when the day already has one, so "look in the
        # briefs folder" would not be enough to find it.
        logger.exception("Failed to deliver brief")
        with suppress(Exception):
            rel_path = Path(path).relative_to(vault_path).as_posix()
            await answer_text(
                message,
                f"❌ Справка сохранена в `{rel_path}`, "
                "но отправить его в чат не удалось.",
            )
