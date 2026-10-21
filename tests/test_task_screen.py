import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from d_brain.bot import dashboard
from d_brain.bot.handlers import tasks
from d_brain.services import processor as processor_module
from d_brain.services.processor import CliProcessor
from d_brain.services.todoist_tasks import TodoistTask, TodoistTaskError


@pytest.fixture(autouse=True)
def clean_sessions():
    dashboard._SESSIONS.clear()
    yield
    dashboard._SESSIONS.clear()


def task_and_query(action="complete"):
    task = TodoistTask("example", "Example task", "2026-10-20", "p1")
    session = dashboard.get_dashboard_session(1)
    session.dashboard_message_id = 42
    session.current_screen = "tasks"
    session.task_items = [task]
    query = SimpleNamespace(
        data=f"tasks:{action}:{task.id}:{task.fingerprint}",
        message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=42),
        from_user=SimpleNamespace(id=7),
        answer=AsyncMock(),
    )
    return task, session, query


def test_task_rows_have_colored_checkbox_arrow_and_escaped_title():
    task = TodoistTask("example", '<tg-button data="injected">&', "2026-10-20", "p1")
    text, keyboard, rich = tasks.build_tasks_view(
        [task],
        0,
        datetime(2026, 10, 21, tzinfo=ZoneInfo("Europe/Moscow")),
    )
    assert 'style="danger">☐' in rich.html
    assert f"tasks:complete:example:{task.fingerprint}" in rich.html
    assert f"tasks:ask:example:{task.fingerprint}" in rich.html
    assert "↗</tg-button>" in rich.html
    assert "&lt;tg-button" in rich.html
    assert '<tg-button data="injected">' not in rich.html
    assert "Просрочено: 1" in text
    assert keyboard.inline_keyboard[0][0].callback_data.startswith("tasks:complete:")


def test_pages_keep_all_tasks_reachable():
    items = [TodoistTask(str(i), f"Example task {i}", "2026-10-21") for i in range(18)]
    now = datetime(2026, 10, 21, tzinfo=ZoneInfo("UTC"))
    rendered = []
    for page in range(3):
        _text, keyboard, rich = tasks.build_tasks_view(items, page, now)
        rendered.extend(
            button.callback_data.split(":")[2]
            for row in keyboard.inline_keyboard
            for button in row
            if (button.callback_data or "").startswith("tasks:ask:")
        )
        assert f"{page + 1}/3" in rich.html
    assert rendered == [str(i) for i in range(18)]


def test_completed_task_is_checked_and_has_no_completion_callback():
    task = TodoistTask("example", "Example task", "2026-10-20", completed=True)
    text, keyboard, rich = tasks.build_tasks_view(
        [task],
        0,
        datetime(2026, 10, 21, tzinfo=ZoneInfo("UTC")),
    )
    assert "☑" in rich.html
    assert "<s>Example task</s>" in rich.html
    assert "tasks:complete:" not in rich.html
    assert keyboard.inline_keyboard[0][0].callback_data == "menu:noop"
    assert "Просрочено: 0" in text


@pytest.mark.asyncio
async def test_complete_is_once_and_failed_completion_stays_unchecked(monkeypatch):
    task, session, query = task_and_query()
    client = SimpleNamespace(complete=Mock())
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path="/fictional",
            todoist_api_key="",
        ),
    )
    monkeypatch.setattr(tasks, "TodoistTasks", lambda *a: client)
    render = AsyncMock()
    monkeypatch.setattr(tasks, "render_tasks", render)
    state = SimpleNamespace(clear=AsyncMock())
    await tasks.handle_task_callback(query, None, state)
    await tasks.handle_task_callback(query, None, state)
    assert client.complete.call_count == 1
    assert task.completed
    assert not session.task_busy
    assert render.await_count == 1

    task.completed = False
    client.complete.side_effect = TodoistTaskError("Не подтверждено")
    reply = AsyncMock()
    monkeypatch.setattr(tasks, "answer_text", reply)
    await tasks.handle_task_callback(query, None, state)
    assert not task.completed
    assert not session.task_busy
    assert render.await_count == 1
    assert "Не подтверждено" in reply.await_args.args[1]


@pytest.mark.asyncio
async def test_simultaneous_completion_taps_do_not_send_two_mutations(monkeypatch):
    task, _session, query = task_and_query()
    complete = Mock()
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path="/fictional",
            todoist_api_key="",
        ),
    )
    monkeypatch.setattr(
        tasks, "TodoistTasks", lambda *a: SimpleNamespace(complete=complete)
    )
    monkeypatch.setattr(tasks, "render_tasks", AsyncMock())
    entered, release = asyncio.Event(), asyncio.Event()

    async def to_thread(function, *args):
        entered.set()
        await release.wait()
        return function(*args)

    monkeypatch.setattr(tasks.asyncio, "to_thread", to_thread)
    state = SimpleNamespace(clear=AsyncMock())
    first = asyncio.create_task(tasks.handle_task_callback(query, None, state))
    await entered.wait()
    await tasks.handle_task_callback(query, None, state)
    release.set()
    await first
    assert task.completed
    assert complete.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["message", "occurrence", "restart"])
