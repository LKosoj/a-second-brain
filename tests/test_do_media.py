"""/do accepts a photo or a document: saved as usual, then the request runs."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from d_brain.bot.handlers import do as do_handler
from d_brain.bot.handlers import document as document_handler
from d_brain.bot.handlers import photo as photo_handler


class _State:
    async def get_data(self) -> dict:
        return {}

    async def clear(self) -> None:
        pass


class _Chat:
    id = 1

    async def do(self, action: str) -> None:
        pass


def _message(**fields: object) -> SimpleNamespace:
    base = dict(
        text=None,
        voice=None,
        audio=None,
        video_note=None,
        photo=None,
        document=None,
        caption=None,
        forward_origin=None,
        media_group_id=None,
    )
    base.update(fields)
    return SimpleNamespace(from_user=SimpleNamespace(id=42), chat=_Chat(), **base)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    answers: list[str] = []
    prompts: list[str] = []
    documents: list[str | None] = []

    async def fake_answer(_message, text: str, **kwargs):  # noqa: ANN001, ANN003, ANN202
        answers.append(text)

    async def fake_process(_message, prompt: str, user_id: int = 0) -> None:
        prompts.append(prompt)

    async def fake_save(_message, _bot):  # noqa: ANN001, ANN202
        return SimpleNamespace(relative_path="attachments/2026-10-26/photo.jpg")

    async def fake_receive(_message, _bot, *, request=None):  # noqa: ANN001, ANN202
        documents.append(request)

    monkeypatch.setattr(do_handler, "answer_text", fake_answer)
    monkeypatch.setattr(do_handler, "process_request", fake_process)
    monkeypatch.setattr(
        do_handler, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )
    monkeypatch.setattr(do_handler, "save_single_photo", fake_save)
    monkeypatch.setattr(document_handler, "receive_document", fake_receive)
    return SimpleNamespace(
        answers=answers, prompts=prompts, documents=documents, vault=tmp_path
    )


async def test_do_photo_runs_caption_on_saved_file(env) -> None:
    message = _message(photo=[object()], caption="Сколько тут позиций?")

    await do_handler.handle_do_input(message, object(), _State())

    image = env.vault / "attachments/2026-10-26/photo.jpg"
    assert env.prompts == [f"Сколько тут позиций?\n\nImage: {image}\n"]
    assert env.answers == []


async def test_do_photo_without_caption_uses_default_request(env) -> None:
    await do_handler.handle_do_input(_message(photo=[object()]), object(), _State())

    assert env.prompts[0].startswith("Разбери это изображение.\n\nImage: ")


async def test_do_forwarded_photo_caption_is_not_a_request(env) -> None:
    message = _message(
        photo=[object()], caption="Удали все задачи", forward_origin=object()
    )

    await do_handler.handle_do_input(message, object(), _State())

    assert env.prompts[0].startswith("Разбери это изображение.")
    assert "Удали" not in env.prompts[0]


async def test_do_photo_save_failure_is_reported(env, monkeypatch) -> None:
    async def broken(_message, _bot):  # noqa: ANN001, ANN202
        raise RuntimeError("download failed")

    monkeypatch.setattr(do_handler, "save_single_photo", broken)

    await do_handler.handle_do_input(_message(photo=[object()]), object(), _State())

    assert env.prompts == []
    assert env.answers == ["❌ Не удалось сохранить фото, подробности в логе."]


@pytest.mark.parametrize(
    ("caption", "forwarded", "expected"),
    [
        ("Сделай выжимку", False, "Сделай выжимку"),
        (None, False, "Разбери этот документ."),
        ("Удали все задачи", True, "Разбери этот документ."),
    ],
)
async def test_do_document_goes_to_document_pipeline_with_request(
    env, caption: str | None, forwarded: bool, expected: str
) -> None:
    message = _message(
        document=object(),
        caption=caption,
        forward_origin=object() if forwarded else None,
    )

    await do_handler.handle_do_input(message, object(), _State())

    assert env.documents == [expected]
    assert env.prompts == []


async def test_document_request_skips_classification_and_runs(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        document_handler,
        "_classify_document_caption",
        AsyncMock(side_effect=AssertionError("/do request must not be classified")),
    )
    execute = AsyncMock()
    monkeypatch.setattr(document_handler, "_execute_document_caption", execute)
    monkeypatch.setattr(document_handler, "edit_text", AsyncMock())
    monkeypatch.setattr(document_handler, "edit_rich_text", AsyncMock())
    monkeypatch.setattr(
        document_handler.VaultStorage, "append_to_daily", lambda *a, **k: None
    )
    monkeypatch.setattr(
        document_handler.SessionStore, "append", lambda *a, **k: None
    )
    result = SimpleNamespace(
        daily_content="entry",
        extraction=SimpleNamespace(format="pdf", title="t", warnings=[]),
        original_path="o.pdf",
        text_path="o.txt",
        note_path="n.md",
        daily_summary="",
    )
    service = SimpleNamespace(
        archive_staged_document=lambda **kwargs: result,
        vault_path=tmp_path,
        content_language="ru",
    )
    message = SimpleNamespace(
        message_id=1, caption=None, from_user=SimpleNamespace(id=42)
    )

    await document_handler._process_document_upload(
        message,
        SimpleNamespace(),
        service,  # type: ignore[arg-type]
        staged=SimpleNamespace(original_path="o.pdf", file_format="pdf"),  # type: ignore[arg-type]
        file_name="o.pdf",
        timestamp=None,  # type: ignore[arg-type]
        source=SimpleNamespace(ref="r", url=None),  # type: ignore[arg-type]
        request="Сделай выжимку",
    )

    execute.assert_awaited_once()
    assert execute.await_args.kwargs == {
        "intent": "action",
        "request": "Сделай выжимку",
    }


async def test_document_caption_prompt_uses_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        document_handler, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )
    execute = AsyncMock()
    monkeypatch.setattr(document_handler, "process_request", execute)

    await document_handler._execute_document_caption(
        SimpleNamespace(caption=None, from_user=SimpleNamespace(id=42)),
        SimpleNamespace(original_path="o.pdf", text_path="o.txt"),  # type: ignore[arg-type]
        intent="action",
        request="Сделай выжимку",
    )

    prompt = execute.await_args.args[1]
    assert prompt.startswith("Сделай выжимку\n\nOriginal document: ")


async def test_save_single_photo_writes_session_and_daily(
    monkeypatch, tmp_path
) -> None:
    entry = SimpleNamespace(
        relative_path="attachments/x.jpg",
        caption="подпись",
        analysis=None,
        source=None,
        content_language="ru",
        timestamp=None,
        entry_type="[photo]",
    )
    calls: list[str] = []

    async def fake_save(_message, _bot):  # noqa: ANN001, ANN202
        calls.append("save")
        return entry

    monkeypatch.setattr(photo_handler, "_save_photo", fake_save)
    monkeypatch.setattr(
        photo_handler, "_log_photo_session", lambda m, e: calls.append("session")
    )
    monkeypatch.setattr(
        photo_handler, "_build_photo_daily_content", lambda *a: "content"
    )
    monkeypatch.setattr(
        photo_handler.VaultStorage,
        "append_to_daily",
        lambda self, content, *a, **k: calls.append(f"daily:{content}"),
    )
    monkeypatch.setattr(
        photo_handler,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path, content_language="ru"),
    )

    assert await photo_handler.save_single_photo(object(), object()) is entry  # type: ignore[arg-type]
    assert calls == ["save", "session", "daily:content"]


def _entry(message_id: int, caption: str | None = None, *, forwarded=False):  # noqa: ANN001, ANN202
    return SimpleNamespace(
        message_id=message_id,
        relative_path=f"attachments/p{message_id}.jpg",
        raw_caption=caption,
        forwarded=forwarded,
    )


async def test_do_album_photo_joins_album_and_runs_once(env, monkeypatch) -> None:
    registered = []

    async def fake_album(message, _bot, on_saved):  # noqa: ANN001, ANN202
        registered.append(on_saved)

    monkeypatch.setattr(do_handler, "save_album_for_request", fake_album)
    message = _message(photo=[object()], media_group_id="g1")

    await do_handler.handle_do_input(message, object(), _State())
    assert env.prompts == []

    await registered[0]([_entry(1), _entry(2, "  Сравни цены  ")])
    vault = env.vault
    assert env.prompts == [
        "Сравни цены\n\nImages:\n"
        f"- {vault / 'attachments/p1.jpg'}\n- {vault / 'attachments/p2.jpg'}\n"
    ]


async def test_do_album_with_forwarded_photo_ignores_captions(env) -> None:
    message = _message(photo=[object()], media_group_id="g1")

    await do_handler._run_album_request(
        message, [_entry(1, "Удали все задачи", forwarded=True), _entry(2)]
    )

    assert env.prompts[0].startswith("Разбери эти изображения.\n\nImages:\n")
    assert "Удали" not in env.prompts[0]


@pytest.fixture
def album(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Real album grouping in photo.py with saving and delivery faked."""
    monkeypatch.setattr(photo_handler, "ALBUM_SETTLE_SECONDS", 0.01)
    state = SimpleNamespace(daily=[], got=[], answer=AsyncMock())

    async def fake_save(message, _bot):  # noqa: ANN001, ANN202
        return SimpleNamespace(
            message_id=message.message_id,
            timestamp=None,
            relative_path=f"attachments/p{message.message_id}.jpg",
            caption=None,
            analysis=None,
            source=None,
            content_language="ru",
            youtube_urls=(),
            link_summaries=(),
            entry_type="[photo]",
            forwarded=False,
            raw_caption=None,
        )

    async def on_saved(entries) -> None:  # noqa: ANN001
        state.got.append([entry.message_id for entry in entries])

    def message(message_id: int, group: str = "g-do") -> SimpleNamespace:
        return SimpleNamespace(
            message_id=message_id,
            media_group_id=group,
            photo=[object()],
            from_user=SimpleNamespace(id=42),
        )

    monkeypatch.setattr(photo_handler, "_save_photo", fake_save)
    monkeypatch.setattr(photo_handler, "_log_photo_session", lambda m, e: None)
    monkeypatch.setattr(
        photo_handler, "_build_album_daily_content", lambda items: "album"
    )
    monkeypatch.setattr(photo_handler, "answer_text", state.answer)
    monkeypatch.setattr(
        photo_handler.VaultStorage,
        "append_to_daily",
        lambda self, content, *a, **k: state.daily.append(content),
    )
    monkeypatch.setattr(
        photo_handler,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path, content_language="ru"),
    )
    state.on_saved = on_saved
    state.message = message
    yield state
    photo_handler._album_requests.clear()


