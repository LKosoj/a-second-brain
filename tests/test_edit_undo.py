"""Editing a saved Telegram message and /undo for the last daily entry (G2)."""

import json
import threading
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import _write_vault_manifest

from d_brain.bot import main as bot_main
from d_brain.bot.handlers import edit as edit_handler
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.entry_status import (
    ENTRY_STATUS_ALREADY_PROCESSED,
    entry_block_digest,
    parse_daily_entry_statuses,
)
from d_brain.services.processor import (
    REFLECT_DAILY_END_MARKER,
    REFLECT_DAILY_START_MARKER,
)
from d_brain.services.session import SessionStore
from d_brain.services.source_links import SourceInfo
from d_brain.services.storage import VaultStorage

DAY = date(2026, 10, 20)
TODAY = datetime(2026, 10, 21, 12, 30).astimezone()
OWNER = 7
PROCESSED = f"<!-- d-brain:entry-status: {ENTRY_STATUS_ALREADY_PROCESSED} -->"


@pytest.fixture(autouse=True)
def _isolated_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_vault_manifest(tmp_path / "vault")
    monkeypatch.setattr(VaultStorage, "_refresh_qmd_index", lambda self: None)
    monkeypatch.setattr(
        CompiledBriefingService,
        "enqueue_refresh",
        lambda self, **kwargs: {"queued": True, "errors": []},
    )
    monkeypatch.setattr(
        CompiledBriefingService, "spawn_background_drain", lambda self: True
    )
    monkeypatch.setattr(edit_handler, "_today", lambda: TODAY)

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(edit_handler, "_sleep", no_sleep)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / "vault").mkdir(exist_ok=True)
    return tmp_path / "vault"


@pytest.fixture
def storage(vault: Path) -> VaultStorage:
    return VaultStorage(vault)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    monkeypatch.setattr(
        edit_handler,
        "get_settings",
        lambda: SimpleNamespace(vault_path=vault, content_language="ru"),
    )


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    answer = AsyncMock()
    monkeypatch.setattr(edit_handler, "answer_text", answer)
    return answer


def _save(
    storage: VaultStorage,
    text: str,
    hour: int,
    msg_id: int,
    *,
    kind: str = "[text]",
    day: date = DAY,
) -> None:
    storage.append_to_daily(
        text,
        datetime(day.year, day.month, day.day, hour, 0),
        kind,
        source=SourceInfo(kind="telegram", ref=f"telegram:1:{msg_id}"),
    )


def _mark_processed(storage: VaultStorage, heading: str, day: date = DAY) -> None:
    path = storage.get_daily_file(day)
    content = path.read_text(encoding="utf-8")
    assert heading in content
    path.write_text(
        content.replace(f"{heading}\n", f"{heading}\n{PROCESSED}\n", 1),
        encoding="utf-8",
    )


def _log(
    vault: Path, kind: str, msg_id: int, *, day: date = DAY, **data: Any
) -> None:
    """Session record as the capture handlers wrote it on the message day."""
    path = vault / ".sessions" / str(OWNER) / f"{day.isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": day.isoformat(), "type": kind, "msg_id": msg_id, **data}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _last_edit_record(vault: Path) -> dict[str, Any]:
    edits = [e for e in SessionStore(vault).get_recent(OWNER) if e["type"] == "edit"]
    return edits[-1]


def _edited(
    msg_id: int,
    *,
    text: str | None = None,
    caption: str | None = None,
    sent: datetime | None = None,
):
    return SimpleNamespace(
        message_id=msg_id,
        chat=SimpleNamespace(id=1, type="private"),
        from_user=SimpleNamespace(id=OWNER),
        date=(sent or datetime(2026, 10, 20, 10, 0)).astimezone(),
        text=text,
        caption=caption,
        photo=[object()] if caption is not None else None,
    )


def _sent(replies: AsyncMock) -> str:
    return str(replies.await_args.args[1])


# --- storage: edit_daily_entry ------------------------------------------------


