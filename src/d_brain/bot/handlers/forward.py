"""Forwarded message handler."""

import asyncio
import logging

from aiogram import Router
from aiogram.types import Message

from d_brain.bot.replies import answer_text
from d_brain.config import get_settings
from d_brain.services.link_summary import (
    LinkSummaryService,
    format_link_summary_message,
)
from d_brain.services.qmd import QmdService
from d_brain.services.session import SessionStore
from d_brain.services.source_links import (
    build_telegram_source_info,
    forward_source_name,
)
from d_brain.services.storage import VaultStorage

router = Router(name="forward")
logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task[None]] = set()


def _track_background_task(task: asyncio.Task[None]) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _refresh_qmd_in_background(vault_path) -> None:  # type: ignore[no-untyped-def]
    try:
        await asyncio.to_thread(QmdService(vault_path).refresh_after_searchable_write)
    except Exception as exc:
        logger.warning("Forward qmd refresh skipped: %s", exc)


@router.message(lambda m: m.forward_origin is not None)
async def handle_forward(message: Message) -> None:
    """Handle forwarded messages."""
    if not message.from_user:
        return

    settings = get_settings()
    storage = VaultStorage(settings.vault_path, settings.content_language)

    # Wrapped like photo/voice/document (code review): this handler used to
    # catch nothing at all, so a failed download, enrichment or daily write
    # escaped into aiogram's dispatcher, which logs and returns. The owner
    # forwarded a message and got back silence -- no confirmation and no
    # error -- with no way to tell a saved forward from a lost one.
    try:
        source = build_telegram_source_info(
            message, language=settings.content_language
        )
        timestamp = message.date.astimezone()

        # Determine and sanitize source name (shared with photo/voice/document
        # so this class of injection is fixed in one place, see
        # forward_source_name's docstring).
        source_name = forward_source_name(message)

        raw_text = message.text or message.caption or ""
        if raw_text.strip():
            raw_content = raw_text
        else:
            media_kind = next(
                (
                    kind
                    for kind, present in (
                        ("video", message.video),
                        ("animation", message.animation),
                        ("video_note", message.video_note),
                        ("voice", message.voice),
                        ("audio", message.audio),
                        ("photo", message.photo),
                        ("sticker", message.sticker),
                        ("document", message.document),
                    )
                    if present
                ),
                "media",
            )
            raw_content = f"[{media_kind}]"
        link_summary = LinkSummaryService(
            str(settings.vault_path),
            settings.ai_cli,
            settings.content_language,
            tavily_api_key=getattr(settings, "tavily_api_key", ""),
            jina_api_key=getattr(settings, "jina_api_key", ""),
            zai_api_key=getattr(settings, "zai_api_key", ""),
            proxy_url=getattr(settings, "proxy_url", ""),
        )
        if raw_text.strip():
            result = await asyncio.to_thread(
                link_summary.enrich_text,
                raw_content,
                timestamp=timestamp,
                source=source,
                refresh_qmd=False,
            )
        else:
            from d_brain.services.link_summary import LinkEnrichmentResult

            result = LinkEnrichmentResult(
                content=raw_content,
                transcripts=[],
                summaries=[],
                youtube_summaries=[],
            )
        msg_type = f"[forward from: {source_name}]"
        await asyncio.to_thread(
            storage.append_to_daily,
            result.content,
            timestamp,
            msg_type,
            source=source,
            refresh_qmd=False,
        )

        # Log to session
        session = SessionStore(settings.vault_path)
        session.append(
            message.from_user.id,
            "forward",
            text=result.content,
            youtube_urls=[item.url for item in result.transcripts],
            source=source_name,
            msg_id=message.message_id,
            source_ref=source.ref,
            source_url=source.url,
        )

        reply = f"✓ Сохранено (от {source_name})"
        link_summary_message = format_link_summary_message(
            result.summaries,
            language=settings.content_language,
            youtube_urls={item.url for item in result.youtube_summaries},
        )
        if link_summary_message:
            reply += "\n\n" + link_summary_message

        # Guarded separately from the handler above it (code review): the
        # daily entry and the link summaries are already on disk, so a
        # failure to *show* the confirmation is not a failure to save the
        # forward and must not be reported as one.
        try:
            await answer_text(message, reply, parse_mode=None)
        except Exception:
            logger.exception("Failed to deliver forward confirmation")
        _track_background_task(
            asyncio.create_task(_refresh_qmd_in_background(settings.vault_path))
        )
        logger.info("Forwarded message saved from: %s", source_name)

    except Exception as exc:
        logger.exception("Error processing forwarded message")
        try:
            await answer_text(message, f"Ошибка: {exc}", parse_mode=None)
        except Exception:
            logger.exception("Failed to deliver forward failure report")
