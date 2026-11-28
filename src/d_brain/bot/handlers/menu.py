"""Inline dashboard and file browser handlers."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from d_brain.bot.dashboard import (
    DashboardScreen,
    DashboardSession,
    close_dashboard,
    file_browser_parent_dir,
    file_entries_fingerprint,
    get_dashboard_session,
    lesson_fingerprint,
    page_fingerprint,
    reenable_job_step,
    render_brief_type,
    render_disabled_steps,
    render_file_directory,
    render_file_roots,
    render_home,
    render_input,
    render_learnings,
    render_meeting_prep,
    render_queue,
    render_queue_item,
    render_section,
    render_stats,
    render_weekly_goal,
    render_weekly_review,
    send_browser_file,
    update_weekly_one_big_thing,
)
from d_brain.bot.handlers.brief import start_brief_flow
from d_brain.bot.handlers.do import start_do_flow
from d_brain.bot.handlers.process import run_full_process, run_interactive_process
from d_brain.bot.handlers.tasks import show_tasks
from d_brain.bot.replies import (
    answer_callback_once,
    answer_rich_text,
    answer_text,
    edit_rich_text,
    edit_text,
)
from d_brain.bot.states import wait_expired, wait_started_at
from d_brain.bot.vault_busy import (
    VAULT_LOCK_WAIT_SECONDS,
    VaultBusyError,
    run_with_vault_lock,
)
from d_brain.clock import owner_today
from d_brain.config import get_settings
from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_briefs import BRIEF_TYPES
from d_brain.services.compiled_enrich_report import (
    build_daily_digest,
    digest_path,
    read_pass_status,
    render_digest_note,
)
from d_brain.services.conversation_learning import delete_lesson
from d_brain.services.decisions_queue import (
    apply_response,
    mark_page_human_reviewed,
    queue_item_fingerprint,
)
from d_brain.services.file_browser import FileBrowserError
from d_brain.services.frontmatter import write_validated_vault_markdown
from d_brain.services.vault_lock import VaultLockTimeout, vault_write_lock
from d_brain.services.vault_map import export_vault_map

router = Router(name="menu")
logger = logging.getLogger(__name__)


class GoalEditState(StatesGroup):
    """FSM state for editing the weekly ONE Big Thing from /menu."""

    waiting_for_text = State()


@dataclass(frozen=True, slots=True)
class _CallbackContext:
    query: CallbackQuery
    bot: Bot
    state: FSMContext
    message: Message
    chat_id: int
    message_id: int
    data: str
    settings: Any
    session: DashboardSession


_QUEUE_ITEM_STALE_MESSAGE = "Пункт очереди устарел, открой список заново."
_WEEKLY_REVIEW_STALE_MESSAGE = "Сводка недели устарела, открой экран заново."
_LEARNING_STALE_MESSAGE = "Список уроков устарел, открой экран заново."
_QUEUE_ACTION_FAILED_MESSAGE = (
    "Не удалось применить действие — часть изменений могла не сохраниться. "
    "Открой список заново."
)
_SCREEN_REDRAW_FAILED_MESSAGE = (
    "Действие применено, но список не обновился. Открой его заново."
)
_FILE_SENT_REDRAW_FAILED_MESSAGE = (
    "Файл отправлен, но список не обновился. Открой его заново."
)

# Kept under its old private name for existing tests calling it directly.
_read_enrich_pass_status = read_pass_status


async def _deliver_daily_digest(message: Message, *, vault_path: str | Path) -> None:
    """Build and file today's compiled-enrichment digest."""
    resolved_vault_path = Path(vault_path)
    status_msg = await answer_text(message, "⏳ Строю дайджест...")
    today = owner_today()

    try:
        digest = await asyncio.to_thread(
            build_daily_digest,
            resolved_vault_path,
            today,
            pass_status=_read_enrich_pass_status(resolved_vault_path),
        )
    except Exception:
        logger.exception("Failed to build compiled digest")
        await edit_text(status_msg, "❌ Не удалось построить дайджест.")
        return

    if digest is None:
        await edit_text(status_msg, "Сегодня без изменений — дайджест не нужен.")
        return

    try:
        manifest = load_manifest_for_vault(resolved_vault_path)

        def write_digest(lock: Any) -> None:
            write_validated_vault_markdown(
                resolved_vault_path,
                digest_path(resolved_vault_path, today),
                render_digest_note(today, digest),
                manifest=manifest,
                existing_lock=lock,
            )

        await run_with_vault_lock(
            resolved_vault_path, write_digest, lock_factory=vault_write_lock
        )
    except VaultBusyError:
        await edit_text(status_msg, "⏳ Идёт обработка, попробуйте позже.")
        return
    except Exception:
        logger.exception("Failed to write compiled digest")
        await edit_text(
            status_msg, "❌ Дайджест построен, но не удалось сохранить файл."
        )
        return

    try:
        await edit_rich_text(status_msg, digest)
        return
    except TelegramBadRequest:
        pass
    except Exception:
        logger.exception("Failed to deliver compiled digest")

    try:
        await answer_rich_text(message, digest)
    except Exception:
        logger.exception("Failed to deliver compiled digest as a new message")
        with suppress(Exception):
            await edit_text(
                status_msg,
                "❌ Дайджест сохранён в файл, но отправить его в чат не удалось.",
            )


