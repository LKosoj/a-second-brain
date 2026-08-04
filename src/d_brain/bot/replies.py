"""Helpers for text replies that should dismiss custom reply keyboards."""

from __future__ import annotations

import html
import logging
import re
from typing import Any, cast

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputRichMessage, Message

from d_brain.bot.formatters import (
    TELEGRAM_TEXT_LIMIT,
    markdown_to_plain_text,
    normalize_markdown_input,
)
from d_brain.services.telegram_markup import markdown_to_html, markdown_to_markdown_v2

logger = logging.getLogger(__name__)
RICH_TEXT_LIMIT = 32768
RICH_MEDIA_MARKUP_RE = re.compile(
    r"!\[[^\]]*\]\(|<(?:audio|img|tg-collage|tg-map|tg-slideshow|video)\b",
    re.IGNORECASE,
)


def text_reply_kwargs(**kwargs: Any) -> dict[str, Any]:
    """Pass text reply kwargs through unchanged."""
    return kwargs


def _should_retry_without_reply_markup(exc: TypeError) -> bool:
    """Handle narrow fake message objects that do not accept reply_markup."""
    return "reply_markup" in str(exc)


def _telegram_message_payload(
    text: str,
    *,
    parse_mode: str | None,
) -> tuple[str, str | None]:
    """Normalize one logical text payload for MarkdownV2 Telegram delivery."""
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    return markdown_to_markdown_v2(markdown), "MarkdownV2"


def _render_html_document(text: str, *, parse_mode: str | None) -> bytes:
    """Render one long markdown payload into a standalone HTML document."""
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    body = markdown_to_html(markdown)
    if not body:
        body = f"<pre>{html.escape(markdown_to_plain_text(markdown))}</pre>"
    document = (
        "<!doctype html>\n"
        "<html lang=\"ru\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "  <title>d-brain message</title>\n"
        "  <style>\n"
        "    body { font-family: system-ui, sans-serif; margin: 24px; "
        "line-height: 1.5; }\n"
        "    pre { white-space: pre-wrap; word-break: break-word; "
        "font-family: ui-monospace, monospace; }\n"
        "  </style>\n"
        "</head>\n"
        f"<body>\n{body}\n</body>\n"
        "</html>\n"
    )
    return document.encode("utf-8")


async def _answer_one(message: Message, text: str, **kwargs: Any) -> Message:
    """Send one Telegram answer payload."""
    payload = text_reply_kwargs(**kwargs)
    try:
        return await message.answer(text, **payload)
    except TypeError as exc:
        if not _should_retry_without_reply_markup(exc):
            raise
        payload.pop("reply_markup", None)
        return await message.answer(text, **payload)


async def _answer_document(
    message: Message,
    document: BufferedInputFile,
    **kwargs: Any,
) -> Message:
    """Send one Telegram document answer payload."""
    payload = text_reply_kwargs(**kwargs)
    try:
        return await message.answer_document(document, **payload)
    except TypeError as exc:
        if not _should_retry_without_reply_markup(exc):
            raise
        payload.pop("reply_markup", None)
        return await message.answer_document(document, **payload)


async def _send_one(bot: Bot, *, chat_id: int, text: str, **kwargs: Any) -> Message:
    """Send one Telegram bot payload."""
    payload = text_reply_kwargs(**kwargs)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            **payload,
        )
    except TypeError as exc:
        if not _should_retry_without_reply_markup(exc):
            raise
        payload.pop("reply_markup", None)
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            **payload,
        )


async def _send_document(
    bot: Bot,
    *,
    chat_id: int,
    document: BufferedInputFile,
    **kwargs: Any,
) -> Message:
    """Send one Telegram document payload."""
    payload = text_reply_kwargs(**kwargs)
    try:
        return await bot.send_document(
            chat_id=chat_id,
            document=document,
            **payload,
        )
    except TypeError as exc:
        if not _should_retry_without_reply_markup(exc):
            raise
        payload.pop("reply_markup", None)
        return await bot.send_document(
            chat_id=chat_id,
            document=document,
            **payload,
        )


def _rich_markdown_payload(
    text: str,
    *,
    parse_mode: str | None,
) -> str | None:
    """Return normalized text-only rich markdown when it is safe to send."""
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    if RICH_MEDIA_MARKUP_RE.search(markdown):
        return None
    return markdown


async def _answer_rich_one(
    message: Message,
    rich_message: InputRichMessage,
    **kwargs: Any,
) -> Message:
    """Send one Telegram rich answer payload."""
    payload = text_reply_kwargs(**kwargs)
    try:
        return await message.answer_rich(rich_message, **payload)
    except TypeError as exc:
        if not _should_retry_without_reply_markup(exc):
            raise
        payload.pop("reply_markup", None)
        return await message.answer_rich(rich_message, **payload)


async def _send_rich_one(
    bot: Bot,
    *,
    chat_id: int,
    rich_message: InputRichMessage,
    **kwargs: Any,
) -> Message:
    """Send one Telegram bot rich payload."""
    payload = text_reply_kwargs(**kwargs)
    try:
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich_message,
            **payload,
        )
    except TypeError as exc:
        if not _should_retry_without_reply_markup(exc):
            raise
        payload.pop("reply_markup", None)
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich_message,
            **payload,
        )


