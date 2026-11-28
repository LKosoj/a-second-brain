"""Shared Telegram delivery helpers for CLI entrypoints and bot replies."""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import mimetypes
import re
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    InputRichMessage,
    Message,
)

from d_brain.config import get_settings
from d_brain.services.file_browser import FileBrowserError, FileBrowserService
from d_brain.services.telegram_markup import (
    TELEGRAM_TEXT_LIMIT,
    extract_attachment_image_refs,
    markdown_to_document_html,
    markdown_to_markdown_v2,
    markdown_to_plain_text,
    normalize_markdown_input,
    replace_markdown_image_links,
)

logger = logging.getLogger(__name__)
RICH_TEXT_LIMIT = 32768
RICH_MEDIA_MARKUP_RE = re.compile(
    r"!\[[^\]]*\]\(|<(?:audio|img|tg-collage|tg-map|tg-slideshow|video)\b",
    re.IGNORECASE,
)
MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024


def _resolve_attachment_image_data_uri(href: str) -> str | None:
    """Resolve one `attachments/...` markdown href to an inline data URI."""
    relative_path = href.split("/", 1)[1] if "/" in href else ""
    if not relative_path:
        return None
    browser = FileBrowserService(get_settings().vault_path)
    try:
        _root, resolved_path = browser.resolve_file(
            root_id="attachments",
            relative_path=relative_path,
        )
        if resolved_path.stat().st_size > MAX_INLINE_IMAGE_BYTES:
            return None
        data = resolved_path.read_bytes()
    except (FileBrowserError, OSError):
        return None
    mime_type = (
        mimetypes.guess_type(resolved_path.name)[0] or "application/octet-stream"
    )
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def _embed_attachment_images(markdown: str) -> str:
    """Inline attachment-image markdown links as base64 data URIs.

    Returns the markdown unchanged (without calling ``get_settings()``) when
    there is nothing to embed, so callers without a configured environment
    (e.g. tests) are unaffected.
    """
    if not extract_attachment_image_refs(markdown):
        return markdown
    return replace_markdown_image_links(markdown, _resolve_attachment_image_data_uri)


def text_reply_kwargs(**kwargs: Any) -> dict[str, Any]:
    """Pass text reply kwargs through unchanged."""
    return kwargs


def should_retry_without_reply_markup(exc: TypeError) -> bool:
    """Handle narrow fake message objects that do not accept reply_markup."""
    return "reply_markup" in str(exc)


def telegram_message_payload(
    text: str,
    *,
    parse_mode: str | None,
) -> tuple[str, str | None]:
    """Normalize one logical text payload for MarkdownV2 Telegram delivery."""
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    return markdown_to_markdown_v2(markdown), "MarkdownV2"


def render_html_document(text: str, *, parse_mode: str | None) -> bytes:
    """Render one long markdown payload into a standalone HTML document."""
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    markdown = _embed_attachment_images(markdown)
    body = markdown_to_document_html(markdown)
    if not body:
        body = f"<pre>{html.escape(markdown_to_plain_text(markdown))}</pre>"
    document = (
        "<!doctype html>\n"
        '<html lang="ru">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "  <title>d-brain message</title>\n"
        "  <style>\n"
        "    body { font-family: system-ui, sans-serif; margin: 24px auto; "
        "max-width: 44rem; padding: 0 16px; line-height: 1.6; }\n"
        "    h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.6em 0 0.5em; }\n"
        "    pre { white-space: pre-wrap; word-break: break-word; "
        "background: #f5f5f5; padding: 12px; border-radius: 6px; }\n"
        "    code { font-family: ui-monospace, monospace; }\n"
        "    blockquote { margin: 1em 0; padding-left: 1em; "
        "border-left: 3px solid #ddd; color: #555; }\n"
        "    table { border-collapse: collapse; margin: 1em 0; }\n"
        "    th, td { border: 1px solid #ddd; padding: 6px 10px; "
        "text-align: left; }\n"
        "    img { max-width: 100%; }\n"
        "  </style>\n"
        "</head>\n"
        f"<body>\n{body}\n</body>\n"
        "</html>\n"
    )
    return document.encode("utf-8")


def rich_markdown_payload(
    text: str,
    *,
    parse_mode: str | None,
) -> str | None:
    """Return normalized text-only rich markdown when it is safe to send."""
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    if RICH_MEDIA_MARKUP_RE.search(markdown):
        return None
    return markdown


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
        if not should_retry_without_reply_markup(exc):
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
        if not should_retry_without_reply_markup(exc):
            raise
        payload.pop("reply_markup", None)
        return await bot.send_document(
            chat_id=chat_id,
            document=document,
            **payload,
        )


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
        if not should_retry_without_reply_markup(exc):
            raise
        payload.pop("reply_markup", None)
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich_message,
            **payload,
        )


async def send_text(bot: Bot, *, chat_id: int, text: str, **kwargs: Any) -> Message:
    """Send one logical text message; long payloads become one HTML document.

    A payload with attachment-image links always becomes an HTML document
    (bypassing the length threshold): those images are only ever delivered
    inlined inside that document, never as separate files.
    """
    parse_mode = kwargs.get("parse_mode")
    markdown = normalize_markdown_input(text, parse_mode=parse_mode)
    has_inline_images = bool(extract_attachment_image_refs(markdown))
    payload_text, payload_parse_mode = telegram_message_payload(
        text,
        parse_mode=parse_mode,
    )
    if not has_inline_images and len(payload_text) <= TELEGRAM_TEXT_LIMIT:
        payload_kwargs = dict(kwargs)
        payload_kwargs["parse_mode"] = payload_parse_mode
        try:
            return await _send_one(
                bot,
                chat_id=chat_id,
                text=payload_text,
                **payload_kwargs,
            )
        except TelegramBadRequest as exc:
            if payload_kwargs.get("parse_mode") is None:
                raise
            logger.warning("Formatted send rejected, retrying plain text: %s", exc)
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["parse_mode"] = None
            plain_text = markdown_to_plain_text(markdown) or text
            return await _send_one(
                bot,
                chat_id=chat_id,
                text=plain_text,
                **fallback_kwargs,
            )
    document = BufferedInputFile(
        render_html_document(text, parse_mode=parse_mode),
        filename="d-brain-message.html",
    )
    return await _send_document(
        bot,
        chat_id=chat_id,
        document=document,
        caption=None,
        reply_markup=kwargs.get("reply_markup"),
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
    markdown = rich_markdown_payload(text, parse_mode=parse_mode)
    if markdown is None:
        return await send_text(bot, chat_id=chat_id, text=text, **kwargs)
    if len(markdown) > RICH_TEXT_LIMIT:
        document = BufferedInputFile(
            render_html_document(text, parse_mode=parse_mode),
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
    "MAX_INLINE_IMAGE_BYTES",
    "RICH_MEDIA_MARKUP_RE",
    "RICH_TEXT_LIMIT",
    "render_html_document",
    "rich_markdown_payload",
    "send_rich_text",
    "send_telegram_text",
    "send_telegram_text_sync",
    "send_text",
    "should_retry_without_reply_markup",
    "telegram_message_payload",
    "text_reply_kwargs",
]
