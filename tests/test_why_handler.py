"""FSM-flow tests for /why (задача L, ТЗ 7.3) -- same FakeState/FakeMessage
approach ``tests/test_do_cancel.py`` uses for /do, extended with
``get_data``/``update_data`` for the ambiguous-choice callback flow."""

from types import SimpleNamespace

from d_brain.bot.handlers import why as why_handler
from d_brain.bot.states import DoCommandState, WhyCommandState
from d_brain.services.compiled_why import WhyChoice, WhyOutcome, WhyResult


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
        self.voice = None
        self.from_user = SimpleNamespace(id=42)
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.answers.append((text, kwargs))


def _stub_settings(monkeypatch, vault_path: str = "vault") -> None:
    monkeypatch.setattr(
        why_handler, "get_settings", lambda: SimpleNamespace(vault_path=vault_path)
    )


class FakeCallbackQuery:
    def __init__(self, data: str, message: FakeMessage | None) -> None:
        self.data = data
        self.message = message
        self.answer_calls: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answer_calls.append((text, show_alert))


async def test_cmd_why_repeated_call_cancels_waiting_state() -> None:
    message = FakeMessage()
    state = FakeState(WhyCommandState.waiting_for_input.state)

    await why_handler.cmd_why(message, SimpleNamespace(args=None), state)

    assert state.cleared is True
    assert message.answers[0][0] == "❌ */why отменён*"