async def _render_file_directory_or_roots(
    bot: Bot,
    *,
    chat_id: int,
    vault_path: str | Path,
    root_id: str,
    current_dir: str = "",
    page: int = 0,
    preferred_message_id: int | None = None,
) -> None:
    """Render one browser directory, falling back to the root picker."""
    try:
        await render_file_directory(
            bot,
            chat_id=chat_id,
            vault_path=vault_path,
            root_id=root_id,
            current_dir=current_dir,
            page=page,
            preferred_message_id=preferred_message_id,
        )
    except (FileBrowserError, OSError):
        logger.warning("File browser directory is gone: %s", current_dir or "/")
        await render_file_roots(
            bot,
            chat_id=chat_id,
            vault_path=vault_path,
            preferred_message_id=preferred_message_id,
            notice="Папка больше не доступна — начни с раздела.",
        )


@router.message(Command("menu"))
async def cmd_menu(message: Message, bot: Bot, state: FSMContext) -> None:
    """Open or recreate the inline dashboard."""
    settings = get_settings()
    await state.clear()
    await render_home(
        bot,
        chat_id=message.chat.id,
        vault_path=settings.vault_path,
        user_id=message.from_user.id if message.from_user else message.chat.id,
    )


@router.message(Command("files"))
async def cmd_files(message: Message, bot: Bot, state: FSMContext) -> None:
    """Open the file browser root picker."""
    settings = get_settings()
    await state.clear()
    await render_file_roots(
        bot,
        chat_id=message.chat.id,
        vault_path=settings.vault_path,
    )


