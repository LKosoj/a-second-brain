"""Wait states of /do, /find, /why and /brief on a real aiogram dispatcher.

Unit tests with a fake state cannot see how the next router reads the state
after ``SkipHandler``, so these go through ``Dispatcher.feed_update``.
"""

import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, MessageEntity, Update, User

from d_brain.bot.handlers import brief, do, find, reminders, why
from d_brain.bot.states import (
    BriefCommandState,
    DoCommandState,
    FindCommandState,
    WhyCommandState,
)


class Harness:
    def __init__(self, monkeypatch) -> None:  # noqa: ANN001
        self.captured: list[Message] = []
        self.saved: list[str] = []
        self.processed: list[str] = []
        self.do_prompts: list[str] = []
        self.find_queries: list[str] = []

        async def fake_create(message, text):  # noqa: ANN001, ANN202
            self.saved.append(text)
            return SimpleNamespace(due_at=datetime(2026, 1, 2, 9, 0), text=text)

        async def fake_why(message, query, state):  # noqa: ANN001, ANN202
            self.processed.append(query)

        async def fake_brief(message, brief_type, query):  # noqa: ANN001, ANN202
            self.processed.append(query)

        async def fake_do(message, prompt, user_id=0):  # noqa: ANN001, ANN202
            self.do_prompts.append(prompt)

        async def fake_find(message, query, state):  # noqa: ANN001, ANN202
            self.find_queries.append(query)

        monkeypatch.setattr(do, "process_request", fake_do)
        monkeypatch.setattr(find, "process_find_request", fake_find)
        monkeypatch.setattr(reminders, "create_reminder_from_text", fake_create)
        monkeypatch.setattr(reminders, "answer_text", AsyncMock())
        monkeypatch.setattr(
            reminders, "_pending_for_chat", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(why, "process_why_request", fake_why)
        monkeypatch.setattr(brief, "process_brief_request", fake_brief)
        monkeypatch.setattr(brief, "_restore_menu", AsyncMock())

        self.dp = Dispatcher(storage=MemoryStorage())
        # Same order as bot/main.py: find, do, why, brief, then reminders.
        self.dp.include_router(find.router)
        self.dp.include_router(do.router)
        self.dp.include_router(why.router)
        self.dp.include_router(brief.router)
        self.dp.include_router(reminders.router)
        sink = Router(name="sink")

        @sink.message()
        async def catch_all(message: Message) -> None:
            self.captured.append(message)

        self.dp.include_router(sink)
        self.bot = Bot("123456:TEST")
        self.bot.session.make_request = AsyncMock()  # type: ignore[method-assign]
        self.key = StorageKey(bot_id=self.bot.id, chat_id=5, user_id=5)

    async def wait(self, state, **data) -> None:  # noqa: ANN001, ANN003
        await self.dp.storage.set_state(self.key, state)
        await self.dp.storage.update_data(self.key, data)

    async def send(self, text: str, reply_to: Message | None = None) -> None:
        entities = (
            [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
            if text.startswith("/")
            else None
        )
        message = Message(
            message_id=2,
            date=datetime(2026, 1, 1),
            chat=Chat(id=5, type="private"),
            from_user=User(id=5, is_bot=False, first_name="o"),
            text=text,
            entities=entities,
            reply_to_message=reply_to,
        )
        await self.dp.feed_update(self.bot, Update(update_id=1, message=message))


def _assert_reached_next_router(harness: Harness, command: str) -> None:
    if command == "/reminders":  # claimed by its own handler, not the sink
        reminders._pending_for_chat.assert_awaited()  # type: ignore[attr-defined]
        assert harness.captured == []
    else:
        assert [m.text for m in harness.captured] == [command]


@pytest.fixture
def harness(monkeypatch) -> Harness:  # noqa: ANN001
    return Harness(monkeypatch)


@pytest.mark.parametrize("command", ["/reminders", "/foo bar"])
async def test_why_wait_lets_later_commands_through(harness, command) -> None:  # noqa: ANN001
    await harness.wait(
        WhyCommandState.waiting_for_input, why_entered_at=time.time()
    )

    await harness.send(command)

    assert harness.processed == []
    assert await harness.dp.storage.get_state(harness.key) is None
    _assert_reached_next_router(harness, command)


@pytest.mark.parametrize("command", ["/reminders", "/foo bar"])
async def test_brief_wait_lets_later_commands_through(harness, command) -> None:  # noqa: ANN001
    await harness.wait(
        BriefCommandState.waiting_for_query,
        brief_type="topic",
        brief_entered_at=time.time(),
        menu_message_id=11,
    )

    await harness.send(command)

    assert harness.processed == []
    assert await harness.dp.storage.get_state(harness.key) is None
    _assert_reached_next_router(harness, command)
    brief._restore_menu.assert_awaited()  # type: ignore[attr-defined]


async def test_expired_why_wait_still_creates_reminder(harness) -> None:  # noqa: ANN001
    await harness.wait(
        WhyCommandState.waiting_for_input, why_entered_at=time.time() - 10**6
    )

    await harness.send("напомни завтра в 9 позвонить")

    assert harness.saved == ["напомни завтра в 9 позвонить"]
    assert harness.captured == []


async def test_do_wait_takes_reminder_looking_text(harness) -> None:  # noqa: ANN001
    await harness.wait(DoCommandState.waiting_for_input)

    await harness.send("напомни завтра в 9 позвонить")

    assert harness.do_prompts == ["напомни завтра в 9 позвонить"]
    assert harness.saved == []
    assert harness.captured == []


async def test_find_wait_takes_reminder_looking_text(harness) -> None:  # noqa: ANN001
    await harness.wait(
        FindCommandState.waiting_for_input, find_entered_at=time.time()
    )

    await harness.send("напомни завтра в 9 позвонить")

    assert harness.find_queries == ["напомни завтра в 9 позвонить"]
    assert harness.saved == []
    assert harness.captured == []


async def test_expired_find_wait_still_creates_reminder(harness) -> None:  # noqa: ANN001
    await harness.wait(
        FindCommandState.waiting_for_input, find_entered_at=time.time() - 10**6
    )

    await harness.send("напомни завтра в 9 позвонить")

    assert harness.find_queries == []
    assert harness.saved == ["напомни завтра в 9 позвонить"]
    assert harness.captured == []
    assert await harness.dp.storage.get_state(harness.key) is None


async def test_reply_to_bot_is_not_a_reminder(harness) -> None:  # noqa: ANN001
    bot_message = Message(
        message_id=1,
        date=datetime(2026, 1, 1),
        chat=Chat(id=5, type="private"),
        from_user=User(id=harness.bot.id, is_bot=True, first_name="bot"),
        text="ответ",
    )

    await harness.send("напомни завтра в 9 позвонить", reply_to=bot_message)

    assert harness.saved == []
    assert len(harness.captured) == 1
