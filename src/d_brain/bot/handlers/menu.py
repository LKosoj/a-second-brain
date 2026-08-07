"""Inline dashboard and file browser handlers."""

import logging
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import cast

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from d_brain.bot.dashboard import (
    close_dashboard,
    file_browser_parent_dir,
    get_dashboard_session,
    page_fingerprint,
    render_brief_type,
    render_file_directory,
    render_file_roots,
    render_home,
    render_queue,
    render_queue_item,
    render_stats,
    render_weekly_review,
    send_browser_file,
)
from d_brain.bot.handlers.brief import start_brief_flow
from d_brain.bot.handlers.do import start_do_flow
from d_brain.bot.handlers.process import run_full_process, run_interactive_process
from d_brain.bot.replies import answer_rich_text, answer_text, edit_rich_text, edit_text
from d_brain.config import get_settings
from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_briefs import BRIEF_TYPES
from d_brain.services.compiled_enrich_report import (
    build_daily_digest,
    digest_path,
    read_pass_status,
    render_digest_note,
)
from d_brain.services.decisions_queue import (
    apply_response,
    mark_page_human_reviewed,
    queue_item_fingerprint,
)
from d_brain.services.file_browser import FileBrowserError
from d_brain.services.frontmatter import write_validated_vault_markdown
from d_brain.services.vault_lock import vault_write_lock

router = Router(name="menu")
logger = logging.getLogger(__name__)

_QUEUE_ITEM_STALE_MESSAGE = "Пункт очереди устарел, открой список заново."
_WEEKLY_REVIEW_STALE_MESSAGE = "Сводка недели устарела, открой экран заново."
# Both queue and weekly-review buttons write to the vault (page frontmatter,
# the queue document, the response journal). A write that fails for a reason
# neither handler can interpret -- a full disk, a read-only vault -- must
# still come back to the owner: an unanswered callback just spins until
# Telegram times it out, which reads as "the bot ignored me".
_QUEUE_ACTION_FAILED_MESSAGE = (
    "Не удалось применить действие — часть изменений могла не сохраниться. "
    "Открой список заново."
)
# The redraw that follows a *successful* write is a different failure: the
# vault already has the owner's decision, only the screen could not be
# rebuilt (``render_queue``/``render_weekly_review`` re-read every
# ``compiled/**`` page, which the nightly pass can be moving underneath
# them). Saying "не удалось" there would be a lie that invites a second tap
# on a decision already applied, so this says the opposite -- and it must
# still reach the owner, because an unanswered callback spins until
# Telegram's own timeout.
_SCREEN_REDRAW_FAILED_MESSAGE = (
    "Действие применено, но список не обновился. Открой его заново."
)
# The file browser's own version of the same distinction (code review): the
# document is already in the owner's chat when the redraw below it runs, so
# reporting "не удалось открыть файл" there would be a lie about work that
# succeeded -- and would invite a second tap that sends the file twice.
_FILE_SENT_REDRAW_FAILED_MESSAGE = (
    "Файл отправлен, но список не обновился. Открой его заново."
)

# ``_read_enrich_pass_status`` used to be defined locally here (задача L
# code review, defect 5); it now lives in ``compiled_enrich_report.py`` as
# the public ``read_pass_status`` (задача N) so the CLI and the nightly
# ``maintenance.compiled-digest`` cycle can share it too. Kept under its old
# private name so this module's own tests (which call that name directly)
# stay unchanged.
_read_enrich_pass_status = read_pass_status


