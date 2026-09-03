"""Regression tests for the G4 handlers-fsm audit fixes.

A) photo.py: a media-group album used to register a photo's membership only
   *after* its slow AI analysis finished, so photos whose analysis times
   differed by more than ALBUM_SETTLE_SECONDS were flushed as separate
   single-photo entries instead of one grouped entry.
B) do.py: any text sent while DoCommandState.waiting_for_input was active
   -- including a bot command like /why -- was forwarded to the AI CLI as a
   literal prompt instead of letting the command reach its own router.
"""

import asyncio
import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from d_brain.bot.handlers import do as do_handler
from d_brain.bot.handlers import photo as photo_handler
from d_brain.bot.handlers import why as why_handler
from d_brain.bot.states import DoCommandState, WhyCommandState
from d_brain.services.source_links import SourceInfo


@contextmanager
def _isolated_dispatcher(*routers: Router):  # noqa: ANN201
    """Attach the given process-wide singleton routers to a throwaway
    Dispatcher, restoring each one's original parent afterwards.

    do.router/why.router are singletons that aiogram only lets attach to one
    parent Router for their whole lifetime; bot/main.py's create_dispatcher()
    attaches them for the life of the process, and a dispatcher-inspection
    test elsewhere already calls it once. Reusing that dispatcher (or
    calling create_dispatcher() again) would make these tests' outcome
    depend on which test happens to run first.
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


class _FakeBot:
    """Minimal bot double for handle_photo -- only get_file/download_file
    are exercised by the fake _save_photo below, so nothing else is faked.
    """

    async def get_file(self, file_id: str):  # noqa: ANN201
        return SimpleNamespace(file_path=f"{file_id}.jpg")

    async def download_file(self, file_path: str):  # noqa: ANN201
        return SimpleNamespace(read=lambda: b"bytes")


class _FakeAlbumMessage:
    """Just enough of a Message for handle_photo's media-group path."""

    def __init__(self, message_id: int, media_group_id: str, sleep_s: float) -> None:
        self.photo = [SimpleNamespace(file_id=f"file-{message_id}")]
        self.caption = None
        self.from_user = SimpleNamespace(id=1)
        self.date = datetime(2026, 4, 4, 12, 0, 0)
        self.message_id = message_id
        self.media_group_id = media_group_id
        self.forward_origin = None
        self.sleep_s = sleep_s
        self.answers: list[str] = []

    async def answer(self, text: str, parse_mode=None) -> None:  # noqa: ANN001
        self.answers.append(text)


