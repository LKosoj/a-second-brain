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
from d_brain.services.source_links import (
    SourceInfo,
    build_telegram_source_info,
    forward_source_name,
)
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

    forwarded = message.forward_origin is not None
    if forwarded:
        # A forwarded document is someone else's file, not the owner's own
        # integration -- see TRUST_RANK / CONSEQUENTIAL_ACTION_TRUST_LEVELS
        # in compiled_briefings.py. Archiving it under the normal
        # imports/documents/notes/ pipeline would unconditionally grant it
        # "integration" trust (any imports/ path except imports/plaud/),
        # letting a stranger's forwarded file silently supersede the
        # owner's own facts. It still gets the full extraction/summary
        # pipeline below, but archive_staged_document(forwarded=True)
        # routes the note under imports/documents/forwarded/ instead, which
        # _source_trust_level caps at "forwarded" -- the same treatment
        # IMPORTS_PLAUD_PREFIX already gives PLAUD recordings.
        source_name = forward_source_name(message)
        status_text = (
            f"📎 Файл сохранён (переслано от {source_name}): {file_name}. "
            "Извлечение текста запущено в фоне."
        )
        entry_type = f"[forward from: {source_name}]"
    else:
        status_text = (
            f"📄 Файл сохранён: {file_name}. Извлечение текста запущено в фоне."
        )
        entry_type = "[document]"

    status_msg = await answer_text(message, status_text, parse_mode=None)
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
            forwarded=forwarded,
            entry_type=entry_type,
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
    forwarded: bool = False,
    entry_type: str = "[document]",
) -> None:
    """Finish extraction and persistence for a durably staged document.

    ``forwarded``/``entry_type`` carry a forwarded document's trust-relevant
    provenance through the same pipeline as an owned upload: ``forwarded``
    routes the note under imports/documents/forwarded/ (see
    archive_staged_document), and ``entry_type`` is the daily-entry marker
    ("[forward from: ...]") that _source_trust_level's daily/ branch reads.
    """

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
            forwarded=forwarded,
        )

        storage = VaultStorage(service.vault_path, service.content_language)
        await asyncio.to_thread(
            storage.append_to_daily,
            result.daily_content,
            timestamp,
            entry_type,
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
            forwarded=forwarded,
            msg_id=message.message_id,
            source_ref=source.ref,
            source_url=source.url,
        )

        final_text = service.build_result_message(result, file_name=file_name)
        # Nested so the fallback send is covered too (code review): the
        # archive, the daily entry and the session record above have all
        # committed by now, so no failure of the *delivery* may reach the
        # outer handler -- it reports "не удалось обработать документ", and
        # the fallback was the one path here that could still get there.
        try:
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
        # Nested for the same reason as the success path above (code review):
        # this whole coroutine runs as a background task nobody awaits, so a
        # fallback that raised here died in the task and took the error
        # report with it, leaving the owner on "Извлечение текста запущено в
        # фоне" with nothing ever arriving -- neither result nor failure.
        try:
            try:
                await edit_text(status_msg, error_text, parse_mode=None)
            except Exception:
                await answer_text(message, error_text, parse_mode=None)
        except Exception:
            logger.exception("Failed to deliver document failure report")
