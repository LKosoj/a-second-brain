"""Todoist tasks for the bot's interactive daily list."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from d_brain.services.cli_runner import build_subprocess_env
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.todoist_projects import TodoistProjectCatalog


class TodoistTaskError(RuntimeError):
    """A task could not be read or its completion could not be confirmed."""


@dataclass
class TodoistTask:
    id: str
    content: str
    due: str
    priority: str = "p4"
    project: str = ""
    recurring: bool = False
    completed: bool = False

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.id}:{self.due}".encode()).hexdigest()[:10]

    def overdue(self, now: datetime) -> bool:
        if not self.due:
            return False
        due = datetime.fromisoformat(self.due.replace("Z", "+00:00"))
        if "T" not in self.due:
            return due.date() < now.date()
        if due.tzinfo is None:
            due = due.replace(tzinfo=now.tzinfo)
        return due < now


class TodoistTasks:
    def __init__(self, vault_path: Path, api_key: str = "") -> None:
        self.vault_path = vault_path
        self.env = build_subprocess_env({"TODOIST_API_KEY": api_key})
        config = (vault_path.parent / "mcp-config.json").resolve()
        if config.exists():
            self.env.setdefault("MCP_CONFIG_PATH", str(config))

    def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        # Reads can be retried. A repeated completion can advance another
        # occurrence of a recurring task, so writes are sent only once.
        attempts = 1 if tool == "complete-tasks" else 3
        error = "Todoist returned no result"
        for _attempt in range(attempts):
            try:
                result = subprocess.run(
                    [
                        "mcp-cli",
                        "call",
                        "todoist",
                        tool,
                        json.dumps(args, ensure_ascii=False),
                    ],
                    cwd=self.vault_path,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode:
                    raise TodoistTaskError(
                        result.stderr.strip() or result.stdout.strip()
                    )
                payload = extract_first_json_dict(
                    result.stdout, error_context="Todoist tasks MCP output"
                )
                if payload.get("isError"):
                    raise TodoistTaskError(str(payload.get("content")))
                structured = payload.get("structuredContent")
                if isinstance(structured, dict):
                    return structured
                for block in payload.get("content", []):
                    if block.get("type") == "text":
                        try:
                            data = json.loads(block["text"])
                        except ValueError:
                            continue
                        if isinstance(data, dict):
                            return data
                return payload
            except (
                OSError,
                ValueError,
                subprocess.TimeoutExpired,
                TodoistTaskError,
            ) as exc:
                error = str(exc)
        raise TodoistTaskError(error)

    def today(self) -> tuple[str, list[TodoistTask]]:
        timezone = str(self._call("user-info", {}).get("timezone") or "UTC")
        cached = TodoistProjectCatalog(self.vault_path).load_cached() or {}
        projects = {str(p["id"]): str(p["name"]) for p in cached.get("projects", [])}
        args: dict[str, Any] = {
            "startDate": "today",
            "daysCount": 1,
            "overdueOption": "include-overdue",
            "limit": 100,
            "responsibleUserFiltering": "unassignedOrMe",
        }
        tasks: list[TodoistTask] = []
        while True:
            data = self._call("find-tasks-by-date", args)
            if not isinstance(data.get("tasks"), list):
                raise TodoistTaskError("Todoist returned no task list")
            tasks.extend(
                TodoistTask(
                    id=str(task["id"]),
                    content=str(task["content"]),
                    due=str(task.get("dueDate") or task.get("deadlineDate") or ""),
                    priority=str(task.get("priority") or "p4"),
                    project=projects.get(str(task.get("projectId")), ""),
                    recurring=bool(task.get("recurring")),
                )
                for task in data["tasks"]
                if not task.get("checked")
            )
            if not data.get("hasMore"):
                return timezone, tasks
            if not data.get("nextCursor"):
                raise TodoistTaskError("Todoist returned an incomplete task list")
            args["cursor"] = data["nextCursor"]

    def complete(self, task: TodoistTask) -> None:
        current = self._call("fetch-object", {"type": "task", "id": task.id})["object"]
        if current.get("checked"):
            return
        due = str(current.get("dueDate") or current.get("deadlineDate") or "")
        if due != task.due:
            raise TodoistTaskError("Срок задачи изменился. Обновите список.")
        try:
            result = self._call("complete-tasks", {"ids": [task.id]})
        except TodoistTaskError as exc:
            raise TodoistTaskError(
                "Не удалось подтвердить выполнение. Обновите список перед повтором."
            ) from exc
        if task.id not in result.get("completed", []):
            raise TodoistTaskError(
                "Todoist не подтвердил выполнение. Обновите список перед повтором."
            )

    def details(self, task_id: str) -> dict[str, Any]:
        task = self._call("fetch-object", {"type": "task", "id": task_id})["object"]
        comments = self._call("find-comments", {"taskId": task_id, "limit": 10})
        return {"task": task, "comments": comments}