def test_edit_replaces_unprocessed_text_entry(storage: VaultStorage) -> None:
    _save(storage, "соседняя запись", 9, 9)
    _save(storage, "старый", 10, 10)
    _save(storage, "ещё одна", 11, 11)
    before = storage.get_daily_file(DAY).read_text(encoding="utf-8")

    assert storage.edit_daily_entry(DAY, "telegram:1:10", "новый") == "replaced"

    after = storage.get_daily_file(DAY).read_text(encoding="utf-8")
    assert "новый" in after
    assert "старый" not in after
    assert after.count("## 10:00 [text]") == 1
    assert "`telegram:1:10`" in after
    assert after == before.replace("старый", "новый")


def test_edit_processed_entry_returns_processed_and_keeps_text(
    storage: VaultStorage,
) -> None:
    _save(storage, "старый", 10, 10)
    _mark_processed(storage, "## 10:00 [text]")
    path = storage.get_daily_file(DAY)
    before = path.read_bytes()

    assert storage.edit_daily_entry(DAY, "telegram:1:10", "новый") == "processed"
    assert path.read_bytes() == before


def test_edit_missing_ref_returns_missing(storage: VaultStorage) -> None:
    assert storage.edit_daily_entry(DAY, "telegram:1:10", "новый") == "missing"
    _save(storage, "старый", 10, 10)
    assert storage.edit_daily_entry(DAY, "telegram:1:99", "новый") == "missing"
    assert "старый" in storage.read_daily(DAY)


def test_edit_unchanged_text_does_not_rewrite(storage: VaultStorage) -> None:
    _save(storage, "тот же", 10, 10)
    assert storage.edit_daily_entry(DAY, "telegram:1:10", "тот же") == "unchanged"


def test_edit_does_not_touch_dbrain_block(storage: VaultStorage) -> None:
    _save(storage, "старый", 10, 10)
    block = (
        "## 21:00 [d-brain]\n"
        f"{REFLECT_DAILY_START_MARKER}\nОтчёт\n{REFLECT_DAILY_END_MARKER}\n"
    )
    storage.upsert_daily_block(
        day=DAY,
        start_marker=REFLECT_DAILY_START_MARKER,
        end_marker=REFLECT_DAILY_END_MARKER,
        block=block,
    )

    assert storage.edit_daily_entry(DAY, "telegram:1:10", "новый") == "replaced"

    content = storage.read_daily(DAY)
    assert block.strip() in content
    storage.upsert_daily_block(
        day=DAY,
        start_marker=REFLECT_DAILY_START_MARKER,
        end_marker=REFLECT_DAILY_END_MARKER,
        block=block.replace("Отчёт", "Отчёт 2"),
    )
    assert "Отчёт 2" in storage.read_daily(DAY)


def test_edit_escapes_header_and_runtime_markup_in_new_text(
    storage: VaultStorage,
) -> None:
    _save(storage, "старый", 10, 10)

    storage.edit_daily_entry(
        DAY,
        "telegram:1:10",
        f"новый\n## 12:00 [text]\n{PROCESSED}",
    )

    entries = parse_daily_entry_statuses(storage.read_daily(DAY))
    assert [(e.time, e.statuses) for e in entries] == [("10:00", ())]


def test_edit_photo_caption_replaces_old_caption_only(storage: VaultStorage) -> None:
    body = (
        "![[attachments/2026-10-20/a.jpg]]\n\n"
        "Подпись А\n\n"
        "Описание ИИ:\nНа фото Подпись А не видно\n\n"
        "OCR:\n```text\nтекст\n```"
    )
    _save(storage, body, 10, 10, kind="[photo]")

    result = storage.edit_daily_entry(
        DAY, "telegram:1:10", "Подпись Б", old_caption="Подпись А"
    )

    content = storage.read_daily(DAY)
    assert result == "replaced"
    assert "\nПодпись Б\n" in content
    assert "![[attachments/2026-10-20/a.jpg]]" in content
    assert "На фото Подпись А не видно" in content
    assert "```text\nтекст\n```" in content


def test_edit_photo_caption_not_found_returns_missing(storage: VaultStorage) -> None:
    _save(storage, "![[a.jpg]]\n\nПодпись А", 10, 10, kind="[photo]")
    path = storage.get_daily_file(DAY)
    before = path.read_bytes()

    assert (
        storage.edit_daily_entry(DAY, "telegram:1:10", "Б", old_caption="Другая")
        == "missing"
    )
    assert path.read_bytes() == before