async def test_cmd_why_inline_args_clears_state_and_processes(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_process(message, query, state) -> None:  # noqa: ANN001
        calls.append(query)

    monkeypatch.setattr(why_handler, "process_why_request", fake_process)
    message = FakeMessage()
    state = FakeState(WhyCommandState.waiting_for_input.state)

    await why_handler.cmd_why(message, SimpleNamespace(args="закупка сервера"), state)

    assert state.cleared is True
    assert calls == ["закупка сервера"]


async def test_handle_why_input_dash_cancels_without_processing(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_process(message, query, state) -> None:  # noqa: ANN001
        calls.append(query)

    monkeypatch.setattr(why_handler, "process_why_request", fake_process)
    message = FakeMessage(text=" - ")
    state = FakeState(WhyCommandState.waiting_for_input.state)

    await why_handler.handle_why_input(message, state)

    assert state.cleared is True
    assert calls == []
    assert message.answers[0][0] == "❌ */why отменён*"


async def test_handle_why_input_rejects_non_text_and_keeps_waiting(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_process(message, query, state) -> None:  # noqa: ANN001
        calls.append(query)

    monkeypatch.setattr(why_handler, "process_why_request", fake_process)
    message = FakeMessage(text=None)
    state = FakeState(WhyCommandState.waiting_for_input.state)

    await why_handler.handle_why_input(message, state)

    assert state.cleared is False
    assert calls == []
    assert "только текст" in message.answers[0][0]


async def test_process_why_request_not_found_sends_friendly_message(
    monkeypatch,
) -> None:
    _stub_settings(monkeypatch)
    monkeypatch.setattr(
        why_handler,
        "build_why",
        lambda vault_path, query: WhyOutcome(status="not_found"),
    )
    message = FakeMessage()
    state = FakeState()

    await why_handler.process_why_request(message, "неизвестная страница", state)

    assert "Не нашёл" in message.answers[0][0]


async def test_process_why_request_ambiguous_stores_choices_and_shows_keyboard(
    monkeypatch,
) -> None:
    _stub_settings(monkeypatch)
    choices = (
        WhyChoice(
            rel_path="compiled/decisions/a.md", title="Решение А", domain="decisions"
        ),
        WhyChoice(
            rel_path="compiled/decisions/b.md", title="Решение Б", domain="decisions"
        ),
    )
    monkeypatch.setattr(
        why_handler,
        "build_why",
        lambda vault_path, query: WhyOutcome(status="ambiguous", choices=choices),
    )
    message = FakeMessage()
    state = FakeState()

    await why_handler.process_why_request(message, "решение", state)

    assert state.data["why_choices"] == [
        {"rel_path": "compiled/decisions/a.md", "title": "Решение А"},
        {"rel_path": "compiled/decisions/b.md", "title": "Решение Б"},
    ]
    assert state.current_state == WhyCommandState.waiting_for_input.state
    text, kwargs = message.answers[0]
    assert "уточни" in text
    keyboard = kwargs["reply_markup"]
    assert len(keyboard.inline_keyboard) == 2
    token = state.data["why_choices_token"]
    assert keyboard.inline_keyboard[0][0].callback_data == f"why:choice:{token}:0"
    assert keyboard.inline_keyboard[1][0].callback_data == f"why:choice:{token}:1"
    # Telegram's hard ceiling for callback_data.
    assert len(keyboard.inline_keyboard[0][0].callback_data.encode("utf-8")) <= 64


async def test_process_why_request_three_choices_are_all_offered_and_counted(
    monkeypatch,
) -> None:
    """The prompt used to say "нашлось две похожие страницы" no matter how
    many candidates there were. An exact slug hit lands in as many of the
    six compiled domains as use that slug, so a three-way collision showed
    three buttons under a sentence claiming there were two."""
    _stub_settings(monkeypatch)
    choices = tuple(
        WhyChoice(
            rel_path=f"compiled/{domain}/roadmap.md",
            title=f"Дорожная карта ({domain})",
            domain=domain,
        )
        for domain in ("decisions", "topics", "projects")
    )
    monkeypatch.setattr(
        why_handler,
        "build_why",
        lambda vault_path, query: WhyOutcome(status="ambiguous", choices=choices),
    )
    message = FakeMessage()
    state = FakeState()

    await why_handler.process_why_request(message, "roadmap", state)

    text, kwargs = message.answers[0]
    assert "нашлось похожих страниц: 3" in text
    assert "две" not in text
    keyboard = kwargs["reply_markup"]
    assert len(keyboard.inline_keyboard) == 3
    token = state.data["why_choices_token"]
    assert keyboard.inline_keyboard[2][0].callback_data == f"why:choice:{token}:2"


async def test_process_why_request_resolved_delivers_and_touches(monkeypatch) -> None:
    _stub_settings(monkeypatch)
    result = WhyResult(
        rel_path="compiled/decisions/a.md",
        title="Решение А",
        domain="decisions",
        markdown="**Почему страница говорит так**",
    )
    monkeypatch.setattr(
        why_handler,
        "build_why",
        lambda vault_path, query: WhyOutcome(status="resolved", result=result),
    )
    touched: list[list[str]] = []

    class FakeQmd:
        def __init__(self, vault_path) -> None:  # noqa: ANN001
            pass

        def touch_notes(self, targets: list[str]) -> None:
            touched.append(targets)

    monkeypatch.setattr(why_handler, "QmdService", FakeQmd)
    delivered: list[str] = []

    async def fake_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        delivered.append(text)

    monkeypatch.setattr(why_handler, "answer_rich_text", fake_answer_rich_text)
    message = FakeMessage()
    state = FakeState()

    await why_handler.process_why_request(message, "решение А", state)

    assert touched == [["compiled/decisions/a.md"]]
    assert delivered == ["**Почему страница говорит так**"]


async def test_deliver_why_result_touch_failure_does_not_prevent_reply(
    monkeypatch,
) -> None:
    class FailingQmd:
        def __init__(self, vault_path) -> None:  # noqa: ANN001
            pass

        def touch_notes(self, targets: list[str]) -> None:
            raise RuntimeError("qmd unavailable")

    monkeypatch.setattr(why_handler, "QmdService", FailingQmd)
    delivered: list[str] = []

    async def fake_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        delivered.append(text)

    monkeypatch.setattr(why_handler, "answer_rich_text", fake_answer_rich_text)
    result = WhyResult(
        rel_path="compiled/decisions/a.md",
        title="A",
        domain="decisions",
        markdown="text",
    )

    await why_handler.deliver_why_result(FakeMessage(), "vault", result)

    assert delivered == ["text"]


async def test_handle_why_choice_resolves_and_clears_state(monkeypatch) -> None:
    _stub_settings(monkeypatch)
    result = WhyResult(
        rel_path="compiled/decisions/b.md",
        title="Решение Б",
        domain="decisions",
        markdown="**Б**",
    )
    monkeypatch.setattr(
        why_handler, "build_why_for_path", lambda vault_path, rel_path: result
    )
    delivered: list[str] = []

    async def fake_answer_rich_text(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        delivered.append(text)

    monkeypatch.setattr(why_handler, "answer_rich_text", fake_answer_rich_text)
    monkeypatch.setattr(
        why_handler,
        "QmdService",
        lambda vault_path: SimpleNamespace(touch_notes=lambda t: None),
    )

    message = FakeMessage()
    # The state ``process_why_request`` sets alongside the choices -- the
    # callback now requires it, so the fixture has to model the real flow.
    state = FakeState(WhyCommandState.waiting_for_input.state)
    state.data["why_choices"] = [
        {"rel_path": "compiled/decisions/a.md", "title": "Решение А"},
        {"rel_path": "compiled/decisions/b.md", "title": "Решение Б"},
    ]
    token = why_handler.why_choices_token(
        ["compiled/decisions/a.md", "compiled/decisions/b.md"]
    )
    state.data["why_choices_token"] = token
    query = FakeCallbackQuery(f"why:choice:{token}:1", message)

    await why_handler.handle_why_choice(query, state)

    assert state.cleared is True
    assert delivered == ["**Б**"]
    assert query.answer_calls == [(None, False)]


async def test_handle_why_choice_stale_index_shows_alert() -> None:
    message = FakeMessage()
    state = FakeState(WhyCommandState.waiting_for_input.state)
    state.data["why_choices"] = [
        {"rel_path": "compiled/decisions/a.md", "title": "Решение А"},
    ]
    token = why_handler.why_choices_token(["compiled/decisions/a.md"])
    state.data["why_choices_token"] = token
    query = FakeCallbackQuery(f"why:choice:{token}:5", message)

    await why_handler.handle_why_choice(query, state)

    assert state.cleared is False
    assert query.answer_calls[0][1] is True


async def test_handle_why_choice_from_a_superseded_question_shows_alert(
    monkeypatch,
) -> None:
    """Code review: FSM state holds exactly one ``why_choices`` list, so a
    second ambiguous /why overwrites the first one's. Tapping the older
    message's button carries an in-range index -- the bounds check cannot
    see the mismatch -- and used to silently answer about whichever page
    sat at that index in the *new* candidate set."""
    resolved: list[str] = []
    monkeypatch.setattr(
        why_handler,
        "build_why_for_path",
        lambda vault_path, rel_path: resolved.append(rel_path),
    )
    message = FakeMessage()
    state = FakeState(WhyCommandState.waiting_for_input.state)
    # The owner's second /why replaced the first query's candidates...
    state.data["why_choices"] = [
        {"rel_path": "compiled/topics/x.md", "title": "Тема X"},
        {"rel_path": "compiled/topics/y.md", "title": "Тема Y"},
    ]
    state.data["why_choices_token"] = why_handler.why_choices_token(
        ["compiled/topics/x.md", "compiled/topics/y.md"]
    )
    # ...but this button came from the first one.
    stale_token = why_handler.why_choices_token(
        ["compiled/decisions/a.md", "compiled/decisions/b.md"]
    )
    query = FakeCallbackQuery(f"why:choice:{stale_token}:1", message)

    await why_handler.handle_why_choice(query, state)

    assert resolved == []
    assert state.cleared is False
    assert query.answer_calls[0][1] is True


async def test_handle_why_choice_missing_page_reports_gone(monkeypatch) -> None:
    _stub_settings(monkeypatch)
    monkeypatch.setattr(
        why_handler, "build_why_for_path", lambda vault_path, rel_path: None
    )
    message = FakeMessage()
    state = FakeState(WhyCommandState.waiting_for_input.state)
    state.data["why_choices"] = [{"rel_path": "compiled/decisions/a.md", "title": "A"}]
    token = why_handler.why_choices_token(["compiled/decisions/a.md"])
    state.data["why_choices_token"] = token
    query = FakeCallbackQuery(f"why:choice:{token}:0", message)

    await why_handler.handle_why_choice(query, state)

    assert state.cleared is True
    assert "не существует" in message.answers[0][0]


async def test_process_why_request_build_crash_still_answers_the_owner(
    monkeypatch,
) -> None:
    """``build_why`` re-reads every ``compiled/**`` page, which the nightly
    pass can be rewriting or archiving underneath it. Letting that escape
    left the owner with no reply at all -- ``do.py``, the flow this module
    is modeled on, always answers even when the work itself crashed."""
    _stub_settings(monkeypatch)

    def raising_build_why(vault_path, query):  # noqa: ANN001
        raise RuntimeError("compiled page vanished mid-read")

    monkeypatch.setattr(why_handler, "build_why", raising_build_why)
    message = FakeMessage()
    state = FakeState()

    await why_handler.process_why_request(message, "аврора", state)

    assert message.answers
    assert "Не удалось собрать ответ" in message.answers[0][0]


async def test_handle_why_choice_build_crash_still_answers_and_stops_the_spinner(
    monkeypatch,
) -> None:
    """The disambiguation button reaches the same ``compiled/**`` re-read
    the direct query does, but had no guard around it. With the FSM already
    cleared by the time it runs, a crash left the owner with a button still
    spinning, no answer, and no flow left to retry in."""
    _stub_settings(monkeypatch)

    def raising_build(vault_path, rel_path):  # noqa: ANN001
        raise RuntimeError("compiled page vanished mid-read")

    monkeypatch.setattr(why_handler, "build_why_for_path", raising_build)
    message = FakeMessage()
    state = FakeState(WhyCommandState.waiting_for_input.state)
    state.data["why_choices"] = [{"rel_path": "compiled/decisions/a.md", "title": "A"}]
    token = why_handler.why_choices_token(["compiled/decisions/a.md"])
    state.data["why_choices_token"] = token
    query = FakeCallbackQuery(f"why:choice:{token}:0", message)

    await why_handler.handle_why_choice(query, state)

    assert query.answer_calls == [(None, False)]
    assert message.answers
    assert "Не удалось собрать ответ" in message.answers[0][0]


async def test_deliver_why_result_survives_a_failed_send(monkeypatch) -> None:
    """``answer_rich_text`` only falls back on ``TelegramBadRequest``; a
    network error on the send escaped into a dispatcher with no error
    handler. ``do.py``, which this module's header names as its model,
    wraps its own final reply -- this did not."""

    async def raising_send(message, text: str, **kwargs) -> None:  # noqa: ANN001, ANN003
        raise RuntimeError("telegram network error")

    monkeypatch.setattr(why_handler, "answer_rich_text", raising_send)
    monkeypatch.setattr(
        why_handler,
        "QmdService",
        lambda vault_path: SimpleNamespace(touch_notes=lambda t: None),
    )
    result = WhyResult(
        rel_path="compiled/decisions/a.md",
        title="A",
        domain="decisions",
        markdown="text",
    )

    await why_handler.deliver_why_result(FakeMessage(), "vault", result)


async def test_handle_why_choice_from_another_flow_does_not_clear_it(
    monkeypatch,
) -> None:
    """Code review: ``start_do_flow``/``start_brief_flow`` switch state
    without clearing data, so an abandoned ambiguous /why leaves its
    ``why_choices_token`` behind and the token check alone still matched.
    Tapping that old button fell through to ``state.clear()`` and wiped the
    /do or /brief the owner had since started -- their next message went to
    the plain capture handlers instead, with nothing said either way."""
    resolved: list[str] = []
    monkeypatch.setattr(
        why_handler,
        "build_why_for_path",
        lambda vault_path, rel_path: resolved.append(rel_path),
    )
    message = FakeMessage()
    # The ambiguous /why's data survives, but the owner has since started
    # another flow, so the state no longer belongs to /why.
    state = FakeState(DoCommandState.waiting_for_input.state)
    state.data["why_choices"] = [{"rel_path": "compiled/decisions/a.md", "title": "A"}]
    token = why_handler.why_choices_token(["compiled/decisions/a.md"])
    state.data["why_choices_token"] = token
    query = FakeCallbackQuery(f"why:choice:{token}:0", message)

    await why_handler.handle_why_choice(query, state)

    assert resolved == []
    assert state.cleared is False
    assert state.current_state == DoCommandState.waiting_for_input.state
    assert query.answer_calls[0][1] is True
