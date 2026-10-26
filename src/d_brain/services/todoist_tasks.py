"""Todoist tasks for the bot's interactive daily list."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from d_brain.services.cli_runner import build_subprocess_env
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.subprocess_group import run_in_process_group
from d_brain.services.todoist_projects import (
    TodoistProjectCatalog,
    TodoistProjectRouter,
)

logger = logging.getLogger(__name__)

# Between the old 30 s (too short to retry safely) and 3600 s (a hung call
# would block the whole nightly cycle).
TODOIST_ADD_TASKS_TIMEOUT_SECONDS = 300
TODOIST_ADD_TASKS_ATTEMPTS = 3


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
    completed_at: datetime | None = None

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
        attempts = 1 if tool in {"complete-tasks", "uncomplete-tasks"} else 3
        error = "Todoist returned no result"
        for _attempt in range(attempts):
            try:
                result = run_in_process_group(
                    [
                        "mcp-cli",
                        "call",
                        "todoist",
                        tool,
                        json.dumps(args, ensure_ascii=False),
                    ],
                    cwd=self.vault_path,
                    env=self.env,
                    timeout=30,
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

    def reopen(self, task: TodoistTask) -> None:
        current = self._call("fetch-object", {"type": "task", "id": task.id})["object"]
        if not current.get("checked"):
            return
        try:
            result = self._call("uncomplete-tasks", {"ids": [task.id]})
        except TodoistTaskError as exc:
            raise TodoistTaskError(
                "Не удалось вернуть задачу. Обновите список перед повтором."
            ) from exc
        if task.id not in result.get("uncompleted", []):
            raise TodoistTaskError(
                "Todoist не подтвердил возврат. Обновите список перед повтором."
            )

    def details(self, task_id: str) -> dict[str, Any]:
        task = self._call("fetch-object", {"type": "task", "id": task_id})["object"]
        comments = self._call("find-comments", {"taskId": task_id, "limit": 10})
        return {"task": task, "comments": comments}


class TodoistCreateStatus(StrEnum):
    CREATED = "created"
    # The request may have reached Todoist; repeating it could duplicate tasks.
    UNCONFIRMED = "unconfirmed"
    FAILED = "failed"
    NO_KEY = "no_key"
    EMPTY = "empty"


@dataclass(frozen=True)
class TodoistCreateResult:
    status: TodoistCreateStatus
    created: list[dict[str, str]]
    fingerprint: str
    error: str = ""
    # Sent tasks Todoist returned no id for: they may or may not exist there.
    unconfirmed: list[dict[str, Any]] = field(default_factory=list)


def tasks_fingerprint(tasks: list[dict[str, Any]]) -> str:
    # Stored in PLAUD entry state, so the algorithm must never change.
    return hashlib.sha256(
        json.dumps(tasks, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def task_identity(task: dict[str, Any]) -> str:
    """Key for "the same task": wording of priority/hints may differ per run."""
    content = " ".join(str(task.get("content") or "").split()).casefold()
    due_hint = " ".join(str(task.get("due_hint") or "").split())
    project_hint = " ".join(str(task.get("project_hint") or "").split()).casefold()
    return f"{content}|{due_hint}|{project_hint}"


def create_todoist_tasks(
    tasks: list[dict[str, Any]],
    *,
    vault_path: Path,
    api_key: str,
    extra_env: Mapping[str, str],
    project_catalog: TodoistProjectCatalog,
    project_router: TodoistProjectRouter,
    source_context: str = "",
) -> TodoistCreateResult:
    """Create routed Todoist tasks with one retry and outcome policy."""
    normalized = [
        {**task, "content": " ".join(str(task.get("content") or "").split())}
        for task in tasks
        if " ".join(str(task.get("content") or "").split())
    ]
    fingerprint = tasks_fingerprint(normalized)
    if not normalized:
        return TodoistCreateResult(TodoistCreateStatus.EMPTY, [], fingerprint)
    if not api_key:
        return TodoistCreateResult(
            TodoistCreateStatus.NO_KEY, [], fingerprint, "no Todoist API key"
        )

    catalog_result = project_catalog.get_catalog(force_refresh=True)
    catalog = catalog_result["catalog"] if catalog_result["available"] else None
    payload_tasks: list[dict[str, Any]] = []
    for task in normalized:
        try:
            priority = int(task.get("priority") or 1)
        except (TypeError, ValueError):
            priority = 1
        due_hint = " ".join(str(task.get("due_hint") or "").split())
        route = project_router.route_task(
            task, source_context=source_context, catalog=catalog
        )
        payload_task: dict[str, Any] = {
            "content": task["content"],
            "priority": f"p{max(1, min(priority, 4))}",
        }
        if due_hint:
            payload_task["dueString"] = due_hint
        if route.get("project_id"):
            payload_task["projectId"] = route["project_id"]
        payload_tasks.append(payload_task)

    command = [
        "mcp-cli",
        "call",
        "todoist",
        "add-tasks",
        json.dumps({"tasks": payload_tasks}, ensure_ascii=False),
    ]
    env = build_subprocess_env(extra_env)
    errors: list[str] = []
    for attempt in range(TODOIST_ADD_TASKS_ATTEMPTS):
        if attempt > 0:
            time.sleep(2 * attempt)
        try:
            result = run_in_process_group(
                command,
                cwd=vault_path,
                env=env,
                timeout=TODOIST_ADD_TASKS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Todoist creation timed out; external outcome is unknown")
            return TodoistCreateResult(
                TodoistCreateStatus.UNCONFIRMED, [], fingerprint, "timeout"
            )
        except OSError as exc:
            # Nothing was sent, so repeating is safe.
            errors.append(str(exc))
            continue
        if result.returncode < 0:
            logger.warning("Todoist creation was killed by a signal; outcome unknown")
            return TodoistCreateResult(
                TodoistCreateStatus.UNCONFIRMED,
                [],
                fingerprint,
                f"killed by signal {-result.returncode}",
            )
        if result.returncode > 0:
            errors.append(result.stderr.strip() or result.stdout.strip())
            continue
        try:
            parsed = extract_first_json_dict(
                result.stdout, error_context="Todoist MCP output"
            )
        except ValueError:
            logger.warning(
                "Todoist creation returned malformed success output; "
                "external outcome is unknown"
            )
            return TodoistCreateResult(
                TodoistCreateStatus.UNCONFIRMED, [], fingerprint, "malformed output"
            )
        if parsed.get("isError"):
            errors.append(str(parsed.get("content") or "Todoist reported an error"))
            continue
        task_items = parsed.get("tasks")
        if not isinstance(task_items, list):
            structured = parsed.get("structuredContent")
            if isinstance(structured, dict):
                task_items = structured.get("tasks") or structured.get("created")
        created: list[dict[str, str]] = []
        items = [i for i in task_items or [] if isinstance(i, dict) and i.get("id")]
        remaining = list(range(len(normalized)))
        matched: list[tuple[dict[str, Any], int | None]] = []
        for item in items:
            # By content first: Todoist may return fewer items or reorder them.
            text = " ".join(str(item.get("content") or "").split()).casefold()
            hit = next(
                (i for i in remaining if normalized[i]["content"].casefold() == text),
                None,
            )
            if hit is not None:
                remaining.remove(hit)
            matched.append((item, hit))
        for item, hit in matched:
            if hit is None and remaining:
                hit = remaining.pop(0)
            fallback = normalized[hit]["content"] if hit is not None else ""
            created.append(
                {
                    "id": str(item["id"]),
                    "content": str(item.get("content") or fallback).strip()
                    or fallback,
                }
            )
        unconfirmed = [normalized[i] for i in remaining]
        if created:
            if unconfirmed:
                logger.warning(
                    "Todoist returned %d ids for %d tasks; the rest are unconfirmed",
                    len(created),
                    len(normalized),
                )
            return TodoistCreateResult(
                TodoistCreateStatus.CREATED,
                created,
                fingerprint,
                unconfirmed=unconfirmed,
            )
        logger.warning(
            "Todoist creation returned no task IDs; external outcome is unknown"
        )
        return TodoistCreateResult(
            TodoistCreateStatus.UNCONFIRMED, [], fingerprint, "no task ids"
        )
    error = " | ".join(errors)
    logger.warning("Todoist creation failed after retries: %s", error)
    return TodoistCreateResult(TodoistCreateStatus.FAILED, [], fingerprint, error)
