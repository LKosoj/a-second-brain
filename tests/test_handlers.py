import asyncio
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from d_brain.bot.handlers import document as document_handler
from d_brain.bot.handlers import forward as forward_handler
from d_brain.bot.handlers import photo as photo_handler
from d_brain.bot.handlers import process as process_handler
from d_brain.bot.handlers import text as text_handler
from d_brain.bot.handlers import voice as voice_handler
from d_brain.bot.progress import wait_for_task_with_progress
from d_brain.services.documents import (
    DocumentArchiveResult,
    DocumentExtractionResult,
    UnsupportedDocumentError,
)
from d_brain.services.processor import (
    INTERACTIVE_MODE,
)
from d_brain.services.source_links import (
    SourceInfo,
)


async def test_wait_for_task_with_progress_returns_fast_result_immediately() -> None:
    progress_calls: list[float] = []

    async def immediate() -> str:
        return "done"

    async def on_progress(elapsed_seconds: float) -> None:
        progress_calls.append(elapsed_seconds)

    task = asyncio.create_task(immediate())

    result = await wait_for_task_with_progress(
        task,
        interval_seconds=30,
        on_progress=on_progress,
    )

    assert result == "done"
    assert progress_calls == []


async def test_wait_for_task_with_progress_reports_each_elapsed_interval() -> None:
    release = asyncio.Event()
    progress_calls: list[float] = []

    async def delayed() -> str:
        await release.wait()
        return "done"

    async def on_progress(elapsed_seconds: float) -> None:
        progress_calls.append(elapsed_seconds)
        if len(progress_calls) == 2:
            release.set()

    task = asyncio.create_task(delayed())

    result = await wait_for_task_with_progress(
        task,
        interval_seconds=0.01,
        on_progress=on_progress,
    )

    assert result == "done"
    assert progress_calls == pytest.approx([0.01, 0.02])


async def test_wait_for_task_with_progress_propagates_task_error() -> None:
    async def fail() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(fail())

    with pytest.raises(RuntimeError, match="boom"):
        await wait_for_task_with_progress(
            task,
            interval_seconds=30,
            on_progress=lambda _elapsed: asyncio.sleep(0),
        )


