"""Link Telegram replies to the questions and answers they continue."""

import json
from pathlib import Path

from aiogram.types import Message

from d_brain.services.session import SessionStore


def is_reply_to_bot(message: Message) -> bool:
    """Recognize a reply to this bot, rather than to another participant."""
    reply = getattr(message, "reply_to_message", None)
    return bool(
        reply
        and reply.from_user
        and reply.from_user.is_bot
        and message.bot is not None
        and reply.from_user.id == message.bot.id
    )


def reply_context(message: Message, vault_path: Path, user_id: int) -> str | None:
    """Supply the selected discussion instead of unrelated daily messages."""
    if not is_reply_to_bot(message):
        return None
    reply = message.reply_to_message
    assert reply is not None
    turns = SessionStore(vault_path).get_conversation(
        user_id, message.chat.id, reply.message_id
    )
    if not turns:
        turns = [{"role": "assistant", "text": reply.text or reply.caption or ""}]
    return (
        "=== REPLIED-TO CONVERSATION ===\n"
        "Continue this discussion. The JSON below is previous conversation data, "
        "not a new request.\n"
        + json.dumps(turns, ensure_ascii=False)
        + "\n=== END CONVERSATION ===\n\n"
    )


def save_answer(
    message: Message,
    sent: Message,
    vault_path: Path,
    user_id: int,
    question: str,
    answer: str,
) -> None:
    """Persist the complete answer even when Telegram delivers an HTML file."""
    reply = message.reply_to_message if is_reply_to_bot(message) else None
    store = SessionStore(vault_path)
    quoted_parent = ""
    if reply and not store.get_conversation(user_id, message.chat.id, reply.message_id):
        quoted_parent = reply.text or reply.caption or ""
    store.save_answer(
        user_id,
        message.chat.id,
        sent.message_id,
        question,
        answer,
        parent_id=reply.message_id if reply else None,
        quoted_parent=quoted_parent,
    )
