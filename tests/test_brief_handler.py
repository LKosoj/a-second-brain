"""FSM-flow tests for the dashboard's "Собрать бриф" sub-flow (задача L, ТЗ
7.6) -- same FakeState/FakeMessage approach ``tests/test_do_cancel.py`` and
``tests/test_why_handler.py`` use."""

import asyncio
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from d_brain.bot import vault_busy
from d_brain.bot.handlers import brief as brief_handler
from d_brain.bot.states import BriefCommandState
from d_brain.services.compiled_briefs import BriefResult
from d_brain.services.vault_lock import VaultLockTimeout


class FakeState:
    def __init__(self, current_state: str | None = None) -> None:
        self.current_state = current_state
        self.data: dict[str, object] = {}
        self.cleared = False
        self.set_calls: list[object] = []

    async def get_state(self) -> str | None:
        return self.current_state

    async def set_state(self, state: object) -> None:
        self.set_calls.append(state)
        self.current_state = getattr(state, "state", str(state))

    async def clear(self) -> None:
        self.cleared = True
        self.current_state = None
        self.data = {}

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **kwargs: object) -> None:
        self.data.update(kwargs)


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.forward_origin = None
        self.chat = SimpleNamespace(id=1)
        self.bot = object()
        self.from_user = SimpleNamespace(id=42)
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.answers.append((text, kwargs))


def _stub_settings(monkeypatch, vault_path: str = "vault") -> None:
    monkeypatch.setattr(
        brief_handler, "get_settings", lambda: SimpleNamespace(vault_path=vault_path)
    )


@contextmanager
def _fake_vault_write_lock(vault_path, timeout=None):  # noqa: ANN001
    yield SimpleNamespace()


async def test_start_brief_flow_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        await brief_handler.start_brief_flow(FakeMessage(), FakeState(), "unknown")


async def test_start_brief_flow_sets_state_and_data() -> None:
    message = FakeMessage()
    state = FakeState()

    await brief_handler.start_brief_flow(message, state, "decision")

    assert state.data["brief_type"] == "decision"
    assert state.current_state == BriefCommandState.waiting_for_query.state
    assert "решению" in message.answers[0][0]


