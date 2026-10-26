"""Download timeouts and error texts of ``handle_document`` (C8, B4)."""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from d_brain.bot.handlers import document as document_handler


class _Service:
    content_language = "ru"
    stage_calls = 0
    archive_error: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def validate_input(self, **kwargs) -> str:  # noqa: ANN003
        return "pdf"

    def stage_document_upload(self, **kwargs):  # noqa: ANN003, ANN202
        type(self).stage_calls += 1
        raise RuntimeError("secret /srv/path")

    def archive_staged_document(self, **kwargs):  # noqa: ANN003, ANN202
        raise RuntimeError("secret /srv/path")


class _Message:
    document = SimpleNamespace(
        file_id="doc-file", file_name="r.pdf", mime_type="application/pdf",
        file_size=10,
    )
    from_user = SimpleNamespace(id=42)
    chat = SimpleNamespace(id=1, type="private", username="")
    date = datetime(2026, 4, 4, 12, 0, 0)
    caption = None
    message_id = 7
    link = None
    forward_origin = None


def _setup(monkeypatch, tmp_path: Path) -> list[str]:
    answers: list[str] = []

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ANN003, ANN202
        answers.append(text)
        return SimpleNamespace()

    _Service.stage_calls = 0
    monkeypatch.setattr(
        document_handler, "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path, content_language="ru", ai_cli="codex"
        ),
    )
    monkeypatch.setattr(document_handler, "DocumentArchiveService", _Service)
    monkeypatch.setattr(document_handler, "answer_text", fake_answer_text)
    return answers


def test_handle_document_download_has_timeouts(monkeypatch, tmp_path: Path) -> None:
    answers = _setup(monkeypatch, tmp_path)
    requested: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout=None):  # noqa: ANN001, ANN202
        requested.append(timeout)
        return await real_wait_for(awaitable, 0.05)

    class HangingBot:
        async def get_file(self, file_id: str):  # noqa: ANN202
            await asyncio.sleep(3600)

    monkeypatch.setattr(document_handler.asyncio, "wait_for", short_wait_for)

    asyncio.run(
        real_wait_for(document_handler.handle_document(_Message(), HangingBot()), 5)
    )

    assert requested == [60]
    assert answers and answers[-1].startswith("❌ Не удалось сохранить документ")
    assert _Service.stage_calls == 0


def test_handle_document_download_file_has_its_own_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    answers = _setup(monkeypatch, tmp_path)
    requested: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout=None):  # noqa: ANN001, ANN202
        requested.append(timeout)
        return await real_wait_for(awaitable, 0.05)

    class HangingDownloadBot:
        async def get_file(self, file_id: str):  # noqa: ANN202
            return SimpleNamespace(file_path="docs/r.pdf")

        async def download_file(self, file_path: str):  # noqa: ANN202
            await asyncio.sleep(3600)

    monkeypatch.setattr(document_handler.asyncio, "wait_for", short_wait_for)

    asyncio.run(
        real_wait_for(
            document_handler.handle_document(_Message(), HangingDownloadBot()), 5
        )
    )

    assert requested == [60, 120]
    assert answers[-1].startswith("❌ Не удалось сохранить документ")


def test_handle_document_staging_failure_does_not_leak_exception_text(
    monkeypatch, tmp_path: Path
) -> None:
    answers = _setup(monkeypatch, tmp_path)

    class Bot:
        async def get_file(self, file_id: str):  # noqa: ANN202
            return SimpleNamespace(file_path="docs/r.pdf")

        async def download_file(self, file_path: str):  # noqa: ANN202
            return SimpleNamespace(read=lambda: b"%PDF")

    asyncio.run(document_handler.handle_document(_Message(), Bot()))

    assert answers[-1].startswith("❌ Не удалось сохранить документ")
    assert "secret" not in answers[-1]


def test_process_document_failure_does_not_leak_exception_text(
    monkeypatch, tmp_path: Path
) -> None:
    from d_brain.services.source_links import SourceInfo

    edits: list[str] = []

    async def fake_edit_text(_message, text: str, **kwargs):  # noqa: ANN001, ANN003, ANN202
        edits.append(text)

    monkeypatch.setattr(document_handler, "edit_text", fake_edit_text)

    asyncio.run(
        document_handler._process_document_upload(
            _Message(),
            SimpleNamespace(),
            _Service(),
            staged=SimpleNamespace(original_path="imports/x.pdf", file_format="pdf"),
            file_name="x.pdf",
            timestamp=datetime(2026, 4, 4, 12, 0, 0),
            source=SourceInfo(kind="telegram", ref="telegram:42:7"),
        )
    )

    assert edits and edits[0].startswith("❌ Не удалось обработать документ")
    assert "secret" not in edits[0]
