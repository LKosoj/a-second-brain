"""Telegram document upload handler."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from d_brain.bot.replies import (
    answer_rich_text,
    answer_text,
    edit_rich_text,
    edit_text,
)
from d_brain.config import get_settings
from d_brain.services.documents import (
    DocumentArchiveService,
    DocumentTooLargeError,
    StagedDocumentUpload,
    UnsupportedDocumentError,
)
from d_brain.services.session import SessionStore
from d_brain.services.source_links import SourceInfo, build_telegram_source_info
from d_brain.services.storage import VaultStorage

router = Router(name="document")
logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task[None]] = set()


def _track_background_task(task: asyncio.Task[None]) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.message(lambda m: m.document is not None)
async def handle_document(message: Message, bot: Bot) -> None:
    """Persist one uploaded document first, then continue extraction in background."""

    if not message.document or not message.from_user:
        return

    settings = get_settings()
    service = DocumentArchiveService(
        settings.vault_path,
        settings.content_language,
        settings.ai_cli,
    )
    file_name = message.document.file_name or f"document-{message.message_id}"
    mime_type = message.document.mime_type or ""
    file_size = message.document.file_size or 0

    try:
        service.validate_input(
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
        )
    except UnsupportedDocumentError as exc:
        await answer_text(message, str(exc), parse_mode=None)
        return
    except DocumentTooLargeError as exc:
        await answer_text(message, str(exc), parse_mode=None)
        return

    timestamp = message.date.astimezone()
    document = message.document
    if document is None:
        return

    try:
        file = await bot.get_file(document.file_id)
        if not file.file_path:
            raise ValueError("Telegram did not return file_path")

        file_bytes = await bot.download_file(file.file_path)
        if not file_bytes:
            raise ValueError("Failed to download document")

        raw_bytes = file_bytes.read()
        staged = await asyncio.to_thread(
            service.stage_document_upload,
            data=raw_bytes,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size or len(raw_bytes),
            timestamp=timestamp,
            name_hint=str(message.message_id),
        )
    except Exception as exc:
        logger.exception("Document staging failed")
        await answer_text(
            message,
            f"❌ Не удалось сохранить документ: {exc}",
            parse_mode=None,
        )
        return

    status_msg = await answer_text(
        message,
        f"📄 Файл сохранён: {file_name}. Извлечение текста запущено в фоне.",
        parse_mode=None,
    )
    source = build_telegram_source_info(message, language=service.content_language)
    task = asyncio.create_task(
        _process_document_upload(
            message,
            status_msg,
            service,
            staged=staged,
            file_name=file_name,
            timestamp=timestamp,
            source=source,
        )
    )
    _track_background_task(task)


async def _process_document_upload(
    message: Message,
    status_msg: Message,
    service: DocumentArchiveService,
    *,
    staged: StagedDocumentUpload,
    file_name: str,
    timestamp: datetime,
    source: SourceInfo,
) -> None:
    """Finish extraction and persistence for a durably staged document."""

    try:
        result = await asyncio.to_thread(
            service.archive_staged_document,
            original_path=staged.original_path,
            file_name=file_name,
            file_format=staged.file_format,
            timestamp=timestamp,
            source=source,
            name_hint=str(message.message_id),
            caption=message.caption,
            refresh_qmd=False,
        )

        storage = VaultStorage(service.vault_path, service.content_language)
        await asyncio.to_thread(
            storage.append_to_daily,
            result.daily_content,
            timestamp,
            "[document]",
            source=source,
        )

        session = SessionStore(service.vault_path)
        await asyncio.to_thread(
            session.append,
            message.from_user.id if message.from_user else 0,
            "document",
            file_name=file_name,
            format=result.extraction.format,
            title=result.extraction.title,
            original_path=result.original_path,
            text_path=result.text_path,
            note_path=result.note_path,
            summary=result.daily_summary,
            warnings=result.extraction.warnings,
            msg_id=message.message_id,
            source_ref=source.ref,
            source_url=source.url,
        )

        final_text = service.build_result_message(result, file_name=file_name)
        try:
            await edit_rich_text(status_msg, final_text, parse_mode=None)
        except TelegramBadRequest:
            await answer_rich_text(
                message,
                final_text,
                parse_mode=None,
            )
        except Exception:
            logger.exception("Failed to deliver rich document result")

        logger.info("Document saved: %s", file_name)

    except Exception as exc:
        logger.exception("Document processing failed")
        error_text = f"❌ Не удалось обработать документ: {exc}"
        try:
            await edit_text(status_msg, error_text, parse_mode=None)
        except Exception:
            await answer_text(message, error_text, parse_mode=None)