async def test_handle_brief_query_dash_cancels_without_processing(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_process(message, brief_type, query) -> None:  # noqa: ANN001
        calls.append((brief_type, query))

    monkeypatch.setattr(brief_handler, "process_brief_request", fake_process)
    message = FakeMessage(text=" - ")
    state = FakeState(BriefCommandState.waiting_for_query.state)
    state.data["brief_type"] = "decision"

    await brief_handler.handle_brief_query(message, state)

    assert state.cleared is True
    assert calls == []
    assert "отменён" in message.answers[0][0].lower()


def _prepare_release(monkeypatch):  # noqa: ANN001, ANN202
    calls: list[tuple[str, str]] = []
    rendered: list[int] = []

    async def fake_process(message, brief_type, query) -> None:  # noqa: ANN001
        calls.append((brief_type, query))

    async def fake_render_section(bot, **kwargs) -> None:  # noqa: ANN001, ANN003
        rendered.append(kwargs["preferred_message_id"])

    monkeypatch.setattr(brief_handler, "process_brief_request", fake_process)
    monkeypatch.setattr(brief_handler, "render_section", fake_render_section)
    state = FakeState(BriefCommandState.waiting_for_query.state)
    state.data.update(brief_type="decision", menu_message_id=77)
    return calls, rendered, state


async def test_handle_brief_query_non_text_releases_state_and_skips(
    monkeypatch,
) -> None:
    calls, rendered, state = _prepare_release(monkeypatch)
    message = FakeMessage(text=None)

    with pytest.raises(SkipHandler):
        await brief_handler.handle_brief_query(message, state)

    assert state.cleared is True
    assert calls == []
    assert message.answers == []
    assert rendered == [77]


async def test_handle_brief_query_forwarded_text_releases_state(monkeypatch) -> None:
    calls, rendered, state = _prepare_release(monkeypatch)
    message = FakeMessage(text="пересланное")
    message.forward_origin = object()

    with pytest.raises(SkipHandler):
        await brief_handler.handle_brief_query(message, state)

    assert state.cleared is True
    assert calls == []
    assert rendered == [77]


async def test_handle_brief_query_expired_text_goes_to_capture(monkeypatch) -> None:
    calls, rendered, state = _prepare_release(monkeypatch)
    state.data["brief_entered_at"] = time.time() - 601

    with pytest.raises(SkipHandler):
        await brief_handler.handle_brief_query(FakeMessage(text="заметка"), state)

    assert state.cleared is True
    assert calls == []
    assert rendered == [77]


async def test_handle_brief_query_expired_dash_just_cancels(monkeypatch) -> None:
    calls, rendered, state = _prepare_release(monkeypatch)
    state.data["brief_entered_at"] = time.time() - 601
    message = FakeMessage(text="-")

    await brief_handler.handle_brief_query(message, state)

    assert state.cleared is True
    assert calls == []
    assert "отменён" in message.answers[0][0].lower()


async def test_handle_brief_query_fresh_stamp_is_processed(monkeypatch) -> None:
    calls, rendered, state = _prepare_release(monkeypatch)
    state.data["brief_entered_at"] = time.time() - 5

    await brief_handler.handle_brief_query(FakeMessage(text="цены"), state)

    assert calls == [("decision", "цены")]


async def test_start_brief_flow_stamps_entry_time() -> None:
    state = FakeState()
    before = time.time()

    await brief_handler.start_brief_flow(FakeMessage(), state, "decision")

    assert before <= state.data["brief_entered_at"] <= time.time()


class _BlockedLock:
    """Lock factory imitating the nightly pass holding the vault lock."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.done = threading.Event()

    @contextmanager
    def __call__(self, vault_path, timeout=None):  # noqa: ANN001, ANN204
        if not self.release.wait(10 if timeout is None else timeout):
            self.done.set()
            raise VaultLockTimeout
        assert self.release.wait(10)
        try:
            yield SimpleNamespace()
        finally:
            self.done.set()


async def test_run_with_vault_lock_does_not_block_event_loop() -> None:
    lock = _BlockedLock()
    ticks: list[int] = []

    async def ticker() -> None:
        for i in range(3):
            await asyncio.sleep(0.01)
            ticks.append(i)
        lock.release.set()

    result, _ = await asyncio.gather(
        vault_busy.run_with_vault_lock(
            Path("v"), lambda held: "done", lock_factory=lock, timeout=5
        ),
        ticker(),
    )

    assert result == "done"
    assert ticks == [0, 1, 2]


async def test_run_with_vault_lock_busy_vault_raises_and_never_runs_work() -> None:
    lock = _BlockedLock()
    ran: list[str] = []

    with pytest.raises(vault_busy.VaultBusyError):
        await vault_busy.run_with_vault_lock(
            Path("v"), lambda held: ran.append("x"), lock_factory=lock, timeout=0.05
        )
    lock.release.set()
    assert await asyncio.to_thread(lock.done.wait, 5)

    assert ran == []


async def test_run_with_vault_lock_slow_work_after_acquire_still_returns() -> None:
    def slow_work(held):  # noqa: ANN001, ANN202
        time.sleep(0.2)
        return "written"

    result = await vault_busy.run_with_vault_lock(
        Path("v"), slow_work, lock_factory=_fake_vault_write_lock, timeout=0.05
    )

    assert result == "written"


async def test_process_brief_request_reports_busy_when_lock_times_out(
    monkeypatch,
) -> None:
    _stub_settings(monkeypatch)
    lock = _BlockedLock()
    monkeypatch.setattr(vault_busy, "VAULT_LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(brief_handler, "vault_write_lock", lock)
    monkeypatch.setattr(
        brief_handler, "build_brief",
        lambda vault_path, *, brief_type, query: _sample_brief_result(),
    )
    monkeypatch.setattr(
        brief_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    written: list[object] = []
    monkeypatch.setattr(
        brief_handler, "write_validated_vault_markdown",
        lambda *args, **kwargs: written.append(args),
    )
    monkeypatch.setattr(brief_handler, "brief_path", lambda *a: Path("b.md"))
    message = FakeMessage()

    await brief_handler.process_brief_request(message, "decision", "q")
    lock.release.set()
    assert await asyncio.to_thread(lock.done.wait, 5)

    assert len(message.answers) == 1
    assert "Идёт обработка" in message.answers[0][0]
    assert written == []


async def test_handle_brief_query_missing_type_cancels_defensively(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_process(message, brief_type, query) -> None:  # noqa: ANN001
        calls.append((brief_type, query))

    monkeypatch.setattr(brief_handler, "process_brief_request", fake_process)
    message = FakeMessage(text="закупка сервера")
    state = FakeState(BriefCommandState.waiting_for_query.state)
    # brief_type deliberately missing from state data.

    await brief_handler.handle_brief_query(message, state)

    assert state.cleared is True
    assert calls == []


async def test_handle_brief_query_valid_text_processes(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_process(message, brief_type, query) -> None:  # noqa: ANN001
        calls.append((brief_type, query))

    monkeypatch.setattr(brief_handler, "process_brief_request", fake_process)
    message = FakeMessage(text=" закупка сервера ")
    state = FakeState(BriefCommandState.waiting_for_query.state)
    state.data["brief_type"] = "decision"

    await brief_handler.handle_brief_query(message, state)

    assert state.cleared is True
    assert calls == [("decision", "закупка сервера")]


async def test_process_brief_request_not_found_sends_friendly_message(
    monkeypatch,
) -> None:
    _stub_settings(monkeypatch)
    monkeypatch.setattr(
        brief_handler, "build_brief", lambda vault_path, *, brief_type, query: None
    )
    message = FakeMessage()

    await brief_handler.process_brief_request(message, "decision", "закупка сервера")

    assert "Не нашёл" in message.answers[0][0]


async def test_process_brief_request_answers_even_when_the_build_blows_up(
    monkeypatch,
) -> None:
    """``build_brief`` re-reads every ``compiled/**`` page, which the nightly
    pass can be rewriting or archiving underneath it. The FSM state is
    already cleared by the time this runs, so an escaping exception left the
    owner with no reply and no flow to retry in -- the same hole ``why.py``
    already closed around ``build_why``."""

    _stub_settings(monkeypatch)

    def _boom(vault_path, *, brief_type: str, query: str) -> BriefResult:  # noqa: ANN001
        raise RuntimeError("compiled/ rewritten mid-scan")

    monkeypatch.setattr(brief_handler, "build_brief", _boom)
    message = FakeMessage()

    await brief_handler.process_brief_request(message, "decision", "закупка сервера")

    # MarkdownV2-escaped on the way out, like every other reply this handler
    # sends through ``answer_text``.
    assert message.answers[0][0] == "❌ Не удалось собрать справку\\."


def _sample_brief_result() -> BriefResult:
    return BriefResult(
        brief_type="decision",
        domain="decisions",
        slug="закупка-сервера",
        source_rel_path="compiled/decisions/zakupka-servera.md",
        title="Закупка сервера",
        markdown="**Бриф по решению: Закупка сервера**",
    )


async def test_process_brief_request_writes_touches_and_delivers(monkeypatch) -> None:
    _stub_settings(monkeypatch)
    result = _sample_brief_result()
    monkeypatch.setattr(
        brief_handler, "build_brief", lambda vault_path, *, brief_type, query: result
    )
    monkeypatch.setattr(
        brief_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    monkeypatch.setattr(brief_handler, "vault_write_lock", _fake_vault_write_lock)
    monkeypatch.setattr(
        brief_handler, "brief_path", lambda vault_path, result, today: "brief.md"
    )
    written: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        brief_handler,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: written.append((path, content)),
    )
    monkeypatch.setattr(
        brief_handler, "render_brief_note", lambda result, today: b"rendered"
    )
    touched: list[list[str]] = []

    class FakeQmd:
        def __init__(self, vault_path) -> None:  # noqa: ANN001
            pass

        def touch_notes(self, targets: list[str]) -> None:
            touched.append(targets)

    monkeypatch.setattr(brief_handler, "QmdService", FakeQmd)
    delivered: list[str] = []

    async def fake_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        delivered.append(text)

    monkeypatch.setattr(brief_handler, "answer_rich_text", fake_answer_rich_text)
    message = FakeMessage()

    await brief_handler.process_brief_request(message, "decision", "закупка сервера")

    assert written == [("brief.md", b"rendered")]
    assert touched == [["compiled/decisions/zakupka-servera.md"]]
    assert delivered == ["**Бриф по решению: Закупка сервера**"]


async def test_process_brief_request_write_failure_reports_error(monkeypatch) -> None:
    _stub_settings(monkeypatch)
    result = _sample_brief_result()
    monkeypatch.setattr(
        brief_handler, "build_brief", lambda vault_path, *, brief_type, query: result
    )
    monkeypatch.setattr(
        brief_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    monkeypatch.setattr(brief_handler, "vault_write_lock", _fake_vault_write_lock)
    monkeypatch.setattr(
        brief_handler, "brief_path", lambda vault_path, result, today: "brief.md"
    )

    def _raise(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("disk full")

    monkeypatch.setattr(brief_handler, "write_validated_vault_markdown", _raise)
    monkeypatch.setattr(
        brief_handler, "render_brief_note", lambda result, today: b"rendered"
    )
    delivered: list[str] = []

    async def fake_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        delivered.append(text)

    monkeypatch.setattr(brief_handler, "answer_rich_text", fake_answer_rich_text)
    message = FakeMessage()

    await brief_handler.process_brief_request(message, "decision", "закупка сервера")

    assert delivered == []
    assert "не удалось сохранить справку" in message.answers[0][0].lower()


async def test_process_brief_request_delivery_failure_still_names_the_saved_file(
    monkeypatch,
) -> None:
    """The brief is already filed in the vault by the time it is delivered, so
    a failed send must not pass in silence: the owner would be left with a
    new file they never learn about. Mirrors
    ``menu.py::_deliver_daily_digest``'s own fallback."""
    _stub_settings(monkeypatch, vault_path="/tmp/vault")
    result = _sample_brief_result()
    monkeypatch.setattr(
        brief_handler, "build_brief", lambda vault_path, *, brief_type, query: result
    )
    monkeypatch.setattr(
        brief_handler, "load_manifest_for_vault", lambda vault_path: None
    )
    monkeypatch.setattr(brief_handler, "vault_write_lock", _fake_vault_write_lock)
    monkeypatch.setattr(
        brief_handler,
        "brief_path",
        lambda vault_path, result, today: Path("/tmp/vault/summaries/briefs/b.md"),
    )
    written: list[Path] = []
    monkeypatch.setattr(
        brief_handler,
        "write_validated_vault_markdown",
        lambda vault_path, path, content, **kwargs: written.append(path),
    )
    monkeypatch.setattr(
        brief_handler, "render_brief_note", lambda result, today: b"rendered"
    )

    class FakeQmd:
        def __init__(self, vault_path) -> None:  # noqa: ANN001
            pass

        def touch_notes(self, targets: list[str]) -> None:
            pass

    monkeypatch.setattr(brief_handler, "QmdService", FakeQmd)

    async def failing_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        raise RuntimeError("Telegram API unreachable")

    monkeypatch.setattr(
        brief_handler, "answer_rich_text", failing_answer_rich_text
    )
    fallback: list[str] = []

    async def fake_answer_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        fallback.append(text)

    monkeypatch.setattr(brief_handler, "answer_text", fake_answer_text)
    message = FakeMessage()

    await brief_handler.process_brief_request(message, "decision", "закупка сервера")

    assert written == [Path("/tmp/vault/summaries/briefs/b.md")]
    assert fallback == [
        "❌ Справка сохранена в `summaries/briefs/b.md`, "
        "но отправить его в чат не удалось."
    ]


async def test_handle_brief_query_restore_menu_failure_still_skips(
    monkeypatch, caplog
) -> None:
    calls, rendered, state = _prepare_release(monkeypatch)

    async def boom(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise RuntimeError("telegram down")

    monkeypatch.setattr(brief_handler, "render_section", boom)

    with caplog.at_level("ERROR"), pytest.raises(SkipHandler):
        await brief_handler.handle_brief_query(FakeMessage(text=None), state)

    assert state.cleared is True
    assert "telegram down" in caplog.text
