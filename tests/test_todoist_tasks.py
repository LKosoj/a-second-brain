import json
import subprocess
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from d_brain.services.todoist_tasks import TodoistTask, TodoistTaskError, TodoistTasks


def test_today_includes_unassigned_tasks_and_reads_all_pages(tmp_path, monkeypatch):
    client = TodoistTasks(tmp_path)
    calls = []

    def call(tool, args):
        calls.append((tool, dict(args)))
        if tool == "user-info":
            return {"timezone": "Europe/Moscow"}
        if "cursor" not in args:
            return {
                "tasks": [
                    {
                        "id": "first",
                        "content": "Review the example plan",
                        "dueDate": "2026-10-20",
                        "priority": "p1",
                        "recurring": False,
                    }
                ],
                "hasMore": True,
                "nextCursor": "second-page",
            }
        return {
            "tasks": [
                {
                    "id": "second",
                    "content": "Contact Example Ltd",
                    "priority": "p2",
                    "dueDate": "2026-10-21T12:00:00Z",
                    "recurring": "every day",
                },
                {"id": "done", "content": "Done", "checked": True},
            ],
            "hasMore": False,
        }

    monkeypatch.setattr(client, "_call", call)
    timezone, tasks = client.today()
    assert timezone == "Europe/Moscow"
    assert [task.id for task in tasks] == ["first", "second"]
    assert tasks[1].recurring
    assert calls[1][1]["responsibleUserFiltering"] == "unassignedOrMe"
    assert "responsibleUser" not in calls[1][1]
    assert calls[1][1]["overdueOption"] == "include-overdue"
    assert calls[2][1]["cursor"] == "second-page"


@pytest.mark.parametrize(
    ("due", "overdue"),
    [
        ("2026-10-20", True),
        ("2026-10-21", False),
        ("2026-10-21T09:00:00Z", True),
        ("2026-10-21T11:00:00Z", False),
        ("2026-10-21T12:00:00", True),
        ("", False),
    ],
)
def test_overdue_respects_account_timezone_and_date_only_deadlines(due, overdue):
    now = datetime(2026, 10, 21, 13, tzinfo=ZoneInfo("Europe/Moscow"))
    assert TodoistTask("example", "Example task", due).overdue(now) is overdue


def test_recurring_task_with_changed_due_is_not_completed(tmp_path, monkeypatch):
    client = TodoistTasks(tmp_path)
    call = Mock(return_value={"object": {"dueDate": "2026-10-22", "checked": False}})
    monkeypatch.setattr(client, "_call", call)
    with pytest.raises(TodoistTaskError, match="Срок задачи изменился"):
        client.complete(TodoistTask("example", "Example task", "2026-10-21"))
    assert call.call_count == 1
    assert call.call_args.args[0] == "fetch-object"


@pytest.mark.parametrize("already_done", [False, True])
def test_completion_requires_confirmation_and_skips_already_done(
    tmp_path, monkeypatch, already_done
):
    client = TodoistTasks(tmp_path)
    call = Mock(
        side_effect=[
            {"object": {"dueDate": "2026-10-21", "checked": already_done}},
            {"completed": ["example"]},
        ]
    )
    monkeypatch.setattr(client, "_call", call)
    client.complete(TodoistTask("example", "Example task", "2026-10-21"))
    assert call.call_count == (1 if already_done else 2)


def test_failed_completion_is_not_reported_as_success(tmp_path, monkeypatch):
    client = TodoistTasks(tmp_path)
    monkeypatch.setattr(
        client,
        "_call",
        Mock(
            side_effect=[
                {"object": {"dueDate": "2026-10-21"}},
                {"completed": [], "failures": [{"item": "example", "error": "denied"}]},
            ]
        ),
    )
    with pytest.raises(TodoistTaskError, match="не подтвердил"):
        client.complete(TodoistTask("example", "Example task", "2026-10-21"))


def test_mutation_timeout_is_not_retried_but_reads_are(tmp_path, monkeypatch):
    client = TodoistTasks(tmp_path)
    run = Mock(side_effect=subprocess.TimeoutExpired("mcp-cli", 30))
    monkeypatch.setattr("d_brain.services.todoist_tasks.subprocess.run", run)
    with pytest.raises(TodoistTaskError):
        client._call("complete-tasks", {"ids": ["example"]})
    assert run.call_count == 1
    run.reset_mock()
    with pytest.raises(TodoistTaskError):
        client._call("user-info", {})
    assert run.call_count == 3


def test_runtime_mcp_config_is_preserved_and_structured_result_used(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_CONFIG_PATH", "/fictional/instance/mcp-config.json")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-reach-mcp")
    (tmp_path / "vault").mkdir()
    (tmp_path / "mcp-config.json").write_text("{}")
    client = TodoistTasks(tmp_path / "vault", "example-key")
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "structuredContent": {"timezone": "Europe/Moscow"},
                }
            ),
        )
    )
    monkeypatch.setattr("d_brain.services.todoist_tasks.subprocess.run", run)
    assert client._call("user-info", {}) == {"timezone": "Europe/Moscow"}
    env = run.call_args.kwargs["env"]
    assert env["MCP_CONFIG_PATH"] == "/fictional/instance/mcp-config.json"
    assert env["TODOIST_API_KEY"] == "example-key"
    assert "TELEGRAM_BOT_TOKEN" not in env


def test_mcp_error_with_zero_exit_is_an_error(tmp_path, monkeypatch):
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "isError": True,
                    "content": [{"type": "text", "text": "Access denied"}],
                }
            ),
        )
    )
    monkeypatch.setattr("d_brain.services.todoist_tasks.subprocess.run", run)
    with pytest.raises(TodoistTaskError, match="Access denied"):
        TodoistTasks(tmp_path)._call("user-info", {})
    assert run.call_count == 3


def test_completion_fingerprint_changes_with_occurrence():
    a = TodoistTask("example", "Example task", "2026-10-21", recurring=True)
    b = TodoistTask("example", "Example task", "2026-10-22", recurring=True)
    assert a.fingerprint != b.fingerprint