@router.message(GoalEditState.waiting_for_text)
async def handle_goal_input(message: Message, state: FSMContext) -> None:
    """Save the updated weekly ONE Big Thing entered by the owner."""
    data = await state.get_data()
    menu_message_id = data.get("menu_message_id")
    raw_text = (message.text or "").strip()
    expired = wait_expired(data.get("goal_entered_at"))
    settings = get_settings()

    if (
        message.text is None
        or message.forward_origin is not None
        or (expired and raw_text != "-")
        or raw_text.startswith("/")
    ):
        await state.clear()
        if isinstance(menu_message_id, int):
            with suppress(Exception):
                await render_weekly_goal(
                    cast(Bot, message.bot),
                    chat_id=message.chat.id,
                    vault_path=settings.vault_path,
                    preferred_message_id=menu_message_id,
                )
        raise SkipHandler

    await state.clear()
    if raw_text == "-" or not raw_text:
        if isinstance(menu_message_id, int):
            await render_weekly_goal(
                cast(Bot, message.bot),
                chat_id=message.chat.id,
                vault_path=settings.vault_path,
                preferred_message_id=menu_message_id,
                notice="Обновление цели отменено.",
            )
        else:
            await answer_text(message, "Обновление цели отменено. Открыть меню: /menu")
        return

    try:
        await asyncio.to_thread(
            update_weekly_one_big_thing,
            settings.vault_path,
            raw_text,
            timeout_seconds=VAULT_LOCK_WAIT_SECONDS,
        )
    except VaultLockTimeout:
        notice = "⏳ Идёт обработка, попробуйте позже."
        if isinstance(menu_message_id, int):
            with suppress(Exception):
                await render_weekly_goal(
                    cast(Bot, message.bot),
                    chat_id=message.chat.id,
                    vault_path=settings.vault_path,
                    preferred_message_id=menu_message_id,
                    notice=notice,
                )
        else:
            await answer_text(message, notice)
        return
    except Exception:
        logger.exception("Failed to update weekly ONE Big Thing")
        if isinstance(menu_message_id, int):
            with suppress(Exception):
                await render_weekly_goal(
                    cast(Bot, message.bot),
                    chat_id=message.chat.id,
                    vault_path=settings.vault_path,
                    preferred_message_id=menu_message_id,
                    notice="Не удалось обновить цель недели.",
                )
        else:
            await answer_text(message, "❌ Не удалось обновить цель недели.")
        return

    if isinstance(menu_message_id, int):
        await render_weekly_goal(
            cast(Bot, message.bot),
            chat_id=message.chat.id,
            vault_path=settings.vault_path,
            preferred_message_id=menu_message_id,
            notice="Главная цель недели обновлена.",
        )
    else:
        await answer_text(message, "✅ Главная цель недели обновлена.")


# --- Callback sub-handlers ------------------------------------------------