async def test_album_flush_waits_for_slowest_photo_regardless_of_analysis_time(
    monkeypatch,
) -> None:
    """Reproduces the bug: three photos whose _save_photo (download + AI
    analysis) finishes 0.01s/0.15s/0.3s apart must still land in ONE grouped
    daily entry with ONE "Сохранено 3 фото" reply -- not three separate
    entries, which is what happened when album membership was only
    registered after each photo's own analysis finished.
    """
    media_group_id = "audit-g4-album"
    appended: list[tuple[str, str]] = []
    replies: list[str] = []

    class FakeStorage:
        def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
            pass

        def append_to_daily(self, content: str, timestamp, entry_type: str) -> None:  # noqa: ANN001
            appended.append((content, entry_type))

    async def fake_save_photo(message, bot) -> photo_handler.PhotoEntry:  # noqa: ANN001
        await asyncio.sleep(message.sleep_s)
        return photo_handler.PhotoEntry(
            message_id=message.message_id,
            timestamp=datetime(2026, 4, 4, 12, 0, 0),
            relative_path=f"attachments/2026-04-04/{message.message_id}.jpg",
            caption=None,
            analysis=None,
            source=SourceInfo(
                kind="telegram", ref=f"telegram:1:{message.message_id}"
            ),
            content_language="ru",
        )

    async def fake_answer_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        replies.append(text)

    monkeypatch.setattr(photo_handler, "ALBUM_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(
        photo_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path="vault", content_language="ru", ai_cli="codex"
        ),
    )
    monkeypatch.setattr(photo_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(photo_handler, "_save_photo", fake_save_photo)
    monkeypatch.setattr(photo_handler, "_log_photo_session", lambda *a, **kw: None)
    monkeypatch.setattr(photo_handler, "answer_text", fake_answer_text)

    bot = _FakeBot()
    messages = [
        _FakeAlbumMessage(1, media_group_id, 0.01),
        _FakeAlbumMessage(2, media_group_id, 0.15),
        _FakeAlbumMessage(3, media_group_id, 0.3),
    ]

    await asyncio.gather(*(photo_handler.handle_photo(m, bot) for m in messages))

    # None of the three handle_photo calls send a per-photo reply -- the
    # grouped reply is sent later by the flush task once it drains.
    assert not replies

    task = photo_handler._album_tasks.get(media_group_id)
    if task is not None:
        await asyncio.wait_for(task, timeout=2)

    assert media_group_id not in photo_handler._album_items
    assert media_group_id not in photo_handler._album_pending

    assert len(appended) == 1
    content, entry_type = appended[0]
    assert entry_type == "[photo]"
    assert "### Фото 1" in content
    assert "### Фото 2" in content
    assert "### Фото 3" in content

    assert replies == ["📷 ✓ Сохранено 3 фото"]


async def test_flush_album_gives_up_waiting_after_max_wait_and_logs_a_warning(
    monkeypatch,
    caplog,
) -> None:
    """Reproduces the bug: _save_photo's AI analysis step has no timeout of
    its own, so one photo stuck analyzing forever left _album_pending above
    zero forever too -- _flush_album looped waiting indefinitely and the
    already-saved sibling photos in the group were never written to daily.
    Past ALBUM_MAX_WAIT_SECONDS it must flush what it already has instead.
    """
    media_group_id = "audit-g4-album-stuck"
    appended: list[tuple[str, str]] = []

    class FakeStorage:
        def append_to_daily(self, content: str, timestamp, entry_type: str) -> None:  # noqa: ANN001
            appended.append((content, entry_type))

    saved_entry = photo_handler.PhotoEntry(
        message_id=1,
        timestamp=datetime(2026, 4, 4, 12, 0, 0),
        relative_path="attachments/2026-04-04/1.jpg",
        caption=None,
        analysis=None,
        source=SourceInfo(kind="telegram", ref="telegram:1:1"),
        content_language="ru",
    )
    photo_handler._album_items[media_group_id] = [saved_entry]
    # Simulates a second photo whose _save_photo (AI analysis) never
    # returns -- nothing in this test ever decrements this back to 0.
    photo_handler._album_pending[media_group_id] = 1

    async def fake_answer_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        pass

    monkeypatch.setattr(photo_handler, "ALBUM_SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(photo_handler, "ALBUM_MAX_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(photo_handler, "answer_text", fake_answer_text)

    with caplog.at_level(logging.WARNING, logger=photo_handler.logger.name):
        await asyncio.wait_for(
            photo_handler._flush_album(media_group_id, object(), FakeStorage()),
            timeout=2,
        )

    assert len(appended) == 1
    content, entry_type = appended[0]
    assert entry_type == "[photo]"
    assert "attachments/2026-04-04/1.jpg" in content

    assert media_group_id not in photo_handler._album_items
    assert media_group_id not in photo_handler._album_pending
    assert media_group_id not in photo_handler._album_tasks

    assert any(
        media_group_id in record.getMessage() and "still saving" in record.getMessage()
        for record in caplog.records
    )


async def test_photo_that_finishes_after_album_already_flushed_gets_flushed_separately(
    monkeypatch,
    caplog,
) -> None:
    """Reproduces the bug: once ALBUM_MAX_WAIT_SECONDS elapses, _flush_album
    pops _album_items/_album_pending/_album_tasks for the whole group and
    writes what it already has. A sibling photo whose _save_photo was still
    running past that point used to just append itself into a freshly empty
    _album_items with no flush task left to ever drain it -- it was saved to
    the vault but never written to daily, and it sat in _album_items forever.
    It must instead get a fresh flush task and land in daily as its own
    belated entry.
    """
    media_group_id = "audit-g4-album-late"
    appended: list[tuple[str, str]] = []

    class FakeStorage:
        def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
            pass

        def append_to_daily(self, content: str, timestamp, entry_type: str) -> None:  # noqa: ANN001
            appended.append((content, entry_type))

    async def fake_save_photo(message, bot) -> photo_handler.PhotoEntry:  # noqa: ANN001
        await asyncio.sleep(message.sleep_s)
        return photo_handler.PhotoEntry(
            message_id=message.message_id,
            timestamp=datetime(2026, 4, 4, 12, 0, 0),
            relative_path=f"attachments/2026-04-04/{message.message_id}.jpg",
            caption=None,
            analysis=None,
            source=SourceInfo(
                kind="telegram", ref=f"telegram:1:{message.message_id}"
            ),
            content_language="ru",
        )

    async def fake_answer_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        pass

    monkeypatch.setattr(photo_handler, "ALBUM_SETTLE_SECONDS", 0.02)
    monkeypatch.setattr(photo_handler, "ALBUM_MAX_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(
        photo_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path="vault", content_language="ru", ai_cli="codex"
        ),
    )
    monkeypatch.setattr(photo_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(photo_handler, "_save_photo", fake_save_photo)
    monkeypatch.setattr(photo_handler, "_log_photo_session", lambda *a, **kw: None)
    monkeypatch.setattr(photo_handler, "answer_text", fake_answer_text)

    bot = _FakeBot()
    fast_message = _FakeAlbumMessage(1, media_group_id, 0.01)
    slow_message = _FakeAlbumMessage(2, media_group_id, 0.2)

    with caplog.at_level(logging.WARNING, logger=photo_handler.logger.name):
        await asyncio.gather(
            photo_handler.handle_photo(fast_message, bot),
            photo_handler.handle_photo(slow_message, bot),
        )

        # By the time both handle_photo calls return, the slow photo's own
        # success branch has already created a fresh (belated) flush task,
        # since the fast photo's flush fired past ALBUM_MAX_WAIT_SECONDS
        # while the slow one was still saving.
        late_task = photo_handler._album_tasks.get(media_group_id)
        if late_task is not None:
            await asyncio.wait_for(late_task, timeout=2)

    assert media_group_id not in photo_handler._album_items
    assert media_group_id not in photo_handler._album_pending
    assert media_group_id not in photo_handler._album_tasks

    assert len(appended) == 2
    contents = [content for content, _entry_type in appended]
    assert any("attachments/2026-04-04/1.jpg" in c for c in contents)
    assert any("attachments/2026-04-04/2.jpg" in c for c in contents)

    assert any(
        media_group_id in record.getMessage()
        and "already flushed" in record.getMessage()
        for record in caplog.records
    )


async def test_do_input_command_falls_through_to_its_own_router(
    monkeypatch,
) -> None:
    """Reproduces the bug: typing "/why" while /do is waiting for input used
    to be swallowed by handle_do_input and sent to the AI CLI as a literal
    prompt. Drives a real Dispatcher over the actual do.router/why.router
    singletons (do before why, matching bot/main.py's registration order)
    so the fix is checked at the level it actually operates -- router
    filtering -- not just by calling the handler function directly.
    """
    calls: list[str] = []

    async def fake_process_request(message, prompt: str, user_id: int = 0) -> None:
        calls.append(prompt)

    monkeypatch.setattr(do_handler, "process_request", fake_process_request)
    monkeypatch.setattr(_bot_client_module(), "__call__", _fake_bot_call)

    with _isolated_dispatcher(do_handler.router, why_handler.router) as dp:
        await _run_do_then_why(dp)

    assert calls == []


async def test_do_input_slash_text_that_is_not_a_command_is_treated_as_prompt(
    monkeypatch,
) -> None:
    """Reproduces the narrower bug in the first fix: a plain
    ``F.text.startswith("/")`` filter also excluded prompts that merely
    start with "/" without being a bot command, e.g. "/etc/hosts" -- no
    other router claims that text either (text.router's own catch-all
    excludes anything starting with "/"), so it was silently dropped
    instead of reaching handle_do_input as the literal /do prompt it is.
    """
    calls: list[str] = []

    async def fake_process_request(message, prompt: str, user_id: int = 0) -> None:
        calls.append(prompt)

    monkeypatch.setattr(do_handler, "process_request", fake_process_request)
    monkeypatch.setattr(_bot_client_module(), "__call__", _fake_bot_call)

    with _isolated_dispatcher(do_handler.router) as dp:
        bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")
        chat = Chat(id=200, type="private")
        user = User(id=44, is_bot=False, first_name="Test")
        storage_key = StorageKey(bot_id=bot.id, chat_id=chat.id, user_id=user.id)
        state = FSMContext(storage=dp.storage, key=storage_key)
        await state.set_state(DoCommandState.waiting_for_input)

        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text="/etc/hosts",
        )
        await dp.feed_update(bot, Update(update_id=1, message=message))

    assert calls == ["/etc/hosts"]


async def test_do_input_unregistered_slash_command_is_treated_as_prompt(
    monkeypatch,
) -> None:
    """Reproduces the narrowed-too-far risk on the other side of the fix:
    matching any "/word" shape as a command (the first fix's regex) also
    caught "/cancel" and "/brief", which are not registered via Command(...)
    anywhere in bot/handlers/*.py -- no router would claim them, so a /do
    prompt that happened to look like one of those silently vanished too.
    _BOT_COMMAND_RE must only match the ten commands actually registered.
    """
    calls: list[str] = []

    async def fake_process_request(message, prompt: str, user_id: int = 0) -> None:
        calls.append(prompt)

    monkeypatch.setattr(do_handler, "process_request", fake_process_request)
    monkeypatch.setattr(_bot_client_module(), "__call__", _fake_bot_call)

    with _isolated_dispatcher(do_handler.router) as dp:
        bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")
        chat = Chat(id=201, type="private")
        user = User(id=45, is_bot=False, first_name="Test")
        storage_key = StorageKey(bot_id=bot.id, chat_id=chat.id, user_id=user.id)
        state = FSMContext(storage=dp.storage, key=storage_key)
        await state.set_state(DoCommandState.waiting_for_input)

        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text="/cancel",
        )
        await dp.feed_update(bot, Update(update_id=1, message=message))

    assert calls == ["/cancel"]