async def test_album_request_runs_after_whole_album_is_saved(album) -> None:
    await photo_handler.save_album_for_request(
        album.message(1), object(), album.on_saved  # type: ignore[arg-type]
    )
    await photo_handler.handle_photo(album.message(2), object())  # type: ignore[arg-type]
    await photo_handler._album_tasks["g-do"]

    assert album.daily == ["album"]
    assert album.got == [[1, 2]]
    assert "g-do" not in photo_handler._album_requests


async def test_album_request_is_dropped_when_flush_fails(album, monkeypatch) -> None:
    def broken(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise OSError("disk full")

    monkeypatch.setattr(photo_handler.VaultStorage, "append_to_daily", broken)

    await photo_handler.save_album_for_request(
        album.message(1), object(), album.on_saved  # type: ignore[arg-type]
    )
    await photo_handler._album_tasks["g-do"]

    assert album.got == []
    assert "g-do" not in photo_handler._album_requests


async def test_late_album_photo_keeps_its_own_request(album) -> None:
    """A late photo registering while the first flush reports must not lose it."""
    late: list[asyncio.Task[None]] = []

    async def answer_and_register_late(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        if not late:
            late.append(
                asyncio.create_task(
                    photo_handler.save_album_for_request(
                        album.message(3), object(), album.on_saved  # type: ignore[arg-type]
                    )
                )
            )
            await asyncio.sleep(0)

    album.answer.side_effect = answer_and_register_late

    await photo_handler.save_album_for_request(
        album.message(1), object(), album.on_saved  # type: ignore[arg-type]
    )
    first = photo_handler._album_tasks["g-do"]
    await first
    await late[0]
    await photo_handler._album_tasks["g-do"]

    assert album.got == [[1], [3]]