async def answer_text(message: Message, text: str, **kwargs: Any) -> Message:
    """Send one logical answer; long payloads become one HTML document."""
    parse_mode = kwargs.get("parse_mode")
    payload_text, payload_parse_mode = _telegram_message_payload(
        text,
        parse_mode=parse_mode,
    )
    if len(payload_text) <= TELEGRAM_TEXT_LIMIT:
        payload_kwargs = dict(kwargs)
        payload_kwargs["parse_mode"] = payload_parse_mode
        return await _answer_one(message, payload_text, **payload_kwargs)
    document = BufferedInputFile(
        _render_html_document(text, parse_mode=parse_mode),
        filename="d-brain-message.html",
    )
    return await _answer_document(message, document, caption=None)


async def answer_rich_text(message: Message, text: str, **kwargs: Any) -> Message:
    """Send a text-only rich answer with a narrow legacy fallback."""
    parse_mode = kwargs.get("parse_mode")
    markdown = _rich_markdown_payload(text, parse_mode=parse_mode)
    if markdown is None:
        return await answer_text(message, text, **kwargs)
    if len(markdown) > RICH_TEXT_LIMIT:
        document = BufferedInputFile(
            _render_html_document(text, parse_mode=parse_mode),
            filename="d-brain-message.html",
        )
        return await _answer_document(message, document, caption=None)

    payload = dict(kwargs)
    payload.pop("parse_mode", None)
    try:
        return await _answer_rich_one(
            message,
            InputRichMessage(markdown=markdown),
            **payload,
        )
    except TelegramBadRequest as exc:
        logger.warning("Rich answer rejected, using legacy delivery: %s", exc)
        return await answer_text(message, text, **kwargs)


async def send_text(bot: Bot, *, chat_id: int, text: str, **kwargs: Any) -> Message:
    """Send one logical text message; long payloads become one HTML document."""
    parse_mode = kwargs.get("parse_mode")
    payload_text, payload_parse_mode = _telegram_message_payload(
        text,
        parse_mode=parse_mode,
    )
    if len(payload_text) <= TELEGRAM_TEXT_LIMIT:
        payload_kwargs = dict(kwargs)
        payload_kwargs["parse_mode"] = payload_parse_mode
        return await _send_one(
            bot,
            chat_id=chat_id,
            text=payload_text,
            **payload_kwargs,
        )
    document = BufferedInputFile(
        _render_html_document(text, parse_mode=parse_mode),
        filename="d-brain-message.html",
    )
    return await _send_document(
        bot,
        chat_id=chat_id,
        document=document,
        caption=None,
    )


async def send_rich_text(
    bot: Bot,
    *,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message:
    """Send text-only rich content with a narrow legacy fallback."""
    parse_mode = kwargs.get("parse_mode")
    markdown = _rich_markdown_payload(text, parse_mode=parse_mode)
    if markdown is None:
        return await send_text(bot, chat_id=chat_id, text=text, **kwargs)
    if len(markdown) > RICH_TEXT_LIMIT:
        document = BufferedInputFile(
            _render_html_document(text, parse_mode=parse_mode),
            filename="d-brain-message.html",
        )
        return await _send_document(
            bot,
            chat_id=chat_id,
            document=document,
            caption=None,
        )

    payload = dict(kwargs)
    payload.pop("parse_mode", None)
    try:
        return await _send_rich_one(
            bot,
            chat_id=chat_id,
            rich_message=InputRichMessage(markdown=markdown),
            **payload,
        )
    except TelegramBadRequest as exc:
        logger.warning("Rich message rejected, using legacy delivery: %s", exc)
        return await send_text(bot, chat_id=chat_id, text=text, **kwargs)


async def edit_text(message: Message, text: str, **kwargs: Any) -> Message:
    """Edit one status message; long payloads become one HTML document."""
    parse_mode = kwargs.get("parse_mode")
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    payload_text = markdown_to_markdown_v2(markdown)
    payload_parse_mode: str | None = "MarkdownV2"
    if len(payload_text) <= TELEGRAM_TEXT_LIMIT:
        payload = dict(kwargs)
        payload["parse_mode"] = payload_parse_mode
        return cast(Message, await message.edit_text(payload_text, **payload))

    payload = dict(kwargs)
    payload["parse_mode"] = None
    await message.edit_text(
        "Сообщение слишком длинное, отправляю HTML-файлом.",
        **payload,
    )
    document = BufferedInputFile(
        _render_html_document(text, parse_mode=parse_mode),
        filename="d-brain-message.html",
    )
    return await _answer_document(message, document, caption=None)


async def edit_rich_text(message: Message, text: str, **kwargs: Any) -> Message:
    """Edit a message to text-only rich content with a narrow fallback."""
    parse_mode = kwargs.get("parse_mode")
    markdown = _rich_markdown_payload(text, parse_mode=parse_mode)
    if markdown is None:
        return await edit_text(message, text, **kwargs)
    if len(markdown) > RICH_TEXT_LIMIT:
        payload = dict(kwargs)
        payload["parse_mode"] = None
        await message.edit_text(
            "Сообщение слишком длинное, отправляю HTML-файлом.",
            **payload,
        )
        document = BufferedInputFile(
            _render_html_document(text, parse_mode=parse_mode),
            filename="d-brain-message.html",
        )
        return await _answer_document(message, document, caption=None)

    payload = dict(kwargs)
    payload.pop("parse_mode", None)
    try:
        return cast(
            Message,
            await message.edit_text(
                rich_message=InputRichMessage(markdown=markdown),
                **payload,
            ),
        )
    except TelegramBadRequest as exc:
        logger.warning("Rich edit rejected, using legacy delivery: %s", exc)
        return await edit_text(message, text, **kwargs)
