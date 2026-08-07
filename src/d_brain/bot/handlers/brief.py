"""Handler for the dashboard's "Собрать бриф" button sub-flow (задача L, ТЗ 7.6).

The type buttons themselves (decision/topic/project) live in
``bot/handlers/menu.py`` alongside the rest of the dashboard callbacks (same
pattern as ``menu:do`` there importing ``start_do_flow``); this module only
owns the FSM step that follows a type choice: waiting for the free-text
query, then building, filing, and delivering the brief the same way
``run_compiled_brief.py``'s CLI already does.
"""

import logging
from contextlib import suppress
from datetime import date
from pathlib import Path

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from d_brain.bot.replies import answer_rich_text, answer_text
from d_brain.bot.states import BriefCommandState
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
    message: Message, state: FSMContext, brief_type: str
) -> None:
    """Prompt for the brief's target query after a type button was tapped."""
    if brief_type not in BRIEF_TYPES:
        raise ValueError(f"unknown brief type: {brief_type!r}")
    await state.update_data(brief_type=brief_type)
    await state.set_state(BriefCommandState.waiting_for_query)
    label = _BRIEF_TYPE_LABELS[brief_type]
    await answer_text(
        message,
        f"📝 **Бриф по {label}**\n\n"
        "Отправь текстом название или запрос, чтобы найти страницу.\n"
        "Отправь `-`, чтобы отменить.",
    )


@router.message(BriefCommandState.waiting_for_query)
async def handle_brief_query(message: Message, state: FSMContext) -> None:
    """Handle the query text after a brief type was chosen -- text only."""
    if not message.text:
        await answer_text(
            message,
            "❌ Нужен текстовый запрос. Отправь текст или `-` для отмены.",
        )
        return  # stay in waiting_for_query -- owner can just retry with text

    query = message.text.strip()
    data = await state.get_data()
    brief_type = data.get("brief_type")
    await state.clear()

    if query == "-" or not isinstance(brief_type, str) or brief_type not in BRIEF_TYPES:
        await answer_text(message, "❌ **Сбор брифа отменён**")
        return

    await process_brief_request(message, brief_type, query)


async def process_brief_request(message: Message, brief_type: str, query: str) -> None:
    """Build, file, and deliver one brief -- mirrors ``run_compiled_brief.py``'s
    ``main()`` write path exactly (задача L, ТЗ 7.6)."""
    settings = get_settings()
    vault_path = Path(settings.vault_path)

    try:
        result = build_brief(vault_path, brief_type=brief_type, query=query)
    except Exception:
        # Same guard, and for the same reason, as ``why.py``'s around
        # ``build_why`` (code review): both re-read every ``compiled/**``
        # page, which the nightly pass can be rewriting or archiving
        # underneath them. The FSM state was already cleared by the caller,
        # so letting this escape leaves the owner with no reply at all and
        # no flow left to retry in -- just a message that vanished.
        logger.exception("Failed to build brief for %r", query)
        await answer_text(message, "❌ Не удалось собрать бриф.")
        return
    if result is None:
        await answer_text(
            message, f"🤷 Не нашёл страницу «{brief_type}» по запросу «{query}»."
        )
        return

    today = date.today()
    try:
        manifest = load_manifest_for_vault(vault_path)
        # Path selection under the vault write lock -- see
        # run_compiled_brief.py's main() for why (two near-simultaneous
        # callers must not both see the same counter suffix as free).
        with vault_write_lock(vault_path) as lock:
            path = brief_path(vault_path, result, today)
            write_validated_vault_markdown(
                vault_path,
                path,
                render_brief_note(result, today),
                manifest=manifest,
                existing_lock=lock,
            )
    except Exception:
        logger.exception("Failed to write brief")
        await answer_text(message, "❌ Не удалось сохранить бриф.")
        return

    # ТЗ 6.2: a page that reaches a brief counts as "used" -- best-effort,
    # a touch failure must not take down the brief reply itself.
    try:
        QmdService(vault_path).touch_notes([result.source_rel_path])
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
                f"❌ Бриф сохранён в `{rel_path}`, "
                "но отправить его в чат не удалось.",
            )
