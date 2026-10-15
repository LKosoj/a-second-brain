"""Regression/coverage tests for the B3 audit fix (пункт 13): reminders.

Covers three layers: the deterministic Russian time-phrase parser
(``services/reminders.parse_reminder``), the append-only JSONL storage
(``add_reminder``/``list_pending``/``mark_sent``), and the bot-facing pieces
(the "напомни ..." text trigger, ``/reminders``, and one pass of the
periodic ticker) in ``bot/handlers/reminders.py``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from d_brain.bot.handlers import reminders as reminders_handler
from d_brain.services.reminders import (
    Reminder,
    add_reminder,
    is_reminder_request,
    list_pending,
    mark_sent,
    parse_reminder,
)

# The ticker needs an aiogram Bot to send with, so it lives in the bot layer
# (bot/handlers/reminders.py), not in the plain-data services module.
run_reminder_ticker = reminders_handler.run_reminder_ticker


# ---------------------------------------------------------------------------
# parse_reminder
# ---------------------------------------------------------------------------


def test_parse_tomorrow_with_clock_time() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    result = parse_reminder("напомни завтра в 9 позвонить Ивану", now)
    assert result == Reminder(
        due_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC), text="позвонить Ивану"
    )


def test_parse_relative_minutes_case_insensitive_trigger() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    result = parse_reminder("Напомни через 30 минут выключить плиту", now)
    assert result == Reminder(
        due_at=now + timedelta(minutes=30), text="выключить плиту"
    )


def test_parse_relative_hours() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    result = parse_reminder("напомни через 2 часа перезвонить", now)
    assert result == Reminder(due_at=now + timedelta(hours=2), text="перезвонить")


def test_parse_clock_time_already_passed_today_rolls_to_tomorrow() -> None:
    now = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)
    result = parse_reminder("напомни в 18:30 купить молоко", now)
    assert result == Reminder(
        due_at=datetime(2026, 9, 4, 18, 30, tzinfo=UTC), text="купить молоко"
    )


def test_parse_today_without_time_after_default_hour_returns_none() -> None:
    now = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)
    assert parse_reminder("напомни сегодня купить молоко", now) is None


def test_parse_today_with_time_still_ahead_stays_today() -> None:
    now = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)
    result = parse_reminder("напомни сегодня в 18 купить молоко", now)
    assert result == Reminder(
        due_at=datetime(2026, 9, 3, 18, 0, tzinfo=UTC), text="купить молоко"
    )


def test_parse_weekday_defaults_to_nine_am() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    result = parse_reminder("напомни в пятницу заплатить за интернет", now)
    assert result is not None
    assert result.text == "заплатить за интернет"
    days_ahead = (4 - now.weekday()) % 7 or 7  # 4 == Friday
    expected_date = (now + timedelta(days=days_ahead)).date()
    assert result.due_at.date() == expected_date
    assert (result.due_at.hour, result.due_at.minute) == (9, 0)


def test_parse_day_after_tomorrow_with_clock_time() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    result = parse_reminder("напомни послезавтра в 14:00 забрать документы", now)
    assert result == Reminder(
        due_at=datetime(2026, 9, 5, 14, 0, tzinfo=UTC), text="забрать документы"
    )


def test_parse_explicit_date_with_month_name() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    result = parse_reminder(
        "напомни 5 сентября в 10 встретиться с клиентом", now
    )
    assert result == Reminder(
        due_at=datetime(2026, 9, 5, 10, 0, tzinfo=UTC), text="встретиться с клиентом"
    )


def test_parse_unrecognized_time_phrase_returns_none() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert parse_reminder("напомни как погода", now) is None


def test_parse_phrase_without_trigger_word_returns_none() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert parse_reminder("купи молоко", now) is None


def test_parse_bare_day_word_without_task_text_returns_none() -> None:
    """"напомни завтра" alone has nothing to remind about -- ask again."""
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert parse_reminder("напомни завтра", now) is None


# --- out-of-range times must not raise (ошибка 1) --------------------------


def test_parse_out_of_range_clock_time_returns_none() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert parse_reminder("напомни в 25:70 полить цветы", now) is None


def test_parse_out_of_range_day_word_hour_returns_none() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert parse_reminder("напомни завтра в 25 сделать", now) is None


def test_parse_out_of_range_weekday_time_returns_none() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert parse_reminder("напомни в пятницу в 30:99 отчёт", now) is None


# --- trigger word must not match past tense (ошибка 2) ---------------------


def test_is_reminder_request_true_for_imperative_infinitive_and_ka_suffix() -> None:
    assert is_reminder_request("напомни завтра в 9 позвонить")
    assert is_reminder_request("напомнить мне завтра в 9 позвонить бабушке")
    assert is_reminder_request("Напомни-ка купить хлеб")


def test_is_reminder_request_false_for_past_tense_forms() -> None:
    assert not is_reminder_request("Напомнила мама, что нужно позвонить бабушке")
    assert not is_reminder_request("напомнили на работе про отчёт")
    assert not is_reminder_request("напомнил себе взять зонт")


def test_parse_reminder_returns_none_for_past_tense_forms() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert parse_reminder("Напомнила мама, что нужно позвонить бабушке", now) is None
    assert parse_reminder("напомнили на работе про отчёт", now) is None
    assert parse_reminder("напомнил себе взять зонт", now) is None


def test_parse_infinitive_trigger_with_mne() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    result = parse_reminder("напомнить мне завтра в 9 позвонить бабушке", now)
    assert result == Reminder(
        due_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC), text="позвонить бабушке"
    )


def test_parse_tolerates_punctuation_and_please_after_trigger() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    expected = Reminder(
        due_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC), text="купить хлеб"
    )
    assert parse_reminder("напомни: завтра в 9 купить хлеб", now) == expected
    assert (
        parse_reminder("Напомни, пожалуйста, завтра в 9 купить хлеб", now) == expected
    )
    for phrase in (
        "напомни мне, пожалуйста, завтра в 9 купить хлеб",
        "напомни мне пожалуйста завтра в 9 купить хлеб",
    ):
        assert parse_reminder(phrase, now) == expected


def test_list_pending_skips_json_line_that_is_not_an_object(tmp_path: Path) -> None:
    session = tmp_path / ".session"
    session.mkdir()
    (session / "reminders.jsonl").write_text('[1, 2, 3]\n"строка"\n', encoding="utf-8")
    assert list_pending(tmp_path) == []


def test_parse_ka_suffix_trigger() -> None:
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    result = parse_reminder("Напомни-ка через 10 минут купить хлеб", now)
    assert result == Reminder(
        due_at=now + timedelta(minutes=10), text="купить хлеб"
    )


# ---------------------------------------------------------------------------
# storage: add_reminder / list_pending / mark_sent
# ---------------------------------------------------------------------------


def test_list_pending_on_a_vault_with_no_session_dir_returns_empty(
    tmp_path: Path,
) -> None:
    assert list_pending(tmp_path) == []
    assert not (tmp_path / ".session").exists()


def test_add_reminder_creates_missing_session_dir(tmp_path: Path) -> None:
    session_dir = tmp_path / ".session"
    assert not session_dir.exists()

    reminder = Reminder(
        due_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC), text="позвонить"
    )
    record = add_reminder(tmp_path, 555, reminder)

    assert (session_dir / "reminders.jsonl").exists()
    assert record.chat_id == 555
    assert record.sent_at is None


def test_add_list_mark_sent_roundtrip(tmp_path: Path) -> None:
    reminder = Reminder(
        due_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC), text="позвонить"
    )
    record = add_reminder(
        tmp_path, 555, reminder, now=datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    )

    pending = list_pending(tmp_path)
    assert [r.id for r in pending] == [record.id]

    mark_sent(tmp_path, record.id, now=datetime(2026, 9, 4, 9, 1, tzinfo=UTC))

    assert list_pending(tmp_path) == []

    lines = (tmp_path / ".session" / "reminders.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored["sent_at"] == "2026-09-04T09:01:00+00:00"


def test_mark_sent_on_unknown_id_is_a_no_op(tmp_path: Path) -> None:
    reminder = Reminder(due_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC), text="x")
    add_reminder(tmp_path, 1, reminder)

    mark_sent(tmp_path, "does-not-exist")

    assert len(list_pending(tmp_path)) == 1


def test_mark_sent_writes_atomically_and_leaves_no_temp_file(tmp_path: Path) -> None:
    """mark_sent rewrites via tempfile + os.replace (предупреждение 3) --
    the session directory must contain only the final journal afterwards."""
    reminder = Reminder(due_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC), text="x")
    record = add_reminder(tmp_path, 1, reminder)

    mark_sent(tmp_path, record.id)

    session_dir = tmp_path / ".session"
    assert [p.name for p in session_dir.iterdir()] == ["reminders.jsonl"]


# ---------------------------------------------------------------------------
# ticker: one pass
# ---------------------------------------------------------------------------


class _FakeBot:
    """Minimal stand-in for aiogram's Bot -- only send_message is used by
    ``send_text`` for a plain-text payload under the length limit."""

    def __init__(self, fail_chat_ids: set[int] | None = None) -> None:
        self.fail_chat_ids = fail_chat_ids or set()
        self.sent: list[tuple[int, str]] = []

    async def send_message(
        self, chat_id: int, text: str, **kwargs: Any
    ) -> SimpleNamespace:
        if chat_id in self.fail_chat_ids:
            raise RuntimeError("boom")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)


async def test_ticker_sends_due_reminder_and_skips_future_one(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    due = add_reminder(
        tmp_path, 1, Reminder(due_at=now - timedelta(minutes=1), text="due"), now=now
    )
    future = add_reminder(
        tmp_path,
        1,
        Reminder(due_at=now + timedelta(hours=1), text="future"),
        now=now,
    )

    bot = _FakeBot()
    await run_reminder_ticker(bot, tmp_path, now_fn=lambda: now, once=True)

    assert bot.sent == [(1, "⏰ Напоминание: due")]
    pending_ids = {record.id for record in list_pending(tmp_path)}
    assert pending_ids == {future.id}

    lines = (tmp_path / ".session" / "reminders.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    stored_by_id = {json.loads(line)["id"]: json.loads(line) for line in lines}
    assert stored_by_id[due.id]["sent_at"] is not None


async def test_ticker_failed_send_leaves_reminder_pending_and_does_not_raise(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    record = add_reminder(
        tmp_path, 1, Reminder(due_at=now - timedelta(minutes=1), text="due"), now=now
    )

    bot = _FakeBot(fail_chat_ids={1})
    await run_reminder_ticker(bot, tmp_path, now_fn=lambda: now, once=True)

    assert bot.sent == []
    pending_ids = {r.id for r in list_pending(tmp_path)}
    assert pending_ids == {record.id}


async def test_ticker_survives_a_corrupted_journal_file(tmp_path: Path) -> None:
    """предупреждение 4: ``_read_all``'s ``path.read_text`` is unguarded, so a
    corrupted (non-UTF-8) journal used to raise ``UnicodeDecodeError`` out of
    the tick body and kill the background ticker task forever. One tick over
    a broken file must log and return instead of raising.
    """
    session_dir = tmp_path / ".session"
    session_dir.mkdir(parents=True)
    (session_dir / "reminders.jsonl").write_bytes(b"\xff\xfe not valid utf-8 at all")

    bot = _FakeBot()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

    await run_reminder_ticker(bot, tmp_path, now_fn=lambda: now, once=True)

    assert bot.sent == []


# ---------------------------------------------------------------------------
# handler: real Dispatcher over reminders.router
# ---------------------------------------------------------------------------


@contextmanager
def _isolated_dispatcher(*routers: Router):  # noqa: ANN201
    """Attach reminders.router (a process-wide singleton, like do.router in
    test_fix_handlers_fsm.py) to a throwaway Dispatcher for one test, then
    restore its original parent -- bot/main.py's create_dispatcher() attaches
    it for the life of the process, and other tests in this suite call that.
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


