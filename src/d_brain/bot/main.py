"""Telegram bot initialization and polling."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, TelegramObject

from d_brain.config import Settings

logger = logging.getLogger(__name__)


def create_bot(settings: Settings) -> Bot:
    """Create and configure the Telegram bot."""
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )


def create_dispatcher() -> Dispatcher:
    """Create and configure the dispatcher with routers."""
    from d_brain.bot.handlers import (
        brief,
        commands,
        do,
        document,
        edit,
        fallback,
        find,
        forward,
        menu,
        photo,
        process,
        reminders,
        tasks,
        text,
        voice,
        why,
    )

    # Use memory storage for FSM (required for /do command state)
    dp = Dispatcher(storage=MemoryStorage())

    # Register routers - ORDER MATTERS
    dp.include_router(commands.router)
    dp.include_router(edit.router)  # /undo and edited_message; before text's catch-all
    dp.include_router(find.router)  # /find and its wait state; before do/why/brief
    dp.include_router(menu.router)
    dp.include_router(tasks.router)
    dp.include_router(process.router)
    dp.include_router(do.router)  # Before voice/text to catch FSM state
    dp.include_router(why.router)  # Before voice/text to catch FSM state
    dp.include_router(brief.router)  # Before voice/text to catch FSM state
    dp.include_router(voice.router)
    dp.include_router(photo.router)
    dp.include_router(document.router)
    dp.include_router(forward.router)
    dp.include_router(reminders.router)  # Before text.router (catch-all for text)
    dp.include_router(text.router)  # Before fallback.router
    dp.include_router(fallback.router)  # Must be last (unsupported-type stub)
    return dp


MiddlewareHandler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
MiddlewareType = Callable[
    [MiddlewareHandler, TelegramObject, dict[str, Any]],
    Awaitable[Any],
]


_USER_BEARING_UPDATE_FIELDS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "shipping_query",
    "pre_checkout_query",
    "poll_answer",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
)


def create_auth_middleware(settings: Settings) -> MiddlewareType:
    """Create middleware to check user authorization."""

    async def auth_middleware(
        handler: MiddlewareHandler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = None
        for field in _USER_BEARING_UPDATE_FIELDS:
            sub_event = getattr(event, field, None)
            if sub_event is None:
                continue
            user = getattr(sub_event, "from_user", None)
            if user is not None:
                break

        if user is None:
            logger.warning("Access denied: update has no user context")
            return None

        if user.id != settings.owner_telegram_id:
            logger.warning("Unauthorized access attempt from user %s", user.id)
            return None

        # The owner may also sit in groups with the bot; the bot is personal,
        # so only private chats are served. A callback carries its chat on
        # the message the button is attached to.
        chat = getattr(sub_event, "chat", None) or getattr(
            getattr(sub_event, "message", None), "chat", None
        )
        chat_type = getattr(chat, "type", None)
        chat_type = getattr(chat_type, "value", chat_type)
        if chat_type is not None and chat_type != "private":
            logger.info(
                "Ignoring update from non-private chat %s (type=%s)",
                getattr(chat, "id", None),
                chat_type,
            )
            return None

        return await handler(event, data)

    return auth_middleware


async def configure_bot_commands(bot: Bot) -> None:
    """Register the visible Telegram command menu."""
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="показать справку"),
            BotCommand(command="menu", description="открыть меню"),
            BotCommand(command="do", description="произвольный запрос"),
            BotCommand(command="undo", description="отменить последнюю запись"),
            BotCommand(command="reminders", description="список напоминаний"),
            BotCommand(command="why", description="почему страница так говорит"),
            BotCommand(command="find", description="поиск по заметкам"),
            BotCommand(command="help", description="справка"),
            BotCommand(command="stats", description="статистика"),
            BotCommand(command="files", description="файлы vault"),
            BotCommand(command="process", description="быстрый preview"),
            BotCommand(command="process_full", description="полная обработка"),
        ]
    )


async def run_bot(settings: Settings) -> None:
    """Run the bot with polling."""
    from d_brain.bot.handlers.reminders import run_reminder_ticker
    from d_brain.services import claude_tmux

    bot = create_bot(settings)
    dp = create_dispatcher()

    # Single-user bot: every update must come from the configured owner.
    dp.update.middleware(create_auth_middleware(settings))
    await configure_bot_commands(bot)
    # A crashed or SIGKILLed predecessor leaves its detached tmux servers running.
    # Housekeeping only: a failure here must not keep the bot from starting.
    try:
        await asyncio.to_thread(claude_tmux.cleanup_orphan_sessions)
    except Exception:
        logger.exception("Orphan tmux session cleanup failed")

    reminder_ticker_task = asyncio.create_task(
        run_reminder_ticker(bot, settings.vault_path)
    )

    logger.info("Starting bot polling...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await asyncio.to_thread(claude_tmux.shutdown_tmux_sessions)
        reminder_ticker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminder_ticker_task
        await bot.session.close()
