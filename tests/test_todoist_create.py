from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from d_brain.services import todoist_tasks
from d_brain.services.todoist_tasks import (
    TodoistCreateStatus,
    create_todoist_tasks,
    task_identity,
    tasks_fingerprint,
)

TASKS = [{"content": "Позвонить", "priority": 2, "due_hint": "завтра"}]


class _Catalog:
    def get_catalog(self, *, force_refresh: bool = False) -> dict[str, Any]:
        return {"available": False, "catalog": None, "errors": []}


class _Router:
    def route_task(self, task: Any, **kwargs: Any) -> dict[str, Any]:
        return {}


def _create(tmp_path: Path, tasks: list[dict[str, Any]] | None = None, **kw: Any):
    params: dict[str, Any] = {
        "vault_path": tmp_path,
        "api_key": "token",
        "extra_env": {"TODOIST_API_KEY": "token"},
        "project_catalog": _Catalog(),
        "project_router": _Router(),
    }
    params.update(kw)
    return create_todoist_tasks(TASKS if tasks is None else tasks, **params)


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    monkeypatch.setattr(todoist_tasks.time, "sleep", lambda _: None)
    return []


def _patch(monkeypatch: pytest.MonkeyPatch, runs: list, outcomes: list) -> None:
    def fake(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        runs.append(command)
        outcome = outcomes[min(len(runs), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        code, out = outcome
        return subprocess.CompletedProcess(command, code, out, "boom")

    monkeypatch.setattr(todoist_tasks, "run_in_process_group", fake)


OK = (0, '{"tasks": [{"id": "42", "content": "Позвонить"}]}')


def test_created(tmp_path, monkeypatch, runs) -> None:
    _patch(monkeypatch, runs, [OK])
    result = _create(tmp_path)
    assert result.status is TodoistCreateStatus.CREATED
    assert result.created == [{"id": "42", "content": "Позвонить"}]


def test_timeout_is_unconfirmed_and_not_retried(tmp_path, monkeypatch, runs) -> None:
    _patch(monkeypatch, runs, [subprocess.TimeoutExpired("mcp-cli", 300)])
    assert _create(tmp_path).status is TodoistCreateStatus.UNCONFIRMED
    assert len(runs) == 1


def test_launch_error_is_retried_three_times_then_failed(
    tmp_path, monkeypatch, runs
) -> None:
    _patch(monkeypatch, runs, [FileNotFoundError("mcp-cli")])
    assert _create(tmp_path).status is TodoistCreateStatus.FAILED
    assert len(runs) == 3


def test_nonzero_exit_retried_until_success(tmp_path, monkeypatch, runs) -> None:
    _patch(monkeypatch, runs, [(1, ""), (1, ""), OK])
    assert _create(tmp_path).status is TodoistCreateStatus.CREATED
    assert len(runs) == 3


def test_negative_returncode_is_unconfirmed(tmp_path, monkeypatch, runs) -> None:
    _patch(monkeypatch, runs, [(-9, "")])
    assert _create(tmp_path).status is TodoistCreateStatus.UNCONFIRMED
    assert len(runs) == 1


def test_malformed_success_output_is_unconfirmed(tmp_path, monkeypatch, runs) -> None:
    _patch(monkeypatch, runs, [(0, "not json")])
    assert _create(tmp_path).status is TodoistCreateStatus.UNCONFIRMED


def test_success_without_ids_is_unconfirmed(tmp_path, monkeypatch, runs) -> None:
    _patch(monkeypatch, runs, [(0, '{"tasks": []}')])
    assert _create(tmp_path).status is TodoistCreateStatus.UNCONFIRMED


def test_is_error_payload_is_failed_and_retried(tmp_path, monkeypatch, runs) -> None:
    _patch(monkeypatch, runs, [(0, '{"isError": true, "content": "bad"}')])
    result = _create(tmp_path)
    assert result.status is TodoistCreateStatus.FAILED
    assert len(runs) == 3


def test_no_key_returns_no_key_without_subprocess(tmp_path, monkeypatch, runs) -> None:
    _patch(monkeypatch, runs, [OK])
    assert _create(tmp_path, api_key="").status is TodoistCreateStatus.NO_KEY
    assert runs == []


def test_empty_content_is_empty(tmp_path, monkeypatch, runs) -> None:
    _patch(monkeypatch, runs, [OK])
    assert _create(tmp_path, [{"content": "  "}]).status is TodoistCreateStatus.EMPTY
    assert runs == []


def test_env_is_allowlisted(tmp_path, monkeypatch, runs) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "leak-me")
    monkeypatch.setenv("PATH", "/usr/bin")
    captured: dict[str, str] = {}

    def fake(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, OK[1], "")

    monkeypatch.setattr(todoist_tasks, "run_in_process_group", fake)
    _create(tmp_path)
    assert "TELEGRAM_BOT_TOKEN" not in captured
    assert captured["TODOIST_API_KEY"] == "token"


def test_fingerprint_is_stable_and_matches_legacy() -> None:
    tasks = [
        {
            "content": "Купить молоко",
            "due_hint": "завтра",
            "priority": 2,
            "evidence": "x",
        }
    ]
    assert tasks_fingerprint(tasks) == (
        "4416b1fee45f482a128570b893c5e9ed41d18d37c4f649da60d8eb0b7e6905f0"
    )


def test_task_identity_ignores_priority_and_case() -> None:
    a = {"content": "Купить  Молоко", "due_hint": "завтра", "priority": 1}
    b = {"content": "купить молоко", "due_hint": "завтра", "priority": 4, "x": 1}
    assert task_identity(a) == task_identity(b)
    assert task_identity(a) != task_identity({**a, "due_hint": ""})


def test_add_tasks_literal_appears_once_in_src() -> None:
    root = Path(todoist_tasks.__file__).resolve().parents[1]
    hits = [
        path.name
        for path in root.rglob("*.py")
        for _ in range(path.read_text(encoding="utf-8").count('"add-tasks"'))
    ]
    assert hits == ["todoist_tasks.py"]


def test_partial_ids_leave_rest_unconfirmed(tmp_path, monkeypatch, runs) -> None:
    tasks = [{"content": "A"}, {"content": "B"}, {"content": "C"}]
    # Matched by content, not by position: "B" has no id.
    _patch(
        monkeypatch,
        runs,
        [(0, '{"tasks": [{"id": "1", "content": "A"}, {"id": "3", "content": "C"}]}')],
    )
    result = _create(tmp_path, tasks)
    assert result.status is TodoistCreateStatus.CREATED
    assert [item["id"] for item in result.created] == ["1", "3"]
    assert result.unconfirmed == [{"content": "B"}]


def test_partial_ids_without_content_match_by_remainder(
    tmp_path, monkeypatch, runs
) -> None:
    tasks = [{"content": "A"}, {"content": "B"}, {"content": "C"}]
    _patch(monkeypatch, runs, [(0, '{"tasks": [{"id": "1"}, {"id": "2"}]}')])
    result = _create(tmp_path, tasks)
    assert [item["content"] for item in result.created] == ["A", "B"]
    assert result.unconfirmed == [{"content": "C"}]


def test_identity_distinguishes_project_hint() -> None:
    a = {"content": "Позвонить", "project_hint": "Альфа"}
    b = {"content": "Позвонить", "project_hint": "Бета"}
    assert task_identity(a) != task_identity(b)
    assert task_identity(a) == task_identity({**a, "priority": 4})
