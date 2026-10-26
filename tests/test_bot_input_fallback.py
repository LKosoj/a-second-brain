"""Bot input gaps: reminder buttons, voice reminders, audio/video notes,
unknown commands, video/location/contact, unsupported types and error texts
(review G3, C5, B4)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User
from conftest import _write_vault_manifest

from d_brain.bot import voice_input
from d_brain.bot.handlers import (
    commands as commands_handler,
)
from d_brain.bot.handlers import (
    do as do_handler,
)
from d_brain.bot.handlers import (
    fallback as fallback_handler,
)
from d_brain.bot.handlers import (
    forward as forward_handler,
)
from d_brain.bot.handlers import (
    photo as photo_handler,
)
from d_brain.bot.handlers import (
    reminders as reminders_handler,
)
from d_brain.bot.handlers import (
    text as text_handler,
)
from d_brain.bot.handlers import (
    voice as voice_handler,
)
from d_brain.services import reminders as reminders_service
from d_brain.services.reminders import Reminder, add_reminder, list_pending

NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
CHAT_ID = 100


@pytest.fixture(autouse=True)
def _vault_manifest(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _write_vault_manifest(tmp_path)
    # The post-write qmd/compiled refresh is not under test and costs seconds.
    monkeypatch.setattr(
        "d_brain.services.storage.VaultStorage._post_daily_write",
        lambda self, *args, **kwargs: None,
    )


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        vault_path=tmp_path,
        content_language="ru",
        deepgram_api_key="key",
        ai_cli="codex",
        todoist_api_key="",
        owner_full_name="Иван Иванов",
    )


def _daily_text(tmp_path: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((tmp_path / "daily").glob("*.md"))
    )


def _session_lines(tmp_path: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((tmp_path / ".sessions").rglob("*.jsonl"))
    )


@contextmanager
def _isolated_dispatcher(*routers: Router):  # noqa: ANN201
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


def _real_message(message_id: int = 1, **fields: Any) -> Message:
    return Message(
        message_id=message_id,
        date=NOW,
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Test"),
        **fields,
    )


# --- G3: reminder buttons -------------------------------------------------


def _two_reminders(tmp_path: Path):
    first = add_reminder(
        tmp_path,
        CHAT_ID,
        Reminder(due_at=NOW + timedelta(hours=5), text="позвонить Ивану"),
        now=NOW,
    )
    second = add_reminder(
        tmp_path,
        CHAT_ID,
        Reminder(due_at=NOW + timedelta(days=1), text="купить хлеб"),
        now=NOW,
    )
    return first, second


def _keyboard_data(reply_markup) -> list[str]:  # noqa: ANN001
    return [b.callback_data for row in reply_markup.inline_keyboard for b in row]


async def test_reminders_list_has_cancel_and_postpone_buttons_within_callback_limit(
    monkeypatch, tmp_path: Path
) -> None:
    _two_reminders(tmp_path)
    sent: list[dict[str, Any]] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        sent.append({"text": text, **kwargs})

    monkeypatch.setattr(reminders_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(reminders_handler, "get_settings", lambda: _settings(tmp_path))

    await reminders_handler.handle_reminders_list(_real_message(text="/reminders"))

    data = _keyboard_data(sent[0]["reply_markup"])
    assert len(data) == 4
    assert all(len(item.encode()) <= 64 for item in data)
    assert all(item.startswith("rem:") for item in data)
    assert "позвонить Ивану" in sent[0]["text"]


async def test_reminders_list_without_pending_has_no_keyboard(
    monkeypatch, tmp_path: Path
) -> None:
    sent: list[dict[str, Any]] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        sent.append({"text": text, **kwargs})

    monkeypatch.setattr(reminders_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(reminders_handler, "get_settings", lambda: _settings(tmp_path))

    await reminders_handler.handle_reminders_list(_real_message(text="/reminders"))

    assert sent[0]["text"] == "Нет активных напоминаний"
    assert sent[0]["reply_markup"] is None


def _query(data: str, chat_id: int = CHAT_ID) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
        answer=AsyncMock(),
    )


def _patch_action_env(monkeypatch, tmp_path: Path) -> list[dict[str, Any]]:
    edits: list[dict[str, Any]] = []

    async def fake_edit_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        edits.append({"text": text, **kwargs})

    monkeypatch.setattr(reminders_handler, "edit_text", fake_edit_text)
    monkeypatch.setattr(reminders_handler, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(reminders_handler, "_now", lambda: NOW)
    return edits


def _button(tmp_path: Path, record, action: str) -> str:  # noqa: ANN001
    return f"rem:{action}:{record.id}:{reminders_handler._fingerprint(record)}"


async def test_reminder_cancel_callback_removes_record_and_answers_once(
    monkeypatch, tmp_path: Path
) -> None:
    first, second = _two_reminders(tmp_path)
    edits = _patch_action_env(monkeypatch, tmp_path)
    query = _query(_button(tmp_path, first, "c"))

    await reminders_handler.handle_reminder_action(query)

    query.answer.assert_awaited_once()
    assert "Отменено" in query.answer.await_args.args[0]
    assert [r.id for r in list_pending(tmp_path)] == [second.id]
    assert len(edits) == 1
    assert "купить хлеб" in edits[0]["text"]
    assert "позвонить Ивану" not in edits[0]["text"]


async def test_reminder_postpone_callback_changes_due_at(
    monkeypatch, tmp_path: Path
) -> None:
    first, _ = _two_reminders(tmp_path)
    _patch_action_env(monkeypatch, tmp_path)
    query = _query(_button(tmp_path, first, "p"))

    await reminders_handler.handle_reminder_action(query)

    query.answer.assert_awaited_once()
    moved = next(r for r in list_pending(tmp_path) if r.id == first.id)
    tomorrow = (NOW + timedelta(days=1)).date()
    assert moved.due_at.astimezone(NOW.tzinfo).date() == tomorrow
    assert f"{moved.due_at:%d.%m %H:%M}" in query.answer.await_args.args[0]


async def test_reminder_callback_with_stale_fingerprint_does_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    first, _ = _two_reminders(tmp_path)
    _patch_action_env(monkeypatch, tmp_path)
    old_button = _button(tmp_path, first, "p")

    await reminders_handler.handle_reminder_action(_query(old_button))
    journal = reminders_service._reminders_path(tmp_path).read_bytes()

    # The same (now outdated) button must be rejected after the postpone.
    query = _query(old_button)
    await reminders_handler.handle_reminder_action(query)
    query.answer.assert_awaited_once()
    assert "устарел" in query.answer.await_args.args[0]
    assert reminders_service._reminders_path(tmp_path).read_bytes() == journal

    bogus = _query(f"rem:c:{first.id}:deadbeef")
    await reminders_handler.handle_reminder_action(bogus)
    assert "устарел" in bogus.answer.await_args.args[0]
    assert len(list_pending(tmp_path)) == 2


async def test_reminder_callback_ignores_other_chats_record(
    monkeypatch, tmp_path: Path
) -> None:
    first, _ = _two_reminders(tmp_path)
    _patch_action_env(monkeypatch, tmp_path)
    journal = reminders_service._reminders_path(tmp_path).read_bytes()

    query = _query(_button(tmp_path, first, "c"), chat_id=999)
    await reminders_handler.handle_reminder_action(query)

    assert reminders_service._reminders_path(tmp_path).read_bytes() == journal
    assert "устарел" in query.answer.await_args.args[0]


async def test_reminder_callback_with_garbage_data_is_rejected(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_action_env(monkeypatch, tmp_path)
    query = _query("rem:x")

    await reminders_handler.handle_reminder_action(query)

    query.answer.assert_awaited_once()
    assert "Некорректная" in query.answer.await_args.args[0]


# --- voice ------------------------------------------------------------------


class _FakeBot:
    def __init__(self) -> None:
        self.requested: list[str] = []

    async def get_file(self, file_id: str):  # noqa: ANN202
        self.requested.append(file_id)
        return SimpleNamespace(file_path=f"{file_id}.bin")

    async def download_file(self, file_path: str):  # noqa: ANN202
        return SimpleNamespace(read=lambda: b"bytes")


class _FakeChat:
    id = CHAT_ID
    type = "private"
    username = ""

    async def do(self, action: str) -> None:
        return None


def _voice_message(
    *, voice=None, audio=None, video_note=None, forward_origin=None  # noqa: ANN001
) -> SimpleNamespace:
    answers: list[str] = []

    async def answer(text: str, parse_mode=None) -> None:  # noqa: ANN001
        answers.append(text)

    return SimpleNamespace(
        voice=voice,
        audio=audio,
        video_note=video_note,
        from_user=SimpleNamespace(id=42),
        chat=_FakeChat(),
        date=datetime(2026, 10, 25, 12, 0),
        message_id=7,
        forward_origin=forward_origin,
        answer=answer,
        answers=answers,
    )


def _patch_voice(monkeypatch, tmp_path: Path, transcript: str) -> list[str]:  # noqa: ANN001
    answers: list[str] = []

    class FakeTranscriber:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def transcribe(self, audio_bytes: bytes) -> str:
            return transcript

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)
        return SimpleNamespace()

    async def noop_refresh(vault_path) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(voice_handler, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(voice_input, "DeepgramTranscriber", FakeTranscriber)
    monkeypatch.setattr(voice_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(voice_handler, "_refresh_qmd_in_background", noop_refresh)
    monkeypatch.setattr(
        reminders_handler, "get_settings", lambda: _settings(tmp_path)
    )
    return answers


_VOICE = SimpleNamespace(file_id="voice-file", duration=7, file_size=1000)


async def test_voice_reminder_phrase_creates_reminder_not_daily_entry(
    monkeypatch, tmp_path: Path
) -> None:
    answers = _patch_voice(monkeypatch, tmp_path, "напомни завтра в 10 позвонить Ивану")

    await voice_handler.handle_voice(_voice_message(voice=_VOICE), _FakeBot())

    pending = list_pending(tmp_path)
    assert len(pending) == 1
    assert pending[0].chat_id == CHAT_ID
    assert "Ивану" in pending[0].text
    assert _daily_text(tmp_path) == ""
    assert len(answers) == 1
    assert "Напомню" in answers[0]


async def test_voice_unparseable_reminder_phrase_still_saved_to_daily(
    monkeypatch, tmp_path: Path
) -> None:
    answers = _patch_voice(monkeypatch, tmp_path, "напомни как погода")

    await voice_handler.handle_voice(_voice_message(voice=_VOICE), _FakeBot())

    assert list_pending(tmp_path) == []
    assert "[voice]" in _daily_text(tmp_path)
    assert "напомни как погода" in _daily_text(tmp_path)
    assert "Напоминание не создано" in answers[0]


async def test_forwarded_voice_with_reminder_phrase_is_not_a_reminder(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_voice(monkeypatch, tmp_path, "напомни завтра в 10 позвонить Ивану")
    origin = SimpleNamespace(
        sender_user=SimpleNamespace(full_name="Пётр Петров"),
        sender_user_name=None,
        chat=None,
        sender_name=None,
    )

    await voice_handler.handle_voice(
        _voice_message(voice=_VOICE, forward_origin=origin), _FakeBot()
    )

    assert list_pending(tmp_path) == []
    assert "[forward from: Пётр Петров]" in _daily_text(tmp_path)


async def test_voice_reminder_creation_crash_still_saves_transcript_to_daily(
    monkeypatch, tmp_path: Path
) -> None:
    answers = _patch_voice(monkeypatch, tmp_path, "напомни завтра в 10 позвонить Ивану")

    async def boom(message, text):  # noqa: ANN001, ANN202
        raise RuntimeError("disk full")

    monkeypatch.setattr(voice_handler, "create_reminder_from_text", boom)

    await voice_handler.handle_voice(_voice_message(voice=_VOICE), _FakeBot())

    assert "позвонить Ивану" in _daily_text(tmp_path)
    assert "Не удалось сохранить" not in answers[0]
    assert "Напоминание не создано" in answers[0]


async def test_voice_reminder_created_but_session_crash_is_not_reported_as_lost(
    monkeypatch, tmp_path: Path
) -> None:
    answers = _patch_voice(monkeypatch, tmp_path, "напомни завтра в 10 позвонить Ивану")
    monkeypatch.setattr(voice_handler, "SessionStore", _boom)

    await voice_handler.handle_voice(_voice_message(voice=_VOICE), _FakeBot())

    assert len(list_pending(tmp_path)) == 1
    assert all("Не удалось сохранить" not in a for a in answers)


async def test_voice_reminder_created_but_reply_crash_is_not_reported_as_lost(
    monkeypatch, tmp_path: Path
) -> None:
    answers = _patch_voice(monkeypatch, tmp_path, "напомни завтра в 10 позвонить Ивану")
    calls: list[str] = []

    async def failing_answer(message, text, **kwargs):  # noqa: ANN001, ANN202
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("telegram down")
        answers.append(text)

    monkeypatch.setattr(voice_handler, "answer_text", failing_answer)

    await voice_handler.handle_voice(_voice_message(voice=_VOICE), _FakeBot())

    assert len(list_pending(tmp_path)) == 1
    assert answers == []
    assert len(calls) == 1


def test_voice_router_filter_accepts_audio_and_video_note() -> None:
    filt = voice_handler.router.message.handlers[0].filters[0].callback

    assert filt(SimpleNamespace(voice=_VOICE, audio=None, video_note=None))
    assert filt(SimpleNamespace(voice=None, audio=_VOICE, video_note=None))
    assert filt(SimpleNamespace(voice=None, audio=None, video_note=_VOICE))
    assert not filt(SimpleNamespace(voice=None, audio=None, video_note=None))


@pytest.mark.parametrize("kind", ["audio", "video_note"])
async def test_audio_and_video_note_are_transcribed_and_saved_as_voice_entry(
    monkeypatch, tmp_path: Path, kind: str
) -> None:
    answers = _patch_voice(monkeypatch, tmp_path, "текст из файла")
    bot = _FakeBot()
    media = SimpleNamespace(file_id=f"{kind}-file", duration=30, file_size=1000)

    await voice_handler.handle_voice(_voice_message(**{kind: media}), bot)

    assert bot.requested == [f"{kind}-file"]
    daily = _daily_text(tmp_path)
    assert "[voice]" in daily
    assert "текст из файла" in daily
    assert "✓ Сохранено" in answers[0]


async def test_oversized_audio_is_rejected_before_download(
    monkeypatch, tmp_path: Path
) -> None:
    answers = _patch_voice(monkeypatch, tmp_path, "не должно дойти")
    bot = _FakeBot()
    media = SimpleNamespace(file_id="big", duration=30, file_size=25 * 1024 * 1024)

    await voice_handler.handle_voice(_voice_message(audio=media), bot)

    assert bot.requested == []
    assert "20 МБ" in answers[0]
    assert _daily_text(tmp_path) == ""


# --- C5a: unknown command ---------------------------------------------------


def _patch_text_env(monkeypatch, tmp_path: Path) -> list[str]:  # noqa: ANN001
    answers: list[str] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)
        return SimpleNamespace()

    class NoClassifier:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            raise AssertionError("unknown command must not reach the classifier")

    monkeypatch.setattr(text_handler, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(text_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(text_handler, "CliProcessor", NoClassifier)
    return answers


async def test_unknown_command_is_saved_as_text_entry(
    monkeypatch, tmp_path: Path
) -> None:
    answers = _patch_text_env(monkeypatch, tmp_path)

    await text_handler.handle_unknown_command(_real_message(text="/srv тормозит"))

    daily = _daily_text(tmp_path)
    assert "[text]" in daily
    assert "/srv тормозит" in daily
    assert len(answers) == 1
    assert answers[0].startswith("✓ Сохранено")


async def test_unknown_command_routing_in_real_dispatcher(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    async def fake_help(message) -> None:  # noqa: ANN001
        calls.append("help")

    async def fake_unknown(message) -> None:  # noqa: ANN001
        calls.append("unknown")

    async def fake_text(message) -> None:  # noqa: ANN001
        calls.append("text")

    def _patch(router, name: str, fake) -> None:  # noqa: ANN001
        for handler in router.message.handlers:
            if handler.callback.__name__ == name:
                monkeypatch.setattr(handler, "callback", fake)
                return
        raise AssertionError(name)

    _patch(commands_handler.router, "cmd_help", fake_help)
    _patch(text_handler.router, "handle_unknown_command", fake_unknown)
    _patch(text_handler.router, "handle_text", fake_text)
    bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")

    with _isolated_dispatcher(
        commands_handler.router, text_handler.router, fallback_handler.router
    ) as dp:
        for i, body in enumerate(["/help", "/srv тормозит", "обычный текст"], 1):
            await dp.feed_update(
                bot, Update(update_id=i, message=_real_message(i, text=body))
            )

    assert calls == ["help", "unknown", "text"]


# --- C5c: video, location, contact, stub ------------------------------------


def _patch_fallback(monkeypatch, tmp_path: Path) -> list[str]:  # noqa: ANN001
    answers: list[str] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)
        return SimpleNamespace()

    monkeypatch.setattr(fallback_handler, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(fallback_handler, "answer_text", fake_answer_text)
    return answers


async def test_video_is_saved_to_daily_with_metadata(
    monkeypatch, tmp_path: Path
) -> None:
    from aiogram.types import Video

    answers = _patch_fallback(monkeypatch, tmp_path)
    video = Video(
        file_id="v",
        file_unique_id="vu",
        width=640,
        height=480,
        duration=12,
        file_name="clip.mp4",
        file_size=3 * 1024 * 1024,
    )

    await fallback_handler.handle_video(_real_message(video=video, caption="демо"))

    daily = _daily_text(tmp_path)
    assert "[video]" in daily
    assert "демо" in daily
    assert "clip.mp4" in daily
    assert "12 с" in daily
    assert answers == ["✓ Сохранено"]
    assert '"type": "video"' in _session_lines(tmp_path)


async def test_location_is_saved_to_daily_with_coordinates_and_map_link(
    monkeypatch, tmp_path: Path
) -> None:
    from aiogram.types import Location

    answers = _patch_fallback(monkeypatch, tmp_path)

    await fallback_handler.handle_location(
        _real_message(location=Location(latitude=55.751244, longitude=37.618423))
    )

    daily = _daily_text(tmp_path)
    assert "[location]" in daily
    assert "55.751244, 37.618423" in daily
    assert "openstreetmap.org" in daily
    assert answers == ["✓ Сохранено"]


async def test_venue_location_includes_title_and_address(
    monkeypatch, tmp_path: Path
) -> None:
    from aiogram.types import Location, Venue

    _patch_fallback(monkeypatch, tmp_path)
    location = Location(latitude=1.5, longitude=2.5)

    await fallback_handler.handle_location(
        _real_message(
            location=location,
            venue=Venue(
                location=location, title="Кафе Пример", address="ул. Тестовая, 1"
            ),
        )
    )

    daily = _daily_text(tmp_path)
    assert "Кафе Пример" in daily
    assert "ул. Тестовая, 1" in daily


async def test_contact_is_saved_to_daily(monkeypatch, tmp_path: Path) -> None:
    from aiogram.types import Contact

    answers = _patch_fallback(monkeypatch, tmp_path)

    await fallback_handler.handle_contact(
        _real_message(
            contact=Contact(
                phone_number="+70000000000",
                first_name="Анна",
                last_name="Примерова",
                user_id=555,
            )
        )
    )

    daily = _daily_text(tmp_path)
    assert "[contact]" in daily
    assert "Анна Примерова" in daily
    assert "+70000000000" in daily
    assert "555" in daily
    assert answers == ["✓ Сохранено"]


async def test_unsupported_message_type_gets_explicit_reply(monkeypatch) -> None:
    from aiogram.types import Sticker

    replies: list[str] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        replies.append(text)

    monkeypatch.setattr(fallback_handler, "answer_text", fake_answer_text)
    sticker = Sticker(
        file_id="s",
        file_unique_id="su",
        type="regular",
        width=1,
        height=1,
        is_animated=False,
        is_video=False,
    )
    bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")

    with _isolated_dispatcher(fallback_handler.router) as dp:
        await dp.feed_update(
            bot, Update(update_id=1, message=_real_message(sticker=sticker))
        )

    assert replies == ["Этот тип сообщения не поддерживается"]


def test_fallback_router_is_registered_last() -> None:
    from d_brain.bot.main import create_dispatcher

    names = [router.name for router in create_dispatcher().sub_routers]

    assert names[-1] == "fallback"
    assert names[-2] == "text"


# --- B4: error texts --------------------------------------------------------

SECRET = "SECRET-DETAIL"


def _boom(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
    raise RuntimeError(SECRET)


async def _leak_reminders_save(monkeypatch, tmp_path: Path) -> list[str]:  # noqa: ANN001
    answers: list[str] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)

    monkeypatch.setattr(reminders_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(reminders_handler, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(reminders_handler, "add_reminder", _boom)
    await reminders_handler.handle_reminder_text(
        _real_message(text="напомни завтра в 9 позвонить Ивану")
    )
    return answers


async def _leak_reminders_list(monkeypatch, tmp_path: Path) -> list[str]:  # noqa: ANN001
    answers: list[str] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)

    monkeypatch.setattr(reminders_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(reminders_handler, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(reminders_handler, "list_pending", _boom)
    await reminders_handler.handle_reminders_list(_real_message(text="/reminders"))
    return answers


async def _leak_voice(monkeypatch, tmp_path: Path) -> list[str]:  # noqa: ANN001
    answers = _patch_voice(monkeypatch, tmp_path, "просто мысль")
    monkeypatch.setattr(voice_handler, "SessionStore", _boom)
    await voice_handler.handle_voice(_voice_message(voice=_VOICE), _FakeBot())
    return answers


async def _leak_photo(monkeypatch, tmp_path: Path) -> list[str]:  # noqa: ANN001
    answers: list[str] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)

    monkeypatch.setattr(photo_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(photo_handler, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(photo_handler, "_save_photo", _boom)
    message = SimpleNamespace(
        photo=[object()], from_user=SimpleNamespace(id=42), media_group_id=None
    )
    await photo_handler.handle_photo(message, _FakeBot())
    return answers


async def _leak_forward(monkeypatch, tmp_path: Path) -> list[str]:  # noqa: ANN001
    answers: list[str] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)

    monkeypatch.setattr(forward_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(forward_handler, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(forward_handler, "build_telegram_source_info", _boom)
    await forward_handler.handle_forward(_real_message(text="чужой текст"))
    return answers


async def _leak_text(monkeypatch, tmp_path: Path) -> list[str]:  # noqa: ANN001
    answers: list[str] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)

    monkeypatch.setattr(text_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(text_handler, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(text_handler, "scrub_secrets", _boom)
    await text_handler.handle_unknown_command(_real_message(text="/srv тормозит"))
    return answers


async def _leak_do(monkeypatch, tmp_path: Path) -> list[str]:  # noqa: ANN001
    answers: list[str] = []

    async def fake_answer_text(message, text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)

    class BoomBot:
        async def get_file(self, file_id: str):  # noqa: ANN202
            raise RuntimeError(SECRET)

    monkeypatch.setattr(do_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(do_handler, "get_settings", lambda: _settings(tmp_path))
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={}), clear=AsyncMock()
    )
    message = SimpleNamespace(
        voice=_VOICE,
        text=None,
        from_user=SimpleNamespace(id=42),
        chat=_FakeChat(),
    )
    await do_handler.handle_do_input(message, BoomBot(), state)
    return answers


@pytest.mark.parametrize(
    "scenario",
    [
        _leak_reminders_save,
        _leak_reminders_list,
        _leak_voice,
        _leak_photo,
        _leak_forward,
        _leak_text,
        _leak_do,
    ],
)
async def test_error_replies_do_not_leak_exception_text(
    monkeypatch, tmp_path: Path, scenario
) -> None:
    answers = await scenario(monkeypatch, tmp_path)

    assert answers, "handler must answer, not stay silent"
    assert all(SECRET not in answer for answer in answers)
    assert any(
        answer.lstrip("❌ ").startswith("Не удалось") for answer in answers
    )