def _bot_client_module():  # noqa: ANN202
    import aiogram.client.bot as bot_module

    return bot_module.Bot


async def _fake_bot_call(self, method, request_timeout=None):  # noqa: ANN001
    # Stand in for the Telegram API: every reply a flow sends (start_do_flow's
    # / start_why_flow's prompt) needs somewhere to land without touching the
    # network.
    return SimpleNamespace(message_id=1)


async def _run_do_then_why(dp: Dispatcher) -> None:
    """Feed "/do" then "/why" through dp and assert the state transition."""
    bot = Bot(token="123456:FAKE-TOKEN-FOR-TESTS-ONLYX")

    chat = Chat(id=100, type="private")
    user = User(id=42, is_bot=False, first_name="Test")
    storage_key = StorageKey(bot_id=bot.id, chat_id=chat.id, user_id=user.id)
    state = FSMContext(storage=dp.storage, key=storage_key)

    do_message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text="/do",
    )
    await dp.feed_update(bot, Update(update_id=1, message=do_message))
    assert await state.get_state() == DoCommandState.waiting_for_input.state

    why_message = Message(
        message_id=2,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text="/why",
    )
    await dp.feed_update(bot, Update(update_id=2, message=why_message))

    # The do-state was replaced by why's own flow, not left stale.
    assert await state.get_state() == WhyCommandState.waiting_for_input.state
