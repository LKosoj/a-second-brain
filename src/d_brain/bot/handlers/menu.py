"""Inline dashboard and file browser handlers."""

from typing import cast

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from d_brain.bot.dashboard import (
    close_dashboard,
    file_browser_parent_dir,
    get_dashboard_session,
    render_file_directory,
    render_file_roots,
    render_home,
    render_stats,
    send_browser_file,
)
from d_brain.bot.handlers.do import start_do_flow
from d_brain.bot.handlers.process import run_full_process, run_interactive_process
from d_brain.config import get_settings
from d_brain.services.file_browser import FileBrowserError

router = Router(name="menu")


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
            await render_file_directory(
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
            await render_file_directory(
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
            await render_file_directory(
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
            await render_file_directory(
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
            await query.answer(f"Отправлен: {file_name}")
        except ValueError as error:
            if str(error) == "FILE_TOO_LARGE":
                await query.answer(
                    "Файл слишком большой для отправки через Telegram.",
                    show_alert=True,
                )
                return
            await query.answer("Не удалось отправить файл.", show_alert=True)
        except (FileBrowserError, OSError):
            await query.answer("Не удалось открыть файл.", show_alert=True)
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

    if data == "menu:close":
        await close_dashboard(bot, chat_id=chat_id, preferred_message_id=message_id)
        await query.answer("Меню закрыто. Открыть снова: /menu")
        return

    await query.answer("Неизвестное действие.", show_alert=False)
