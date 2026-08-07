"""PLAUD sync service: import recordings, store notes, and create tasks."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import httpx

from d_brain.control_plane.contracts import WorkflowSpec
from d_brain.control_plane.registry import get_workflow
from d_brain.manifest import VaultManifest, load_manifest_for_vault
from d_brain.services.cli_runner import CliRunner
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.entry_status import (
    ENTRY_STATUS_ALREADY_PROCESSED,
    format_entry_status_comments,
)
from d_brain.services.frontmatter import (
    FrontmatterError,
    move_validated_vault_markdown,
    read_vault_file_bytes,
    read_vault_file_text,
    require_safe_vault_root,
    vault_relative_file_lock,
    write_import_vault_markdown,
    write_validated_vault_markdown,
    write_vault_file_text,
)
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.localization import normalize_language, translate
from d_brain.services.qmd import QmdService
from d_brain.services.source_links import (
    build_plaud_source_info,
    collapse_to_single_line,
)
from d_brain.services.storage import VaultStorage
from d_brain.services.todoist_projects import (
    TodoistProjectCatalog,
    TodoistProjectRouter,
)

logger = logging.getLogger(__name__)

PLAUD_TIMEOUT = 30.0
PLAUD_PAGE_SIZE = 100
PLAUD_TASK_WINDOW_DAYS = 7
PLAUD_TASK_TIMEOUT = 1200


class PlaudAuthError(RuntimeError):
    """PLAUD rejected the bearer token or session."""


class PlaudSyncAlreadyRunningError(RuntimeError):
    """Another PLAUD sync process is already holding the state lock."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _plaud_state_vault_path(path: Path) -> Path:
    state_path = Path(path).absolute()
    if state_path.name != "plaud-state.json" or state_path.parent.name != ".sync":
        raise ValueError("PLAUD state path must be vault/.sync/plaud-state.json")
    return state_path.parent.parent


