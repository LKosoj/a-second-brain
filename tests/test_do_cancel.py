import asyncio
from types import SimpleNamespace

from d_brain.bot.handlers import do as do_handler
from d_brain.bot.states import DoCommandState


class FakeState:
    def __init__(self, current_state: str | None = None) -> None:
        self.current_state = current_state
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


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.voice = None
        self.from_user = SimpleNamespace(id=42)
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.answers.append((text, kwargs))


async def test_cmd_do_repeated_call_cancels_waiting_state() -> None:
    message = FakeMessage()
    state = FakeState(DoCommandState.waiting_for_input.state)

    await do_handler.cmd_do(message, SimpleNamespace(args=None), state)

    assert state.cleared is True
    assert message.answers[0][0] == "❌ */do отменён*"
    assert "reply_markup" not in message.answers[0][1]


async def test_cmd_do_inline_clears_waiting_state(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    async def fake_process_request(message, prompt: str, user_id: int = 0) -> None:
        del message
        calls.append((prompt, user_id))

    monkeypatch.setattr(do_handler, "process_request", fake_process_request)
    message = FakeMessage()
    state = FakeState(DoCommandState.waiting_for_input.state)

    await do_handler.cmd_do(message, SimpleNamespace(args="Проверить статус"), state)

    assert state.cleared is True
    assert calls == [("Проверить статус", 42)]


async def test_handle_do_input_dash_cancels_without_processing(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_process_request(message, prompt: str, user_id: int = 0) -> None:
        calls.append(prompt)

    monkeypatch.setattr(do_handler, "process_request", fake_process_request)

    message = FakeMessage(text=" - ")
    state = FakeState(DoCommandState.waiting_for_input.state)

    await do_handler.handle_do_input(message, SimpleNamespace(), state)

    assert state.cleared is True
    assert calls == []
    assert message.answers[0][0] == "❌ */do отменён*"
    assert "reply_markup" not in message.answers[0][1]


async def test_process_request_sends_rich_final_after_deleting_status(
    monkeypatch,
) -> None:
    fallback_answers: list[tuple[str, str | None]] = []
    status_messages: list[object] = []
    class FakeStatusMessage:
        deleted = False

        async def delete(self) -> None:
            self.deleted = True

    class FakeProcessor:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def execute_prompt(self, prompt: str, user_id: int = 0) -> dict[str, object]:
            assert prompt == "Проверить статус"
            assert user_id == 42
            return {"report": "**Готово**", "processed_entries": 1}

    async def fake_answer_text(message, text: str, **kwargs):  # noqa: ANN001, ANN003
        if text == "⏳ Выполняю...":
            status = FakeStatusMessage()
            status_messages.append(status)
            return status
        fallback_answers.append((text, kwargs.get("parse_mode")))
        return SimpleNamespace(text=text)

    async def fake_answer_rich_text(  # noqa: ANN001, ANN003
        message,
        text: str,
        **kwargs,
    ):
        fallback_answers.append((text, kwargs.get("parse_mode")))
        return SimpleNamespace(text=text)

    async def fake_to_thread(func, *args, **kwargs):  # noqa: ANN001, ANN003
        return func(*args, **kwargs)

    monkeypatch.setattr(do_handler, "answer_text", fake_answer_text)
    monkeypatch.setattr(do_handler, "answer_rich_text", fake_answer_rich_text)
    monkeypatch.setattr(do_handler, "CliProcessor", FakeProcessor)
    monkeypatch.setattr(
        do_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path="vault",
            todoist_api_key="",
            ai_cli="qwen",
            owner_full_name="Alexey",
            content_language="ru",
            openai_api_key="",
            openai_base_url="",
            openai_model="",
        ),
    )
    monkeypatch.setattr(do_handler.asyncio, "to_thread", fake_to_thread)
    await asyncio.wait_for(
        do_handler.process_request(
            FakeMessage(text="Проверить статус"),
            "Проверить статус",
            42,
        ),
        timeout=0.5,
    )

    assert len(status_messages) == 1
    assert status_messages[0].deleted is True
    assert fallback_answers == [("**Готово**", None)]
