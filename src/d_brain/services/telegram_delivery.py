"""Shared Telegram delivery helpers for CLI entrypoints."""

from __future__ import annotations

import asyncio

from aiogram import Bot

from d_brain.bot.replies import send_rich_text, send_text
from d_brain.config import get_settings


def _owner_telegram_target() -> tuple[str, int]:
    """Load the configured owner delivery target from runtime settings."""
    settings = get_settings()
    token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    chat_id = int(getattr(settings, "owner_telegram_id", 0) or 0)
    if not token:
        raise RuntimeError("telegram_bot_token is not configured")
    if not chat_id:
        raise RuntimeError("owner_telegram_id is not configured")
    return token, chat_id


async def _send_telegram_text_to_target(
    token: str,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    rich: bool = False,
) -> None:
    """Send one Telegram message and always close the bot session."""
    bot = Bot(token=token)
    try:
        sender = send_rich_text if rich else send_text
        await sender(bot, chat_id=chat_id, text=text, parse_mode=parse_mode)
    finally:
        await bot.session.close()


async def send_telegram_text(
    text: str,
    *,
    parse_mode: str | None = None,
    rich: bool = False,
) -> None:
    """Send one Telegram message to the configured owner."""
    token, chat_id = _owner_telegram_target()
    await _send_telegram_text_to_target(
        token,
        chat_id,
        text,
        parse_mode=parse_mode,
        rich=rich,
    )


def send_telegram_text_sync(
    text: str,
    *,
    parse_mode: str | None = None,
    rich: bool = False,
) -> None:
    """Synchronous owner-target wrapper for CLI entrypoints."""
    asyncio.run(
        send_telegram_text(
            text,
            parse_mode=parse_mode,
            rich=rich,
        )
    )


__all__ = [
    "send_telegram_text",
    "send_telegram_text_sync",
]