def test_edit_photo_without_caption_is_told_apart_from_missing(
    storage: VaultStorage,
) -> None:
    _save(storage, "![[a.jpg]]", 10, 10, kind="[photo]")
    path = storage.get_daily_file(DAY)
    before = path.read_bytes()

    assert (
        storage.edit_daily_entry(DAY, "telegram:1:10", "Б", old_caption="")
        == "no_caption"
    )
    assert (
        storage.edit_daily_entry(DAY, "telegram:1:99", "Б", old_caption="")
        == "missing"
    )
    assert path.read_bytes() == before


# --- handler: edited_message --------------------------------------------------


async def test_handle_edited_updates_entry_and_logs_edit_session(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    _save(storage, "старый", 10, 10)
    _log(vault, "text", 10, text="старый")

    await edit_handler.handle_edited(_edited(10, text="новый"))

    content = storage.read_daily(DAY)
    assert "новый" in content and "старый" not in content
    assert "Запись обновлена" in _sent(replies)
    assert _last_edit_record(vault)["text"] == "новый"


async def test_handle_edited_twice_uses_the_ref_not_old_text(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    _save(storage, "первый", 10, 10)
    _log(vault, "text", 10, text="первый")

    await edit_handler.handle_edited(_edited(10, text="второй"))
    await edit_handler.handle_edited(_edited(10, text="третий"))

    content = storage.read_daily(DAY)
    assert "третий" in content and "второй" not in content
    assert content.count("## 10:00 [text]") == 1


async def test_handle_edited_photo_caption(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    _save(storage, "![[a.jpg]]\n\nПодпись А", 10, 10, kind="[photo]")
    _log(vault, "photo", 10, caption="Подпись А")

    await edit_handler.handle_edited(_edited(10, caption="Подпись Б"))

    content = storage.read_daily(DAY)
    assert "Подпись Б" in content and "Подпись А" not in content
    assert _last_edit_record(vault)["caption"] == "Подпись Б"


async def test_handle_edited_processed_appends_correction_entry(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    _save(storage, "старый", 10, 10)
    _mark_processed(storage, "## 10:00 [text]")
    _log(vault, "text", 10, text="старый")
    original = storage.get_daily_file(DAY).read_bytes()

    await edit_handler.handle_edited(_edited(10, text="новый"))

    assert storage.get_daily_file(DAY).read_bytes() == original
    today = storage.read_daily(TODAY.date())
    assert "## 12:30 [text]" in today
    assert "Исправление записи от 20.10 10:00" in today
    assert "`telegram:1:10:edit`" in today
    assert "новый" in today
    assert "отдельной записью" in _sent(replies)


async def test_handle_edited_second_correction_replaces_unprocessed_correction(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    _save(storage, "старый", 10, 10)
    _mark_processed(storage, "## 10:00 [text]")
    _log(vault, "text", 10, text="старый")

    await edit_handler.handle_edited(_edited(10, text="первая правка"))
    await edit_handler.handle_edited(_edited(10, text="вторая правка"))

    today = storage.read_daily(TODAY.date())
    assert today.count("`telegram:1:10:edit`") == 1
    assert "вторая правка" in today and "первая правка" not in today


async def test_handle_edited_question_without_daily_entry_is_silent(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    _save(storage, "старый", 10, 99)
    before = storage.get_daily_file(DAY).read_bytes()

    _log(vault, "question", 11, text="вопрос")
    await edit_handler.handle_edited(_edited(11, text="другой вопрос"))

    assert storage.get_daily_file(DAY).read_bytes() == before
    replies.assert_not_awaited()


async def test_handle_edited_legacy_entry_without_ref_appends_correction(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    storage.append_to_daily("старый", datetime(2026, 10, 20, 10, 0), "[text]")
    _log(vault, "text", 10, text="старый")

    await edit_handler.handle_edited(_edited(10, text="новый"))

    assert "старый" in storage.read_daily(DAY)
    today = storage.read_daily(TODAY.date())
    assert "новый" in today and "`telegram:1:10:edit`" in today
    assert "исходной записи не нашёл" in _sent(replies)


async def test_handle_edited_voice_record_reports_unsupported(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    _save(storage, "голос", 10, 10, kind="[voice]")
    _log(vault, "voice", 10, text="голос")
    before = storage.get_daily_file(DAY).read_bytes()

    await edit_handler.handle_edited(_edited(10, text="новый"))

    assert "Правку этого типа" in _sent(replies)
    assert storage.get_daily_file(DAY).read_bytes() == before


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the real pause of the edit handler; returns the pauses asked for."""
    asked: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        asked.append(seconds)

    monkeypatch.setattr(edit_handler, "_sleep", fake_sleep)
    return asked


async def test_handle_edited_waits_for_session_record_then_edits_in_place(
    storage: VaultStorage,
    vault: Path,
    replies: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save(storage, "опечаткаа", 10, 10)
    asked: list[float] = []

    async def sleep_then_log(seconds: float) -> None:
        asked.append(seconds)
        if len(asked) == 2:
            _log(vault, "text", 10, text="опечаткаа")

    monkeypatch.setattr(edit_handler, "_sleep", sleep_then_log)

    await edit_handler.handle_edited(_edited(10, text="опечатка"))

    content = storage.read_daily(DAY)
    assert "опечатка" in content and "опечаткаа" not in content
    assert "Запись обновлена" in _sent(replies)
    assert len(asked) == 2
    assert _last_edit_record(vault)["text"] == "опечатка"


async def test_handle_edited_without_session_adds_separate_correction(
    storage: VaultStorage, vault: Path, replies: AsyncMock, waits: list[float]
) -> None:
    _save(storage, "старый", 10, 10)
    before = storage.get_daily_file(DAY).read_bytes()

    await edit_handler.handle_edited(_edited(10, text="новый"))

    assert waits and sum(waits) <= 10
    assert storage.get_daily_file(DAY).read_bytes() == before
    today = storage.read_daily(TODAY.date())
    assert "Исправление записи от 20.10 10:00" in today
    assert "`telegram:1:10:edit`" in today and "новый" in today
    assert "отдельной записью" in _sent(replies)


async def test_handle_edited_command_or_unsaved_message_stays_silent(
    storage: VaultStorage, vault: Path, replies: AsyncMock, waits: list[float]
) -> None:
    # Nothing in daily and nothing in the session: a question, /do, a reminder.
    await edit_handler.handle_edited(_edited(12, text="что у меня на завтра?"))

    assert not storage.get_daily_file(TODAY.date()).exists()
    replies.assert_not_awaited()


def test_edited_filter_ignores_commands_and_empty_messages() -> None:
    accepts = edit_handler.router.edited_message.handlers[0].filters[0].callback
    assert accepts(_edited(1, text="обычный текст"))
    assert accepts(_edited(1, caption="подпись"))
    assert not accepts(_edited(1, text="/do сделай"))
    assert not accepts(_edited(1, text=None))


# --- /undo ----------------------------------------------------------------------


def _undo_message() -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=OWNER))


def _buttons(replies: AsyncMock) -> list[Any]:
    markup = replies.await_args.kwargs["reply_markup"]
    return [button for row in markup.inline_keyboard for button in row]


def _fill_today(storage: VaultStorage) -> None:
    day = TODAY.date()
    _save(storage, "первая", 9, 1, day=day)
    _save(storage, "вторая", 10, 2, day=day)
    _save(storage, "третья", 11, 3, day=day)
    storage.upsert_daily_block(
        day=day,
        start_marker=REFLECT_DAILY_START_MARKER,
        end_marker=REFLECT_DAILY_END_MARKER,
        block=(
            "## 21:00 [d-brain]\n"
            f"{REFLECT_DAILY_START_MARKER}\nОтчёт\n{REFLECT_DAILY_END_MARKER}\n"
        ),
    )
    storage.append_to_daily(
        "встреча",
        datetime(2026, 10, 21, 22, 0),
        "[plaud]",
        source=SourceInfo(kind="plaud", ref="plaud:abc"),
    )


async def test_undo_offers_last_telegram_entry_with_digest_button(
    storage: VaultStorage, replies: AsyncMock
) -> None:
    _fill_today(storage)

    await edit_handler.cmd_undo(_undo_message())

    assert "третья" in _sent(replies)
    assert "встреча" not in _sent(replies)
    buttons = _buttons(replies)
    assert len(buttons) == 2
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert buttons[0].callback_data.startswith("undo:y:20261021:")
    assert buttons[1].callback_data == "undo:n"


async def test_undo_refuses_processed_last_entry(
    storage: VaultStorage, replies: AsyncMock
) -> None:
    _fill_today(storage)
    _mark_processed(storage, "## 11:00 [text]", TODAY.date())
    before = storage.get_daily_file(TODAY.date()).read_bytes()

    await edit_handler.cmd_undo(_undo_message())

    assert "уже обработана" in _sent(replies)
    assert "reply_markup" not in replies.await_args.kwargs
    assert storage.get_daily_file(TODAY.date()).read_bytes() == before


async def test_undo_empty_day_says_nothing_to_undo(replies: AsyncMock) -> None:
    await edit_handler.cmd_undo(_undo_message())

    assert "нечего отменять" in _sent(replies)


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=OWNER),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )


def _digest_of(storage: VaultStorage, heading_time: str) -> str:
    summary = storage.last_undoable_entry(TODAY.date())
    assert summary is not None and summary.time == heading_time
    return summary.digest


async def test_undo_confirm_deletes_only_selected_entry(
    storage: VaultStorage, vault: Path
) -> None:
    _fill_today(storage)
    day = TODAY.date()
    digest = _digest_of(storage, "11:00")
    callback = _callback(f"undo:y:{day:%Y%m%d}:{digest}")

    await edit_handler.cb_undo(callback)

    content = storage.read_daily(day)
    assert "третья" not in content
    assert "первая" in content and "вторая" in content
    assert "встреча" in content
    assert f"{REFLECT_DAILY_START_MARKER}\nОтчёт\n{REFLECT_DAILY_END_MARKER}" in content
    assert "\n\n\n" not in content
    assert any(
        entry["type"] == "undo" for entry in SessionStore(vault).get_today(OWNER)
    )
    callback.answer.assert_awaited()


async def test_undo_confirm_after_entry_changed_is_rejected(
    storage: VaultStorage,
) -> None:
    _fill_today(storage)
    day = TODAY.date()
    digest = _digest_of(storage, "11:00")
    storage.edit_daily_entry(day, "telegram:1:3", "переписанная")
    before = storage.get_daily_file(day).read_bytes()
    callback = _callback(f"undo:y:{day:%Y%m%d}:{digest}")

    await edit_handler.cb_undo(callback)

    assert storage.delete_daily_entry(day, digest) == "missing"
    assert storage.get_daily_file(day).read_bytes() == before
    assert "изменилась" in callback.message.edit_text.await_args.args[0]


async def test_undo_confirm_processed_between_prompt_and_click(
    storage: VaultStorage,
) -> None:
    _fill_today(storage)
    day = TODAY.date()
    digest = _digest_of(storage, "11:00")
    _mark_processed(storage, "## 11:00 [text]", day)
    before = storage.get_daily_file(day).read_bytes()

    assert storage.delete_daily_entry(day, digest) == "processed"
    await edit_handler.cb_undo(_callback(f"undo:y:{day:%Y%m%d}:{digest}"))

    assert storage.get_daily_file(day).read_bytes() == before


async def test_undo_cancel_button_changes_nothing(storage: VaultStorage) -> None:
    _fill_today(storage)
    before = storage.get_daily_file(TODAY.date()).read_bytes()
    callback = _callback("undo:n")

    await edit_handler.cb_undo(callback)

    assert storage.get_daily_file(TODAY.date()).read_bytes() == before
    callback.answer.assert_awaited()


async def test_undo_broken_callback_data_is_reported_stale() -> None:
    callback = _callback("undo:y:garbage")

    await edit_handler.cb_undo(callback)

    callback.answer.assert_awaited_with("Кнопка устарела")


def test_delete_never_removes_dbrain_or_refless_blocks(storage: VaultStorage) -> None:
    _fill_today(storage)
    day = TODAY.date()
    content = storage.read_daily(day)
    plaud_digest = entry_block_digest(content.split("## 22:00 [plaud]")[1])

    assert storage.delete_daily_entry(day, plaud_digest) == "missing"
    assert storage.delete_daily_entry(day, "0" * 16) == "missing"


def test_edit_and_delete_race_with_parallel_append(storage: VaultStorage) -> None:
    _save(storage, "правим", 8, 1)
    _save(storage, "удаляем", 9, 2)
    digest = entry_block_digest(
        storage.read_daily(DAY).split("## 09:00 [text]")[1]
    )
    errors: list[BaseException] = []

    def appender() -> None:
        try:
            for index in range(20):
                storage.append_to_daily(
                    f"параллельная {index}",
                    datetime(2026, 10, 20, 12, index),
                    "[text]",
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=appender)
    thread.start()
    result_edit = storage.edit_daily_entry(DAY, "telegram:1:1", "поправлено")
    result_delete = storage.delete_daily_entry(DAY, digest)
    thread.join()

    content = storage.read_daily(DAY)
    assert not errors
    assert (result_edit, result_delete) == ("replaced", "deleted")
    assert all(f"параллельная {index}" in content for index in range(20))
    assert "поправлено" in content and "удаляем" not in content


# --- wiring ---------------------------------------------------------------------


def test_dispatcher_serves_edits_and_keeps_router_order() -> None:
    dp = bot_main.create_dispatcher()
    used = dp.resolve_used_update_types()
    names = [router.name for router in dp.sub_routers]

    assert "edited_message" in used
    assert "callback_query" in used
    assert names[-1] == "fallback"
    assert names.index("edit") < names.index("text")


async def test_auth_middleware_edited_message_owner_and_stranger() -> None:
    middleware = bot_main.create_auth_middleware(
        SimpleNamespace(owner_telegram_id=OWNER)  # type: ignore[arg-type]
    )

    def update(user_id: int, chat_type: str) -> SimpleNamespace:
        return SimpleNamespace(
            message=None,
            edited_message=SimpleNamespace(
                from_user=SimpleNamespace(id=user_id),
                chat=SimpleNamespace(id=1, type=chat_type),
            ),
        )

    async def handler(event: Any, data: dict[str, Any]) -> str:
        return "handled"

    assert await middleware(handler, update(OWNER, "private"), {}) == "handled"
    assert await middleware(handler, update(99, "private"), {}) is None
    assert await middleware(handler, update(OWNER, "supergroup"), {}) is None


async def test_configure_bot_commands_lists_undo() -> None:
    bot = SimpleNamespace(set_my_commands=AsyncMock())

    await bot_main.configure_bot_commands(bot)  # type: ignore[arg-type]

    commands = bot.set_my_commands.await_args.args[0]
    assert "undo" in [command.command for command in commands]


# --- session.find_by_msg_id -----------------------------------------------------


def test_session_find_by_msg_id_returns_latest_and_skips_question(
    vault: Path,
) -> None:
    store = SessionStore(vault)
    store.append(OWNER, "text", msg_id=5, text="первый")
    store.append(OWNER, "edit", msg_id=5, text="второй")
    store.append(OWNER, "question", msg_id=6, text="вопрос")
    store.append(OWNER, "command", msg_id=7, cmd="/stats")
    today = store._local_today()

    found = store.find_by_msg_id(OWNER, 5, today)
    assert found is not None and found["text"] == "второй"
    assert store.find_by_msg_id(OWNER, 6, today) is None
    assert store.find_by_msg_id(OWNER, 7, today) is None
    # The record is stored under the day it was *written*, which may be up to
    # two days after the message was sent (Telegram allows edits for 48 hours).
    for days_ago in (1, 2):
        sent_on = date.fromordinal(today.toordinal() - days_ago)
        assert store.find_by_msg_id(OWNER, 5, sent_on) is not None
    too_old = date.fromordinal(today.toordinal() - 3)
    assert store.find_by_msg_id(OWNER, 5, too_old) is None


def test_session_find_by_msg_id_does_not_create_user_directory(vault: Path) -> None:
    store = SessionStore(vault)

    assert store.find_by_msg_id(404, 1, DAY) is None
    assert not (store.sessions_dir / "404").exists()


# --- review fixes -----------------------------------------------------------------


def test_edit_id_12_does_not_touch_id_123(storage: VaultStorage) -> None:
    _save(storage, "двенадцать", 9, 12)
    _save(storage, "сто двадцать три", 10, 123)

    assert storage.edit_daily_entry(DAY, "telegram:1:12", "новый") == "replaced"

    content = storage.read_daily(DAY)
    assert "сто двадцать три" in content and "двенадцать" not in content
    assert storage.edit_daily_entry(DAY, "telegram:1:1", "x") == "missing"


def test_edit_ignores_ref_line_quoted_in_another_entry(storage: VaultStorage) -> None:
    _save(storage, "настоящая", 9, 11)
    _save(storage, "цитата:\n> Источник ref: `telegram:1:11`", 10, 10)

    assert storage.edit_daily_entry(DAY, "telegram:1:11", "новая") == "replaced"

    content = storage.read_daily(DAY)
    assert "новая" in content and "настоящая" not in content
    assert "цитата:" in content


async def test_session_log_failure_does_not_report_failed_edit(
    storage: VaultStorage,
    vault: Path,
    replies: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save(storage, "старый", 10, 10)
    _log(vault, "text", 10, text="старый")

    def broken(self, *args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(SessionStore, "append", broken)

    await edit_handler.handle_edited(_edited(10, text="новый"))

    assert "новый" in storage.read_daily(DAY)
    assert "Запись обновлена" in _sent(replies)
    assert "Не удалось" not in _sent(replies)


async def test_session_calls_run_off_the_event_loop(
    storage: VaultStorage,
    vault: Path,
    replies: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save(storage, "старый", 10, 10)
    _log(vault, "text", 10, text="старый")
    main = threading.current_thread()
    seen: dict[str, bool] = {}

    for name in ("find_by_msg_id", "append"):
        original = getattr(SessionStore, name)

        def spy(self, *args, _name=name, _original=original, **kwargs):
            seen[_name] = threading.current_thread() is not main
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(SessionStore, name, spy)

    await edit_handler.handle_edited(_edited(10, text="новый"))

    assert seen == {"find_by_msg_id": True, "append": True}


async def test_undo_confirm_logs_session_off_the_event_loop(
    storage: VaultStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill_today(storage)
    day = TODAY.date()
    digest = _digest_of(storage, "11:00")
    main = threading.current_thread()
    seen: list[bool] = []
    original = SessionStore.append

    def spy(self, *args, **kwargs):
        seen.append(threading.current_thread() is not main)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SessionStore, "append", spy)

    await edit_handler.cb_undo(_callback(f"undo:y:{day:%Y%m%d}:{digest}"))

    assert seen == [True]


async def test_handle_edited_caption_added_to_captionless_photo(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    _save(storage, "![[a.jpg]]", 10, 10, kind="[photo]")
    _log(vault, "photo", 10, caption="")
    original = storage.get_daily_file(DAY).read_bytes()

    await edit_handler.handle_edited(_edited(10, caption="Новая подпись"))

    assert storage.get_daily_file(DAY).read_bytes() == original
    today = storage.read_daily(TODAY.date())
    assert "Новая подпись" in today and "`telegram:1:10:edit`" in today
    assert "Подпись добавлена отдельной записью" in _sent(replies)
    assert "не нашёл" not in _sent(replies)


async def test_handle_edited_after_undo_does_not_resurrect_entry(
    storage: VaultStorage, vault: Path, replies: AsyncMock
) -> None:
    now = datetime.now().astimezone()
    today = now.date()
    storage.append_to_daily(
        "ошибочная",
        now.replace(hour=10, minute=0),
        "[text]",
        source=SourceInfo(kind="telegram", ref="telegram:1:50"),
    )
    _log(vault, "text", 50, day=today, text="ошибочная")
    digest = storage.last_undoable_entry(today).digest

    await edit_handler.cb_undo(_callback(f"undo:y:{today:%Y%m%d}:{digest}"))
    after_undo = storage.get_daily_file(today).read_bytes()
    await edit_handler.handle_edited(_edited(50, text="снова", sent=now))

    assert storage.get_daily_file(today).read_bytes() == after_undo
    assert "снова" not in storage.read_daily(today)
    assert "/undo" in _sent(replies)
