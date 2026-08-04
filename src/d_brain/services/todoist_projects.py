"""Todoist project catalog and routing helpers."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from d_brain.services.cli_runner import CliRunner
from d_brain.services.json_normalizer import extract_first_json_dict

logger = logging.getLogger(__name__)

TODOIST_PROJECTS_LIMIT = 200
TODOIST_PROJECT_ROUTING_TIMEOUT = 180
TODOIST_PROJECT_CATALOG_TTL_SECONDS = 3600


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


class TodoistProjectCatalog:
    """Read Todoist projects from MCP and cache them inside the vault."""

    def __init__(self, vault_path: Path | str, *, todoist_api_key: str = "") -> None:
        self.vault_path = Path(vault_path)
        self.todoist_api_key = todoist_api_key

    @property
    def _cache_path(self) -> Path:
        return self.vault_path / ".sync" / "todoist-projects.json"

    def build_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.todoist_api_key:
            env["TODOIST_API_KEY"] = self.todoist_api_key
        mcp_config_path = (self.vault_path.parent / "mcp-config.json").resolve()
        if mcp_config_path.exists():
            env["MCP_CONFIG_PATH"] = str(mcp_config_path)
        return env

    def load_cached(self) -> dict[str, Any] | None:
        if not self._cache_path.exists():
            return None
        try:
            payload: object = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Invalid Todoist project cache: %s", self._cache_path)
            return None
        if not isinstance(payload, dict):
            logger.warning("Invalid Todoist project cache: %s", self._cache_path)
            return None
        return payload

    def _is_stale(self, payload: dict[str, Any] | None) -> bool:
        if not payload:
            return True
        fetched_at = _safe_text(payload.get("fetched_at"))
        if not fetched_at:
            return True
        try:
            fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return (datetime.now(UTC) - fetched).total_seconds() > (
            TODOIST_PROJECT_CATALOG_TTL_SECONDS
        )

    def refresh(self) -> dict[str, Any]:
        payload = {"limit": TODOIST_PROJECTS_LIMIT}
        result = subprocess.run(
            [
                "mcp-cli",
                "call",
                "todoist",
                "find-projects",
                json.dumps(payload, ensure_ascii=False),
            ],
            cwd=self.vault_path,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **self.build_env()},
        )
        if result.returncode != 0:
            error_text = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(error_text or "Todoist find-projects failed")

        parsed = extract_first_json_dict(
            result.stdout,
            error_context="Todoist MCP output",
        )
        structured = parsed.get("structuredContent")
        if isinstance(structured, dict):
            projects_raw = structured.get("projects", [])
        else:
            projects_raw = parsed.get("projects", [])

        projects: list[dict[str, Any]] = []
        inbox_project_id = ""
        for item in projects_raw:
            if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                continue
            normalized: dict[str, Any] = {
                "id": str(item["id"]),
                "name": str(item["name"]),
                "inboxProject": bool(item.get("inboxProject")),
                "color": _safe_text(item.get("color")),
                "isFavorite": bool(item.get("isFavorite")),
                "childOrder": int(item.get("childOrder", 0) or 0),
            }
            if normalized["inboxProject"] and not inbox_project_id:
                inbox_project_id = normalized["id"]
            projects.append(normalized)

        projects.sort(key=lambda item: (item["childOrder"], item["name"].lower()))
        catalog = {
            "fetched_at": _utc_now_iso(),
            "inbox_project_id": inbox_project_id,
            "projects": projects,
        }
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return catalog

    def get_catalog(self, *, force_refresh: bool = False) -> dict[str, Any]:
        cached = self.load_cached()
        errors: list[str] = []
        refreshed = False

        if force_refresh or self._is_stale(cached):
            try:
                cached = self.refresh()
                refreshed = True
            except Exception as exc:
                errors.append(str(exc))

        available = bool(cached and isinstance(cached.get("projects"), list))
        return {
            "available": available,
            "refreshed": refreshed,
            "catalog": cached
            if available
            else {"fetched_at": None, "inbox_project_id": "", "projects": []},
            "errors": errors,
        }


class TodoistProjectRouter:
    """Route a task to the best Todoist project using the live project catalog."""

    def __init__(
        self,
        vault_path: Path | str,
        *,
        ai_cli: str = "claude",
        todoist_api_key: str = "",
        catalog: TodoistProjectCatalog | None = None,
    ) -> None:
        self.vault_path = Path(vault_path)
        self.todoist_api_key = todoist_api_key
        self.runner = CliRunner(self.vault_path, ai_cli)
        self.catalog = catalog or TodoistProjectCatalog(
            self.vault_path,
            todoist_api_key=todoist_api_key,
        )

    def _load_reference(self) -> str:
        ref_path = (
            self.vault_path.parent
            / "skills/dbrain-processor/references/todoist-project-routing.md"
        )
        if ref_path.exists():
            return ref_path.read_text(encoding="utf-8")
        return ""

    def _project_lookup(self, catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
        lookup: dict[str, dict[str, Any]] = {}
        for project in catalog.get("projects", []):
            if not isinstance(project, dict):
                continue
            project_id = _safe_text(project.get("id"))
            project_name = _safe_text(project.get("name"))
            if project_id:
                lookup[project_id] = project
            if project_name:
                lookup[project_name.lower()] = project
        return lookup

    def _deterministic_hint_match(
        self,
        *,
        project_hint: str,
        catalog: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not project_hint:
            return None
        lookup = self._project_lookup(catalog)
        matched = lookup.get(project_hint.lower()) or lookup.get(project_hint)
        if not matched:
            return None
        return {
            "project_id": matched["id"],
            "confidence": "high",
            "reason": "exact project_hint match",
        }

    def _build_prompt(
        self,
        *,
        task: dict[str, Any],
        source_context: str,
        catalog: dict[str, Any],
    ) -> str:
        reference = self._load_reference()
        return (
            "Choose the Todoist project for one task candidate. "
            "Return only JSON.\n\n"
            "=== ROUTING REFERENCE ===\n"
            f"{reference}\n"
            "=== END ROUTING REFERENCE ===\n\n"
            "[TASK]\n"
            f"{json.dumps(task, ensure_ascii=False, indent=2)}\n\n"
            "[SOURCE_CONTEXT]\n"
            f"{source_context.strip()}\n\n"
            "[TODOIST_PROJECT_CATALOG]\n"
            f"{json.dumps(catalog, ensure_ascii=False, indent=2)}\n"
        )

    def route_task(
        self,
        task: dict[str, Any],
        *,
        source_context: str = "",
        catalog: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.todoist_api_key:
            return {
                "project_id": None,
                "confidence": "none",
                "reason": "todoist-disabled",
            }

        catalog_result = None
        if catalog is None:
            catalog_result = self.catalog.get_catalog(force_refresh=True)
            catalog = catalog_result["catalog"]
            if not catalog_result["available"]:
                return {
                    "project_id": None,
                    "confidence": "none",
                    "reason": "project-catalog-unavailable",
                }

        project_hint = _safe_text(task.get("project_hint"))
        deterministic = self._deterministic_hint_match(
            project_hint=project_hint,
            catalog=catalog,
        )
        if deterministic:
            return deterministic

        output = self.runner.run(
            self._build_prompt(
                task=task,
                source_context=source_context,
                catalog=catalog,
            ),
            timeout=TODOIST_PROJECT_ROUTING_TIMEOUT,
            extra_env=self.catalog.build_env(),
        )
        verdict = extract_first_json_dict(
            output,
            error_context="Todoist routing output",
        )
        project_id = _safe_text(verdict.get("project_id"))
        confidence = _safe_text(verdict.get("confidence")).lower() or "none"
        reason = _safe_text(verdict.get("reason"))
        valid_ids = {
            _safe_text(project.get("id"))
            for project in catalog.get("projects", [])
            if isinstance(project, dict)
        }
        if project_id.lower() in {"", "inbox", "none", "default"}:
            return {"project_id": None, "confidence": confidence, "reason": reason}
        if project_id not in valid_ids:
            return {
                "project_id": None,
                "confidence": "none",
                "reason": "router returned unknown project id",
            }
        if confidence == "low":
            return {"project_id": None, "confidence": confidence, "reason": reason}
        return {"project_id": project_id, "confidence": confidence, "reason": reason}
