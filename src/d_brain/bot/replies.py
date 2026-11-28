"""Helpers for text replies that should dismiss custom reply keyboards."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InputRichMessage,
    Message,
)

from d_brain.bot.formatters import (
    TELEGRAM_TEXT_LIMIT,
    markdown_to_plain_text,
    normalize_markdown_input,
)
from d_brain.services.telegram_delivery import (
    MAX_INLINE_IMAGE_BYTES as MAX_INLINE_IMAGE_BYTES,
)
from d_brain.services.telegram_delivery import (
    RICH_MEDIA_MARKUP_RE as RICH_MEDIA_MARKUP_RE,
)
from d_brain.services.telegram_delivery import (
    RICH_TEXT_LIMIT as RICH_TEXT_LIMIT,
)
from d_brain.services.telegram_delivery import (
    render_html_document,
    rich_markdown_payload,
    should_retry_without_reply_markup,
    telegram_message_payload,
)
from d_brain.services.telegram_delivery import (
    send_rich_text as send_rich_text,
)
from d_brain.services.telegram_delivery import (
    send_text as send_text,
)
from d_brain.services.telegram_delivery import (
    text_reply_kwargs as text_reply_kwargs,
)
from d_brain.services.telegram_markup import (
    extract_attachment_image_refs,
    markdown_to_markdown_v2,
)

logger = logging.getLogger(__name__)
_render_html_document = render_html_document
_rich_markdown_payload = rich_markdown_payload
_should_retry_without_reply_markup = should_retry_without_reply_markup
_telegram_message_payload = telegram_message_payload


async def answer_callback_once(
    query: CallbackQuery, text: str | None = None, *, show_alert: bool = False
) -> None:
    """Final callback answer that tolerates an expired callback.

    A large file can take longer to send than Telegram keeps the callback
    alive; the file is already delivered by then, so a refused answer is only
    worth a log line.
    """
    try:
        await query.answer(text, show_alert=show_alert)
    except TelegramBadRequest as error:
        logger.warning("Callback answer was rejected: %s", error)


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


async def answer_text(message: Message, text: str, **kwargs: Any) -> Message:
    """Send one logical answer; long payloads become one HTML document.

    A payload with attachment-image links always becomes an HTML document
    (bypassing the length threshold): those images are only ever delivered
    inlined inside that document, never as separate files.
    """
    parse_mode = kwargs.get("parse_mode")
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    has_inline_images = bool(extract_attachment_image_refs(markdown))
    payload_text, payload_parse_mode = _telegram_message_payload(
        text,
        parse_mode=parse_mode,
    )
    if not has_inline_images and len(payload_text) <= TELEGRAM_TEXT_LIMIT:
        payload_kwargs = dict(kwargs)
        payload_kwargs["parse_mode"] = payload_parse_mode
        try:
            return await _answer_one(message, payload_text, **payload_kwargs)
        except TelegramBadRequest as exc:
            if payload_kwargs.get("parse_mode") is None:
                raise
            logger.warning("Formatted answer rejected, retrying plain text: %s", exc)
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["parse_mode"] = None
            plain_text = markdown_to_plain_text(markdown) or text
            return await _answer_one(message, plain_text, **fallback_kwargs)
    document = BufferedInputFile(
        _render_html_document(text, parse_mode=parse_mode),
        filename="d-brain-message.html",
    )
    return await _answer_document(
        message, document, caption=None, reply_markup=kwargs.get("reply_markup")
    )


async def answer_files(message: Message, paths: list[str]) -> list[Message]:
    """Send user-facing files produced by an assistant request."""
    sent: list[Message] = []
    for raw_path in paths:
        path = Path(raw_path)
        sent.append(
            await message.answer_document(FSInputFile(path, filename=path.name))
        )
    return sent


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


async def edit_text(message: Message, text: str, **kwargs: Any) -> Message:
    """Edit one status message; long payloads become one HTML document."""
    parse_mode = kwargs.get("parse_mode")
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    payload_text = markdown_to_markdown_v2(markdown)
    payload_parse_mode: str | None = "MarkdownV2"
    if len(payload_text) <= TELEGRAM_TEXT_LIMIT:
        payload = dict(kwargs)
        payload["parse_mode"] = payload_parse_mode
        try:
            return cast(Message, await message.edit_text(payload_text, **payload))
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                raise
            logger.warning("Formatted edit rejected, retrying plain text: %s", exc)
            fallback_payload = dict(kwargs)
            fallback_payload["parse_mode"] = None
            plain_text = markdown_to_plain_text(markdown) or text
            return cast(
                Message,
                await message.edit_text(plain_text, **fallback_payload),
            )

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