@contextmanager
def plaud_state_lock(state_path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Serialize PLAUD state access across sync and alert paths."""
    path = Path(state_path).absolute()
    try:
        with vault_relative_file_lock(
            _plaud_state_vault_path(path),
            path.with_suffix(".lock"),
            blocking=blocking,
        ):
            yield
    except BlockingIOError as exc:
        raise PlaudSyncAlreadyRunningError("PLAUD sync is already running") from exc


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Persist PLAUD state JSON through the shared vault transaction."""
    state_path = Path(path).absolute()
    write_vault_file_text(
        _plaud_state_vault_path(state_path),
        state_path,
        _dump_json(dict(payload)) + "\n",
    )


def _iso_week_label(value: date) -> str:
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return _dump_json(value).strip()
    return str(value).strip()


def _content_blob_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("summary", "ai_content", "content", "text", "markdown"):
            text = _content_blob_to_text(value.get(key))
            if text:
                return text
        return _safe_text(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                content = _content_blob_to_text(
                    item.get("content") or item.get("topic") or item
                )
                if content:
                    parts.append(content)
            else:
                text = _safe_text(item)
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return _safe_text(value)


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            raw = int(stripped)
            if raw > 10_000_000_000:
                raw /= 1000
            return datetime.fromtimestamp(raw, tz=UTC)
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _clip_summary(text: str, *, limit: int = 240) -> str:
    del limit
    return re.sub(r"\s+", " ", text).strip()


def _clip_prompt_text(text: str, *, limit: int = 0) -> str:
    del limit
    return text


def _replace_rel_path_token(text: str, old_rel_path: str, new_rel_path: str) -> str:
    """Replace one vault-relative path only when it appears as a standalone token."""
    pattern = re.compile(
        rf"(?<![0-9A-Za-zА-Яа-яЁё_./-]){re.escape(old_rel_path)}(?![0-9A-Za-zА-Яа-яЁё_./-])"
    )
    return pattern.sub(new_rel_path, text)


class PlaudClient:
    """Thin PLAUD HTTP client around the reverse-engineered web endpoints."""

    def __init__(self, bearer_token: str, region: str = "api") -> None:
        self.base_url = f"https://{region}.plaud.ai"
        self._client: httpx.Client = httpx.Client(
            base_url=self.base_url,
            timeout=PLAUD_TIMEOUT,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
                "edit-from": "web",
                "app-platform": "web",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36"
                ),
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def list_recordings(
        self,
        *,
        limit: int = PLAUD_PAGE_SIZE,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        response = self._client.get(
            "/file/simple/web",
            params={
                "skip": skip,
                "limit": limit,
                "is_trash": 2,
                "sort_by": "start_time",
                "is_desc": "true",
            },
        )
        self._raise_for_status(response)
        data = response.json()
        if isinstance(data, dict):
            items = (
                data.get("data_file_list")
                or data.get("data")
                or data.get("items")
                or []
            )
        else:
            items = data
        if not isinstance(items, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            payload = dict(item)
            if payload.get("id") and not payload.get("file_id"):
                payload["file_id"] = payload["id"]
            if payload.get("filename") and not payload.get("title"):
                payload["title"] = payload["filename"]
            if payload.get("start_time") and not payload.get("record_time"):
                payload["record_time"] = payload["start_time"]
            normalized.append(payload)
        return normalized

    def iter_recordings(
        self,
        *,
        limit: int = PLAUD_PAGE_SIZE,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        skip = 0
        page = 0
        seen_page_markers: set[tuple[str, int]] = set()
        while True:
            if max_pages is not None and page >= max_pages:
                return
            items = self.list_recordings(limit=limit, skip=skip)
            if not items:
                return
            first_id = _safe_text(items[0].get("file_id") or items[0].get("id"))
            marker = (first_id, len(items))
            if first_id and marker in seen_page_markers:
                return
            if first_id:
                seen_page_markers.add(marker)
            yield from items
            page += 1
            if len(items) < limit:
                return
            skip += limit

    def get_recording(self, file_id: str) -> dict[str, Any]:
        response = self._client.get(f"/file/detail/{file_id}")
        self._raise_for_status(response)
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            detail = dict(data["data"])
        elif isinstance(data, dict):
            detail = dict(data)
        else:
            detail = {}

        detail["file_id"] = file_id
        if detail.get("file_name") and not detail.get("title"):
            detail["title"] = detail["file_name"]
        if detail.get("start_time") and not detail.get("record_time"):
            detail["record_time"] = detail["start_time"]

        preloaded = {}
        for item in detail.get("pre_download_content_list", []):
            if not isinstance(item, dict) or not item.get("data_id"):
                continue
            preloaded[str(item["data_id"])] = item.get("data_content")

        summary_text = ""
        transcript_text = ""
        for item in detail.get("content_list", []):
            if not isinstance(item, dict):
                continue
            data_id = _safe_text(item.get("data_id"))
            data_type = _safe_text(item.get("data_type"))
            content: Any = preloaded.get(data_id)
            if (
                content is None
                and item.get("data_link")
                and data_type
                in {
                    "transaction",
                    "transaction_polish",
                    "auto_sum_note",
                }
            ):
                content = self._fetch_signed_blob(str(item["data_link"]))

            if data_type == "auto_sum_note" and not summary_text:
                summary_text = _content_blob_to_text(content)
            elif data_type == "transaction_polish" and not transcript_text:
                transcript_text = _content_blob_to_text(content)
            elif data_type == "transaction" and not transcript_text:
                transcript_text = _content_blob_to_text(content)

        detail["ai_content"] = summary_text
        detail["trans_result"] = transcript_text
        return detail

    @staticmethod
    def _fetch_signed_blob(url: str) -> Any:
        response = httpx.get(url, timeout=PLAUD_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise PlaudAuthError(
                "PLAUD auth failed "
                f"({response.status_code}) for {response.request.url.path}"
            )
        response.raise_for_status()


class PlaudSyncService:
    """Import PLAUD recordings into the vault and create owner tasks."""

    CONTROL_PLANE_WORKFLOW = "integration.plaud.sync"

    def __init__(
        self,
        vault_path: Path,
        *,
        bearer_token: str,
        region: str = "api",
        ai_cli: str = "claude",
        todoist_api_key: str = "",
        owner_full_name: str = "",
        content_language: str = "ru",
        client: PlaudClient | None = None,
    ) -> None:
        self.vault_path = Path(vault_path).absolute()
        self.content_language = normalize_language(content_language)
        self._manifest: VaultManifest | None = None
        self.storage = VaultStorage(self.vault_path, self.content_language)
        self.client = client or PlaudClient(bearer_token, region)
        self.ai_cli = ai_cli
        self.todoist_api_key = todoist_api_key
        self.owner_full_name = owner_full_name.strip()
        self.runner = CliRunner(self.vault_path, ai_cli)
        self.project_catalog = TodoistProjectCatalog(
            self.vault_path,
            todoist_api_key=todoist_api_key,
        )
        self.project_router = TodoistProjectRouter(
            self.vault_path,
            ai_cli=ai_cli,
            todoist_api_key=todoist_api_key,
            catalog=self.project_catalog,
        )
        self._owns_client = client is None
        self._workflow = self._load_control_plane_workflow()

    @classmethod
    def _load_control_plane_workflow(cls) -> WorkflowSpec:
        workflow = get_workflow(cls.CONTROL_PLANE_WORKFLOW)
        expected_entrypoint = f"{cls.__module__}.{cls.__name__}.sync"
        if workflow.entrypoint != expected_entrypoint:
            raise ValueError(
                "Control-plane registry drift for PLAUD sync: "
                f"expected {expected_entrypoint}, got {workflow.entrypoint}"
            )
        return workflow

    @property
    def workflow_name(self) -> str:
        """Canonical control-plane workflow name for this service."""
        return self._workflow.name

    def _manifest_for_writes(self) -> VaultManifest:
        require_safe_vault_root(self.vault_path)
        if self._manifest is None:
            self._manifest = load_manifest_for_vault(self.vault_path)
        return self._manifest

    def close(self) -> None:
        """Close the HTTP client if we own it."""
        if self._owns_client:
            self.client.close()

    @property
    def _state_path(self) -> Path:
        return self.vault_path / ".sync" / "plaud-state.json"

    @property
    def _raw_root(self) -> Path:
        return self.vault_path / "imports" / "plaud" / "raw"

    @property
    def _notes_root(self) -> Path:
        return self.vault_path / "imports" / "plaud" / "notes"

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(
                read_vault_file_text(self.vault_path, self._state_path)
            )
        except FileNotFoundError:
            return {"recordings": {}, "dirty_weeks": [], "last_sync_at": None}
        except json.JSONDecodeError:
            logger.warning("Invalid PLAUD state file, starting fresh")
            return {"recordings": {}, "dirty_weeks": [], "last_sync_at": None}
        payload.setdefault("recordings", {})
        payload.setdefault("dirty_weeks", [])
        payload.setdefault("last_sync_at", None)
        return cast(dict[str, Any], payload)

    def _save_state(self, payload: Mapping[str, Any]) -> None:
        write_json_atomic(self._state_path, payload)

    def _load_reference(self) -> str:
        ref_path = (
            self.vault_path.parent / "skills/dbrain-processor/references/plaud.md"
        )
        if ref_path.exists():
            reference = ref_path.read_text(encoding="utf-8")
            return reference.replace(
                "{OWNER_FULL_NAME}",
                self.owner_full_name or "assistant owner",
            )
        return ""

    def _cli_extra_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.todoist_api_key:
            env["TODOIST_API_KEY"] = self.todoist_api_key
        mcp_config_path = (self.vault_path.parent / "mcp-config.json").resolve()
        if mcp_config_path.exists():
            env["MCP_CONFIG_PATH"] = str(mcp_config_path)
        return env

    def _run_prompt(self, prompt: str) -> str:
        return self.runner.run(
            prompt,
            timeout=PLAUD_TASK_TIMEOUT,
            extra_env=self._cli_extra_env(),
        )

    def _recorded_at(self, payload: Mapping[str, Any]) -> datetime:
        for key in ("record_time", "create_time", "created_at", "updated_at"):
            parsed = _parse_timestamp(payload.get(key))
            if parsed is not None:
                return parsed.astimezone()
        return _utc_now().astimezone()

    def _file_paths(self, file_id: str, recorded_at: datetime) -> tuple[Path, Path]:
        year_dir = f"{recorded_at:%Y}"
        month_dir = f"{recorded_at:%m}"
        note_name = f"{recorded_at:%Y-%m-%d-%H%M%S}-{file_id}.md"
        raw_name = f"{file_id}.json"
        return (
            self._notes_root / year_dir / month_dir / note_name,
            self._raw_root / year_dir / month_dir / raw_name,
        )

    def _find_note_candidates(self, file_id: str) -> list[Path]:
        return sorted(self._notes_root.rglob(f"*{file_id}.md"))

    def _rewrite_note_path_references(self, replacements: Mapping[str, str]) -> int:
        if not replacements:
            return 0

        updated_files = 0
        skipped_roots = {
            self._raw_root.resolve(),
            (self.vault_path / ".qmd").resolve(),
        }

        for path in self.vault_path.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".md", ".json"}:
                continue
            if any(root == path or root in path.parents for root in skipped_roots):
                continue

            content_bytes = read_vault_file_bytes(self.vault_path, path)
            content = content_bytes.decode("utf-8")
            updated = content
            for old_rel_path, new_rel_path in replacements.items():
                updated = _replace_rel_path_token(
                    updated,
                    old_rel_path,
                    new_rel_path,
                )
            if updated != content:
                if path.suffix == ".md":
                    write_validated_vault_markdown(
                        self.vault_path,
                        path,
                        updated.encode("utf-8"),
                        manifest=self._manifest_for_writes(),
                        expected_full_sha256=sha256(content_bytes).hexdigest(),
                    )
                else:
                    write_vault_file_text(
                        self.vault_path,
                        path,
                        updated,
                        expected_full_sha256=sha256(content_bytes).hexdigest(),
                    )
                updated_files += 1
        return updated_files

    def migrate_note_path_drift(self, *, refresh_qmd: bool = True) -> dict[str, Any]:
        """Reconcile historical PLAUD notes with current local-time path rules."""
        self._manifest_for_writes()
        with plaud_state_lock(self._state_path, blocking=False):
            state = self._load_state()
            replacements: dict[str, str] = {}
            counters = {
                "checked": 0,
                "migrated": 0,
                "already_canonical": 0,
                "missing_notes": 0,
                "state_updates": 0,
                "daily_updates": 0,
                "reference_updates": 0,
            }
            dirty_weeks = set(state.get("dirty_weeks", []))

            for raw_path in sorted(self._raw_root.rglob("*.json")):
                detail = json.loads(read_vault_file_text(self.vault_path, raw_path))
                file_id = _safe_text(detail.get("file_id") or detail.get("id"))
                if not file_id:
                    continue

                counters["checked"] += 1
                recorded_at = self._recorded_at(detail)
                canonical_note_path, _ = self._file_paths(file_id, recorded_at)
                note_candidates = self._find_note_candidates(file_id)
                if not note_candidates:
                    counters["missing_notes"] += 1
                    continue

                existing_note_path = (
                    canonical_note_path
                    if canonical_note_path in note_candidates
                    else note_candidates[0]
                )
                if existing_note_path != canonical_note_path:
                    try:
                        move_validated_vault_markdown(
                            self.vault_path,
                            existing_note_path,
                            canonical_note_path,
                            manifest=self._manifest_for_writes(),
                        )
                    except FrontmatterError as exc:
                        raise RuntimeError(
                            "PLAUD canonical path conflict for "
                            f"{file_id}: {canonical_note_path}"
                        ) from exc
                    replacements[
                        existing_note_path.relative_to(self.vault_path).as_posix()
                    ] = canonical_note_path.relative_to(self.vault_path).as_posix()
                    counters["migrated"] += 1
                else:
                    counters["already_canonical"] += 1

                canonical_rel_path = canonical_note_path.relative_to(
                    self.vault_path
                ).as_posix()
                recording_state = state.get("recordings", {}).get(file_id)
                if (
                    isinstance(recording_state, dict)
                    and recording_state.get("note_path") != canonical_rel_path
                ):
                    recording_state["note_path"] = canonical_rel_path
                    counters["state_updates"] += 1

                title = _safe_text(detail.get("title")) or f"PLAUD {file_id}"
                summary = _safe_text(detail.get("ai_content")) or _safe_text(
                    detail.get("trans_result")
                )
                self._upsert_daily_stub(
                    file_id=file_id,
                    recorded_at=recorded_at,
                    note_rel_path=canonical_rel_path,
                    title=title,
                    summary=summary,
                )
                counters["daily_updates"] += 1
                dirty_weeks.add(_iso_week_label(recorded_at.date()))

            counters["reference_updates"] = self._rewrite_note_path_references(
                replacements
            )
            state["dirty_weeks"] = sorted(dirty_weeks)
            state["last_sync_at"] = _utc_now().astimezone().isoformat()
            self._save_state(state)

            result: dict[str, Any] = dict(counters)
            if refresh_qmd and (
                counters["daily_updates"] > 0 or counters["migrated"] > 0
            ):
                result["qmd"] = self._refresh_qmd_index()
            return result

    def _build_note(
        self,
        *,
        detail: Mapping[str, Any],
        recorded_at: datetime,
        imported_at: datetime,
        context_type: str,
        raw_rel_path: str,
    ) -> str:
        file_id = str(detail.get("file_id") or detail.get("id") or "unknown")
        title = _safe_text(detail.get("title")) or f"PLAUD {file_id}"
        transcript = _safe_text(detail.get("trans_result"))
        summary = _content_blob_to_text(detail.get("ai_content"))
        source = build_plaud_source_info(file_id, language=self.content_language)
        frontmatter = [
            "---",
            "type: plaud-recording",
            "source: plaud",
            f"source_id: {file_id}",
            f"source_ref: {source.ref}",
            f"source_url: {source.url}",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"recorded_at: {recorded_at.isoformat()}",
            f"imported_at: {imported_at.isoformat()}",
            f"updated: {imported_at.date().isoformat()}",
            f"week: {_iso_week_label(recorded_at.date())}",
            f"context_type: {context_type}",
            f"raw_path: {raw_rel_path}",
            "---",
            "",
            f"# {title}",
            "",
            f"## {translate(self.content_language, 'summary')}",
            summary or translate(self.content_language, "no_summary_yet"),
            "",
            f"## {translate(self.content_language, 'transcript')}",
            transcript or translate(self.content_language, "no_transcript_yet"),
            "",
            f"## {translate(self.content_language, 'source')}",
            f"- {translate(self.content_language, 'plaud_file_id')}: `{file_id}`",
            f"- PLAUD: [{source.label}]({source.url})",
            f"- {translate(self.content_language, 'source_ref')}: `{source.ref}`",
            (
                f"- {translate(self.content_language, 'raw_payload')}: "
                f"`[[{raw_rel_path}]]`"
            ),
        ]
        return "\n".join(frontmatter).rstrip() + "\n"

    def _upsert_daily_stub(
        self,
        *,
        file_id: str,
        recorded_at: datetime,
        note_rel_path: str,
        title: str,
        summary: str,
    ) -> None:
        # Every value interpolated below has to stay on the single line this
        # block gives it: a newline inside one of them would forge another
        # <!-- d-brain:entry-status: ... --> line (skipping processing) or
        # another marker line (ManagedBlockError on every later write). Only
        # the summary is already collapsed, by _clip_summary. The title comes
        # from the PLAUD cloud as-is (_safe_text only strips the ends), and
        # note_rel_path / source.ref / source.url are all built from file_id
        # (see _file_paths / build_plaud_source_info), which is cloud data
        # too.
        #
        # file_id itself is deliberately NOT collapsed: it is this block's
        # identity, and collapsing is many-to-one, so two recordings whose
        # ids differ only in whitespace would share one marker pair and the
        # second one would silently overwrite the first one's entry. A
        # newline in an id would break the markers instead -- a failed write
        # the sync reports, not a lost entry nobody sees.
        note_rel_path = collapse_to_single_line(note_rel_path)
        title = collapse_to_single_line(title)
        source = build_plaud_source_info(file_id, language=self.content_language)
        source_url = collapse_to_single_line(source.url)
        source_ref = collapse_to_single_line(source.ref)
        start_marker = f"<!-- plaud:{file_id}:start -->"
        end_marker = f"<!-- plaud:{file_id}:end -->"
        status_block = format_entry_status_comments((ENTRY_STATUS_ALREADY_PROCESSED,))
        block = (
            f"\n## {recorded_at:%H:%M} [plaud]\n"
            f"{status_block}\n"
            f"{start_marker}\n"
            f"PLAUD: [[{note_rel_path}|{title}]]\n"
            f"> {translate(self.content_language, 'source')}: "
            f"[{source.label}]({source_url})\n"
            f"> {translate(self.content_language, 'source_ref')}: `{source_ref}`\n"
            f"{translate(self.content_language, 'summary')}: "
            f"{_clip_summary(summary)}\n"
            f"{end_marker}\n"
        )
        self.storage.upsert_daily_block(
            day=recorded_at.date(),
            start_marker=start_marker,
            end_marker=end_marker,
            block=block,
            refresh_qmd=False,
        )

    def _build_classification_prompt(
        self,
        *,
        detail: Mapping[str, Any],
        recorded_at: datetime,
        retro_todo_allowed: bool,
    ) -> str:
        reference = self._load_reference()
        summary_text = _safe_text(detail.get("ai_content"))
        transcript_text = _clip_prompt_text(_safe_text(detail.get("trans_result")))
        metadata = {
            "file_id": detail.get("file_id") or detail.get("id"),
            "title": detail.get("title"),
            "recorded_at": recorded_at.isoformat(),
            "age_days": (_utc_now().astimezone().date() - recorded_at.date()).days,
            "retro_todo_allowed": retro_todo_allowed,
            "summary_length": len(summary_text),
            "transcript_length": len(_safe_text(detail.get("trans_result"))),
        }
        return (
            "Read the PLAUD reference and classify the recording.\n\n"
            f"{reference}\n\n"
            "Use the rules exactly. Return only JSON.\n\n"
            "[RECORD_METADATA]\n"
            f"{_dump_json(metadata)}\n\n"
            "[SUMMARY]\n"
            f"{summary_text}\n\n"
            "[TRANSCRIPT]\n"
            f"{transcript_text}\n"
        )

    def _normalize_tasks(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw_task in payload.get("tasks", []):
            if not isinstance(raw_task, dict):
                continue
            content = _safe_text(raw_task.get("content"))
            if not content:
                continue
            due_hint = _safe_text(raw_task.get("due_hint"))
            priority = raw_task.get("priority", 1)
            try:
                priority_int = int(priority)
            except (TypeError, ValueError):
                priority_int = 1
            priority_int = max(1, min(priority_int, 4))
            normalized.append(
                {
                    "content": content,
                    "due_hint": due_hint,
                    "priority": priority_int,
                    "evidence": _safe_text(raw_task.get("evidence")),
                }
            )
        return normalized

    def _todoist_fingerprint(self, tasks: list[dict[str, Any]]) -> str:
        return sha256(
            json.dumps(tasks, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _create_todoist_tasks(
        self,
        tasks: list[dict[str, Any]],
        *,
        source_context: str = "",
    ) -> list[str] | None:
        """Create tasks, returning ``None`` when the external outcome is unknown."""
        if not tasks or not self.todoist_api_key:
            return []
        catalog_result = self.project_catalog.get_catalog(force_refresh=True)
        catalog = catalog_result["catalog"] if catalog_result["available"] else None
        payload: dict[str, list[dict[str, Any]]] = {"tasks": []}
        for task in tasks:
            route = self.project_router.route_task(
                task,
                source_context=source_context,
                catalog=catalog,
            )
            payload_task = {
                "content": task["content"],
                "priority": f"p{task['priority']}",
                **({"dueString": task["due_hint"]} if task["due_hint"] else {}),
            }
            if route.get("project_id"):
                payload_task["projectId"] = route["project_id"]
            payload["tasks"].append(payload_task)
        env = self._cli_extra_env()
        command = [
            "mcp-cli",
            "call",
            "todoist",
            "add-tasks",
            json.dumps(payload, ensure_ascii=False),
        ]
        errors: list[str] = []
        for attempt in range(3):
            if attempt > 0:
                time.sleep(2 * attempt)
            try:
                result = subprocess.run(
                    command,
                    cwd=self.vault_path,
                    capture_output=True,
                    text=True,
                    check=False,
                    env={**os.environ, **env},
                    timeout=3600,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Todoist creation timed out; external outcome is unknown"
                )
                return None
            if result.returncode == 0:
                try:
                    parsed = extract_first_json_dict(
                        result.stdout,
                        error_context="PLAUD Todoist MCP output",
                    )
                except ValueError:
                    logger.warning(
                        "Todoist creation returned malformed success output; "
                        "external outcome is unknown"
                    )
                    return None
                task_items = parsed.get("tasks")
                if not isinstance(task_items, list):
                    structured = parsed.get("structuredContent")
                    if isinstance(structured, dict):
                        task_items = structured.get("tasks") or structured.get(
                            "created"
                        )
                created_ids: list[str] = []
                for item in task_items or []:
                    if isinstance(item, dict) and item.get("id"):
                        created_ids.append(str(item["id"]))
                if created_ids:
                    return created_ids
                logger.warning(
                    "Todoist creation returned no task IDs; external outcome is unknown"
                )
                return None
            errors.append(result.stderr.strip() or result.stdout.strip())
        logger.warning("Todoist creation failed after retries: %s", " | ".join(errors))
        return []

    def _refresh_qmd_index(self) -> dict[str, Any]:
        """Keep qmd current after PLAUD searchable writes."""
        result = QmdService(self.vault_path).refresh_after_searchable_write()
        if not result["available"]:
            logger.warning("qmd refresh skipped: %s", " | ".join(result["errors"]))
            return result
        if result["errors"]:
            logger.warning("qmd refresh failed: %s", " | ".join(result["errors"]))
        return result

    def _refresh_compiled_briefings(
        self,
        *,
        note_path: str,
        summary: str,
        transcript: str,
    ) -> None:
        """Best-effort queue enqueue after one PLAUD note write."""
        try:
            service = CompiledBriefingService(
                self.vault_path,
                content_language=self.content_language,
                ai_cli=self.ai_cli,
            )
            result = service.enqueue_refresh(
                source_path=note_path,
                source_excerpt=self._compiled_excerpt(
                    summary=summary,
                    transcript=transcript,
                ),
            )
            service.spawn_background_drain()
        except Exception as exc:
            logger.warning("compiled refresh failed for %s: %s", note_path, exc)
            return
        if result["errors"] and result["errors"] not in (
            ["ai-cli-unavailable"],
            ["empty-source"],
            ["unsupported-path"],
        ):
            logger.warning(
                "compiled refresh failed for %s: %s",
                note_path,
                " | ".join(result["errors"]),
            )

    @staticmethod
    def _compiled_excerpt(*, summary: str, transcript: str) -> str:
        """Keep compiled refresh context bounded for the hot PLAUD path."""
        parts = [str(summary or "").strip()]
        transcript_excerpt = str(transcript or "").strip()[:3500]
        if transcript_excerpt:
            parts.append(transcript_excerpt)
        return "\n\n".join(part for part in parts if part).strip()

    def _sync_one(
        self,
        *,
        detail: Mapping[str, Any],
        state: dict[str, Any],
        allow_retro_todo: bool,
    ) -> dict[str, Any]:
        imported_at = _utc_now().astimezone()
        file_id = _safe_text(detail.get("file_id") or detail.get("id"))
        if not file_id:
            return {"status": "skipped", "reason": "missing-file-id"}

        recorded_at = self._recorded_at(detail)
        note_path, raw_path = self._file_paths(file_id, recorded_at)

        detail_hash = sha256(
            json.dumps(detail, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        source = build_plaud_source_info(file_id, language=self.content_language)
        recording_state = dict(state["recordings"].get(file_id, {}))
        raw_previous_task_ids = recording_state.get("task_ids")
        previous_task_ids = (
            list(raw_previous_task_ids)
            if isinstance(raw_previous_task_ids, list)
            else []
        )
        if (
            recording_state.get("content_hash") == detail_hash
            and recording_state.get("status") == "imported"
            and not recording_state.get("owner_eval_pending")
            and recording_state.get("source_url") == source.url
            and recording_state.get("source_ref") == source.ref
        ):
            return {"status": "unchanged", "file_id": file_id}

        summary = _safe_text(detail.get("ai_content"))
        transcript = _safe_text(detail.get("trans_result"))
        if not summary:
            state["recordings"][file_id] = {
                **recording_state,
                "status": "pending_summary",
                "content_hash": detail_hash,
                "recorded_at": recorded_at.isoformat(),
                "last_synced_at": imported_at.isoformat(),
            }
            self._save_state(state)
            return {"status": "pending_summary", "file_id": file_id}

        record_age_days = (_utc_now().astimezone().date() - recorded_at.date()).days
        within_window = record_age_days <= PLAUD_TASK_WINDOW_DAYS
        retro_todo_allowed = allow_retro_todo and within_window
        owner_eval_pending = within_window and not allow_retro_todo
        classification_failed = False
        classification_error = ""
        if within_window:
            try:
                verdict = extract_first_json_dict(
                    self._run_prompt(
                        self._build_classification_prompt(
                            detail=detail,
                            recorded_at=recorded_at,
                            retro_todo_allowed=retro_todo_allowed,
                        )
                    ),
                    error_context="PLAUD classification output",
                )
            except Exception as exc:
                classification_failed = True
                classification_error = str(exc)
                owner_eval_pending = True
                logger.warning(
                    "PLAUD classification failed for %s, continuing import: %s",
                    file_id,
                    exc,
                )
                verdict = {
                    "context_type": _safe_text(recording_state.get("context_type"))
                    or "unknown",
                    "archive": True,
                    "todoist_create": False,
                    "owner_confidence": _safe_text(
                        recording_state.get("owner_confidence")
                    )
                    or "none",
                    "reason": f"classification_failed: {classification_error}",
                    "tasks": [],
                }
            if not retro_todo_allowed:
                verdict["todoist_create"] = False
                verdict["tasks"] = []
        else:
            verdict = {
                "context_type": "unknown",
                "archive": True,
                "todoist_create": False,
                "owner_confidence": "none",
                "reason": "Archive-only import outside the 7-day action window",
                "tasks": [],
            }
        context_type = _safe_text(verdict.get("context_type")) or "unknown"
        note_rel_path = note_path.relative_to(self.vault_path).as_posix()
        raw_rel_path = raw_path.relative_to(self.vault_path).as_posix()
        write_vault_file_text(
            self.vault_path,
            raw_path,
            _dump_json(detail) + "\n",
        )
        try:
            expected_note_hash = sha256(
                read_vault_file_bytes(self.vault_path, note_path)
            ).hexdigest()
        except FileNotFoundError:
            expected_note_hash = None
        write_import_vault_markdown(
            self.vault_path,
            note_path,
            self._build_note(
                detail=detail,
                recorded_at=recorded_at,
                imported_at=imported_at,
                context_type=context_type,
                raw_rel_path=raw_rel_path,
            ),
            manifest=self._manifest_for_writes(),
            expected_full_sha256=expected_note_hash,
            require_absent=expected_note_hash is None,
        )
        self._refresh_compiled_briefings(
            note_path=note_rel_path,
            summary=summary,
            transcript=transcript,
        )
        self._upsert_daily_stub(
            file_id=file_id,
            recorded_at=recorded_at,
            note_rel_path=note_rel_path,
            title=_safe_text(detail.get("title")) or f"PLAUD {file_id}",
            summary=summary or transcript,
        )

        tasks = self._normalize_tasks(verdict)
        created_task_ids: list[str] = []
        todoist_create = bool(verdict.get("todoist_create"))
        owner_confidence = _safe_text(verdict.get("owner_confidence")) or "none"
        tasks_fp = self._todoist_fingerprint(tasks) if tasks else ""
        pending_tasks_fp = _safe_text(
            recording_state.get("todoist_pending_fingerprint")
        )
        task_creation_requested = (
            not classification_failed
            and todoist_create
            and owner_confidence == "high"
            and bool(self.todoist_api_key)
            and bool(tasks)
        )
        task_already_created = recording_state.get(
            "todoist_fingerprint"
        ) == tasks_fp and bool(recording_state.get("task_ids"))
        if task_already_created:
            pending_tasks_fp = ""
        task_creation_pending = bool(pending_tasks_fp)
        task_creation_unknown = False
        if (
            task_creation_requested
            and not task_already_created
            and not task_creation_pending
        ):
            pending_tasks_fp = tasks_fp
            state["recordings"][file_id] = {
                **recording_state,
                "todoist_pending_fingerprint": pending_tasks_fp,
            }
            self._save_state(state)
            try:
                creation_result = self._create_todoist_tasks(
                    tasks,
                    source_context="\n".join(
                        filter(
                            None,
                            [
                                "source_type=plaud-recording",
                                f"title={_safe_text(detail.get('title'))}",
                                f"context_type={context_type}",
                                f"reason={_safe_text(verdict.get('reason'))}",
                            ],
                        )
                    ),
                )
            except Exception:
                state["recordings"][file_id] = {
                    **recording_state,
                    "owner_eval_pending": True,
                }
                self._save_state(state)
                raise

            if creation_result is None:
                task_creation_unknown = True
            else:
                pending_tasks_fp = ""
                created_task_ids = creation_result
                if not created_task_ids:
                    state["recordings"][file_id] = {
                        **recording_state,
                        "owner_eval_pending": True,
                    }
                    self._save_state(state)

        if classification_failed:
            stored_task_ids = previous_task_ids
            stored_tasks_fp = _safe_text(recording_state.get("todoist_fingerprint"))
        else:
            stored_task_ids = (
                previous_task_ids if task_already_created else created_task_ids
            )
            stored_tasks_fp = (
                tasks_fp if task_already_created or created_task_ids else ""
            )
        task_creation_failed = (
            task_creation_requested
            and not task_already_created
            and not task_creation_pending
            and not task_creation_unknown
            and not created_task_ids
        )
        if pending_tasks_fp:
            owner_eval_pending = True
        elif task_creation_requested and (task_already_created or created_task_ids):
            owner_eval_pending = False
        elif task_creation_failed:
            owner_eval_pending = True

        week_label = _iso_week_label(recorded_at.date())
        dirty_weeks = set(state.get("dirty_weeks", []))
        dirty_weeks.add(week_label)
        state["dirty_weeks"] = sorted(dirty_weeks)
        state["recordings"][file_id] = {
            "status": "imported",
            "content_hash": detail_hash,
            "recorded_at": recorded_at.isoformat(),
            "note_path": note_rel_path,
            "raw_path": raw_rel_path,
            "context_type": context_type,
            "owner_confidence": owner_confidence,
            "owner_eval_pending": owner_eval_pending,
            "source_ref": source.ref,
            "source_url": source.url,
            "todoist_fingerprint": stored_tasks_fp,
            "task_ids": stored_task_ids,
            "last_synced_at": imported_at.isoformat(),
            **(
                {"todoist_pending_fingerprint": pending_tasks_fp}
                if pending_tasks_fp
                else {}
            ),
        }
        if classification_error:
            state["recordings"][file_id]["classification_error"] = classification_error
        self._save_state(state)
        return {
            "status": "imported",
            "file_id": file_id,
            "tasks_created": len(created_task_ids),
        }

    def sync(
        self,
        *,
        backfill: bool = False,
        allow_retro_todo: bool = True,
        max_pages: int | None = None,
        refresh_qmd: bool = True,
    ) -> dict[str, Any]:
        self._manifest_for_writes()
        with plaud_state_lock(self._state_path, blocking=False):
            state = self._load_state()
            counters = {
                "seen": 0,
                "imported": 0,
                "pending_summary": 0,
                "unchanged": 0,
                "tasks_created": 0,
            }
            errors: list[str] = []
            page_limit = (
                max_pages if max_pages is not None else (None if backfill else 1)
            )

            for item in self.client.iter_recordings(
                limit=PLAUD_PAGE_SIZE,
                max_pages=page_limit,
            ):
                counters["seen"] += 1
                file_id = _safe_text(item.get("file_id") or item.get("id"))
                if not file_id:
                    continue
                record_state = state["recordings"].get(file_id, {})
                if (
                    not backfill
                    and record_state.get("status") == "imported"
                    and not record_state.get("owner_eval_pending")
                ):
                    continue
                try:
                    detail = self.client.get_recording(file_id)
                    result = self._sync_one(
                        detail=detail,
                        state=state,
                        allow_retro_todo=allow_retro_todo,
                    )
                except PlaudAuthError:
                    raise
                except Exception as exc:
                    logger.warning("PLAUD sync failed for %s: %s", file_id, exc)
                    errors.append(f"{file_id}: {exc}")
                    continue

                status = result.get("status")
                if status == "imported":
                    counters["imported"] += 1
                    counters["tasks_created"] += int(result.get("tasks_created", 0))
                elif status == "pending_summary":
                    counters["pending_summary"] += 1
                elif status == "unchanged":
                    counters["unchanged"] += 1

                self._save_state(state)

            state["last_sync_at"] = _utc_now().astimezone().isoformat()
            self._save_state(state)
            result = {
                **counters,
                "dirty_weeks": state.get("dirty_weeks", []),
                "errors": errors,
            }
            if counters["imported"] > 0 and refresh_qmd:
                result["qmd"] = self._refresh_qmd_index()
            return result
