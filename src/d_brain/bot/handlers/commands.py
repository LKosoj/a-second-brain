"""Command handlers for /start, /help, /stats."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from d_brain.bot.dashboard import build_help_text, build_stats_text
from d_brain.bot.replies import answer_text
from d_brain.config import get_settings
from d_brain.services.session import SessionStore

router = Router(name="commands")


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Handle /start command."""
    await answer_text(message, build_help_text())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command."""
    await answer_text(message, build_help_text())


async def _send_stats(message: Message) -> None:
    """Send the unified stats command response."""
    user_id = message.from_user.id if message.from_user else 0
    settings = get_settings()

    session = SessionStore(settings.vault_path)
    session.append(user_id, "command", cmd="/stats")
    await answer_text(message, build_stats_text(settings.vault_path, user_id))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Handle /stats command."""
    await _send_stats(message)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Handle legacy /status command as an alias to /stats."""
    await _send_stats(message)