async def test_stale_task_buttons_do_not_complete_anything(monkeypatch, stale):
    _task, session, query = task_and_query()
    if stale == "message":
        query.message.message_id = 999
    elif stale == "occurrence":
        session.task_items[0].due = "2026-10-22"
    else:
        dashboard.clear_dashboard_session(1)
    client = Mock()
    monkeypatch.setattr(tasks, "TodoistTasks", client)
    await tasks.handle_task_callback(query, None, SimpleNamespace(clear=AsyncMock()))
    client.assert_not_called()
    assert "устарел" in query.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_arrow_explains_selected_task_with_owner_identity(monkeypatch):
    task, _session, query = task_and_query("ask")
    explain = AsyncMock()
    monkeypatch.setattr(tasks, "explain_task", explain)
    await tasks.handle_task_callback(query, None, SimpleNamespace(clear=AsyncMock()))
    explain.assert_awaited_once_with(query.message, task, 7)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", ["", "  "])
async def test_unconfigured_todoist_shows_setup_without_loading(monkeypatch, api_key):
    monkeypatch.setattr(
        tasks, "get_settings", lambda: SimpleNamespace(todoist_api_key=api_key)
    )
    client = Mock()
    monkeypatch.setattr(tasks, "TodoistTasks", client)
    render = AsyncMock()
    monkeypatch.setattr(tasks, "render_dashboard", render)

    await tasks.show_tasks(None, 1, 42)

    client.assert_not_called()
    render.assert_awaited_once()
    view = render.await_args.kwargs
    assert "Todoist не подключён" in view["text"]
    assert "TODOIST_API_KEY" in view["text"]
    assert ".env" in view["text"]
    assert "перезапустите" in view["text"]
    assert view["preferred_message_id"] == 42
    assert [
        button.callback_data
        for row in view["keyboard"].inline_keyboard
        for button in row
    ] == ["menu:home"]
    session = dashboard.get_dashboard_session(1)
    assert session.current_screen == "tasks"
    assert session.task_items == []
    assert not session.task_busy


@pytest.mark.asyncio
async def test_loading_error_does_not_look_like_empty_task_list(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="example-token",
        ),
    )
    monkeypatch.setattr(
        tasks,
        "TodoistTasks",
        lambda *a: SimpleNamespace(
            today=Mock(side_effect=TodoistTaskError("Offline")),
        ),
    )
    render = AsyncMock()
    monkeypatch.setattr(tasks, "render_dashboard", render)
    await tasks.show_tasks(None, 1, 42)
    assert "Не удалось загрузить" in render.await_args.kwargs["text"]
    assert [
        button.callback_data
        for row in render.await_args.kwargs["keyboard"].inline_keyboard
        for button in row
    ] == ["menu:tasks", "menu:home"]
    assert not dashboard.get_dashboard_session(1).task_busy


