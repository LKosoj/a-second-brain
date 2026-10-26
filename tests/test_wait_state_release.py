"""C4: a forgotten /why or brief prompt must not swallow other messages.

Real ``Dispatcher`` so the hand-over (``SkipHandler`` -> next router) is
proved with the same machinery ``bot/main.py`` relies on.
"""

import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Chat,
    Document,
    Message,
    PhotoSize,
    Update,
    User,
    Voice,
)

from d_brain.bot import states
from d_brain.bot.handlers import brief, why
from d_brain.bot.main import create_dispatcher
from d_brain.bot.states import BriefCommandState, WhyCommandState

CHAT = Chat(id=5, type="private")
USER = User(id=5, is_bot=False, first_name="o")



def test_wait_expired() -> None:
    assert states.wait_expired(None) is False
    assert states.wait_expired("x") is False
    assert states.wait_expired(100.0, now=100 + 599) is False
    assert states.wait_expired(100.0, now=100 + 601) is True


def _message(**kwargs: object) -> Message:
    return Message(
        message_id=1, date=datetime(2026, 1, 1), chat=CHAT, from_user=USER, **kwargs
    )


def _voice() -> Message:
    return _message(
        voice=Voice(file_id="f", file_unique_id="u", duration=1)
    )


def _photo() -> Message:
    return _message(
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]
    )


def _document() -> Message:
    return _message(document=Document(file_id="f", file_unique_id="u"))


def _forward() -> Message:
    return _message(
        text="пересланное",
        forward_origin={
            "type": "user",
            "date": datetime(2026, 1, 1),
            "sender_user": USER.model_dump(),
        },
    )


def _build(received: list[Message]):  # noqa: ANN202
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(why.router)
    dp.include_router(brief.router)
    sink = Router(name="sink")

    @sink.message()
    async def catch_all(message: Message) -> None:
        received.append(message)

    dp.include_router(sink)
    bot = Bot("123456:TEST")
    request = AsyncMock()
    bot.session.make_request = request  # type: ignore[method-assign]
    return dp, bot, request


async def _feed(dp, bot, message: Message) -> None:  # noqa: ANN001
    await dp.feed_update(bot, Update(update_id=1, message=message))


def _key(bot: Bot) -> StorageKey:
    return StorageKey(bot_id=bot.id, chat_id=CHAT.id, user_id=USER.id)


@pytest.mark.parametrize("make", [_voice, _photo, _document, _forward])
@pytest.mark.parametrize(
    "state", [WhyCommandState.waiting_for_input, BriefCommandState.waiting_for_query]
)
async def test_non_text_during_wait_reaches_next_router(make, state) -> None:  # noqa: ANN001
    received: list[Message] = []
    dp, bot, request = _build(received)
    await dp.storage.set_state(_key(bot), state)
    if state is BriefCommandState.waiting_for_query:
        await dp.storage.set_data(_key(bot), {"brief_type": "decision"})

    await _feed(dp, bot, make())

    assert len(received) == 1
    assert await dp.storage.get_state(_key(bot)) is None
    request.assert_not_called()


async def test_expired_text_reaches_capture_and_fresh_text_does_not(
    monkeypatch,
) -> None:
    received: list[Message] = []
    dp, bot, _ = _build(received)
    processed: list[str] = []

    async def fake_process(message, query, state) -> None:  # noqa: ANN001
        processed.append(query)

    monkeypatch.setattr(why, "process_why_request", fake_process)

    await dp.storage.set_state(_key(bot), WhyCommandState.waiting_for_input)
    await dp.storage.set_data(_key(bot), {"why_entered_at": time.time() - 601})
    await _feed(dp, bot, _message(text="обычная заметка"))
    assert [m.text for m in received] == ["обычная заметка"]
    assert processed == []

    await dp.storage.set_state(_key(bot), WhyCommandState.waiting_for_input)
    await dp.storage.set_data(_key(bot), {"why_entered_at": time.time()})
    await _feed(dp, bot, _message(text="решение про цены"))
    assert len(received) == 1
    assert processed == ["решение про цены"]


def test_include_order_why_brief_before_capture_routers() -> None:
    names = [router.name for router in create_dispatcher().sub_routers]
    for early in ("why", "brief"):
        for late in ("voice", "photo", "document", "forward", "text"):
            assert names.index(early) < names.index(late)
