"""Link Telegram replies to the questions and answers they continue."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from aiogram.types import Message

from d_brain.services.document_extractors import (
    detect_document_format,
    extract_document_payload,
)
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


async def reply_context(message: Message, vault_path: Path, user_id: int) -> str | None:
    """Supply the selected discussion instead of unrelated daily messages."""
    if not is_reply_to_bot(message):
        return None
    reply = message.reply_to_message
    assert reply is not None
    store = SessionStore(vault_path)
    turns = store.get_conversation(user_id, message.chat.id, reply.message_id)
    if not turns and reply.document:
        bot = message.bot
        assert bot is not None
        document = reply.document
        name = document.file_name or "document"
        file_format = detect_document_format(name, document.mime_type or "")
        if file_format is None:
            raise ValueError(
                "Не могу прочитать этот формат файла. Пришлите текст отчёта."
            )
        downloaded = await bot.download(document)
        if downloaded is None:
            raise ValueError("Не удалось скачать файл предыдущего ответа.")
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"document.{file_format}"
            path.write_bytes(downloaded.read())
            extracted = await asyncio.to_thread(
                extract_document_payload,
                path,
                file_format=file_format,
                original_name=name,
            )
        text = str(extracted["plain_text"]).strip()
        if not text:
            raise ValueError("В файле предыдущего ответа не удалось прочитать текст.")
        if reply.caption:
            text = f"{reply.caption}\n\n{text}"
        store.save_answer(user_id, message.chat.id, reply.message_id, "", text)
        turns = store.get_conversation(user_id, message.chat.id, reply.message_id)
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
