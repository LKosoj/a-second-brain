"""Tests for saving owner-approved /do and /why answers as vault notes
(аудит 2026-09-03, п.24: "Вопрос -> страница").

``save_answer``/``append_log`` are pure vault-write helpers (``tests/
test_compiled_briefings.py``'s pattern of a real temporary vault plus an
explicit ``vault-manifest.json`` via ``conftest._write_vault_manifest`` is
reused here), so most tests below call the real functions with no mocking.
The ``answer:save:<id>`` callback test drives a real ``Dispatcher`` over the
actual ``do.router`` singleton, mirroring ``tests/test_fix_handlers_fsm.py``'s
``_isolated_dispatcher`` helper, with ``Bot.__call__`` faked the same way so
no Telegram network call is attempted.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from conftest import _write_vault_manifest

from d_brain.bot.handlers import do as do_handler
from d_brain.manifest import load_manifest_for_vault
from d_brain.services import answers as answers_module
from d_brain.services.answers import (
    AnswerPayload,
    append_log,
    pop_pending_answer,
    register_pending_answer,
    save_answer,
)
from d_brain.services.frontmatter import (
    UnsafeVaultPathError,
    read_frontmatter,
    validate_document,
    write_validated_vault_markdown,
)


def _build_vault(tmp_path: Path) -> Path:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _write_vault_manifest(vault_path)
    return vault_path


# -- save_answer --------------------------------------------------------


def test_save_answer_writes_a_valid_card_with_sections(tmp_path: Path) -> None:
    vault_path = _build_vault(tmp_path)
    now = datetime(2026, 9, 3, 14, 30)

    saved_path = save_answer(
        vault_path,
        question="Почему выросли продажи в июле?",
        answer_markdown="Продажи выросли на 12% из-за новой акции.",
        sources=["compiled/topics/sales.md"],
        kind="why",
        now=now,
    )

    assert saved_path.parent == vault_path / "answers"
    assert saved_path.name.startswith("2026-09-03-")
    assert saved_path.exists()

    document = read_frontmatter(saved_path)
    manifest = load_manifest_for_vault(vault_path)
    relative = saved_path.relative_to(vault_path).as_posix()
    _route, missing, invalid = validate_document(relative, document, manifest)
    assert not missing
    assert not invalid

    assert document.fields["type"] == "note"
    assert document.fields["description"] == "Почему выросли продажи в июле?"
    assert document.fields["tags"] == ["answer", "why"]
    assert document.fields["status"] == "active"
    assert document.fields["created"] == "2026-09-03"
    assert document.fields["updated"] == "2026-09-03"

    body = saved_path.read_text(encoding="utf-8")
    assert "## Вопрос" in body
    assert "Почему выросли продажи в июле?" in body
    assert "## Ответ" in body
    assert "Продажи выросли на 12%" in body
    assert "## Источники" in body
    assert "[[compiled/topics/sales]]" in body


def test_save_answer_survives_losing_the_race_for_the_same_filename(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = _build_vault(tmp_path)
    now = datetime(2026, 9, 3, 9, 0)
    calls: list[Path] = []
    real_write = write_validated_vault_markdown

    def racing_write(vault, path, content, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(path)
        if len(calls) == 1:
            # Another thread published the same name between our exists()
            # check and the write: the guarded write refuses to overwrite.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"---\ntype: note\n---\n# Other tap\n")
            raise UnsafeVaultPathError("atomic write target already exists")
        return real_write(vault, path, content, **kwargs)

    monkeypatch.setattr(answers_module, "write_validated_vault_markdown", racing_write)

    saved = save_answer(
        vault_path,
        question="Что сделать по проекту сегодня",
        answer_markdown="Ответ 2",
        sources=[],
        kind="do",
        now=now,
    )

    assert saved.name.endswith("-2.md")
    assert calls[0].name == saved.name.replace("-2.md", ".md")
    assert calls[0].read_text(encoding="utf-8").endswith("# Other tap\n")
    assert "Ответ 2" in saved.read_text(encoding="utf-8")


def test_save_answer_collision_gets_a_numeric_suffix(tmp_path: Path) -> None:
    vault_path = _build_vault(tmp_path)
    now = datetime(2026, 9, 3, 9, 0)

    first = save_answer(
        vault_path,
        question="Что сделать по проекту сегодня",
        answer_markdown="Ответ 1",
        sources=[],
        kind="do",
        now=now,
    )
    second = save_answer(
        vault_path,
        question="Что сделать по проекту сегодня",
        answer_markdown="Ответ 2",
        sources=[],
        kind="do",
        now=now,
    )

    assert first != second
    assert second == first.with_name(f"{first.stem}-2{first.suffix}")
    assert first.exists()
    assert second.exists()
    assert "Ответ 1" in first.read_text(encoding="utf-8")
    assert "Ответ 2" in second.read_text(encoding="utf-8")


def test_save_answer_with_no_sources_uses_a_placeholder(tmp_path: Path) -> None:
    vault_path = _build_vault(tmp_path)
    now = datetime(2026, 9, 3, 9, 0)

    saved_path = save_answer(
        vault_path,
        question="Проверить статус задач",
        answer_markdown="Готово.",
        sources=[],
        kind="do",
        now=now,
    )

    body = saved_path.read_text(encoding="utf-8")
    assert "Источники не указаны" in body


# -- append_log -----------------------------------------------------------


def test_append_log_collapses_multiline_summary_to_one_line(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    now = datetime(2026, 9, 3, 9, 5)

    append_log(vault_path, "do", "Первая строка\n  Вторая строка\tвопроса", now)

    log_path = vault_path / ".session" / "log.md"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["- 2026-09-03 09:05 [do] Первая строка Вторая строка вопроса"]


def test_append_log_creates_session_log_and_appends(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    now = datetime(2026, 9, 3, 9, 5)

    append_log(vault_path, "do", "Проверил статус задач", now)
    append_log(vault_path, "why", "Объяснил рост продаж", now)

    log_path = vault_path / ".session" / "log.md"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "- 2026-09-03 09:05 [do] Проверил статус задач",
        "- 2026-09-03 09:05 [why] Объяснил рост продаж",
    ]


# -- pending answer registry ------------------------------------------------


def test_register_and_pop_pending_answer_roundtrip() -> None:
    payload = AnswerPayload(question="Q", answer_markdown="A", sources=(), kind="do")
    answer_id = register_pending_answer(payload)

    assert pop_pending_answer(answer_id) == payload
    # Consumed -- a second tap on the same button must not save twice.
    assert pop_pending_answer(answer_id) is None


def test_pop_pending_answer_unknown_id_is_none() -> None:
    assert pop_pending_answer("does-not-exist") is None


# -- answer:save: callback via a real Dispatcher -----------------------------


@contextmanager
def _isolated_dispatcher(*routers: Router):  # noqa: ANN201
    """Same approach as ``tests/test_fix_handlers_fsm.py``'s helper of the
    same name: attach the process-wide singleton router(s) to a throwaway
    Dispatcher, restoring each one's original parent afterwards -- do.router
    is a singleton bot/main.py attaches for the life of the process.
    """
    saved = [router._parent_router for router in routers]
    for router in routers:
        router._parent_router = None
    dp = Dispatcher(storage=MemoryStorage())
    for router in routers:
        dp.include_router(router)
    try:
        yield dp
    finally:
        for router, parent in zip(routers, saved, strict=True):
            router._parent_router = parent


def _bot_client_module():  # noqa: ANN202
    import aiogram.client.bot as bot_module

    return bot_module.Bot


async def _fake_bot_call(self, method, request_timeout=None):  # noqa: ANN001
    return SimpleNamespace(message_id=1)


def _make_callback_query(data: str, *, chat_id: int, user_id: int) -> CallbackQuery:
    chat = Chat(id=chat_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="Test")
    bot_message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text="Ответ",
    )
    return CallbackQuery(
        id="cbq1",
        from_user=user,
        chat_instance="ci1",
        message=bot_message,
        data=data,
    )


async def test_answer_save_callback_saves_file_and_replies(
    monkeypatch, tmp_path: Path
) -> None:
    vault_path = _build_vault(tmp_path)
    monkeypatch.setattr(
        do_handler,
        "get_settings",
        lambda: SimpleNamespace(vault_path=str(vault_path)),
    )
    monkeypatch.setattr(_bot_client_module(), "__call__", _fake_bot_call)

    payload = AnswerPayload(
        question="Почему выросли продажи",
        answer_markdown="Ответ на вопрос",
        sources=("compiled/topics/sales.md",),
        kind="why",
    )
    answer_id = register_pending_answer(payload)

    with _isolated_dispatcher(do_handler.router) as dp:
        bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")
        query = _make_callback_query(
            f"answer:save:{answer_id}", chat_id=500, user_id=77
        )
        await dp.feed_update(bot, Update(update_id=1, callback_query=query))

    assert pop_pending_answer(answer_id) is None  # already consumed

    saved_files = list((vault_path / "answers").glob("*.md"))
    assert len(saved_files) == 1
    body = saved_files[0].read_text(encoding="utf-8")
    assert "Почему выросли продажи" in body
    assert "[[compiled/topics/sales]]" in body


async def test_answer_save_callback_unknown_id_replies_stale(
    monkeypatch, tmp_path: Path
) -> None:
    vault_path = _build_vault(tmp_path)
    monkeypatch.setattr(
        do_handler,
        "get_settings",
        lambda: SimpleNamespace(vault_path=str(vault_path)),
    )
    answer_calls: list[tuple[str | None, bool]] = []

    async def fake_call(self, method, request_timeout=None):  # noqa: ANN001
        if isinstance(method, AnswerCallbackQuery):
            answer_calls.append((method.text, bool(method.show_alert)))
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(_bot_client_module(), "__call__", fake_call)

    with _isolated_dispatcher(do_handler.router) as dp:
        bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")
        query = _make_callback_query(
            "answer:save:does-not-exist", chat_id=501, user_id=78
        )
        await dp.feed_update(bot, Update(update_id=1, callback_query=query))

    assert not (vault_path / "answers").exists()
    assert any(
        text and "устарел" in text for text, _show_alert in answer_calls
    )
