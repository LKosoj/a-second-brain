"""Photo message handler."""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from aiogram import Bot, Router
from aiogram.types import Message, PhotoSize

from d_brain.bot.replies import answer_text
from d_brain.config import Settings, get_settings
from d_brain.services.image_analysis import ImageAnalysisService
from d_brain.services.link_summary import (
    LinkSummary,
    LinkSummaryService,
    format_link_summary_message,
)
from d_brain.services.localization import translate
from d_brain.services.session import SessionStore
from d_brain.services.source_links import (
    SourceInfo,
    build_telegram_source_info,
    format_source_markdown,
)
from d_brain.services.storage import VaultStorage

router = Router(name="photo")
logger = logging.getLogger(__name__)
ALBUM_SETTLE_SECONDS = 1.0
_album_lock = asyncio.Lock()
_album_items: dict[str, list["PhotoEntry"]] = {}
_album_tasks: dict[str, asyncio.Task[None]] = {}


@dataclass(slots=True)
class PhotoEntry:
    """Persisted photo payload ready for daily/session writes."""

    message_id: int
    timestamp: datetime
    relative_path: str
    caption: str | None
    analysis: dict[str, str] | None
    source: SourceInfo
    content_language: str
    youtube_urls: tuple[str, ...] = ()
    link_summaries: tuple[LinkSummary, ...] = ()


def _build_photo_daily_content(
    relative_path: str,
    caption: str | None,
    analysis: Mapping[str, str] | None = None,
    source: SourceInfo | None = None,
    content_language: str = "ru",
) -> str:
    """Build daily note content for a saved photo."""
    parts = [f"![[{relative_path}]]"]

    source_block = format_source_markdown(source, language=content_language)
    if source_block:
        parts.extend(["", source_block])

    caption_text = (caption or "").strip()
    if caption_text:
        parts.extend(["", caption_text])

    description = ""
    ocr_text = ""
    if analysis:
        description = (analysis.get("description") or "").strip()
        ocr_text = (analysis.get("ocr_text") or "").strip()

    if description:
        parts.extend(
            ["", f"{translate(content_language, 'ai_description')}:", description]
        )

    if ocr_text:
        parts.extend(
            ["", f"{translate(content_language, 'ocr')}:", "```text", ocr_text, "```"]
        )

    return "\n".join(parts)


def _build_album_daily_content(items: list[PhotoEntry]) -> str:
    """Build one grouped daily entry for a Telegram photo album."""
    parts: list[str] = []
    group_caption = next((item.caption.strip() for item in items if item.caption), "")
    if group_caption:
        parts.append(group_caption)

    for index, item in enumerate(items, start=1):
        if parts:
            parts.append("")
        parts.append(f"### Фото {index}")
        parts.append(
            _build_photo_daily_content(
                item.relative_path,
                None,
                item.analysis,
                item.source,
                item.content_language,
            )
        )

    return "\n".join(parts)


async def _analyze_photo(
    settings: Settings,
    relative_path: str,
) -> dict[str, str] | None:
    analyzer = ImageAnalysisService(
        settings.vault_path,
        settings.ai_cli,
        settings.content_language,
    )
    return await asyncio.to_thread(analyzer.analyze, relative_path)


async def _save_photo(
    message: Message,
    bot: Bot,
) -> PhotoEntry:
    """Download, save, and analyze one Telegram photo."""
    settings = get_settings()
    storage = VaultStorage(settings.vault_path, settings.content_language)

    photo = cast(list[PhotoSize], message.photo)[-1]
    file = await bot.get_file(photo.file_id)
    if not file.file_path:
        raise ValueError("Не удалось скачать фото.")

    file_bytes = await bot.download_file(file.file_path)
    if not file_bytes:
        raise ValueError("Не удалось скачать фото.")

    timestamp = message.date.astimezone()
    photo_bytes = file_bytes.read()

    extension = "jpg"
    if file.file_path and "." in file.file_path:
        extension = file.file_path.rsplit(".", 1)[-1]

    relative_path = await asyncio.to_thread(
        storage.save_attachment,
        photo_bytes,
        timestamp.date(),
        timestamp,
        extension,
        name_hint=str(message.message_id),
    )
    source = build_telegram_source_info(message, language=settings.content_language)

    analysis: dict[str, str] | None = None
    try:
        analysis = await _analyze_photo(settings, relative_path)
    except Exception:
        logger.exception("Photo analysis failed for %s", relative_path)

    caption = message.caption
    youtube_urls: tuple[str, ...] = ()
    link_summaries: tuple[LinkSummary, ...] = ()
    if caption:
        link_summary = LinkSummaryService(
            str(settings.vault_path),
            settings.ai_cli,
            settings.content_language,
            tavily_api_key=getattr(settings, "tavily_api_key", ""),
            jina_api_key=getattr(settings, "jina_api_key", ""),
            zai_api_key=getattr(settings, "zai_api_key", ""),
            proxy_url=getattr(settings, "proxy_url", ""),
        )
        result = await asyncio.to_thread(
            link_summary.enrich_text,
            caption,
            timestamp=timestamp,
            source=source,
            refresh_qmd=False,
        )
        caption = result.content
        youtube_urls = tuple(item.url for item in result.transcripts)
        link_summaries = tuple(result.summaries)

    return PhotoEntry(
        message_id=message.message_id,
        timestamp=timestamp,
        relative_path=relative_path,
        caption=caption,
        analysis=analysis,
        source=source,
        content_language=settings.content_language,
        youtube_urls=youtube_urls,
        link_summaries=link_summaries,
    )