async def _deliver_daily_digest(message: Message, *, vault_path: str | Path) -> None:
    """Build and file today's compiled-enrichment digest -- mirrors
    ``run_compiled_digest.py``'s CLI write path (задача L, ТЗ 7.1). ``pass_status``
    is read from the real pass journal via ``_read_enrich_pass_status`` (the
    CLI and the nightly ``maintenance.compiled-digest`` cycle read the same
    journal now too -- задача N), so a quiet day (no work, no error, empty
    decisions queue) is correctly suppressed for this button. An empty
    result is a friendly "no changes today" message, not an error.
    """
    resolved_vault_path = Path(vault_path)
    status_msg = await answer_text(message, "⏳ Строю дайджест...")
    today = date.today()

    try:
        digest = build_daily_digest(
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
        with vault_write_lock(resolved_vault_path) as lock:
            write_validated_vault_markdown(
                resolved_vault_path,
                digest_path(resolved_vault_path, today),
                render_digest_note(today, digest),
                manifest=manifest,
                existing_lock=lock,
            )
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
        # Telegram refuses an edit whose text is byte-identical to what is
        # already on screen -- pressing "Дайджест" twice on the same day.
        pass
    except Exception:
        logger.exception("Failed to deliver compiled digest")

    # The edit failed for any reason: the digest is already written to the
    # vault, so it must still reach the owner. Falling through without this
    # would leave "⏳ Строю дайджест..." on screen forever -- looking like a
    # hung build rather than a delivered one.
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
    """Render one browser directory, falling back to the root picker.

    ``menu:filesroot:`` already did this inline; the refresh/up/page/enter
    buttons called ``render_file_directory`` bare (code review). All four
    re-read the directory from disk on every tap, and the nightly pass can
    archive or move it underneath an open browser screen -- so the raise
    escaped into aiogram's dispatcher, which logs and returns. The button was
    left spinning until Telegram timed the callback out, which reads as "the
    bot ignored me", and the screen still showed the folder that is gone.
    """
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
async def cmd_menu(message: Message, bot: Bot) -> None:
    """Open or recreate the inline dashboard."""
    settings = get_settings()
    await render_home(
        bot,
        chat_id=message.chat.id,
        vault_path=settings.vault_path,
        user_id=message.from_user.id if message.from_user else message.chat.id,
    )


@router.message(Command("files"))
async def cmd_files(message: Message, bot: Bot) -> None:
    """Open the file browser root picker."""
    settings = get_settings()
    await render_file_roots(
        bot,
        chat_id=message.chat.id,
        vault_path=settings.vault_path,
    )


@router.callback_query(F.data.startswith("menu:"))
async def handle_menu_callback(
    query: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Handle all inline dashboard callbacks."""
    data = query.data or ""
    if query.message is None:
        await query.answer()
        return

    message = cast(Message, query.message)
    chat_id = message.chat.id
    message_id = message.message_id
    settings = get_settings()
    session = get_dashboard_session(chat_id)

    if data == "menu:noop":
        await query.answer()
        return

    if data == "menu:home":
        await render_home(
            bot,
            chat_id=chat_id,
            vault_path=settings.vault_path,
            user_id=query.from_user.id,
            preferred_message_id=message_id,
        )
        await query.answer()
        return

    if data == "menu:stats":
        await render_stats(
            bot,
            chat_id=chat_id,
            vault_path=settings.vault_path,
            user_id=query.from_user.id,
            preferred_message_id=message_id,
        )
        await query.answer()
        return

    if data == "menu:files":
        await render_file_roots(
            bot,
            chat_id=chat_id,
            vault_path=settings.vault_path,
            preferred_message_id=message_id,
        )
        await query.answer()
        return

    if data.startswith("menu:filesroot:"):
        root_id = data.removeprefix("menu:filesroot:")
        try:
            await render_file_directory(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                root_id=root_id,
                preferred_message_id=message_id,
            )
        except FileBrowserError:
            await render_file_roots(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                preferred_message_id=message_id,
                notice="Не удалось открыть выбранный раздел.",
            )
        await query.answer()
        return

    if data == "menu:filesroots":
        await render_file_roots(
            bot,
            chat_id=chat_id,
            vault_path=settings.vault_path,
            preferred_message_id=message_id,
        )
        await query.answer()
        return

    if data == "menu:filesrefresh":
        if session.file_root_id is None:
            await render_file_roots(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                preferred_message_id=message_id,
                notice="Сначала выбери раздел.",
            )
        else:
            await _render_file_directory_or_roots(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                root_id=session.file_root_id,
                current_dir=session.file_current_dir,
                page=session.file_page,
                preferred_message_id=message_id,
            )
        await query.answer()
        return

    if data == "menu:filesup":
        if session.file_root_id is None:
            await render_file_roots(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                preferred_message_id=message_id,
                notice="Сначала выбери раздел.",
            )
        else:
            await _render_file_directory_or_roots(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                root_id=session.file_root_id,
                current_dir=file_browser_parent_dir(session.file_current_dir),
                page=0,
                preferred_message_id=message_id,
            )
        await query.answer()
        return

    if data.startswith("menu:filespage:"):
        if session.file_root_id is None:
            await render_file_roots(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                preferred_message_id=message_id,
                notice="Сначала выбери раздел.",
            )
        else:
            try:
                page = int(data.removeprefix("menu:filespage:"))
            except ValueError:
                page = 0
            await _render_file_directory_or_roots(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                root_id=session.file_root_id,
                current_dir=session.file_current_dir,
                page=page,
                preferred_message_id=message_id,
            )
        await query.answer()
        return

    if data.startswith("menu:filesentry:"):
        if session.file_root_id is None:
            await render_file_roots(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                preferred_message_id=message_id,
                notice="Сначала выбери раздел.",
            )
            await query.answer()
            return

        try:
            entry_index = int(data.removeprefix("menu:filesentry:"))
        except ValueError:
            entry_index = -1

        if entry_index < 0 or entry_index >= len(session.file_entries):
            await query.answer("Файл или папка не найдены.", show_alert=True)
            return

        entry = session.file_entries[entry_index]
        if entry.is_directory:
            await _render_file_directory_or_roots(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                root_id=session.file_root_id,
                current_dir=entry.relative_path,
                page=0,
                preferred_message_id=message_id,
            )
            await query.answer()
            return

        try:
            file_name = await send_browser_file(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                session=session,
                entry_index=entry_index,
            )
        except ValueError as error:
            if str(error) == "FILE_TOO_LARGE":
                await query.answer(
                    "Файл слишком большой для отправки через Telegram.",
                    show_alert=True,
                )
                return
            await query.answer("Не удалось отправить файл.", show_alert=True)
            return
        except (FileBrowserError, OSError):
            await query.answer("Не удалось открыть файл.", show_alert=True)
            return
        except Exception:
            # The send itself fails through aiogram, not the filesystem (code
            # review): ``TelegramNetworkError``/``TelegramRetryAfter`` derive
            # from ``TelegramAPIError``, not ``OSError``, so neither handler
            # above ever saw them. They escaped into the dispatcher and left
            # the button spinning until Telegram timed the callback out.
            logger.exception("Failed to send browser file")
            await query.answer("Не удалось отправить файл.", show_alert=True)
            return

        # Guarded apart from the send above (code review): the document has
        # already reached the owner's chat, so a redraw that fails must not
        # come back as "Не удалось открыть файл." -- the same false report the
        # queue screens keep apart via ``_SCREEN_REDRAW_FAILED_MESSAGE``. The
        # redraw re-reads the directory, which the nightly pass can archive
        # between the send and this call.
        try:
            await render_file_directory(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                root_id=session.file_root_id,
                current_dir=session.file_current_dir,
                page=session.file_page,
                preferred_message_id=message_id,
                notice=f"Файл отправлен: {file_name}",
            )
        except Exception:
            logger.exception("Failed to redraw the file browser after a send")
            await query.answer(_FILE_SENT_REDRAW_FAILED_MESSAGE, show_alert=True)
            return

        await query.answer(f"Отправлен: {file_name}")
        return

    if data == "menu:process":
        await query.answer("Запускаю preview...")
        await run_interactive_process(message, actor_id=query.from_user.id)
        return

    if data == "menu:processfull":
        await query.answer("Запускаю полную обработку...")
        await run_full_process(message, actor_id=query.from_user.id)
        return

    if data == "menu:do":
        await query.answer()
        await start_do_flow(message, state)
        return

    if data == "menu:digest":
        await query.answer("Строю дайджест...")
        await _deliver_daily_digest(message, vault_path=settings.vault_path)
        return

    if data == "menu:queue":
        await render_queue(
            bot,
            chat_id=chat_id,
            vault_path=settings.vault_path,
            preferred_message_id=message_id,
        )
        await query.answer()
        return

    if data.startswith("menu:queuepage:"):
        try:
            page = int(data.removeprefix("menu:queuepage:"))
        except ValueError:
            page = 0
        await render_queue(
            bot,
            chat_id=chat_id,
            vault_path=settings.vault_path,
            page=page,
            preferred_message_id=message_id,
        )
        await query.answer()
        return

    if data.startswith("menu:queueitem:"):
        remainder = data.removeprefix("menu:queueitem:")
        index_str, _sep, fingerprint = remainder.partition(":")
        try:
            entry_index = int(index_str)
        except ValueError:
            entry_index = -1

        # Checked the same way as menu:queueact: below (code review). The
        # bounds check inside render_queue_item cannot tell a stale index
        # from a live one -- see build_queue_keyboard for how an old queue
        # message keeps working buttons after the list has moved on.
        if (
            entry_index < 0
            or entry_index >= len(session.queue_items)
            or queue_item_fingerprint(session.queue_items[entry_index])
            != fingerprint
        ):
            await query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
            return

        item = await render_queue_item(
            bot,
            chat_id=chat_id,
            index=entry_index,
            preferred_message_id=message_id,
        )
        if item is None:
            await query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
            return
        await query.answer()
        return

    if data.startswith("menu:queueact:"):
        remainder = data.removeprefix("menu:queueact:")
        index_str, _sep, rest = remainder.partition(":")
        fingerprint, _sep2, action_id = rest.partition(":")
        try:
            entry_index = int(index_str)
        except ValueError:
            entry_index = -1

        if entry_index < 0 or entry_index >= len(session.queue_items):
            await query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
            return

        item = session.queue_items[entry_index]
        if queue_item_fingerprint(item) != fingerprint:
            # The list shifted since this button was rendered (e.g. a
            # duplicated tap after an earlier response already shortened
            # session.queue_items) -- index alone can silently resolve to a
            # different, still-valid item (задача L code review, defect 4).
            # Refuse to act and redraw instead of applying to the wrong page.
            await render_queue(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                page=session.queue_page,
                preferred_message_id=message_id,
                notice=_QUEUE_ITEM_STALE_MESSAGE,
            )
            await query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
            return

        try:
            outcome = apply_response(Path(settings.vault_path), item, action_id)
        except ValueError:
            await query.answer("Действие недоступно для этого пункта.", show_alert=True)
            return
        except FileNotFoundError:
            # A duplicate response (double tap, or a retried Telegram
            # callback) can lose a race against itself inside apply_response
            # -- decisions_queue.py's own handlers already treat a page that
            # vanished mid-response as a graceful no-op everywhere else (see
            # its module docstring), so this is the same "stale, redraw"
            # outcome as a mismatched fingerprint above, not an uncaught
            # crash that leaves the button spinning until Telegram's timeout.
            await query.answer(_QUEUE_ITEM_STALE_MESSAGE, show_alert=True)
            return
        except Exception:
            # apply_response patches the page first and only then removes the
            # queue entry, journals the response and rewrites the queue
            # document -- a failure in between leaves those two out of sync,
            # so say so instead of letting the callback crash.
            logger.exception(
                "Queue action %s failed for %s", action_id, item.page
            )
            await query.answer(_QUEUE_ACTION_FAILED_MESSAGE, show_alert=True)
            return

        try:
            await render_queue(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                page=session.queue_page,
                preferred_message_id=message_id,
                notice=outcome.message,
            )
        except Exception:
            logger.exception("Queue redraw failed after applying %s", action_id)
            await query.answer(_SCREEN_REDRAW_FAILED_MESSAGE, show_alert=True)
            return
        await query.answer()
        return

    if data == "menu:weekly":
        await render_weekly_review(
            bot,
            chat_id=chat_id,
            vault_path=settings.vault_path,
            preferred_message_id=message_id,
        )
        await query.answer()
        return

    if data.startswith("menu:weeklyreview:"):
        fingerprint = data.removeprefix("menu:weeklyreview:")
        review = session.weekly_review
        if review is None or review.review_pick is None:
            await query.answer(_WEEKLY_REVIEW_STALE_MESSAGE, show_alert=True)
            return

        rel_path, _title = review.review_pick
        if page_fingerprint(rel_path) != fingerprint:
            # Same principle as the queue's index+fingerprint check: the
            # screen was refreshed and the picked page changed between
            # render and tap -- refuse to act on the wrong page.
            await render_weekly_review(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                preferred_message_id=message_id,
                notice=_WEEKLY_REVIEW_STALE_MESSAGE,
            )
            await query.answer(_WEEKLY_REVIEW_STALE_MESSAGE, show_alert=True)
            return

        try:
            outcome = mark_page_human_reviewed(Path(settings.vault_path), rel_path)
        except Exception:
            logger.exception("Weekly review mark failed for %s", rel_path)
            await query.answer(_QUEUE_ACTION_FAILED_MESSAGE, show_alert=True)
            return
        try:
            await render_weekly_review(
                bot,
                chat_id=chat_id,
                vault_path=settings.vault_path,
                preferred_message_id=message_id,
                notice=outcome.message,
            )
        except Exception:
            logger.exception("Weekly review redraw failed for %s", rel_path)
            await query.answer(_SCREEN_REDRAW_FAILED_MESSAGE, show_alert=True)
            return
        await query.answer()
        return

    if data == "menu:brief":
        await render_brief_type(
            bot,
            chat_id=chat_id,
            preferred_message_id=message_id,
        )
        await query.answer()
        return

    if data.startswith("menu:brieftype:"):
        brief_type = data.removeprefix("menu:brieftype:")
        if brief_type not in BRIEF_TYPES:
            await query.answer("Неизвестный тип брифа.", show_alert=True)
            return
        await query.answer()
        await start_brief_flow(message, state, brief_type)
        return

    if data == "menu:close":
        await close_dashboard(bot, chat_id=chat_id, preferred_message_id=message_id)
        await query.answer("Меню закрыто. Открыть снова: /menu")
        return

    await query.answer("Неизвестное действие.", show_alert=False)
