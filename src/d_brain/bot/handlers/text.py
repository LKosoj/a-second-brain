"""Text message handler."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from aiogram import Router
from aiogram.types import Message

from d_brain.bot.formatters import format_process_report
from d_brain.bot.replies import answer_rich_text, answer_text, edit_text
from d_brain.config import get_settings
from d_brain.services.link_summary import (
    LinkSummaryService,
    format_link_summary_message,
)
from d_brain.services.processor import TEXT_INTENT_QUESTION, CliProcessor
from d_brain.services.session import SessionStore
from d_brain.services.source_links import build_telegram_source_info
from d_brain.services.storage import VaultStorage

router = Router(name="text")
logger = logging.getLogger(__name__)


def _text_branch_label(intent: str, workflow: str = "") -> str:
    """Human-readable label for the resolved text route."""
    workflow_suffix = f" / {workflow}" if workflow else ""
    if intent == TEXT_INTENT_QUESTION:
        return f"прямой ответ / question{workflow_suffix}"
    return f"сохранение в daily / capture{workflow_suffix}"


def _text_status(branch: str, step: str) -> str:
    """One compact status payload for text routing."""
    return f"⏳ **Ветка:** {branch}\n**Сейчас:** {step}"


async def _safe_edit_status(
    status_message: Message | None,
    *,
    branch: str,
    step: str,
) -> None:
    """Best-effort status update without breaking the main flow."""
    if status_message is None:
        return
    try:
        await edit_text(status_message, _text_status(branch, step))
    except Exception:
        logger.debug("Failed to edit text status", exc_info=True)


async def _safe_delete_status(status_message: Message | None) -> None:
    """Best-effort status deletion before the final reply."""
    if status_message is None:
        return
    try:
        await status_message.delete()
    except Exception:
        logger.warning("Failed to delete text status", exc_info=True)


async def _upsert_status_message(
    source_message: Message,
    status_message: Message | None,
    *,
    branch: str,
    step: str,
) -> Message | None:
    """Edit the current status message or replace it if edit fails."""
    payload = _text_status(branch, step)
    if status_message is not None:
        try:
            await edit_text(status_message, payload)
            return status_message
        except Exception:
            logger.warning("Failed to edit text status", exc_info=True)
            await _safe_delete_status(status_message)

    try:
        return await answer_text(source_message, payload)
    except Exception:
        logger.warning("Failed to send text status", exc_info=True)
        return None


async def _run_to_thread_with_status(
    source_message: Message,
    status_message: Message | None,
    func: Callable[..., Any],
    *,
    branch: str,
    step: str,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> tuple[Any, Message | None]:
    """Run one blocking step in a worker thread while preserving branch status."""
    runner_kwargs = kwargs or {}
    task = asyncio.create_task(asyncio.to_thread(func, *args, **runner_kwargs))
    elapsed = 0
    status_message = await _upsert_status_message(
        source_message,
        status_message,
        branch=branch,
        step=step,
    )
    while True:
        try:
            return (
                await asyncio.wait_for(asyncio.shield(task), timeout=30),
                status_message,
            )
        except TimeoutError:
            if task.done():
                break
        elapsed += 30
        status_message = await _upsert_status_message(
            source_message,
            status_message,
            branch=branch,
            step=f"{step} ({elapsed // 60}m {elapsed % 60}s)",
        )
    return await task, status_message


@router.message(lambda m: m.text is not None and not m.text.startswith("/"))
async def handle_text(message: Message) -> None:
    """Handle text messages (excluding commands)."""
    if not message.text or not message.from_user:
        return

    settings = get_settings()
    processor = CliProcessor(
        settings.vault_path,
        settings.todoist_api_key,
        settings.ai_cli,
        settings.owner_full_name,
        settings.content_language,
        getattr(settings, "openai_api_key", ""),
        getattr(settings, "openai_base_url", ""),
        getattr(settings, "openai_model", ""),
    )
    status_message = await _upsert_status_message(
        message,
        None,
        branch="определяю ветку",
        step="запускаю роутинг",
    )

    # Wrapped like photo/voice/document (code review): this handler used to
    # catch nothing at all, so a failed routing call, enrichment or daily
    # write escaped into aiogram's dispatcher, which logs and returns. The
    # "⏳" status put up above is never taken down in that case, so the owner
    # was left watching a progress indicator for work that had already died,
    # with no error and no way to tell it apart from a slow run.
    try:
        # Constructed inside the guard, unlike ``processor`` above it (code
        # review): ``SessionStore.__init__`` calls ``mkdir(exist_ok=True)``
        # without ``parents=True``, so a missing vault directory raises here.
        # Left outside, that raise escaped the guard this whole round exists
        # to close. photo.py and document.py already build theirs inside
        # their own guards, via _log_photo_session and the upload task.
        session = SessionStore(settings.vault_path)

        intent, status_message = await _run_to_thread_with_status(
            message,
            status_message,
            processor.classify_text_intent,
            branch="определяю ветку",
            step="анализирую сообщение",
            args=(message.text,),
        )
        branch = _text_branch_label(
            intent["intent"],
            str(intent.get("workflow") or ""),
        )
        if intent["intent"] == TEXT_INTENT_QUESTION:
            report, status_message = await _run_to_thread_with_status(
                message,
                status_message,
                processor.answer_question,
                branch=branch,
                step="готовлю прямой ответ",
                args=(message.text, message.from_user.id),
            )
            session.append(
                message.from_user.id,
                "question",
                text=message.text,
                intent_confidence=intent.get("confidence", ""),
                intent_reason=intent.get("reason", ""),
                workflow_name=intent.get("workflow", ""),
                workflow_kind=intent.get("workflow_kind", ""),
                msg_id=message.message_id,
            )
            formatted = format_process_report(report)
            status_message = await _upsert_status_message(
                message,
                status_message,
                branch=branch,
                step="отправляю ответ",
            )
            await _safe_delete_status(status_message)
            final_sender = answer_text if "error" in report else answer_rich_text
            try:
                await final_sender(message, formatted)
            except Exception:
                logger.exception("Failed to send direct answer")
            logger.info("Text message routed to direct answer")
            return

        storage = VaultStorage(settings.vault_path, settings.content_language)
        link_summary = LinkSummaryService(
            str(settings.vault_path),
            settings.ai_cli,
            settings.content_language,
            tavily_api_key=getattr(settings, "tavily_api_key", ""),
            jina_api_key=getattr(settings, "jina_api_key", ""),
            zai_api_key=getattr(settings, "zai_api_key", ""),
            proxy_url=getattr(settings, "proxy_url", ""),
        )
        timestamp = message.date.astimezone()
        source = build_telegram_source_info(
            message, language=settings.content_language
        )
        result, status_message = await _run_to_thread_with_status(
            message,
            status_message,
            link_summary.enrich_text,
            branch=branch,
            step="обогащаю текст",
            args=(message.text,),
            kwargs={
                "timestamp": timestamp,
                "source": source,
                "refresh_qmd": False,
            },
        )
        _, status_message = await _run_to_thread_with_status(
            message,
            status_message,
            storage.append_to_daily,
            branch=branch,
            step="сохраняю запись в daily",
            args=(result.content, timestamp, "[text]"),
            kwargs={"source": source},
        )

        # Log to session
        session.append(
            message.from_user.id,
            "text",
            text=result.content,
            youtube_urls=[item.url for item in result.transcripts],
            msg_id=message.message_id,
            source_ref=source.ref,
            source_url=source.url,
        )

        reply = "✓ Сохранено"
        link_summary_message = format_link_summary_message(
            result.summaries,
            language=settings.content_language,
            youtube_urls={item.url for item in result.youtube_summaries},
        )
        if link_summary_message:
            reply += "\n\n" + link_summary_message

        status_message = await _upsert_status_message(
            message,
            status_message,
            branch=branch,
            step="отправляю ответ",
        )
        await _safe_delete_status(status_message)
        # Guarded separately from the handler below (code review): the status
        # message has just been deleted and the entry is already saved, so an
        # unguarded failure here would be reported as a failed capture, and
        # told the owner an entry that is on disk had been lost.
        try:
            await answer_text(message, reply, parse_mode=None)
        except Exception:
            logger.exception("Failed to deliver text capture confirmation")
        logger.info("Text message saved: %d chars", len(result.content))

    except Exception as exc:
        logger.exception("Error processing text message")
        await _safe_delete_status(status_message)
        try:
            await answer_text(message, f"Ошибка: {exc}", parse_mode=None)
        except Exception:
            logger.exception("Failed to deliver text failure report")