def _log_photo_session(message: Message, entry: PhotoEntry) -> None:
    """Append photo metadata to session storage."""
    settings = get_settings()
    session = SessionStore(settings.vault_path)
    user_id = message.from_user.id if message.from_user else 0
    session.append(
        user_id,
        "photo",
        path=entry.relative_path,
        caption=entry.caption,
        description=(entry.analysis or {}).get("description", ""),
        ocr_text=(entry.analysis or {}).get("ocr_text", ""),
        youtube_urls=list(entry.youtube_urls),
        msg_id=entry.message_id,
        media_group_id=message.media_group_id or "",
        source_ref=entry.source.ref,
        source_url=entry.source.url,
    )


async def _flush_album(
    media_group_id: str,
    reply_message: Message,
    storage: VaultStorage,
) -> None:
    """Flush one collected album into daily as a grouped entry."""
    try:
        await asyncio.sleep(ALBUM_SETTLE_SECONDS)
    except asyncio.CancelledError:
        return

    async with _album_lock:
        items = sorted(
            _album_items.pop(media_group_id, []),
            key=lambda item: item.message_id,
        )
        if not items:
            _album_tasks.pop(media_group_id, None)
            return

        try:
            content = _build_album_daily_content(items)
            await asyncio.to_thread(
                storage.append_to_daily,
                content,
                items[0].timestamp,
                "[photo]",
            )
            reply = f"📷 ✓ Сохранено {len(items)} фото"
            link_summaries = [
                summary
                for item in items
                for summary in item.link_summaries
            ]
            link_summary_message = format_link_summary_message(
                link_summaries,
                language=items[0].content_language,
                youtube_urls={url for item in items for url in item.youtube_urls},
            )
            if link_summary_message:
                reply += "\n\n" + link_summary_message
            await answer_text(reply_message, reply, parse_mode=None)
        except Exception:
            logger.exception(
                "Failed to flush album %s (%d photos)",
                media_group_id,
                len(items),
            )
            try:
                await answer_text(
                    reply_message,
                    f"❌ Ошибка сохранения альбома ({len(items)} фото)",
                    parse_mode=None,
                )
            except Exception:
                pass
        finally:
            _album_tasks.pop(media_group_id, None)


@router.message(lambda m: m.photo is not None)
async def handle_photo(message: Message, bot: Bot) -> None:
    """Handle photo messages."""
    if not message.photo or not message.from_user:
        return

    settings = get_settings()
    storage = VaultStorage(settings.vault_path, settings.content_language)

    try:
        entry = await _save_photo(message, bot)
        _log_photo_session(message, entry)

        if message.media_group_id:
            async with _album_lock:
                album_items = _album_items.setdefault(message.media_group_id, [])
                album_items.append(entry)
                existing_task = _album_tasks.get(message.media_group_id)
                if existing_task is not None:
                    existing_task.cancel()
                _album_tasks[message.media_group_id] = asyncio.create_task(
                    _flush_album(message.media_group_id, message, storage)
                )
            logger.info(
                "Photo queued in album %s: %s",
                message.media_group_id,
                entry.relative_path,
            )
            return

        content = _build_photo_daily_content(
            entry.relative_path,
            entry.caption,
            entry.analysis,
            entry.source,
            entry.content_language,
        )
        await asyncio.to_thread(
            storage.append_to_daily,
            content,
            entry.timestamp,
            "[photo]",
        )

        reply = "📷 ✓ Сохранено"
        link_summary_message = format_link_summary_message(
            list(entry.link_summaries),
            language=entry.content_language,
            youtube_urls=set(entry.youtube_urls),
        )
        if link_summary_message:
            reply += "\n\n" + link_summary_message

        await answer_text(message, reply, parse_mode=None)
        logger.info("Photo saved: %s", entry.relative_path)

    except Exception as e:
        logger.exception("Error processing photo")
        await answer_text(
            message,
            f"Ошибка: {e}",
            parse_mode=None,
        )