@pytest.mark.asyncio
async def test_task_explanation_requests_light_model_and_saves_conversation(
    tmp_path, monkeypatch
):
    settings = SimpleNamespace(
        vault_path=tmp_path,
        todoist_api_key="",
        ai_cli="codex",
        owner_full_name="",
        content_language="ru",
        openai_api_key="",
        openai_base_url="",
        openai_model="",
        tavily_api_key="",
        recall_planner_disable_thinking=False,
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(
        tasks,
        "TodoistTasks",
        lambda *a: SimpleNamespace(
            details=Mock(
                return_value={"task": {"id": "example", "content": "Example task"}}
            ),
        ),
    )
    answer = Mock(return_value={"report": "Task explanation", "artifact_paths": []})
    monkeypatch.setattr(
        tasks, "CliProcessor", lambda *a: SimpleNamespace(answer_question=answer)
    )
    monkeypatch.setattr(
        tasks,
        "answer_text",
        AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
    )
    final = AsyncMock(return_value=SimpleNamespace(message_id=77))
    monkeypatch.setattr(tasks, "answer_rich_text", final)
    monkeypatch.setattr(tasks, "answer_files", AsyncMock(return_value=[]))
    save = Mock()
    monkeypatch.setattr(tasks, "save_answer", save)
    task = TodoistTask("example", "Example task", "2026-10-21")
    message = SimpleNamespace()
    await tasks.explain_task(message, task, 7)
    assert answer.call_args.kwargs == {"light": True}
    assert answer.call_args.args[1] == 7
    assert '"id": "example"' in answer.call_args.args[0]
    assert "Task explanation" in final.await_args.args[1]
    assert save.call_args.args[3] == 7


def test_question_light_option_reaches_cli_runner(tmp_path, monkeypatch):
    processor = CliProcessor(tmp_path)
    monkeypatch.setattr(processor, "_capture_creative_recall", lambda **k: None)
    monkeypatch.setattr(processor, "_build_question_answer_prompt", lambda *a: "prompt")
    monkeypatch.setattr(
        processor, "_inject_question_context_blocks", lambda p, *a, **k: p
    )
    monkeypatch.setattr(
        processor, "_append_question_provenance", lambda output, q: output
    )
    monkeypatch.setattr(processor, "_file_output_artifact_if_useful", lambda **k: None)
    run = Mock(return_value="Task explanation")
    monkeypatch.setattr(processor._assistant_runner, "run", run)
    result = processor.answer_question("Example question", 7, light=True)
    assert "error" not in result
    assert run.call_args.kwargs["light"] is True


def test_task_retrieval_planner_also_uses_light_model(tmp_path, monkeypatch):
    processor = CliProcessor(tmp_path)
    run = Mock(return_value="retrieval plan")
    monkeypatch.setattr(processor._assistant_runner, "run", run)

    def recall(vault, *, run_prompt, **kwargs):
        return run_prompt("plan retrieval")

    monkeypatch.setattr(processor_module, "build_qmd_recall_block", recall)
    assert (
        processor._build_auto_recall_block(
            "Example task", purpose="question_answer", light=True
        )
        == "retrieval plan"
    )
    assert run.call_args.kwargs["light"] is True


@pytest.mark.asyncio
async def test_show_tasks_groups_today_then_overdue_and_sorts_priorities(
    tmp_path, monkeypatch
):
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    items = [
        TodoistTask("today-p2", "Example", now.date().isoformat(), "p2"),
        TodoistTask("late-p4", "Example", "2020-01-01", "p4"),
        TodoistTask("today-p1", "Example", now.date().isoformat(), "p1"),
        TodoistTask("late-p1", "Example", "2020-01-01", "p1"),
    ]
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="example-token",
        ),
    )
    monkeypatch.setattr(
        tasks,
        "TodoistTasks",
        lambda *a: SimpleNamespace(
            today=Mock(return_value=("Europe/Moscow", items)),
        ),
    )
    monkeypatch.setattr(tasks, "render_dashboard", AsyncMock())
    rendered = AsyncMock()
    monkeypatch.setattr(tasks, "render_tasks", rendered)
    await tasks.show_tasks(None, 1, 42)
    session = dashboard.get_dashboard_session(1)
    assert [task.id for task in session.task_items] == [
        "today-p1",
        "today-p2",
        "late-p1",
        "late-p4",
    ]
    assert session.task_timezone == "Europe/Moscow"
    rendered.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_opens_task_list_and_clears_input_state(monkeypatch):
    from d_brain.bot.handlers import menu

    _task, _session, query = task_and_query()
    query.data = "menu:tasks"
    monkeypatch.setattr(menu, "get_settings", lambda: SimpleNamespace())
    show = AsyncMock()
    monkeypatch.setattr(menu, "show_tasks", show)
    state = SimpleNamespace(clear=AsyncMock())
    await menu.handle_menu_callback(query, None, state)
    show.assert_awaited_once_with(None, 1, 42)
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_screen_preserves_actions_when_rich_is_rejected():
    from aiogram.exceptions import TelegramBadRequest

    task, session, _query = task_and_query()

    async def edit(**kwargs):
        if "rich_message" in kwargs:
            raise TelegramBadRequest(
                method="editMessageText", message="Unsupported rich"
            )

    bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=edit))
    await tasks.render_tasks(bot, session)
    plain = bot.edit_message_text.await_args.kwargs
    assert "rich_message" not in plain
    assert plain["message_id"] == 42
    callbacks = [
        button.callback_data
        for row in plain["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"tasks:complete:{task.id}:{task.fingerprint}" in callbacks
    assert f"tasks:ask:{task.id}:{task.fingerprint}" in callbacks


@pytest.mark.asyncio
async def test_loading_does_not_reopen_menu_after_user_closes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            todoist_api_key="example-token",
        ),
    )

    def load():
        dashboard.clear_dashboard_session(1)
        return "UTC", []

    monkeypatch.setattr(tasks, "TodoistTasks", lambda *a: SimpleNamespace(today=load))
    monkeypatch.setattr(tasks, "render_dashboard", AsyncMock())
    render = AsyncMock()
    monkeypatch.setattr(tasks, "render_tasks", render)
    await tasks.show_tasks(None, 1, 42)
    render.assert_not_awaited()