def _make_message(chat_id: int, user_id: int, message_id: int, text: str) -> Message:
    return Message(
        message_id=message_id,
        date=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


async def test_reminder_text_handler_saves_and_replies_with_confirmation(
    monkeypatch, tmp_path: Path
) -> None:
    replies: list[str] = []

    async def fake_answer_text(message: Message, text: str, **kwargs: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(reminders_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(
        reminders_handler, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )

    message = _make_message(100, 42, 1, "напомни завтра в 9 позвонить Ивану")
    expected_now = message.date.astimezone()
    expected = parse_reminder(message.text or "", expected_now)
    assert expected is not None

    with _isolated_dispatcher(reminders_handler.router) as dp:
        bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")
        await dp.feed_update(bot, Update(update_id=1, message=message))

    expected_reply = (
        f"⏰ Напомню {expected.due_at:%d.%m} в {expected.due_at:%H:%M}: "
        f"{expected.text}"
    )
    assert replies == [expected_reply]

    pending = list_pending(tmp_path)
    assert len(pending) == 1
    assert pending[0].text == expected.text
    assert pending[0].due_at == expected.due_at
    assert pending[0].chat_id == 100


async def test_reminder_text_handler_replies_with_hint_when_unparseable(
    monkeypatch, tmp_path: Path
) -> None:
    replies: list[str] = []

    async def fake_answer_text(message: Message, text: str, **kwargs: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(reminders_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(
        reminders_handler, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )

    message = _make_message(101, 43, 1, "напомни как погода")

    with _isolated_dispatcher(reminders_handler.router) as dp:
        bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")
        await dp.feed_update(bot, Update(update_id=1, message=message))

    assert replies == ["Не понял время, пример: напомни завтра в 9 позвонить Ивану"]
    assert list_pending(tmp_path) == []


async def test_reminders_command_lists_pending_only_for_this_chat(
    monkeypatch, tmp_path: Path
) -> None:
    add_reminder(
        tmp_path,
        100,
        Reminder(
            due_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC), text="позвонить Ивану"
        ),
        now=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
    )
    add_reminder(
        tmp_path,
        999,
        Reminder(due_at=datetime(2026, 9, 4, 10, 0, tzinfo=UTC), text="чужое"),
        now=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
    )

    replies: list[str] = []

    async def fake_answer_text(message: Message, text: str, **kwargs: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(reminders_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(
        reminders_handler, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )

    message = _make_message(100, 42, 2, "/reminders")

    with _isolated_dispatcher(reminders_handler.router) as dp:
        bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")
        await dp.feed_update(bot, Update(update_id=2, message=message))

    assert len(replies) == 1
    assert "позвонить Ивану" in replies[0]
    assert "чужое" not in replies[0]


async def test_reminders_command_reports_empty_list(
    monkeypatch, tmp_path: Path
) -> None:
    replies: list[str] = []

    async def fake_answer_text(message: Message, text: str, **kwargs: Any) -> None:
        replies.append(text)

    monkeypatch.setattr(reminders_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(
        reminders_handler, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )

    message = _make_message(200, 44, 3, "/reminders")

    with _isolated_dispatcher(reminders_handler.router) as dp:
        bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")
        await dp.feed_update(bot, Update(update_id=3, message=message))

    assert replies == ["Нет активных напоминаний"]
