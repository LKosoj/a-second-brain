"""FSM-flow tests for the dashboard's "Собрать бриф" sub-flow (задача L, ТЗ
7.6) -- same FakeState/FakeMessage approach ``tests/test_do_cancel.py`` and
``tests/test_why_handler.py`` use."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from d_brain.bot.handlers import brief as brief_handler
from d_brain.bot.states import BriefCommandState
from d_brain.services.compiled_briefs import BriefResult


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
        self.from_user = SimpleNamespace(id=42)
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.answers.append((text, kwargs))


def _stub_settings(monkeypatch, vault_path: str = "vault") -> None:
    monkeypatch.setattr(
        brief_handler, "get_settings", lambda: SimpleNamespace(vault_path=vault_path)
    )


@contextmanager
def _fake_vault_write_lock(vault_path):  # noqa: ANN001
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


async def test_handle_brief_query_rejects_non_text_and_keeps_waiting(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_process(message, brief_type, query) -> None:  # noqa: ANN001
        calls.append((brief_type, query))

    monkeypatch.setattr(brief_handler, "process_brief_request", fake_process)
    message = FakeMessage(text=None)
    state = FakeState(BriefCommandState.waiting_for_query.state)
    state.data["brief_type"] = "decision"

    await brief_handler.handle_brief_query(message, state)

    assert state.cleared is False
    assert calls == []
    assert "текстовый запрос" in message.answers[0][0]


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
    assert message.answers[0][0] == "❌ Не удалось собрать бриф\\."


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
    assert "не удалось сохранить бриф" in message.answers[0][0].lower()


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
        "❌ Бриф сохранён в `summaries/briefs/b.md`, "
        "но отправить его в чат не удалось."
    ]