async def test_wait_for_task_with_progress_shields_work_from_waiter_cancellation(
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def work() -> str:
        started.set()
        await release.wait()
        return "done"

    task = asyncio.create_task(work())
    waiter = asyncio.create_task(
        wait_for_task_with_progress(
            task,
            interval_seconds=30,
            on_progress=lambda _elapsed: asyncio.sleep(0),
        )
    )
    await started.wait()
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert task.cancelled() is False
    release.set()
    assert await task == "done"


def test_handle_text_routes_questions_to_answer_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    answers: list[str] = []
    status_updates: list[str] = []
    session_entries: list[tuple[int, str, dict]] = []
    deleted = {"value": False}

    class FakeStatusMessage:
        async def delete(self) -> None:
            deleted["value"] = True

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001
        if text.startswith("⏳"):
            status_updates.append(text)
            return FakeStatusMessage()
        answers.append(text)
        return SimpleNamespace(text=text)

    async def fake_edit_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001
        status_updates.append(text)
        return SimpleNamespace(text=text)

    async def fake_answer_rich_text(  # noqa: ANN001, ARG001
        _message,
        text: str,
        **kwargs,
    ):
        answers.append(text)
        return SimpleNamespace(text=text)

    class FakeStorage:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append_to_daily(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            raise AssertionError("question path must not write to daily")

    class FakeLinkSummaryService:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

    class FakeProcessor:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def classify_text_intent(self, text: str) -> dict[str, str]:
            assert text == "Какие приоритеты на эту неделю?"
            return {
                "intent": "question",
                "confidence": "high",
                "reason": "direct answer expected",
            }

        def answer_question(self, question: str, user_id: int) -> dict[str, object]:
            assert question == "Какие приоритеты на эту неделю?"
            assert user_id == 42
            return {
                "report": (
                    "🎯 **Приоритеты недели**\n"
                    "1. Example Bank\n"
                    "2. Example Studio"
                ),
                "processed_entries": 1,
            }

    class FakeSessionStore:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append(self, user_id: int, entry_type: str, **kwargs) -> None:  # noqa: ANN003
            session_entries.append((user_id, entry_type, kwargs))

    class FakeUser:
        id = 42

    FakeDate = lambda: datetime(2026, 4, 4, 12, 0, 0)  # noqa: E731

    class FakeMessage:
        text = "Какие приоритеты на эту неделю?"
        from_user = FakeUser()
        date = FakeDate()
        message_id = 100

    monkeypatch.setattr(
        text_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            content_language="ru",
            ai_cli="codex",
            todoist_api_key="",
            owner_full_name="Иван Иванов",
        ),
    )
    monkeypatch.setattr(text_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(text_handler, "LinkSummaryService", FakeLinkSummaryService)
    monkeypatch.setattr(text_handler, "CliProcessor", FakeProcessor)
    monkeypatch.setattr(text_handler, "SessionStore", FakeSessionStore)
    monkeypatch.setattr(text_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(text_handler, "answer_rich_text", fake_answer_rich_text)
    monkeypatch.setattr(text_handler, "edit_text", fake_edit_text)

    asyncio.run(text_handler.handle_text(FakeMessage()))

    assert len(answers) == 1
    assert "Приоритеты недели" in answers[0]
    assert any("прямой ответ / question" in item for item in status_updates)
    assert any("готовлю прямой ответ" in item for item in status_updates)
    assert any("отправляю ответ" in item for item in status_updates)
    assert deleted["value"] is True
    assert session_entries[0][1] == "question"


def test_process_command_falls_back_to_new_message_when_status_edit_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    answers: list[str] = []
    class FakeStatusMessage:
        pass

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001, ANN202
        answers.append(text)
        return FakeStatusMessage()

    async def fake_edit_rich_text(  # noqa: ANN001, ARG001, ANN202
        _message,
        text: str,
        **kwargs,
    ):
        raise TelegramBadRequest(
            EditMessageText(chat_id=42, message_id=1, text=text),
            "message can't be edited",
        )

    async def fake_answer_rich_text(  # noqa: ANN001, ARG001, ANN202
        _message,
        text: str,
        **kwargs,
    ):
        answers.append(text)
        return FakeStatusMessage()

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    class FakeProcessor:
        def process_daily(self, day: date, *, mode: str) -> dict[str, object]:
            assert day == date.today()
            assert mode == INTERACTIVE_MODE
            return {
                "report": "📊 **Обработка завершена**",
                "processed_entries": 1,
            }

    class FakeUser:
        id = 42

    class FakeMessage:
        from_user = FakeUser()

    monkeypatch.setattr(process_handler, "_build_processor", lambda: FakeProcessor())
    monkeypatch.setattr(process_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(process_handler, "answer_rich_text", fake_answer_rich_text)
    monkeypatch.setattr(process_handler, "edit_rich_text", fake_edit_rich_text)
    monkeypatch.setattr(process_handler.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        process_handler,
        "format_process_report",
        lambda report: "✅ Финальный отчёт",
    )

    asyncio.run(
        asyncio.wait_for(
            process_handler._run_process_command(
                FakeMessage(),
                mode=INTERACTIVE_MODE,
                initial_status="⏳ Старт",
                progress_prefix="⏳ Идёт",
            ),
            timeout=0.5,
        )
    )

    assert answers == ["⏳ Старт", "✅ Финальный отчёт"]


def test_handle_text_capture_offloads_daily_write_to_thread(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    answers: list[str] = []
    status_updates: list[str] = []
    deleted = {"value": False}

    class FakeStatusMessage:
        async def delete(self) -> None:
            deleted["value"] = True

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001
        if text.startswith("⏳"):
            status_updates.append(text)
            return FakeStatusMessage()
        answers.append(text)
        return SimpleNamespace(text=text)

    async def fake_edit_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001
        status_updates.append(text)
        return SimpleNamespace(text=text)

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(getattr(func, "__name__", func.__class__.__name__))
        return func(*args, **kwargs)

    class FakeStorage:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append_to_daily(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return None

    class FakeLinkSummaryService:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def enrich_text(self, text: str, **kwargs):  # noqa: ANN202, ANN003
            assert "timestamp" in kwargs
            assert "source" in kwargs
            assert kwargs["refresh_qmd"] is False
            return SimpleNamespace(
                content=text,
                transcripts=[],
                summaries=[],
                youtube_summaries=[],
            )

    class FakeProcessor:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def classify_text_intent(self, text: str) -> dict[str, str]:
            assert text == "Сохрани это как заметку"
            return {
                "intent": "capture",
                "confidence": "high",
                "reason": "capture",
            }

    class FakeSessionStore:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return None

    class FakeUser:
        id = 42

    class FakeChat:
        id = 123
        type = "private"
        username = ""

    FakeDate = lambda: datetime(2026, 4, 4, 12, 0, 0)  # noqa: E731

    class FakeMessage:
        text = "Сохрани это как заметку"
        from_user = FakeUser()
        chat = FakeChat()
        date = FakeDate()
        message_id = 101
        link = None

    monkeypatch.setattr(
        text_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            content_language="ru",
            ai_cli="codex",
            todoist_api_key="",
            owner_full_name="Иван Иванов",
        ),
    )
    monkeypatch.setattr(text_handler.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(text_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(text_handler, "LinkSummaryService", FakeLinkSummaryService)
    monkeypatch.setattr(text_handler, "CliProcessor", FakeProcessor)
    monkeypatch.setattr(text_handler, "SessionStore", FakeSessionStore)
    monkeypatch.setattr(text_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(text_handler, "edit_text", fake_edit_text)

    asyncio.run(text_handler.handle_text(FakeMessage()))

    assert calls == ["classify_text_intent", "enrich_text", "append_to_daily"]
    assert answers == ["✓ Сохранено"]
    assert any("сохранение в daily / capture" in item for item in status_updates)
    assert any("обогащаю текст" in item for item in status_updates)
    assert any("сохраняю запись в daily" in item for item in status_updates)
    assert any("отправляю ответ" in item for item in status_updates)
    assert deleted["value"] is True


def test_handle_voice_offloads_daily_write_to_thread(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    answers: list[str] = []
    scheduled: list[object] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(getattr(func, "__name__", func.__class__.__name__))
        return func(*args, **kwargs)

    class FakeStorage:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append_to_daily(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return None

    class FakeSessionStore:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return None

    class FakeTranscriber:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def transcribe(self, audio_bytes: bytes) -> str:
            assert audio_bytes == b"voice-bytes"
            return "Транскрибированный текст"

    class FakeBot:
        async def get_file(self, file_id: str):  # noqa: ANN202
            assert file_id == "voice-file"
            return SimpleNamespace(file_path="voice.ogg")

        async def download_file(self, file_path: str):  # noqa: ANN202
            assert file_path == "voice.ogg"
            return SimpleNamespace(read=lambda: b"voice-bytes")

    class FakeUser:
        id = 42

    class FakeChat:
        async def do(self, action: str) -> None:
            assert action == "typing"

        id = 123
        type = "private"
        username = ""

    FakeDate = lambda: datetime(2026, 4, 4, 12, 0, 0)  # noqa: E731

    class FakeMessage:
        voice = SimpleNamespace(file_id="voice-file", duration=7)
        from_user = FakeUser()
        chat = FakeChat()
        date = FakeDate()
        message_id = 202
        link = None

        async def answer(self, text: str, parse_mode=None) -> None:  # noqa: ANN001
            answers.append(text)

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001, ANN202
        answers.append(text)
        return SimpleNamespace()

    class FakeTask:
        def add_done_callback(self, callback) -> None:  # noqa: ANN001, D401
            return None

    def fake_create_task(coro):  # type: ignore[no-untyped-def]
        scheduled.append(coro)
        coro.close()
        return FakeTask()

    monkeypatch.setattr(
        voice_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            content_language="ru",
            deepgram_api_key="deepgram-key",
        ),
    )
    monkeypatch.setattr(voice_handler.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(voice_handler.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(voice_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(voice_handler, "DeepgramTranscriber", FakeTranscriber)
    monkeypatch.setattr(voice_handler, "SessionStore", FakeSessionStore)
    monkeypatch.setattr(voice_handler, "answer_text", fake_answer_text)

    asyncio.run(voice_handler.handle_voice(FakeMessage(), FakeBot()))

    assert calls == ["append_to_daily"]
    assert answers == ["🎤 Транскрибированный текст\n\n✓ Сохранено"]
    assert len(scheduled) == 1


def test_handle_forward_offloads_daily_write_to_thread(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    answers: list[str] = []
    scheduled: list[object] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(getattr(func, "__name__", func.__class__.__name__))
        return func(*args, **kwargs)

    class FakeStorage:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append_to_daily(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return None

    class FakeLinkSummaryService:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def enrich_text(self, text: str, **kwargs):  # noqa: ANN202, ANN003
            assert "timestamp" in kwargs
            assert "source" in kwargs
            assert kwargs["refresh_qmd"] is False
            return SimpleNamespace(
                content=text,
                transcripts=[],
                summaries=[],
                youtube_summaries=[],
            )

    class FakeSessionStore:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return None

    class FakeUser:
        id = 42

    FakeDate = lambda: datetime(2026, 4, 4, 12, 0, 0)  # noqa: E731

    class FakeMessage:
        text = "Пересланный текст"
        from_user = FakeUser()
        forward_origin = SimpleNamespace(
            sender_user=SimpleNamespace(full_name="Sender"),
        )
        date = FakeDate()
        message_id = 303
        link = None

        async def answer(self, text: str, parse_mode=None) -> None:  # noqa: ANN001
            answers.append(text)

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001, ANN202
        answers.append(text)
        return SimpleNamespace()

    class FakeTask:
        def add_done_callback(self, callback) -> None:  # noqa: ANN001, D401
            return None

    def fake_create_task(coro):  # type: ignore[no-untyped-def]
        scheduled.append(coro)
        coro.close()
        return FakeTask()

    monkeypatch.setattr(
        forward_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            content_language="ru",
            ai_cli="codex",
        ),
    )
    monkeypatch.setattr(forward_handler.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(forward_handler.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(forward_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(
        forward_handler,
        "build_telegram_source_info",
        lambda *args, **kwargs: SourceInfo(
            kind="telegram",
            ref="telegram:42:303",
            url="https://t.me/c/123/303",
            label="Открыть",
        ),
    )
    monkeypatch.setattr(
        forward_handler,
        "LinkSummaryService",
        FakeLinkSummaryService,
    )
    monkeypatch.setattr(forward_handler, "SessionStore", FakeSessionStore)
    monkeypatch.setattr(forward_handler, "answer_text", fake_answer_text)

    asyncio.run(forward_handler.handle_forward(FakeMessage()))

    assert calls == ["enrich_text", "append_to_daily"]
    assert answers == ["✓ Сохранено (от Sender)"]
    assert len(scheduled) == 1


def test_handle_photo_offloads_storage_writes_to_thread(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    answers: list[str] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(getattr(func, "__name__", func.__class__.__name__))
        return func(*args, **kwargs)

    class FakeStorage:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def save_attachment(self, *args, **kwargs) -> str:  # noqa: ANN002, ANN003
            return "attachments/2026-04-04/404.jpg"

        def append_to_daily(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return None

    class FakeImageAnalysisService:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def analyze(self, relative_path: str) -> dict[str, str]:
            assert relative_path == "attachments/2026-04-04/404.jpg"
            return {"description": "Описание", "ocr_text": ""}

    class FakeLinkSummaryService:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def enrich_text(self, text: str, **kwargs):  # noqa: ANN202, ANN003
            assert text == "Подпись"
            assert "timestamp" in kwargs
            assert "source" in kwargs
            assert kwargs["refresh_qmd"] is False
            return SimpleNamespace(
                content=text,
                transcripts=[],
                summaries=[],
                youtube_summaries=[],
            )

    class FakeSessionStore:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return None

    class FakeBot:
        async def get_file(self, file_id: str):  # noqa: ANN202
            assert file_id == "photo-file"
            return SimpleNamespace(file_path="photo.jpg")

        async def download_file(self, file_path: str):  # noqa: ANN202
            assert file_path == "photo.jpg"
            return SimpleNamespace(read=lambda: b"photo-bytes")

    class FakeUser:
        id = 42

    FakeDate = lambda: datetime(2026, 4, 4, 12, 0, 0)  # noqa: E731

    class FakeMessage:
        photo = [SimpleNamespace(file_id="photo-file")]
        caption = "Подпись"
        from_user = FakeUser()
        date = FakeDate()
        message_id = 404
        media_group_id = None
        link = None

        async def answer(self, text: str, parse_mode=None) -> None:  # noqa: ANN001
            answers.append(text)

    monkeypatch.setattr(
        photo_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            content_language="ru",
            ai_cli="codex",
        ),
    )
    monkeypatch.setattr(photo_handler.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(photo_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(
        photo_handler,
        "build_telegram_source_info",
        lambda *args, **kwargs: SourceInfo(
            kind="telegram",
            ref="telegram:42:404",
            url="https://t.me/c/123/404",
            label="Открыть",
        ),
    )
    monkeypatch.setattr(
        photo_handler,
        "ImageAnalysisService",
        FakeImageAnalysisService,
    )
    monkeypatch.setattr(
        photo_handler,
        "LinkSummaryService",
        FakeLinkSummaryService,
    )
    monkeypatch.setattr(photo_handler, "SessionStore", FakeSessionStore)

    asyncio.run(photo_handler.handle_photo(FakeMessage(), FakeBot()))

    assert calls == ["save_attachment", "analyze", "enrich_text", "append_to_daily"]
    assert answers == ["📷 ✓ Сохранено"]


def test_handle_document_rejects_unsupported_format(
    monkeypatch,
    tmp_path: Path,
) -> None:
    answers: list[str] = []

    class FakeService:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def validate_input(self, **kwargs) -> str:  # noqa: ANN003, ANN202
            raise UnsupportedDocumentError(
                "Поддерживаются только pdf/docx/xlsx/txt/pptx/md/html/mpp."
            )

    class FakeUser:
        id = 42

    class FakeDocument:
        file_name = "archive.zip"
        mime_type = "application/zip"
        file_size = 123

    class FakeMessage:
        document = FakeDocument()
        from_user = FakeUser()
        message_id = 505

        async def answer(self, text: str, parse_mode=None) -> None:  # noqa: ANN001
            answers.append(text)

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001, ANN202
        answers.append(text)
        return SimpleNamespace()

    monkeypatch.setattr(
        document_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            content_language="ru",
            ai_cli="codex",
        ),
    )
    monkeypatch.setattr(document_handler, "DocumentArchiveService", FakeService)
    monkeypatch.setattr(document_handler, "answer_text", fake_answer_text)

    asyncio.run(document_handler.handle_document(FakeMessage(), SimpleNamespace()))

    assert answers == ["Поддерживаются только pdf/docx/xlsx/txt/pptx/md/html/mpp."]


def test_handle_document_acks_and_processes_in_background(
    monkeypatch,
    tmp_path: Path,
) -> None:
    answers: list[str] = []
    edits: list[str] = []
    storage_calls: list[tuple[str, str]] = []
    session_calls: list[tuple[int, str, dict]] = []
    scheduled: list[object] = []
    order: list[str] = []

    class FakeService:
        content_language = "ru"
        vault_path = tmp_path

        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def validate_input(self, **kwargs) -> str:  # noqa: ANN003, ANN202
            return "pdf"

        def stage_document_upload(self, **kwargs):  # noqa: ANN003, ANN202
            order.append("stage")
            return SimpleNamespace(
                file_format="pdf",
                original_path="imports/documents/raw/2026-04-04/report.pdf",
            )

        def archive_staged_document(self, **kwargs):  # noqa: ANN003, ANN202
            order.append("archive")
            assert kwargs["refresh_qmd"] is False
            extraction = DocumentExtractionResult(
                plain_text="Текст документа",
                title="Quarterly Report",
                format="pdf",
                warnings=["PDF extraction is text-only; OCR is disabled."],
                metadata={"pages": 2},
                truncated=False,
                source_path="imports/documents/raw/2026-04-04/report.pdf",
            )
            return DocumentArchiveResult(
                extraction=extraction,
                original_path="imports/documents/raw/2026-04-04/report.pdf",
                text_path="imports/documents/text/2026-04-04/report.txt",
                note_path="imports/documents/notes/2026/04/report.md",
                daily_summary="Текст документа",
                daily_content=(
                    "📄 [[imports/documents/notes/2026/04/report.md|Quarterly Report]]"
                ),
            )

        def build_result_message(self, result, *, file_name: str) -> str:  # noqa: ANN001, ANN202
            assert file_name == "report.pdf"
            return "✅ Документ сохранён."

    class FakeStorage:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append_to_daily(
            self,
            content: str,
            timestamp: datetime,
            msg_type: str,
            **kwargs,
        ) -> None:  # noqa: ANN003
            storage_calls.append((content, msg_type))

    class FakeSessionStore:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append(self, user_id: int, entry_type: str, **kwargs) -> None:  # noqa: ANN003
            session_calls.append((user_id, entry_type, kwargs))

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    class FakeTask:
        def __init__(self, coro) -> None:  # noqa: ANN001
            self.coro = coro

        def add_done_callback(self, callback) -> None:  # noqa: ANN001, D401
            return None

    def fake_create_task(coro):  # type: ignore[no-untyped-def]
        scheduled.append(coro)
        return FakeTask(coro)

    class FakeBot:
        async def get_file(self, file_id: str):  # noqa: ANN202
            assert file_id == "doc-file"
            return SimpleNamespace(file_path="docs/report.pdf")

        async def download_file(self, file_path: str):  # noqa: ANN202
            assert file_path == "docs/report.pdf"
            return SimpleNamespace(read=lambda: b"%PDF-1.7")

    class FakeUser:
        id = 42

    class FakeChat:
        id = 123
        type = "private"
        username = ""

    FakeDate = lambda: datetime(2026, 4, 4, 12, 0, 0)  # noqa: E731

    class FakeStatusMessage:
        pass

    class FakeDocument:
        file_id = "doc-file"
        file_name = "report.pdf"
        mime_type = "application/pdf"
        file_size = 1024

    class FakeMessage:
        document = FakeDocument()
        from_user = FakeUser()
        chat = FakeChat()
        date = FakeDate()
        caption = "Квартальный отчёт"
        message_id = 606
        link = None

        async def answer(self, text: str, parse_mode=None):  # noqa: ANN001, ANN202
            order.append("answer")
            answers.append(text)
            return FakeStatusMessage()

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001, ANN202
        order.append("answer")
        answers.append(text)
        return FakeStatusMessage()

    async def fake_edit_rich_text(  # noqa: ANN001, ARG001, ANN202
        _message,
        text: str,
        **kwargs,
    ):
        edits.append(text)
        return SimpleNamespace()

    monkeypatch.setattr(
        document_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            content_language="ru",
            ai_cli="codex",
        ),
    )
    monkeypatch.setattr(document_handler, "DocumentArchiveService", FakeService)
    monkeypatch.setattr(document_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(document_handler, "SessionStore", FakeSessionStore)
    monkeypatch.setattr(document_handler.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(document_handler.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(document_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(document_handler, "edit_rich_text", fake_edit_rich_text)

    asyncio.run(document_handler.handle_document(FakeMessage(), FakeBot()))

    assert answers == [
        "📄 Файл сохранён: report.pdf. Извлечение текста запущено в фоне."
    ]
    assert len(scheduled) == 1
    assert order[:2] == ["stage", "answer"]

    asyncio.run(scheduled[0])

    assert edits == ["✅ Документ сохранён."]
    assert order[2] == "archive"
    assert storage_calls == [
        (
            "📄 [[imports/documents/notes/2026/04/report.md|Quarterly Report]]",
            "[document]",
        )
    ]
    assert session_calls[0][0] == 42
    assert session_calls[0][1] == "document"
    assert (
        session_calls[0][2]["note_path"]
        == "imports/documents/notes/2026/04/report.md"
    )