async def _handle_core_callback(ctx: _CallbackContext) -> bool:
    """Handle home, sections, tasks, stats, processing, /do, vaultmap, close."""
    data = ctx.data
    if data == "menu:tasks":
        if ctx.session.task_busy:
            await ctx.query.answer("Подождите, список задач обновляется.")
            return True
        await ctx.query.answer()
        await show_tasks(ctx.bot, ctx.chat_id, ctx.message_id)
        return True

    if data in {"menu:summaries", "menu:processing", "menu:more", "menu:help"}:
        await ctx.query.answer()
        await render_section(
            ctx.bot,
            chat_id=ctx.chat_id,
            section=cast(DashboardScreen, data.removeprefix("menu:")),
            preferred_message_id=ctx.message_id,
            vault_path=ctx.settings.vault_path,
        )
        return True

    if data == "menu:home":
        await ctx.query.answer()
        await render_home(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            user_id=ctx.query.from_user.id,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data == "menu:stats":
        await ctx.query.answer()
        await render_stats(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            user_id=ctx.query.from_user.id,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data == "menu:vaultmap":
        await ctx.query.answer("Собираю карту знаний…")
        try:
            path = await asyncio.to_thread(export_vault_map, ctx.settings.vault_path)
            await ctx.bot.send_document(
                chat_id=ctx.chat_id,
                document=FSInputFile(path),
                parse_mode=None,
                caption=(
                    "Карта знаний: названия, описания и связи. "
                    "Скачайте HTML и откройте в браузере без интернета."
                ),
            )
        except Exception:
            logger.exception("Failed to build or send vault map")
            await answer_text(
                ctx.message, "Не удалось отправить карту знаний. Попробуйте ещё раз."
            )
        return True

    if data == "menu:process":
        await ctx.query.answer("Показываю разбор без сохранения…")
        await run_interactive_process(ctx.message, actor_id=ctx.query.from_user.id)
        return True

    if data == "menu:processfull":
        await ctx.query.answer("Запускаю полную обработку...")
        await run_full_process(ctx.message, actor_id=ctx.query.from_user.id)
        return True

    if data == "menu:do":
        await ctx.query.answer()
        await start_do_flow(ctx.message, ctx.state, menu_message_id=ctx.message_id)
        return True

    if data == "menu:close":
        await ctx.query.answer("Меню закрыто. Открыть снова: /menu")
        await close_dashboard(
            ctx.bot,
            chat_id=ctx.chat_id,
            preferred_message_id=ctx.message_id,
            vault_path=ctx.settings.vault_path,
        )
        return True

    return False


async def _handle_jobhealth_callback(ctx: _CallbackContext) -> bool:
    """Handle disabled-step inspection and re-enabling."""
    data = ctx.data
    if data == "menu:jobhealth":
        await ctx.query.answer()
        await render_disabled_steps(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data.startswith("menu:jobhealthenable:"):
        step_name = data.removeprefix("menu:jobhealthenable:")
        was_disabled = await asyncio.to_thread(
            reenable_job_step, ctx.settings.vault_path, step_name
        )
        notice = (
            "Шаг снова включён."
            if was_disabled
            else "Шаг уже был включён или не найден."
        )
        await render_disabled_steps(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            preferred_message_id=ctx.message_id,
            notice=notice,
        )
        await ctx.query.answer()
        return True

    return False


async def _handle_files_callback(ctx: _CallbackContext) -> bool:
    """Handle all file-browser navigation and download callbacks."""
    data = ctx.data
    if data in {"menu:files", "menu:filesroots"}:
        await ctx.query.answer()
        await render_file_roots(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data.startswith("menu:filesroot:"):
        await ctx.query.answer()
        root_id = data.removeprefix("menu:filesroot:")
        try:
            await render_file_directory(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                root_id=root_id,
                preferred_message_id=ctx.message_id,
            )
        except FileBrowserError:
            await render_file_roots(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                preferred_message_id=ctx.message_id,
                notice="Не удалось открыть выбранный раздел.",
            )
        return True

    if data == "menu:filesrefresh":
        await ctx.query.answer()
        if ctx.session.file_root_id is None:
            await render_file_roots(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                preferred_message_id=ctx.message_id,
                notice="Сначала выбери раздел.",
            )
        else:
            await _render_file_directory_or_roots(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                root_id=ctx.session.file_root_id,
                current_dir=ctx.session.file_current_dir,
                page=ctx.session.file_page,
                preferred_message_id=ctx.message_id,
            )
        return True

    if data == "menu:filesup":
        await ctx.query.answer()
        if ctx.session.file_root_id is None:
            await render_file_roots(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                preferred_message_id=ctx.message_id,
                notice="Сначала выбери раздел.",
            )
        else:
            await _render_file_directory_or_roots(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                root_id=ctx.session.file_root_id,
                current_dir=file_browser_parent_dir(ctx.session.file_current_dir),
                page=0,
                preferred_message_id=ctx.message_id,
            )
        return True

    if data.startswith("menu:filespage:"):
        await ctx.query.answer()
        if ctx.session.file_root_id is None:
            await render_file_roots(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                preferred_message_id=ctx.message_id,
                notice="Сначала выбери раздел.",
            )
        else:
            try:
                page = int(data.removeprefix("menu:filespage:"))
            except ValueError:
                page = 0
            await _render_file_directory_or_roots(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                root_id=ctx.session.file_root_id,
                current_dir=ctx.session.file_current_dir,
                page=page,
                preferred_message_id=ctx.message_id,
            )
        return True

    if data.startswith("menu:filesentry:"):
        await _handle_file_entry(ctx)
        return True

    return False


async def _handle_file_entry(ctx: _CallbackContext) -> None:
    if ctx.session.file_root_id is None:
        await render_file_roots(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            preferred_message_id=ctx.message_id,
            notice="Сначала выбери раздел.",
        )
        await ctx.query.answer()
        return

    raw_index, _, token = ctx.data.removeprefix("menu:filesentry:").partition(":")
    try:
        entry_index = int(raw_index)
    except ValueError:
        entry_index = -1

    if token != file_entries_fingerprint(ctx.session.file_entries):
        await ctx.query.answer("Список устарел, обнови его.", show_alert=True)
        return

    if entry_index < 0 or entry_index >= len(ctx.session.file_entries):
        await ctx.query.answer("Файл или папка не найдены.", show_alert=True)
        return

    entry = ctx.session.file_entries[entry_index]
    if entry.is_directory:
        await ctx.query.answer()
        await _render_file_directory_or_roots(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            root_id=ctx.session.file_root_id,
            current_dir=entry.relative_path,
            page=0,
            preferred_message_id=ctx.message_id,
        )
        return

    with suppress(Exception):
        await ctx.bot.send_chat_action(ctx.chat_id, "upload_document")
    try:
        file_name = await send_browser_file(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            session=ctx.session,
            entry_index=entry_index,
        )
    except ValueError as error:
        if str(error) == "FILE_TOO_LARGE":
            await answer_callback_once(
                ctx.query,
                "Файл слишком большой для отправки через Telegram.",
                show_alert=True,
            )
            return
        await answer_callback_once(
            ctx.query, "Не удалось отправить файл.", show_alert=True
        )
        return
    except (FileBrowserError, OSError):
        await answer_callback_once(
            ctx.query, "Не удалось открыть файл.", show_alert=True
        )
        return
    except Exception:
        logger.exception("Failed to send browser file")
        await answer_callback_once(
            ctx.query, "Не удалось отправить файл.", show_alert=True
        )
        return

    try:
        await render_file_directory(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            root_id=ctx.session.file_root_id,
            current_dir=ctx.session.file_current_dir,
            page=ctx.session.file_page,
            preferred_message_id=ctx.message_id,
            notice=f"Файл отправлен: {file_name}",
        )
    except Exception:
        logger.exception("Failed to redraw the file browser after a send")
        await answer_callback_once(
            ctx.query, _FILE_SENT_REDRAW_FAILED_MESSAGE, show_alert=True
        )
        return

    await answer_callback_once(ctx.query, f"Отправлен: {file_name}")


async def _handle_queue_callback(ctx: _CallbackContext) -> bool:
    """Handle decisions queue listing, inspection, and response actions."""
    data = ctx.data
    if data == "menu:queue":
        await ctx.query.answer()
        await render_queue(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data.startswith("menu:queuepage:"):
        await ctx.query.answer()
        try:
            page = int(data.removeprefix("menu:queuepage:"))
        except ValueError:
            page = 0
        await render_queue(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            page=page,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data.startswith("menu:queueitem:"):
        remainder = data.removeprefix("menu:queueitem:")
        index_str, _sep, fingerprint = remainder.partition(":")
        try:
            entry_index = int(index_str)
        except ValueError:
            entry_index = -1

        if (
            entry_index < 0
            or entry_index >= len(ctx.session.queue_items)
            or queue_item_fingerprint(ctx.session.queue_items[entry_index])
            != fingerprint
        ):
            await ctx.query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
            return True

        item = await render_queue_item(
            ctx.bot,
            chat_id=ctx.chat_id,
            index=entry_index,
            preferred_message_id=ctx.message_id,
            vault_path=ctx.settings.vault_path,
        )
        if item is None:
            await ctx.query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
            return True
        await ctx.query.answer()
        return True

    if data.startswith("menu:queueact:"):
        await _handle_queue_action(ctx)
        return True

    return False


async def _handle_queue_action(ctx: _CallbackContext) -> None:
    remainder = ctx.data.removeprefix("menu:queueact:")
    index_str, _sep, rest = remainder.partition(":")
    fingerprint, _sep2, action_id = rest.partition(":")
    try:
        entry_index = int(index_str)
    except ValueError:
        entry_index = -1

    if entry_index < 0 or entry_index >= len(ctx.session.queue_items):
        await ctx.query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
        return

    item = ctx.session.queue_items[entry_index]
    if queue_item_fingerprint(item) != fingerprint:
        await render_queue(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            page=ctx.session.queue_page,
            preferred_message_id=ctx.message_id,
            notice=_QUEUE_ITEM_STALE_MESSAGE,
        )
        await ctx.query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
        return

    try:
        outcome = await asyncio.to_thread(
            apply_response, Path(ctx.settings.vault_path), item, action_id
        )
    except ValueError:
        await ctx.query.answer("Действие недоступно для этого пункта.", show_alert=True)
        return
    except FileNotFoundError:
        await ctx.query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
        return
    except Exception:
        logger.exception("Queue action %s failed for %s", action_id, item.page)
        await ctx.query.answer(_QUEUE_ACTION_FAILED_MESSAGE, show_alert=True)
        return

    try:
        await render_queue(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            page=ctx.session.queue_page,
            preferred_message_id=ctx.message_id,
            notice=outcome.message,
        )
    except Exception:
        logger.exception("Queue redraw failed after applying %s", action_id)
        await ctx.query.answer(_SCREEN_REDRAW_FAILED_MESSAGE, show_alert=True)
        return
    await ctx.query.answer()


async def _handle_weekly_callback(ctx: _CallbackContext) -> bool:
    """Handle weekly review screen and review-pick confirmation."""
    data = ctx.data
    if data == "menu:weekly":
        await ctx.query.answer()
        await render_weekly_review(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data.startswith("menu:weeklyreview:"):
        fingerprint = data.removeprefix("menu:weeklyreview:")
        review = ctx.session.weekly_review
        if review is None or review.review_pick is None:
            await ctx.query.answer(_WEEKLY_REVIEW_STALE_MESSAGE, show_alert=True)
            return True

        rel_path, _title = review.review_pick
        if page_fingerprint(rel_path) != fingerprint:
            await render_weekly_review(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                preferred_message_id=ctx.message_id,
                notice=_WEEKLY_REVIEW_STALE_MESSAGE,
            )
            await ctx.query.answer(_WEEKLY_REVIEW_STALE_MESSAGE, show_alert=True)
            return True

        try:
            outcome = await asyncio.to_thread(
                mark_page_human_reviewed, Path(ctx.settings.vault_path), rel_path
            )
        except Exception:
            logger.exception("Weekly review mark failed for %s", rel_path)
            await ctx.query.answer(_QUEUE_ACTION_FAILED_MESSAGE, show_alert=True)
            return True
        try:
            await render_weekly_review(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                preferred_message_id=ctx.message_id,
                notice=outcome.message,
            )
        except Exception:
            logger.exception("Weekly review redraw failed for %s", rel_path)
            await ctx.query.answer(_SCREEN_REDRAW_FAILED_MESSAGE, show_alert=True)
            return True
        await ctx.query.answer()
        return True

    return False


async def _handle_briefs_callback(ctx: _CallbackContext) -> bool:
    """Handle daily digest and compiled-brief builder callbacks."""
    data = ctx.data
    if data == "menu:digest":
        await ctx.query.answer("Готовлю сводку дня…")
        await _deliver_daily_digest(ctx.message, vault_path=ctx.settings.vault_path)
        return True

    if data == "menu:brief":
        await ctx.query.answer()
        await render_brief_type(
            ctx.bot,
            chat_id=ctx.chat_id,
            preferred_message_id=ctx.message_id,
            vault_path=ctx.settings.vault_path,
        )
        return True

    if data.startswith("menu:brieftype:"):
        brief_type = data.removeprefix("menu:brieftype:")
        if brief_type not in BRIEF_TYPES:
            await ctx.query.answer("Неизвестный тип справки.", show_alert=True)
            return True
        await ctx.query.answer()
        await start_brief_flow(
            ctx.message, ctx.state, brief_type, menu_message_id=ctx.message_id
        )
        return True

    return False


async def _handle_learning_callback(ctx: _CallbackContext) -> bool:
    """Handle transparent Conversation Learning listing and deletion."""
    data = ctx.data
    if data == "menu:learnings":
        await ctx.query.answer()
        await render_learnings(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data.startswith("menu:learningspage:"):
        await ctx.query.answer()
        try:
            page = int(data.removeprefix("menu:learningspage:"))
        except ValueError:
            page = 0
        await render_learnings(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            page=page,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data.startswith("menu:learningdel:"):
        remainder = data.removeprefix("menu:learningdel:")
        index_str, _sep, fingerprint = remainder.partition(":")
        try:
            entry_index = int(index_str)
        except ValueError:
            entry_index = -1

        if (
            entry_index < 0
            or entry_index >= len(ctx.session.learning_lessons)
            or lesson_fingerprint(ctx.session.learning_lessons[entry_index].lesson_id)
            != fingerprint
        ):
            await ctx.query.answer(_LEARNING_STALE_MESSAGE, show_alert=True)
            return True

        lesson = ctx.session.learning_lessons[entry_index]
        try:
            deleted = await asyncio.to_thread(
                delete_lesson,
                ctx.settings.vault_path,
                lesson.lesson_id,
                timeout_seconds=VAULT_LOCK_WAIT_SECONDS,
            )
        except VaultLockTimeout:
            await ctx.query.answer(
                "⏳ Идёт обработка, попробуйте позже.", show_alert=True
            )
            return True
        except Exception:
            logger.exception("Failed to delete lesson %s", lesson.lesson_id)
            await ctx.query.answer("Не удалось удалить урок.", show_alert=True)
            return True

        notice = (
            "Урок удалён." if deleted else "Урок уже был удалён ранее."
        )
        try:
            await render_learnings(
                ctx.bot,
                chat_id=ctx.chat_id,
                vault_path=ctx.settings.vault_path,
                page=ctx.session.learning_page,
                preferred_message_id=ctx.message_id,
                notice=notice,
            )
        except Exception:
            logger.exception("Failed to redraw learnings screen after delete")
            await ctx.query.answer(_SCREEN_REDRAW_FAILED_MESSAGE, show_alert=True)
            return True
        await ctx.query.answer()
        return True

    return False


async def _handle_goal_callback(ctx: _CallbackContext) -> bool:
    """Handle weekly ONE Big Thing view and edit prompt."""
    data = ctx.data
    if data == "menu:goal":
        await ctx.query.answer()
        await render_weekly_goal(
            ctx.bot,
            chat_id=ctx.chat_id,
            vault_path=ctx.settings.vault_path,
            preferred_message_id=ctx.message_id,
        )
        return True

    if data == "menu:goaledit":
        await ctx.query.answer()
        await ctx.state.update_data(
            menu_message_id=ctx.message_id,
            goal_entered_at=wait_started_at(),
        )
        await ctx.state.set_state(GoalEditState.waiting_for_text)
        await render_input(
            ctx.bot,
            chat_id=ctx.chat_id,
            message_id=ctx.message_id,
            screen="goal_input",
            back="menu:goal",
            vault_path=ctx.settings.vault_path,
            text=(
                "**Обновить цель недели (ONE Big Thing)**\n\n"
                "Отправь новым сообщением формулировку главной цели недели.\n"
                "Для отмены нажми «Назад» или отправь `-`."
            ),
        )
        return True

    return False


async def _handle_meeting_prep_callback(ctx: _CallbackContext) -> bool:
    """Handle deterministic meeting-prep screen."""
    if ctx.data != "menu:meetings":
        return False
    await ctx.query.answer("Собираю контекст к встречам…")
    calendar_url = str(getattr(ctx.settings, "calendar_ics_url", "") or "")
    await render_meeting_prep(
        ctx.bot,
        chat_id=ctx.chat_id,
        vault_path=ctx.settings.vault_path,
        calendar_ics_url=calendar_url,
        preferred_message_id=ctx.message_id,
    )
    return True


_CALLBACK_HANDLERS = (
    _handle_core_callback,
    _handle_jobhealth_callback,
    _handle_files_callback,
    _handle_queue_callback,
    _handle_weekly_callback,
    _handle_briefs_callback,
    _handle_learning_callback,
    _handle_goal_callback,
    _handle_meeting_prep_callback,
)


@router.callback_query(F.data.startswith("menu:"))
async def handle_menu_callback(
    query: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Dispatch inline dashboard callbacks to section sub-handlers."""
    data = query.data or ""
    if query.message is None:
        await query.answer()
        return

    if data == "menu:noop":
        await query.answer()
        return

    await state.clear()

    message = cast(Message, query.message)
    settings = get_settings()
    session = get_dashboard_session(
        message.chat.id, vault_path=getattr(settings, "vault_path", None)
    )
    ctx = _CallbackContext(
        query=query,
        bot=bot,
        state=state,
        message=message,
        chat_id=message.chat.id,
        message_id=message.message_id,
        data=data,
        settings=settings,
        session=session,
    )

    for handler in _CALLBACK_HANDLERS:
        if await handler(ctx):
            return

    await query.answer("Неизвестное действие.", show_alert=False)
