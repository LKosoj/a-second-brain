"""LLM-maintained compiled briefings derived from raw/searchable vault notes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from d_brain.manifest import VaultManifest, load_manifest_for_vault
from d_brain.services.cli_runner import (
    CliExecutionError,
    CliRunner,
    detect_terminal_backend_message,
    normalize_ai_cli,
)
from d_brain.services.frontmatter import (
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    write_validated_vault_markdown,
)
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.localization import normalize_language, prompt_language_name
from d_brain.services.qmd import QmdService
from d_brain.services.vault_lock import vault_write_lock

logger = logging.getLogger(__name__)
QueueLockResult = TypeVar("QueueLockResult")


class CompiledBriefingWriteConflict(RuntimeError):
    """Raised when a briefing changes while its replacement is being built."""


COMPILED_BRIEFING_DOMAINS = (
    "projects",
    "people",
    "topics",
    "decisions",
    "meetings",
)
DOMAIN_HINTS = {
    "projects": "долгоживущие проекты, инициативы, клиенты, pipeline, product threads",
    "people": "люди, контакты, партнёры, клиенты, команды, важные отношения",
    "topics": "устойчивые темы исследования, направления, recurring topics",
    "decisions": "решения с последствиями, договорённости, commitments, constraints",
    "meetings": (
        "повторяющиеся серии встреч, важные переговорные треки, recurring calls"
    ),
}
STATUS_VALUES = {"active", "draft", "pending", "done", "inactive"}
FRESHNESS_VALUES = {"fresh", "watch", "stale"}
CONFIDENCE_VALUES = {"high", "medium", "low"}
RECORD_KIND_VALUES = {"decision", "incident", "briefing"}
DECISION_STATUS_VALUES = {"proposed", "accepted", "rejected", "superseded"}
PROJECT_ROOT_SOURCE_PREFIXES = (
    "src/",
    "scripts/",
    "docs/",
    "deploy/",
    "skills/",
)
PROJECT_ROOT_SOURCE_FILES = {
    "README.md",
    "README.ru.md",
}
HIDDEN_STATE_SOURCE_PREFIXES = {
    "compiled/": ".compiled/",
    "sync/": ".sync/",
}
TIER_RANK = {
    "core": 5,
    "active": 4,
    "warm": 3,
    "cold": 2,
    "archive": 1,
}
QUESTION_DOMAIN_HINTS = {
    "projects": ("проект", "клиент", "лид", "статус", "pipeline"),
    "people": ("человек", "контакт", "клиент", "партнер", "partner", "founder"),
    "topics": ("тема", "исслед", "идея", "направлен", "why", "how"),
    "decisions": ("реш", "договор", "commit", "выбра", "почему"),
    "meetings": ("встреч", "созвон", "call", "meeting", "переговор"),
}
QUESTION_CONTEXT_LIMIT = 3
MAX_SOURCE_EXCERPT_CHARS = 8000
MAX_EXISTING_NOTE_CHARS = 10000
MAX_BODY_SNIPPET_CHARS = 2500
MAX_OUTPUT_ARTIFACT_CHARS = 12000
MAX_JSON_REPAIR_CHARS = 12000
IMPACT_TIMEOUT_SECONDS = 360
COMPILE_TIMEOUT_SECONDS = 180
JSON_REPAIR_TIMEOUT_SECONDS = 45
BATCH_CONSOLIDATION_TIMEOUT_SECONDS = 180
DEFAULT_DEBOUNCE_SECONDS = 45
DEFAULT_QUEUE_BATCH_SIZE = 8
DEFAULT_WORKER_IDLE_SECONDS = DEFAULT_DEBOUNCE_SECONDS + 15
DEFAULT_WORKER_POLL_SECONDS = 5.0
DEFAULT_WORKER_STALE_SECONDS = 90
DEFAULT_QUEUE_CLAIM_STALE_SECONDS = 900
IMPACT_CATALOG_MAX_ITEMS = 48
IMPACT_CATALOG_MAX_CHARS = 48000
BATCH_CONSOLIDATION_MIN_EVENTS = 2
BATCH_CONSOLIDATION_MAX_EVENTS = 6
BATCH_CONSOLIDATION_EXCERPT_CHARS = 1600
SOURCE_STATE_VERSION = 1
SOURCE_STATE_IGNORED_FRONTMATTER_FIELDS = {
    "last_accessed",
    "relevance",
    "tier",
}
SOURCE_STATE_IGNORED_PATH_PREFIXES = (
    "compiled/",
    ".compiled/",
    ".session/",
)


def _atomic_write_text(path: Path, payload: str) -> None:
    """Write text atomically via tempfile + os.replace to survive crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


TOKEN_RE = re.compile(
    r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_-]{2,}"
)
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n*", re.DOTALL)
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
DAILY_ENTRY_SPLIT_RE = re.compile(
    r"(?=^##\s+\d{2}:\d{2}\s+\[[^\]]+\]\s*$)",
    re.MULTILINE,
)
IMPACT_JSON_EXAMPLE = (
    "{\n"
    '  "source_shape": "single|mixed|noisy",\n'
    '  "durable_threads": [\n'
    '    {"label": "short thread label", "why": "why this thread is durable"}\n'
    "  ],\n"
    '  "updates": [\n'
    "    {\n"
    '      "domain": "projects|people|topics|decisions|meetings",\n'
    '      "title": "brief title",\n'
    '      "slug": "brief-slug",\n'
    '      "description": "one-line search snippet",\n'
    '      "reason": "why this briefing should refresh",\n'
    '      "existing_path": "compiled/<domain>/<slug>.md or empty"\n'
    "    }\n"
    "  ]\n"
    "}"
)
COMPILE_JSON_EXAMPLE = (
    "{\n"
    '  "description": "one-line snippet",\n'
    '  "status": "active|draft|pending|done|inactive",\n'
    '  "freshness_state": "fresh|watch|stale",\n'
    '  "confidence": "high|medium|low",\n'
    '  "current_state": "short paragraph",\n'
    '  "recent_changes": ["bullet", "..."],\n'
    '  "open_loops": ["bullet", "..."],\n'
    '  "key_decisions": ["bullet", "..."],\n'
    '  "record_kind": "decision|incident|briefing",\n'
    '  "decision_status": "proposed|accepted|rejected|superseded",\n'
    '  "decision_owner": "owner or empty",\n'
    '  "decision_date": "YYYY-MM-DD or empty",\n'
    '  "rationale": "decision rationale or empty",\n'
    '  "alternatives_considered": ["bullet", "..."],\n'
    '  "supersedes": ["decision identifier", "..."],\n'
    '  "superseded_by": "decision identifier or empty",\n'
    '  "decision_evidence": ["vault/relative/path.md", "..."],\n'
    '  "incident_date": "YYYY-MM-DD or empty",\n'
    '  "severity": "low|medium|high|critical or empty",\n'
    '  "timeline": ["timestamped event", "..."],\n'
    '  "root_cause": "root cause or empty",\n'
    '  "what_worked": ["bullet", "..."],\n'
    '  "what_did_not_work": ["bullet", "..."],\n'
    '  "corrective_actions": ["bullet", "..."],\n'
    '  "generalizable_learning": "reusable learning or empty",\n'
    '  "next_check": "short line",\n'
    '  "source_links": ["vault/relative/path.md", "..."]\n'
    "}"
)
BATCH_CONSOLIDATION_JSON_EXAMPLE = (
    "{\n"
    '  "headline": "short title",\n'
    '  "summary": "2-4 sentence synthesis",\n'
    '  "themes": ["bullet", "..."],\n'
    '  "follow_ups": ["bullet", "..."]\n'
    "}"
)


@dataclass(frozen=True, slots=True)
class CompiledBriefingTarget:
    """Resolved impacted briefing target."""

    domain: str
    title: str
    slug: str
    description: str
    reason: str
    existing_path: str = ""


@dataclass(frozen=True, slots=True)
class CompiledBriefingCandidate:
    """One existing compiled note candidate for routing and ranking."""

    rel_path: str
    domain: str
    slug: str
    title: str
    description: str
    freshness_state: str
    confidence: str
    relevance: float
    tier: str
    text: str


@dataclass(frozen=True, slots=True)
class CompiledBatchConsolidationEvent:
    """One successful queue event eligible for a cross-source consolidation note."""

    source_rel_path: str
    source_excerpt: str
    updated_paths: tuple[str, ...]


class CompiledBriefingService:
    """Maintain a derived compiled layer above raw vault notes."""

    def __init__(
        self,
        vault_path: Path,
        content_language: str = "ru",
        ai_cli: str | None = None,
    ) -> None:
        self.vault_path = Path(vault_path).resolve()
        self.compiled_root = self.vault_path / "compiled"
        self.state_root = self.vault_path / ".compiled"
        self.queue_path = self.state_root / "queue.json"
        self.queue_lock_path = self.state_root / "queue.lock"
        self.source_state_path = self.state_root / "source-state.json"
        self.launcher_lock_path = self.state_root / "launcher.lock"
        self.worker_lock_path = self.state_root / "worker.lock"
        self.worker_state_path = self.state_root / "worker-state.json"
        self.answers_root = self.vault_path / "summaries" / "answers"
        self.content_language = normalize_language(content_language)
        self.ai_cli = self._resolve_ai_cli(ai_cli)
        self.runner = CliRunner(self.vault_path, self.ai_cli)
        self.qmd = QmdService(self.vault_path)

    def enqueue_refresh(
        self,
        *,
        source_path: str | Path,
        source_excerpt: str = "",
        max_updates: int = 3,
        debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
    ) -> dict[str, Any]:
        """Queue one refresh event and debounce by source path."""

        source_rel_path = self._normalize_rel_path(source_path)
        if not source_rel_path or source_rel_path.startswith("compiled/"):
            return {"queued": False, "errors": ["unsupported-path"]}

        self._ensure_state_dirs()
        now = datetime.now().astimezone()
        due_at = now.timestamp() + max(0, debounce_seconds)
        clipped_excerpt = self._clip(source_excerpt, MAX_SOURCE_EXCERPT_CHARS)

        def mutate_queue() -> None:
            queue = self._load_queue()
            updated = False
            for event in queue:
                if str(event.get("source_path") or "") != source_rel_path:
                    continue
                event["source_excerpt"] = clipped_excerpt
                event["due_at"] = due_at
                event["max_updates"] = max_updates
                event["last_enqueued_at"] = now.isoformat()
                event["state"] = "pending"
                event["claim_token"] = ""
                event["claimed_at"] = ""
                event["claimed_pid"] = 0
                updated = True
                break
            if not updated:
                queue.append(
                    {
                        "source_path": source_rel_path,
                        "source_excerpt": clipped_excerpt,
                        "enqueued_at": now.isoformat(),
                        "last_enqueued_at": now.isoformat(),
                        "due_at": due_at,
                        "attempts": 0,
                        "max_updates": max_updates,
                        "state": "pending",
                        "claim_token": "",
                        "claimed_at": "",
                        "claimed_pid": 0,
                    }
                )
            self._save_queue(queue)

        self._with_queue_lock(mutate_queue)
        return {"queued": True, "errors": []}

    def spawn_background_drain(self) -> bool:
        """Start one detached queue-drain worker unless one is already alive."""

        if not self.is_available():
            return False
        self._ensure_state_dirs()
        with self._launcher_lock(blocking=False) as acquired:
            if not acquired:
                return False

            state = self._load_worker_state()
            if self._worker_state_is_live(state):
                return False
            if state:
                self._clear_worker_state_unlocked()

            command = [
                sys.executable,
                "-m",
                "d_brain.run_compiled_maintenance",
                "--queue-only",
            ]
            try:
                process = subprocess.Popen(  # noqa: S603
                    command,
                    cwd=self.vault_path.parent,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
                )
            except Exception as exc:
                logger.warning("Failed to spawn compiled maintenance worker: %s", exc)
                return False

            now = datetime.now().astimezone().isoformat()
            self._save_worker_state(
                {
                    "pid": process.pid,
                    "status": "starting",
                    "started_at": now,
                    "heartbeat_at": now,
                }
            )
            return True

    def drain_queue(
        self,
        *,
        force: bool = False,
        max_events: int = DEFAULT_QUEUE_BATCH_SIZE,
        refresh_qmd: bool = True,
    ) -> dict[str, Any]:
        """Drain queued refresh events through the compiled compiler."""

        if not self.is_available():
            return {
                "drained": 0,
                "updated": [],
                "consolidations": [],
                "errors": ["ai-cli-unavailable"],
            }

        self._ensure_state_dirs()
        with self._worker_lock(blocking=False) as acquired:
            if not acquired:
                return {
                    "drained": 0,
                    "updated": [],
                    "consolidations": [],
                    "errors": ["worker-busy"],
                }

            result = self._drain_queue_once(force=force, max_events=max_events)
            if result["updated"] and refresh_qmd:
                self._refresh_qmd_index()
            return result

    def run_queue_worker(
        self,
        *,
        force: bool = False,
        max_events: int = DEFAULT_QUEUE_BATCH_SIZE,
        refresh_qmd: bool = True,
        idle_seconds: int = DEFAULT_WORKER_IDLE_SECONDS,
        poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS,
    ) -> dict[str, Any]:
        """Run one short-lived burst worker that absorbs nearby queued writes."""

        if not self.is_available():
            return {
                "drained": 0,
                "updated": [],
                "consolidations": [],
                "errors": ["ai-cli-unavailable"],
            }

        self._ensure_state_dirs()
        with self._worker_lock(blocking=False) as acquired:
            if not acquired:
                return {
                    "drained": 0,
                    "updated": [],
                    "consolidations": [],
                    "errors": ["worker-busy"],
                }

            worker_pid = os.getpid()
            started_at = datetime.now().astimezone().isoformat()
            total_drained = 0
            updated_paths: list[str] = []
            consolidation_paths: list[str] = []
            errors: list[str] = []
            idle_deadline: float | None = None

            try:
                self._write_worker_state(
                    pid=worker_pid,
                    status="running",
                    started_at=started_at,
                )
                while True:
                    self._touch_worker_state(worker_pid)
                    batch = self._drain_queue_once(
                        force=force,
                        max_events=max_events,
                    )
                    total_drained += int(batch.get("drained") or 0)
                    updated_paths.extend(
                        str(path) for path in batch.get("updated", [])
                    )
                    consolidation_paths.extend(
                        str(path) for path in batch.get("consolidations", [])
                    )
                    errors.extend(str(item) for item in batch.get("errors", []))

                    queue = self._with_queue_lock(self._load_queue)
                    if queue:
                        now_ts = datetime.now().astimezone().timestamp()
                        next_due_at = min(
                            float(event.get("due_at") or 0) for event in queue
                        )
                        if force or next_due_at <= now_ts:
                            idle_deadline = None
                            continue
                        if idle_deadline is None:
                            idle_deadline = time.monotonic() + max(0, idle_seconds)
                        remaining_idle = idle_deadline - time.monotonic()
                        if remaining_idle <= 0:
                            break
                        sleep_for = min(
                            max(0.1, poll_seconds),
                            remaining_idle,
                            max(0.1, next_due_at - now_ts),
                        )
                        time.sleep(sleep_for)
                        continue

                    if idle_deadline is None:
                        idle_deadline = time.monotonic() + max(0, idle_seconds)
                    remaining_idle = idle_deadline - time.monotonic()
                    if remaining_idle <= 0:
                        break
                    time.sleep(min(max(0.1, poll_seconds), remaining_idle))
            finally:
                self._clear_worker_state(pid=worker_pid)

            if updated_paths and refresh_qmd:
                self._refresh_qmd_index()
            return {
                "drained": total_drained,
                "updated": updated_paths,
                "consolidations": consolidation_paths,
                "errors": errors,
            }

    def _drain_queue_once(
        self,
        *,
        force: bool,
        max_events: int,
    ) -> dict[str, Any]:
        """Drain one ready queue batch without taking the outer worker lock."""

        selected = self._claim_ready_queue_events(
            force=force,
            max_events=max_events,
        )
        if not selected:
            return {
                "drained": 0,
                "updated": [],
                "errors": [],
                "consolidations": [],
            }

        updated_paths: list[str] = []
        errors: list[str] = []
        consolidation_events: list[CompiledBatchConsolidationEvent] = []
        now_ts = datetime.now().astimezone().timestamp()
        for event in selected:
            result = self.refresh_after_write(
                source_path=str(event.get("source_path") or ""),
                source_excerpt=str(event.get("source_excerpt") or ""),
                max_updates=int(event.get("max_updates") or 3),
            )
            updated_paths.extend(str(path) for path in result.get("updated", []))
            event_updated = [
                str(path).strip()
                for path in result.get("updated", [])
                if str(path).strip()
            ]
            event_errors = [
                str(item) for item in result.get("errors", []) if str(item)
            ]
            if not event_errors:
                source_rel_path = str(event.get("source_path") or "").strip()
                source_excerpt = self._source_excerpt(
                    source_rel_path,
                    str(event.get("source_excerpt") or ""),
                )
                if source_rel_path and source_excerpt and event_updated:
                    consolidation_events.append(
                        CompiledBatchConsolidationEvent(
                            source_rel_path=source_rel_path,
                            source_excerpt=self._clip(
                                source_excerpt,
                                BATCH_CONSOLIDATION_EXCERPT_CHARS,
                            ),
                            updated_paths=tuple(event_updated),
                        )
                    )
                self._ack_claimed_queue_event(event)
                continue

            retriable = event_errors not in (
                ["ai-cli-unavailable"],
                ["empty-source"],
                ["unsupported-path"],
            )
            if retriable:
                attempts = int(event.get("attempts") or 0) + 1
                if attempts < 3:
                    self._release_claimed_queue_event(
                        event,
                        attempts=attempts,
                        due_at=now_ts + 300,
                    )
                else:
                    self._ack_claimed_queue_event(event)
                errors.extend(event_errors)
                continue

            errors.extend(event_errors)
            self._ack_claimed_queue_event(event)

        consolidation_paths: list[str] = []
        consolidation_path = self._write_batch_consolidation(consolidation_events)
        if consolidation_path:
            consolidation_paths.append(consolidation_path)

        return {
            "drained": len(selected),
            "updated": updated_paths,
            "errors": errors,
            "consolidations": consolidation_paths,
        }

    def run_nightly_maintenance(
        self,
        *,
        backfill_limit: int = 5,
    ) -> dict[str, Any]:
        """Run queued refresh, lint, and bounded source-aware backfill."""

        drain_result = self.drain_queue(force=True, max_events=50, refresh_qmd=False)
        queue_errors = list(drain_result.get("errors", []))
        queue_busy = False
        queue_worker_pid = 0
        if queue_errors == ["worker-busy"]:
            state = self._load_worker_state()
            if self._worker_state_is_live(state):
                queue_busy = True
                queue_worker_pid = int(state.get("pid") or 0)
                queue_errors = []
        archived = self._archive_stale_notes(limit=backfill_limit)
        backfilled = self._backfill_freshness_notes(
            limit=max(0, backfill_limit - len(archived))
        )
        lint_issues = self.lint_notes()
        freshness_issues = self.freshness_issues()
        if drain_result.get("updated") or archived or backfilled:
            self._refresh_qmd_index()
        return {
            "queued_drained": int(drain_result.get("drained") or 0),
            "queue_updated": list(drain_result.get("updated", [])),
            "consolidations": list(drain_result.get("consolidations", [])),
            "queue_errors": queue_errors,
            "queue_busy": queue_busy,
            "queue_worker_pid": queue_worker_pid,
            "lint_issues": lint_issues,
            "freshness_issues": freshness_issues,
            "backfilled": backfilled,
            "archived": archived,
            "searchable_write": bool(
                drain_result.get("updated") or archived or backfilled
            ),
            "errors": queue_errors,
        }

    def lint_notes(self) -> list[dict[str, str]]:
        """Run deterministic health checks over compiled notes."""

        issues: list[dict[str, str]] = []
        required_sections = {
            "Current State",
            "Recent Changes",
            "Open Loops",
            "Key Decisions",
            "Next Check",
            "Sources",
        }
        for candidate in self._iter_candidates():
            sections = self._sections_from_text(candidate.text)
            if not required_sections.issubset(sections):
                issues.append(
                    {
                        "path": candidate.rel_path,
                        "issue": "missing-sections",
                        "detail": ",".join(sorted(required_sections - sections)),
                    }
                )
            sources = self._source_links_from_note(candidate.text)
            if not sources:
                issues.append(
                    {
                        "path": candidate.rel_path,
                        "issue": "missing-sources",
                        "detail": "compiled note has no source links",
                    }
                )
            for source in sources:
                resolved_source = self._resolve_lint_source_path(source)
                if resolved_source is None or not resolved_source.exists():
                    issues.append(
                        {
                            "path": candidate.rel_path,
                            "issue": "broken-source-link",
                            "detail": source,
                        }
                    )
        return issues

    def freshness_issues(self) -> list[dict[str, str]]:
        """Return compiled notes whose semantic source snapshot needs review."""

        issues: list[dict[str, str]] = []
        source_state = self._load_source_state()
        state_entries = source_state["entries"]
        for candidate in self._iter_candidates():
            issue = self._candidate_freshness_issue(
                candidate,
                state_entries.get(candidate.rel_path),
            )
            if issue is not None:
                issues.append(issue)
        return issues

    def initialize_source_state(self) -> dict[str, Any]:
        """Create the first source-aware baseline without rewriting briefings."""

        candidates = self._iter_candidates()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        initialized = 0
        unchanged = 0
        changed = 0

        with self._source_state_lock():
            state = self._load_source_state_unlocked()
            entries = state["entries"]
            active_paths = {candidate.rel_path for candidate in candidates}
            removed = sorted(set(entries) - active_paths)
            for rel_path in removed:
                del entries[rel_path]

            for candidate in candidates:
                snapshot = self._source_snapshot(candidate.text)
                existing = entries.get(candidate.rel_path)
                if isinstance(existing, dict):
                    if existing.get("sources") == snapshot:
                        unchanged += 1
                    else:
                        changed += 1
                    continue
                entries[candidate.rel_path] = {
                    "evaluated_at": now,
                    "sources": snapshot,
                }
                initialized += 1

            if initialized or removed or not self.source_state_path.exists():
                self._write_source_state_unlocked(state)

        return {
            "evaluated": len(candidates),
            "initialized": initialized,
            "unchanged": unchanged,
            "changed": changed,
            "removed": removed,
            "errors": [],
        }

    def file_output_artifact(
        self,
        *,
        request: str,
        output_markdown: str,
        artifact_type: str,
    ) -> str | None:
        """Persist useful assistant outputs back into the searchable vault."""

        if not self.should_file_output_artifact(request, output_markdown):
            return None
        now = datetime.now().astimezone()
        dir_path = self.answers_root / now.strftime("%Y") / now.strftime("%m")
        title = self._artifact_title(request)
        slug = self._slugify(title) or "assistant-output"
        note = self._render_output_artifact(
            title=title,
            request=request,
            output_markdown=output_markdown,
            artifact_type=artifact_type,
            created_at=now,
        )
        with vault_write_lock(self.vault_path) as lock:
            path = dir_path / f"{now.strftime('%Y-%m-%d-%H%M%S')}-{slug}.md"
            counter = 1
            while path.exists():
                path = dir_path / (
                    f"{now.strftime('%Y-%m-%d-%H%M%S')}-{slug}-{counter}.md"
                )
                counter += 1
            write_validated_vault_markdown(
                self.vault_path,
                path,
                note.encode("utf-8"),
                manifest=self._manifest(),
                existing_lock=lock,
            )
            rel_path = path.relative_to(self.vault_path).as_posix()
            self.enqueue_refresh(
                source_path=rel_path,
                source_excerpt=self._clip(
                    f"{request.strip()}\n\n{output_markdown.strip()}",
                    MAX_SOURCE_EXCERPT_CHARS,
                ),
                max_updates=2,
            )
        self.spawn_background_drain()
        return rel_path

    @staticmethod
    def should_file_output_artifact(request: str, output_markdown: str) -> bool:
        """Heuristic filter so filing loop does not dump every trivial answer."""

        request_text = " ".join(str(request or "").split()).strip()
        output_text = str(output_markdown or "").strip()
        if len(output_text) < 180:
            return False
        if not request_text or len(request_text) < 8:
            return False
        if "\n- " in output_text or "\n1. " in output_text:
            return True
        if len(output_text) >= 350:
            return True
        return any(
            token in request_text.lower()
            for token in ("что", "status", "статус", "решили", "изменил", "summary")
        )

    def _write_batch_consolidation(
        self,
        events: list[CompiledBatchConsolidationEvent],
    ) -> str | None:
        """Persist one best-effort cross-source consolidation note for a queue batch."""

        eligible = [
            event
            for event in events
            if self._batch_consolidation_source_allowed(event.source_rel_path)
        ]
        if len(eligible) < BATCH_CONSOLIDATION_MIN_EVENTS:
            return None

        unique_sources = {event.source_rel_path for event in eligible}
        if len(unique_sources) < BATCH_CONSOLIDATION_MIN_EVENTS:
            return None

        batch = eligible[:BATCH_CONSOLIDATION_MAX_EVENTS]
        prompt = self._build_batch_consolidation_prompt(batch)
        try:
            payload = self._run_json_dict_prompt(
                prompt=prompt,
                timeout=BATCH_CONSOLIDATION_TIMEOUT_SECONDS,
                error_context="compiled batch consolidation",
                json_example=BATCH_CONSOLIDATION_JSON_EXAMPLE,
            )
        except (CliExecutionError, FileNotFoundError, TimeoutError, ValueError) as exc:
            logger.warning(
                "Compiled batch consolidation skipped for %s source events: %s",
                len(batch),
                exc,
            )
            return None

        return self._persist_batch_consolidation(payload=payload, events=batch)

    @staticmethod
    def _batch_consolidation_source_allowed(source_rel_path: str) -> bool:
        raw = str(source_rel_path or "").strip()
        if not raw:
            return False
        return not raw.startswith(("compiled/", "summaries/", "."))

    def _build_batch_consolidation_prompt(
        self,
        events: list[CompiledBatchConsolidationEvent],
    ) -> str:
        batch_payload = [
            {
                "source_path": event.source_rel_path,
                "updated_briefings": list(event.updated_paths),
                "excerpt": event.source_excerpt,
            }
            for event in events
        ]
        return (
            "You are writing a compact cross-source consolidation note for a "
            "personal assistant memory system.\n"
            f"Write generated text in {prompt_language_name(self.content_language)}.\n"
            "Return ONLY JSON.\n\n"
            "Goal: synthesize the strongest recurring patterns across multiple "
            "recent source events that already refreshed compiled briefings.\n\n"
            "Required JSON schema:\n"
            "{\n"
            '  "headline": "short title",\n'
            '  "summary": "2-4 sentence synthesis",\n'
            '  "themes": ["bullet", "..."],\n'
            '  "follow_ups": ["bullet", "..."]\n'
            "}\n\n"
            "Rules:\n"
            "- Only mention patterns supported by multiple sources or strongly "
            "reinforced by the refreshed briefings.\n"
            "- Stay cumulative and operational; do not restate every raw detail.\n"
            "- If the batch is too mixed for a durable synthesis, return a short "
            "headline, an honest summary, and empty lists.\n"
            "- Keep themes and follow_ups short.\n"
            "- Do not invent people, projects, decisions, or dates.\n\n"
            "[BATCH_EVENTS]\n"
            f"{json.dumps(batch_payload, ensure_ascii=False, indent=2)}\n"
        )

    def _persist_batch_consolidation(
        self,
        *,
        payload: dict[str, Any],
        events: list[CompiledBatchConsolidationEvent],
    ) -> str:
        now = datetime.now().astimezone()
        dir_path = (
            self.vault_path
            / "summaries"
            / "consolidations"
            / now.strftime("%Y")
            / now.strftime("%m")
        )
        headline = (
            self._clean_line(payload.get("headline"))
            or "Compiled Batch Consolidation"
        )
        slug = self._slugify(headline) or "compiled-batch-consolidation"
        note = self._render_batch_consolidation(
            headline=headline,
            payload=payload,
            events=events,
            created_at=now,
        )
        with vault_write_lock(self.vault_path) as lock:
            path = dir_path / f"{now.strftime('%Y-%m-%d-%H%M%S')}-{slug}.md"
            counter = 1
            while path.exists():
                path = dir_path / (
                    f"{now.strftime('%Y-%m-%d-%H%M%S')}-{slug}-{counter}.md"
                )
                counter += 1
            write_validated_vault_markdown(
                self.vault_path,
                path,
                note.encode("utf-8"),
                manifest=self._manifest(),
                existing_lock=lock,
            )
        return path.relative_to(self.vault_path).as_posix()

    def refresh_after_write(
        self,
        *,
        source_path: str | Path,
        source_excerpt: str = "",
        max_updates: int = 3,
    ) -> dict[str, Any]:
        """Refresh a small compiled-note budget after one raw/searchable write."""

        if max_updates <= 0:
            return {"available": False, "updated": [], "errors": ["budget-disabled"]}

        source_rel_path = self._normalize_rel_path(source_path)
        if not source_rel_path or source_rel_path.startswith("compiled/"):
            return {"available": False, "updated": [], "errors": ["unsupported-path"]}
        if not self.is_available():
            return {"available": False, "updated": [], "errors": ["ai-cli-unavailable"]}

        excerpt = self._source_excerpt(source_rel_path, source_excerpt)
        if not excerpt:
            return {"available": False, "updated": [], "errors": ["empty-source"]}

        signal = self.qmd._memory_signal_for_rel_path(source_rel_path)
        try:
            targets = self._resolve_targets(
                source_rel_path=source_rel_path,
                source_excerpt=excerpt,
                signal=signal,
                max_updates=max_updates,
            )
        except (CliExecutionError, FileNotFoundError, TimeoutError, ValueError) as exc:
            logger.warning(
                "Compiled briefing impact resolution skipped for %s: %s",
                source_rel_path,
                exc,
            )
            return {"available": False, "updated": [], "errors": [str(exc)]}

        if not targets:
            return {"available": True, "updated": [], "errors": []}

        updated: list[str] = []
        errors: list[str] = []
        for target in targets:
            try:
                rel_path = self._upsert_briefing(
                    target=target,
                    source_rel_path=source_rel_path,
                    source_excerpt=excerpt,
                    signal=signal,
                )
            except (
                CliExecutionError,
                CompiledBriefingWriteConflict,
                FileNotFoundError,
                TimeoutError,
                ValueError,
            ) as exc:
                logger.warning(
                    "Compiled briefing refresh failed for %s -> %s: %s",
                    source_rel_path,
                    target.slug,
                    exc,
                )
                errors.append(f"{target.domain}/{target.slug}: {exc}")
                continue
            updated.append(rel_path)

        return {"available": True, "updated": updated, "errors": errors}

    def refresh_daily_fully(
        self,
        *,
        source_path: str | Path,
        max_updates_per_chunk: int = 3,
        refresh_qmd: bool = True,
        on_chunk: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        (
            "Refresh one daily note chunk-by-chunk without silently dropping "
            "the middle."
        )

        source_rel_path = self._normalize_rel_path(source_path)
        if not source_rel_path.startswith("daily/"):
            return self.refresh_after_write(
                source_path=source_rel_path,
                max_updates=max_updates_per_chunk,
            )

        source_text = self._source_excerpt(source_rel_path, "")
        source_path_obj = self.vault_path / source_rel_path
        if source_path_obj.exists():
            source_text = source_path_obj.read_text(encoding="utf-8", errors="replace")
        if not source_text.strip():
            return {"available": False, "updated": [], "errors": ["empty-source"]}

        chunks = self._daily_source_chunks(source_rel_path, source_text)
        if not chunks:
            return {"available": False, "updated": [], "errors": ["empty-source"]}

        updated: list[str] = []
        errors: list[str] = []
        total_chunks = len(chunks)
        processed_chunks = 0
        for index, chunk in enumerate(chunks, start=1):
            if on_chunk is not None:
                on_chunk(
                    {
                        "index": index,
                        "total": total_chunks,
                        "status": "started",
                        "source_rel_path": source_rel_path,
                    }
                )
            result = self.refresh_after_write(
                source_path=source_rel_path,
                source_excerpt=chunk,
                max_updates=max_updates_per_chunk,
            )
            chunk_updated = [
                str(rel_path) for rel_path in result.get("updated", []) if str(rel_path)
            ]
            chunk_errors = [
                str(error).strip()
                for error in result.get("errors", [])
                if str(error).strip()
            ]
            for rel_path in result.get("updated", []):
                rel_value = str(rel_path)
                if rel_value and rel_value not in updated:
                    updated.append(rel_value)
            for error in result.get("errors", []):
                message = str(error).strip()
                if message:
                    errors.append(f"chunk {index}: {message}")
            processed_chunks = index
            if on_chunk is not None:
                on_chunk(
                    {
                        "index": index,
                        "total": total_chunks,
                        "status": "finished",
                        "source_rel_path": source_rel_path,
                        "updated": chunk_updated,
                        "errors": chunk_errors,
                    }
                )
            if any(detect_terminal_backend_message(error) for error in chunk_errors):
                break

        if updated and refresh_qmd:
            self._refresh_qmd_index()

        return {
            "available": True,
            "updated": updated,
            "errors": errors,
            "chunks": total_chunks,
            "processed_chunks": processed_chunks,
            "source_rel_path": source_rel_path,
        }

    def build_question_context(
        self,
        question: str,
        *,
        limit: int = QUESTION_CONTEXT_LIMIT,
    ) -> str:
        """Return a compiled-briefing block for direct-question prompts."""

        ranked = self._rank_candidates(question, limit=limit)
        if not ranked:
            return ""

        lines = [
            "=== COMPILED BRIEFINGS ===",
            "Use these compiled briefings first.",
            (
                "They are derived summaries, not ground truth. Verify with curated "
                "core or qmd when needed."
            ),
            "",
        ]
        for index, candidate in enumerate(ranked, start=1):
            lines.extend(
                [
                    f"[{index}] {candidate.rel_path}",
                    f"Title: {candidate.title}",
                    f"Domain: {candidate.domain}",
                    f"Description: {candidate.description or '(none)'}",
                    (
                        f"Freshness: {candidate.freshness_state or 'unknown'} | "
                        f"Confidence: {candidate.confidence or 'unknown'}"
                    ),
                    "",
                    candidate.text.strip(),
                    "",
                ]
            )
        lines.append("=== END COMPILED BRIEFINGS ===")
        return "\n".join(lines).strip()

    def is_available(self) -> bool:
        """Check whether the configured CLI exists before doing extra work."""

        binary = self.runner.spec.argv_prefix[0]
        return shutil.which(binary) is not None

    def _resolve_targets(
        self,
        *,
        source_rel_path: str,
        source_excerpt: str,
        signal: dict[str, Any] | None,
        max_updates: int,
    ) -> list[CompiledBriefingTarget]:
        catalog = self._impact_catalog(
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
        )
        prompt = self._build_impact_prompt(
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
            signal=signal,
            catalog=catalog,
            max_updates=max_updates,
        )
        payload = self._run_json_dict_prompt(
            prompt=prompt,
            timeout=IMPACT_TIMEOUT_SECONDS,
            error_context="compiled briefing impact resolution",
            json_example=IMPACT_JSON_EXAMPLE,
        )
        updates = payload.get("updates")
        if not isinstance(updates, list):
            return []

        targets: list[CompiledBriefingTarget] = []
        seen: set[tuple[str, str]] = set()
        for item in updates:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").strip().lower()
            if domain not in COMPILED_BRIEFING_DOMAINS:
                continue
            title = self._clean_line(item.get("title"))
            if not title:
                continue
            description = self._clean_line(item.get("description"))
            slug = self._slugify(str(item.get("slug") or title))
            if not slug:
                continue
            key = (domain, slug)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                CompiledBriefingTarget(
                    domain=domain,
                    title=title,
                    slug=slug,
                    description=description,
                    reason=self._clean_line(item.get("reason")),
                    existing_path=self._clean_line(item.get("existing_path")),
                )
            )
            if len(targets) >= max_updates:
                break
        return targets

    def _upsert_briefing(
        self,
        *,
        target: CompiledBriefingTarget,
        source_rel_path: str,
        source_excerpt: str,
        signal: dict[str, Any] | None,
        record_source_state: bool = True,
    ) -> str:
        self._ensure_dirs()
        note_path = self._target_path(target)
        existing_text = ""
        existing_meta: dict[str, str] = {}
        try:
            existing_bytes = note_path.read_bytes()
        except FileNotFoundError:
            existing_bytes = None
        original_fingerprint = self._full_content_fingerprint(existing_bytes)
        if existing_bytes is not None:
            existing_text = existing_bytes.decode("utf-8", errors="replace")
            existing_meta = self._frontmatter_fields(existing_text)

        prompt = self._build_compile_prompt(
            target=target,
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
            signal=signal,
            existing_text=existing_text,
        )
        payload = self._run_json_dict_prompt(
            prompt=prompt,
            timeout=COMPILE_TIMEOUT_SECONDS,
            error_context="compiled briefing render",
            json_example=COMPILE_JSON_EXAMPLE,
        )
        rendered = self._render_briefing(
            target=target,
            payload=payload,
            source_rel_path=source_rel_path,
            existing_text=existing_text,
            existing_meta=existing_meta,
            signal=signal,
        )
        rel_path = note_path.relative_to(self.vault_path).as_posix()
        with vault_write_lock(self.vault_path) as lock:
            try:
                current_bytes = note_path.read_bytes()
            except FileNotFoundError:
                current_bytes = None
            if self._full_content_fingerprint(current_bytes) != original_fingerprint:
                raise CompiledBriefingWriteConflict(
                    f"compiled briefing changed during build: {rel_path}"
                )
            write_validated_vault_markdown(
                self.vault_path,
                note_path,
                rendered.encode("utf-8"),
                manifest=self._manifest(),
                existing_lock=lock,
            )
            if record_source_state:
                self._record_source_state(rel_path, rendered)
        return rel_path

    @staticmethod
    def _full_content_fingerprint(content: bytes | None) -> bytes | None:
        """Fingerprint the exact briefing bytes; ``None`` represents absence."""
        return hashlib.sha256(content).digest() if content is not None else None

    def _manifest(self) -> VaultManifest:
        """Load the required project manifest before every Markdown write."""
        return load_manifest_for_vault(self.vault_path)

    def _run_json_dict_prompt(
        self,
        *,
        prompt: str,
        timeout: int,
        error_context: str,
        json_example: str,
    ) -> dict[str, Any]:
        raw = self.runner.run(prompt, timeout=timeout)
        try:
            return extract_first_json_dict(raw, error_context=error_context)
        except ValueError as first_error:
            repaired = self._repair_json_dict_output(
                raw_output=raw,
                error_context=error_context,
                json_example=json_example,
            )
            if repaired is not None:
                return repaired
            raise first_error

    def _repair_json_dict_output(
        self,
        *,
        raw_output: str,
        error_context: str,
        json_example: str,
    ) -> dict[str, Any] | None:
        source = str(raw_output or "").strip()
        if not source:
            return None

        repair_prompt = self._build_json_repair_prompt(
            raw_output=source,
            error_context=error_context,
            json_example=json_example,
        )
        try:
            repaired_raw = self.runner.run(
                repair_prompt,
                timeout=JSON_REPAIR_TIMEOUT_SECONDS,
            )
        except (CliExecutionError, FileNotFoundError, TimeoutError):
            return None

        try:
            return extract_first_json_dict(repaired_raw, error_context=error_context)
        except ValueError:
            return None

    def _build_json_repair_prompt(
        self,
        *,
        raw_output: str,
        error_context: str,
        json_example: str,
    ) -> str:
        return (
            "You are repairing model output for a strict JSON-only runtime.\n"
            f"Return ONLY one valid JSON object for {error_context}.\n"
            "Do not add markdown fences, prose, or explanations.\n"
            "Do not invent facts that are not already present in the raw output.\n"
            "If the raw output is partial, salvage the structure conservatively.\n\n"
            "[EXPECTED_JSON_SHAPE]\n"
            f"{json_example}\n\n"
            "[RAW_OUTPUT]\n"
            f"{self._clip(raw_output, MAX_JSON_REPAIR_CHARS)}\n"
        )

    def _target_path(self, target: CompiledBriefingTarget) -> Path:
        existing_path = target.existing_path.strip()
        if existing_path.startswith("compiled/") and existing_path.endswith(".md"):
            candidate = (self.vault_path / existing_path).resolve()
            try:
                rel_path = candidate.relative_to(self.compiled_root)
            except ValueError:
                return self.compiled_root / target.domain / f"{target.slug}.md"
            if rel_path.parts and rel_path.parts[0] == "archive":
                return self.compiled_root / target.domain / f"{target.slug}.md"
            return candidate
        return self.compiled_root / target.domain / f"{target.slug}.md"

    def _build_impact_prompt(
        self,
        *,
        source_rel_path: str,
        source_excerpt: str,
        signal: dict[str, Any] | None,
        catalog: list[dict[str, str]],
        max_updates: int,
    ) -> str:
        return (
            "You maintain an LLM-owned compiled markdown knowledge base for a "
            "personal assistant.\n"
            "Output metadata in "
            f"{prompt_language_name(self.content_language)} when natural.\n"
            "Return ONLY JSON.\n\n"
            "Decide whether one changed source note should refresh 0 to "
            f"{max_updates} compiled briefings.\n"
            "One source note may contain multiple unrelated durable threads.\n"
            "This is especially common in daily notes that bundle several meetings, "
            "incidents, decisions, and project updates in one file.\n"
            "First identify the durable threads inside the source, then map only the "
            "strongest recurring threads to compiled briefing updates.\n"
            "Allowed domains and intent:\n"
            + "\n".join(
                f"- {domain}: {hint}" for domain, hint in DOMAIN_HINTS.items()
            )
            + "\n\nRules:\n"
            "- Prefer updating an existing compiled note when clearly appropriate.\n"
            "- Create a new note only for durable entities or threads likely to "
            "matter again.\n"
            "- Do not create notes for one-off trivial mentions.\n"
            "- Decisions are only for consequential decisions or explicit "
            "commitments.\n"
            "- Meetings are only for recurring series, strategic negotiations, or "
            "threads with ongoing context.\n"
            "- If the source is mixed, split it mentally into 1-6 durable threads "
            "before deciding updates.\n"
            "- Prefer 0-2 strong updates over many weak updates when one daily note "
            "covers many topics.\n"
            "- Return an empty updates list if the source is too weak or too noisy, "
            "but STILL return a valid JSON object.\n"
            "- Use concise stable titles.\n"
            "- Slug must be lowercase kebab-case ASCII.\n\n"
            "Return JSON exactly like:\n"
            "{\n"
            '  "source_shape": "single|mixed|noisy",\n'
            '  "durable_threads": [\n'
            "    {\n"
            '      "label": "short thread label",\n'
            '      "why": "why this thread is durable or recurring"\n'
            "    }\n"
            "  ],\n"
            '  "updates": [\n'
            "    {\n"
            '      "domain": "projects|people|topics|decisions|meetings",\n'
            '      "title": "brief title",\n'
            '      "slug": "brief-slug",\n'
            '      "description": "one-line search snippet",\n'
            '      "reason": "why this briefing should refresh",\n'
            '      "existing_path": "compiled/<domain>/<slug>.md or empty"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Mixed daily-note example:\n"
            "{\n"
            '  "source_shape": "mixed",\n'
            '  "durable_threads": [\n'
            '    {"label": "db incident recovery", "why": "operational risk '
            'likely to matter again"},\n'
            '    {"label": "partner negotiation boundary", "why": "decision '
            'with ongoing consequences"}\n'
            "  ],\n"
            '  "updates": [\n'
            "    {\n"
            '      "domain": "projects",\n'
            '      "title": "Migration Incident Risk",\n'
            '      "slug": "migration-incident-risk",\n'
            '      "description": "Operational incident and recovery risk in '
            'migration track",\n'
            '      "reason": "Daily note contains a durable incident thread that '
            'changes project risk",\n'
            '      "existing_path": "compiled/projects/migration-incident-risk.md"\n'
            "    },\n"
            "    {\n"
            '      "domain": "decisions",\n'
            '      "title": "No self-estimates for partner options",\n'
            '      "slug": "no-self-estimates-for-partner-options",\n'
            '      "description": "Decision boundary on who owns estimates and '
            'commitments",\n'
            '      "reason": "Daily note records an explicit durable decision",\n'
            '      "existing_path": ""\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "If you are unsure, output:\n"
            '{ "source_shape": "noisy", "durable_threads": [], "updates": [] }\n\n'
            "[EXISTING_COMPILED_CATALOG]\n"
            f"{json.dumps(catalog, ensure_ascii=False, indent=2)}\n\n"
            "[SOURCE_PATH]\n"
            f"{source_rel_path}\n\n"
            "[SOURCE_MEMORY_SIGNAL]\n"
            f"{json.dumps(signal or {}, ensure_ascii=False, indent=2)}\n\n"
            "[SOURCE_EXCERPT]\n"
            f"{source_excerpt}\n"
        )

    def _build_compile_prompt(
        self,
        *,
        target: CompiledBriefingTarget,
        source_rel_path: str,
        source_excerpt: str,
        signal: dict[str, Any] | None,
        existing_text: str,
    ) -> str:
        return (
            "Maintain one compiled briefing for a personal assistant knowledge base.\n"
            "Write all generated text in "
            f"{prompt_language_name(self.content_language)}.\n"
            "Return ONLY JSON.\n\n"
            "You are given the current briefing markdown, if any, and one changed "
            "source note.\n"
            "Produce a concise, durable operational briefing. Keep stable facts, "
            "incorporate new evidence, and avoid invention.\n\n"
            "Required JSON schema:\n"
            "{\n"
            '  "description": "one-line snippet",\n'
            '  "status": "active|draft|pending|done|inactive",\n'
            '  "freshness_state": "fresh|watch|stale",\n'
            '  "confidence": "high|medium|low",\n'
            '  "current_state": "short paragraph",\n'
            '  "recent_changes": ["bullet", "..."],\n'
            '  "open_loops": ["bullet", "..."],\n'
            '  "key_decisions": ["bullet", "..."],\n'
            '  "record_kind": "decision|incident|briefing",\n'
            '  "decision_status": "proposed|accepted|rejected|superseded",\n'
            '  "decision_owner": "owner or empty",\n'
            '  "decision_date": "YYYY-MM-DD or empty",\n'
            '  "rationale": "decision rationale or empty",\n'
            '  "alternatives_considered": ["bullet", "..."],\n'
            '  "supersedes": ["decision identifier", "..."],\n'
            '  "superseded_by": "decision identifier or empty",\n'
            '  "decision_evidence": ["vault/relative/path.md", "..."],\n'
            '  "incident_date": "YYYY-MM-DD or empty",\n'
            '  "severity": "low|medium|high|critical or empty",\n'
            '  "timeline": ["timestamped event", "..."],\n'
            '  "root_cause": "root cause or empty",\n'
            '  "what_worked": ["bullet", "..."],\n'
            '  "what_did_not_work": ["bullet", "..."],\n'
            '  "corrective_actions": ["bullet", "..."],\n'
            '  "generalizable_learning": "reusable learning or empty",\n'
            '  "next_check": "short line",\n'
            '  "source_links": ["vault/relative/path.md", "..."]\n'
            "}\n\n"
            "Rules:\n"
            "- Be specific, short, and cumulative.\n"
            "- Do not claim certainty beyond the available evidence.\n"
            "- Preserve older still-valid context from the existing note.\n"
            "- Prefer 2-5 bullets per list. Empty lists are allowed when truly "
            "absent.\n"
            "- source_links must contain only vault-relative paths.\n"
            "- Include the current source path in source_links if it matters.\n"
            "- For the decisions domain, choose record_kind decision for an ADR "
            "or incident for an operational debrief and fill the matching fields.\n"
            "- Accepted decisions are immutable. Preserve their decision, owner, "
            "date, rationale, and alternatives unless a new decision explicitly "
            "supersedes them; then set decision_status=superseded and "
            "superseded_by.\n"
            "- Incident debriefs should capture timeline, root cause, what worked, "
            "what did not work, corrective actions, and generalizable learning.\n"
            "- Do not use markdown fences.\n\n"
            "[TARGET]\n"
            f"{json.dumps(asdict(target), ensure_ascii=False, indent=2)}\n\n"
            "[SOURCE_PATH]\n"
            f"{source_rel_path}\n\n"
            "[SOURCE_MEMORY_SIGNAL]\n"
            f"{json.dumps(signal or {}, ensure_ascii=False, indent=2)}\n\n"
            "[EXISTING_BRIEFING_MARKDOWN]\n"
            f"{self._clip(existing_text, MAX_EXISTING_NOTE_CHARS) or '(none)'}\n\n"
            "[CHANGED_SOURCE_EXCERPT]\n"
            f"{source_excerpt}\n"
        )

    def _render_briefing(
        self,
        *,
        target: CompiledBriefingTarget,
        payload: dict[str, Any],
        source_rel_path: str,
        existing_text: str,
        existing_meta: dict[str, str],
        signal: dict[str, Any] | None,
    ) -> str:
        today = date.today().isoformat()
        compiled_at = datetime.now().astimezone().isoformat(timespec="seconds")
        description = self._clean_line(payload.get("description")) or target.description
        status = str(payload.get("status") or existing_meta.get("status") or "active")
        if status not in STATUS_VALUES:
            status = "active"
        freshness_state = str(
            payload.get("freshness_state")
            or existing_meta.get("freshness_state")
            or "watch"
        )
        if freshness_state not in FRESHNESS_VALUES:
            freshness_state = "watch"
        confidence = str(
            payload.get("confidence")
            or existing_meta.get("confidence")
            or "medium"
        )
        if confidence not in CONFIDENCE_VALUES:
            confidence = "medium"

        current_state = self._paragraph(payload.get("current_state"))
        recent_changes = self._normalize_list(payload.get("recent_changes"))
        open_loops = self._normalize_list(payload.get("open_loops"))
        key_decisions = self._normalize_list(payload.get("key_decisions"))
        next_check = self._clean_line(payload.get("next_check"))

        record_kind = ""
        decision_status = ""
        decision_owner = ""
        decision_date = ""
        rationale = ""
        alternatives: list[str] = []
        supersedes: list[str] = []
        superseded_by = ""
        decision_evidence: list[str] = []
        incident_date = ""
        severity = ""
        timeline: list[str] = []
        root_cause = ""
        what_worked: list[str] = []
        what_did_not_work: list[str] = []
        corrective_actions: list[str] = []
        generalizable_learning = ""
        if target.domain == "decisions":
            existing_record_kind = self._metadata_value(
                existing_meta.get("record_kind")
            )
            existing_decision_status = self._metadata_value(
                existing_meta.get("decision_status")
            )
            record_kind = self._metadata_value(
                payload.get("record_kind")
                or existing_record_kind
                or "decision"
            )
            if (
                existing_record_kind == "decision"
                and existing_decision_status == "accepted"
            ):
                record_kind = "decision"
            if record_kind not in RECORD_KIND_VALUES:
                record_kind = "decision"
            if record_kind == "decision":
                decision_status = self._metadata_value(
                    payload.get("decision_status")
                    or existing_meta.get("decision_status")
                    or "proposed"
                )
                if decision_status not in DECISION_STATUS_VALUES:
                    decision_status = "proposed"
                decision_owner = self._metadata_value(
                    payload.get("decision_owner")
                    or existing_meta.get("decision_owner")
                )
                decision_date = self._metadata_value(
                    payload.get("decision_date")
                    or existing_meta.get("decision_date")
                )
                rationale = self._paragraph(payload.get("rationale"))
                alternatives = self._normalize_list(
                    payload.get("alternatives_considered")
                )
                supersedes = self._normalize_list(payload.get("supersedes"))
                superseded_by = self._clean_line(payload.get("superseded_by"))
                decision_evidence = self._normalize_paths(
                    payload.get("decision_evidence")
                )

                explicit_supersession = (
                    decision_status == "superseded" and bool(superseded_by)
                )
                if existing_decision_status == "accepted" and not explicit_supersession:
                    decision_status = "accepted"
                    decision_owner = self._metadata_value(
                        existing_meta.get("decision_owner")
                    ) or decision_owner
                    decision_date = self._metadata_value(
                        existing_meta.get("decision_date")
                    ) or decision_date
                    rationale = (
                        self._section_text(existing_text, "Rationale") or rationale
                    )
                    alternatives = self._section_bullets(
                        existing_text,
                        "Alternatives Considered",
                    ) or alternatives
                    key_decisions = self._section_bullets(
                        existing_text,
                        "Key Decisions",
                    ) or key_decisions
            elif record_kind == "incident":
                incident_date = self._metadata_value(
                    payload.get("incident_date")
                    or existing_meta.get("incident_date")
                )
                severity = self._metadata_value(
                    payload.get("severity") or existing_meta.get("severity")
                )
                timeline = self._normalize_list(payload.get("timeline"))
                root_cause = self._paragraph(payload.get("root_cause"))
                what_worked = self._normalize_list(payload.get("what_worked"))
                what_did_not_work = self._normalize_list(
                    payload.get("what_did_not_work")
                )
                corrective_actions = self._normalize_list(
                    payload.get("corrective_actions")
                )
                generalizable_learning = self._paragraph(
                    payload.get("generalizable_learning")
                )

        old_sources = self._source_links_from_note(existing_text)
        new_sources = self._normalize_paths(payload.get("source_links"))
        all_sources = self._merge_paths(old_sources, [source_rel_path, *new_sources])
        created = existing_meta.get("created") or today
        relevance = self._merged_relevance(existing_meta, signal)
        tier = self._merged_tier(existing_meta, signal)

        lines = [
            "---",
            "type: compiled-briefing",
            f"domain: {target.domain}",
            f"description: {json.dumps(description, ensure_ascii=False)}",
            f"status: {status}",
            f"created: {created}",
            f"updated: {today}",
            f"last_compiled_at: {compiled_at}",
            f"freshness_state: {freshness_state}",
            f"confidence: {confidence}",
            f"source_count: {len(all_sources)}",
            f"last_accessed: {today}",
            f"relevance: {relevance:.2f}",
            f"tier: {tier}",
        ]
        if record_kind:
            lines.append(f"record_kind: {record_kind}")
        if record_kind == "decision":
            lines.extend(
                [
                    f"decision_status: {decision_status}",
                    f"decision_owner: {json.dumps(decision_owner, ensure_ascii=False)}",
                    f"decision_date: {decision_date}",
                    f"supersedes: {json.dumps(supersedes, ensure_ascii=False)}",
                    f"superseded_by: {json.dumps(superseded_by, ensure_ascii=False)}",
                ]
            )
        elif record_kind == "incident":
            lines.extend(
                [
                    f"incident_date: {incident_date}",
                    f"severity: {severity}",
                ]
            )
        lines.extend(["---", "", f"# {target.title}", ""])
        if record_kind == "decision":
            lines.extend(
                [
                    "## Decision Record",
                    f"- Status: {decision_status}",
                    f"- Date: {decision_date or '(unknown)'}",
                    f"- Owner: {decision_owner or '(unknown)'}",
                    *([f"- Supersedes: {', '.join(supersedes)}"] if supersedes else []),
                    *([f"- Superseded by: {superseded_by}"] if superseded_by else []),
                    "",
                    "## Rationale",
                    rationale or "No rationale captured yet.",
                    "",
                    "## Alternatives Considered",
                    *self._render_bullets(
                        alternatives,
                        empty="No alternatives captured yet.",
                    ),
                    "",
                    "## Decision Evidence",
                    *self._render_sources(decision_evidence),
                    "",
                ]
            )
        elif record_kind == "incident":
            lines.extend(
                [
                    "## Incident Debrief",
                    f"- Date: {incident_date or '(unknown)'}",
                    f"- Severity: {severity or '(unknown)'}",
                    "",
                    "## Timeline",
                    *self._render_bullets(timeline, empty="No timeline captured yet."),
                    "",
                    "## Root Cause",
                    root_cause or "No reliable root cause captured yet.",
                    "",
                    "## What Worked",
                    *self._render_bullets(
                        what_worked,
                        empty="Nothing captured yet.",
                    ),
                    "",
                    "## What Did Not Work",
                    *self._render_bullets(
                        what_did_not_work,
                        empty="Nothing captured yet.",
                    ),
                    "",
                    "## Corrective Actions",
                    *self._render_bullets(
                        corrective_actions,
                        empty="No corrective actions captured yet.",
                    ),
                    "",
                    "## Generalizable Learning",
                    generalizable_learning or "No reusable learning captured yet.",
                    "",
                ]
            )
        lines.extend(
            [
                "## Current State",
                current_state or "No reliable current state yet.",
                "",
                "## Recent Changes",
                *self._render_bullets(
                    recent_changes,
                    empty="No recent changes captured yet.",
                ),
                "",
                "## Open Loops",
                *self._render_bullets(
                    open_loops,
                    empty="No open loops captured yet.",
                ),
                "",
                "## Key Decisions",
                *self._render_bullets(
                    key_decisions,
                    empty="No key decisions captured yet.",
                ),
                "",
                "## Next Check",
                next_check or "Review after the next meaningful update.",
                "",
                "## Sources",
                *self._render_sources(all_sources),
                "",
            ]
        )
        return "\n".join(lines)

    def _catalog(self) -> list[dict[str, str]]:
        catalog: list[dict[str, str]] = []
        for candidate in self._iter_candidates():
            catalog.append(
                {
                    "path": candidate.rel_path,
                    "domain": candidate.domain,
                    "slug": candidate.slug,
                    "title": candidate.title,
                    "description": candidate.description,
                }
            )
        return catalog

    def _impact_catalog(
        self,
        *,
        source_rel_path: str,
        source_excerpt: str,
        max_items: int = IMPACT_CATALOG_MAX_ITEMS,
        max_chars: int = IMPACT_CATALOG_MAX_CHARS,
    ) -> list[dict[str, str]]:
        """Return a bounded, source-aware compiled catalog for impact routing."""
        query_tokens = self._tokens(f"{source_rel_path}\n{source_excerpt}")
        scored: list[tuple[tuple[int, int, float, str], dict[str, str]]] = []

        for candidate in self._iter_candidates():
            source_links = self._source_links_from_note(candidate.text)
            source_link_match = 1 if source_rel_path in source_links else 0
            haystack = " ".join(
                [
                    candidate.domain,
                    candidate.slug,
                    candidate.title,
                    candidate.description,
                    " ".join(source_links),
                ]
            )
            overlap = len(query_tokens & self._tokens(haystack))
            item = {
                "path": candidate.rel_path,
                "domain": candidate.domain,
                "slug": candidate.slug,
                "title": candidate.title,
                "description": candidate.description,
            }
            scored.append(
                (
                    (
                        source_link_match,
                        overlap,
                        round(candidate.relevance, 4),
                        candidate.rel_path,
                    ),
                    item,
                )
            )

        if not scored:
            return []

        scored.sort(key=lambda item: item[0], reverse=True)
        selected: list[dict[str, str]] = []
        current_chars = 2  # []
        for _, item in scored:
            encoded = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if selected and len(selected) >= max_items:
                break
            if selected and current_chars + encoded + 2 > max_chars:
                break
            selected.append(item)
            current_chars += encoded + 2

        return selected or [scored[0][1]]

    def _iter_candidates(self) -> list[CompiledBriefingCandidate]:
        if not self.compiled_root.exists():
            return []
        candidates: list[CompiledBriefingCandidate] = []
        for path in sorted(self.compiled_root.glob("**/*.md")):
            rel_path = path.relative_to(self.vault_path).as_posix()
            if rel_path.startswith("compiled/archive/"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            fields = self._frontmatter_fields(text)
            title = self._title_from_text(text) or path.stem.replace("-", " ")
            candidates.append(
                CompiledBriefingCandidate(
                    rel_path=rel_path,
                    domain=str(fields.get("domain") or path.parent.name),
                    slug=path.stem,
                    title=title.strip(),
                    description=str(fields.get("description") or "").strip().strip('"'),
                    freshness_state=str(fields.get("freshness_state") or ""),
                    confidence=str(fields.get("confidence") or ""),
                    relevance=self._float_value(fields.get("relevance"), default=0.0),
                    tier=str(fields.get("tier") or ""),
                    text=text,
                )
            )
        return candidates

    def _rank_candidates(
        self,
        question: str,
        *,
        limit: int,
    ) -> list[CompiledBriefingCandidate]:
        query = (question or "").strip()
        if not query:
            return []
        query_lower = query.lower()
        query_tokens = self._tokens(query_lower)
        scored: list[tuple[float, CompiledBriefingCandidate]] = []
        for candidate in self._iter_candidates():
            title_lower = candidate.title.lower()
            description_lower = candidate.description.lower()
            body_lower = self._clip(
                self._body_without_frontmatter(candidate.text).lower(),
                MAX_BODY_SNIPPET_CHARS,
            )
            score = 0.0
            if title_lower and title_lower in query_lower:
                score += 8.0
            if candidate.slug and candidate.slug.replace("-", " ") in query_lower:
                score += 5.0
            title_tokens = self._tokens(title_lower)
            description_tokens = self._tokens(description_lower)
            body_tokens = self._tokens(body_lower)
            score += 2.4 * len(query_tokens & title_tokens)
            score += 1.2 * len(query_tokens & description_tokens)
            score += 0.25 * len(query_tokens & body_tokens)
            for hint in QUESTION_DOMAIN_HINTS.get(candidate.domain, ()):
                if hint in query_lower:
                    score += 1.2
                    break
            if candidate.freshness_state == "fresh":
                score += 0.7
            elif candidate.freshness_state == "watch":
                score += 0.25
            elif candidate.freshness_state == "stale":
                score -= 0.2
            if candidate.confidence == "high":
                score += 0.4
            elif candidate.confidence == "medium":
                score += 0.15
            elif candidate.confidence == "low":
                score -= 0.15
            score += max(candidate.relevance, 0.0) * 0.6
            score += TIER_RANK.get(candidate.tier, 0) * 0.05
            if score <= 0:
                continue
            scored.append((score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return []
        top_score = scored[0][0]
        min_score = max(1.5, top_score * 0.35)
        filtered = [
            candidate
            for score, candidate in scored
            if score >= min_score
        ]
        return filtered[:limit]

    def _ensure_dirs(self) -> None:
        for domain in COMPILED_BRIEFING_DOMAINS:
            (self.compiled_root / domain).mkdir(parents=True, exist_ok=True)

    def _ensure_state_dirs(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.answers_root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _launcher_lock(self, *, blocking: bool) -> Iterator[bool]:
        self._ensure_state_dirs()
        with self.launcher_lock_path.open("a+", encoding="utf-8") as handle:
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _worker_lock(self, *, blocking: bool) -> Iterator[bool]:
        self._ensure_state_dirs()
        with self.worker_lock_path.open("a+", encoding="utf-8") as handle:
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _with_queue_lock(
        self,
        callback: Callable[[], QueueLockResult],
    ) -> QueueLockResult:
        self._ensure_state_dirs()
        with self.queue_lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                return callback()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _source_state_lock(self) -> Iterator[None]:
        self._ensure_state_dirs()
        with self.queue_lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_source_state(self) -> dict[str, Any]:
        with self._source_state_lock():
            return self._load_source_state_unlocked()

    def _load_source_state_unlocked(self) -> dict[str, Any]:
        if not self.source_state_path.exists():
            return {"version": SOURCE_STATE_VERSION, "entries": {}}
        try:
            payload = json.loads(
                self.source_state_path.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid compiled source state: {self.source_state_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("compiled source state must be a JSON object")
        if payload.get("version") != SOURCE_STATE_VERSION:
            raise ValueError("unsupported compiled source state version")
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            raise ValueError("compiled source state entries must be a JSON object")
        for rel_path, entry in entries.items():
            if not isinstance(rel_path, str) or not isinstance(entry, dict):
                raise ValueError("invalid compiled source state entry")
            if not isinstance(entry.get("sources"), dict):
                raise ValueError(f"invalid compiled source state entry: {rel_path}")
        return payload

    def _write_source_state_unlocked(self, state: dict[str, Any]) -> None:
        _atomic_write_text(
            self.source_state_path,
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _load_worker_state(self) -> dict[str, Any]:
        if not self.worker_state_path.exists():
            return {}
        try:
            payload = json.loads(self.worker_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning(
                "Invalid compiled worker state payload at %s",
                self.worker_state_path,
            )
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_worker_state(self, payload: dict[str, Any]) -> None:
        _atomic_write_text(
            self.worker_state_path,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _clear_worker_state_unlocked(self) -> None:
        self.worker_state_path.unlink(missing_ok=True)

    def _parse_iso_datetime(self, raw: str) -> datetime | None:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _pid_is_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _worker_state_is_live(
        self,
        state: dict[str, Any],
        *,
        stale_after_seconds: int = DEFAULT_WORKER_STALE_SECONDS,
    ) -> bool:
        pid = int(state.get("pid") or 0)
        heartbeat_raw = str(state.get("heartbeat_at") or state.get("started_at") or "")
        heartbeat_at = self._parse_iso_datetime(heartbeat_raw)
        if not self._pid_is_alive(pid):
            return False
        if heartbeat_at is None:
            return True
        age_seconds = (
            datetime.now().astimezone() - heartbeat_at.astimezone()
        ).total_seconds()
        return age_seconds <= stale_after_seconds

    def _write_worker_state(
        self,
        *,
        pid: int,
        status: str,
        started_at: str | None = None,
    ) -> None:
        now = datetime.now().astimezone().isoformat()

        def write_state() -> None:
            state = self._load_worker_state()
            existing_started_at = str(state.get("started_at") or "").strip()
            self._save_worker_state(
                {
                    "pid": pid,
                    "status": status,
                    "started_at": started_at or existing_started_at or now,
                    "heartbeat_at": now,
                }
            )

        with self._launcher_lock(blocking=True):
            write_state()

    def _touch_worker_state(self, pid: int) -> None:
        self._write_worker_state(pid=pid, status="running")

    def _clear_worker_state(self, *, pid: int | None = None) -> None:
        def clear_state() -> None:
            if pid is None:
                self._clear_worker_state_unlocked()
                return
            state = self._load_worker_state()
            current_pid = int(state.get("pid") or 0)
            if current_pid in {0, pid}:
                self._clear_worker_state_unlocked()

        with self._launcher_lock(blocking=True):
            clear_state()

    def _load_queue(self) -> list[dict[str, Any]]:
        if not self.queue_path.exists():
            return []
        try:
            payload = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Invalid compiled queue payload at %s", self.queue_path)
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _save_queue(self, payload: list[dict[str, Any]]) -> None:
        _atomic_write_text(
            self.queue_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _recover_stale_claims(
        self,
        queue: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        now = datetime.now().astimezone()
        mutated = False
        for event in queue:
            if str(event.get("state") or "pending") != "in_flight":
                continue
            claim_token = str(event.get("claim_token") or "").strip()
            if not claim_token:
                event["state"] = "pending"
                event["claimed_at"] = ""
                event["claimed_pid"] = 0
                mutated = True
                continue
            claimed_at = self._parse_iso_datetime(str(event.get("claimed_at") or ""))
            claimed_pid = int(event.get("claimed_pid") or 0)
            stale_claim = (
                claimed_at is None
                or (now - claimed_at.astimezone()).total_seconds()
                > DEFAULT_QUEUE_CLAIM_STALE_SECONDS
                or not self._pid_is_alive(claimed_pid)
            )
            if not stale_claim:
                continue
            event["state"] = "pending"
            event["claim_token"] = ""
            event["claimed_at"] = ""
            event["claimed_pid"] = 0
            mutated = True
        return queue, mutated

    def _claim_ready_queue_events(
        self,
        *,
        force: bool,
        max_events: int,
    ) -> list[dict[str, Any]]:
        now = datetime.now().astimezone()
        now_ts = now.timestamp()

        def claim_events() -> list[dict[str, Any]]:
            queue = self._load_queue()
            queue, recovered = self._recover_stale_claims(queue)
            ready_indices: list[int] = []
            for index, event in enumerate(queue):
                if str(event.get("state") or "pending") != "pending":
                    continue
                due_at = float(event.get("due_at") or 0)
                if force or due_at <= now_ts:
                    ready_indices.append(index)
            selected: list[dict[str, Any]] = []
            for index in ready_indices[:max_events]:
                event = queue[index]
                claim_token = uuid4().hex
                event["state"] = "in_flight"
                event["claim_token"] = claim_token
                event["claimed_at"] = now.isoformat()
                event["claimed_pid"] = os.getpid()
                selected.append(dict(event))
            if recovered or selected:
                self._save_queue(queue)
            return selected

        return self._with_queue_lock(claim_events)

    def _ack_claimed_queue_event(self, event: dict[str, Any]) -> None:
        source_path = str(event.get("source_path") or "")
        claim_token = str(event.get("claim_token") or "")

        def ack_event() -> None:
            queue = self._load_queue()
            updated = [
                item
                for item in queue
                if not (
                    str(item.get("source_path") or "") == source_path
                    and str(item.get("claim_token") or "") == claim_token
                    and str(item.get("state") or "") == "in_flight"
                )
            ]
            if len(updated) != len(queue):
                self._save_queue(updated)

        self._with_queue_lock(ack_event)

    def _release_claimed_queue_event(
        self,
        event: dict[str, Any],
        *,
        attempts: int,
        due_at: float,
    ) -> None:
        source_path = str(event.get("source_path") or "")
        claim_token = str(event.get("claim_token") or "")

        def release_event() -> None:
            queue = self._load_queue()
            updated = False
            for item in queue:
                if str(item.get("source_path") or "") != source_path:
                    continue
                if str(item.get("claim_token") or "") != claim_token:
                    continue
                if str(item.get("state") or "") != "in_flight":
                    continue
                item["attempts"] = attempts
                item["due_at"] = due_at
                item["state"] = "pending"
                item["claim_token"] = ""
                item["claimed_at"] = ""
                item["claimed_pid"] = 0
                updated = True
                break
            if updated:
                self._save_queue(queue)

        self._with_queue_lock(release_event)

    def _normalize_rel_path(self, value: str | Path) -> str:
        path = Path(value)
        try:
            rel_path = path.relative_to(self.vault_path).as_posix()
        except ValueError:
            rel_path = path.as_posix()
        return self._strip_relative_prefix(rel_path)

    def _resolve_lint_source_path(self, source: str) -> Path | None:
        raw = self._normalize_lint_source(source)
        if not raw:
            return None

        if Path(raw).is_absolute():
            return None

        candidates: list[Path] = []
        for source_variant in self._lint_source_variants(raw):
            base_dir = self._lint_source_base_dir(source_variant)
            source_path = Path(source_variant)
            candidate = (base_dir / source_path).resolve()
            try:
                candidate.relative_to(base_dir)
            except ValueError:
                continue
            candidates.append(candidate)
            if candidate.exists():
                return candidate
            if not source_path.suffix:
                md_candidate = candidate.with_suffix(".md")
                candidates.append(md_candidate)
                if md_candidate.exists():
                    return md_candidate
        return candidates[0] if candidates else None

    def _normalize_lint_source(self, source: str) -> str:
        raw = str(source or "").strip()
        raw = raw.split("#", 1)[0].strip()
        raw = self._strip_relative_prefix(raw)
        if raw.startswith("vault/"):
            raw = raw[len("vault/") :]
        if raw.startswith("claude/"):
            raw = f".{raw}"
        return raw

    @staticmethod
    def _lint_source_variants(raw: str) -> list[str]:
        variants = [raw]
        for visible_prefix, hidden_prefix in HIDDEN_STATE_SOURCE_PREFIXES.items():
            if raw.startswith(visible_prefix):
                variants.append(f"{hidden_prefix}{raw[len(visible_prefix) :]}")
        return variants

    def _lint_source_base_dir(self, raw: str) -> Path:
        if (
            raw.startswith(PROJECT_ROOT_SOURCE_PREFIXES)
            or raw in PROJECT_ROOT_SOURCE_FILES
        ):
            return self.vault_path.parent
        return self.vault_path

    def _candidate_freshness_issue(
        self,
        candidate: CompiledBriefingCandidate,
        state_entry: Any,
    ) -> dict[str, str] | None:
        if candidate.freshness_state == "stale":
            return {
                "path": candidate.rel_path,
                "issue": "stale",
                "detail": "frontmatter freshness_state=stale",
            }
        if state_entry is None:
            return {
                "path": candidate.rel_path,
                "issue": "source-untracked",
                "detail": "source snapshot has not been initialized",
            }
        stored_sources = state_entry["sources"]
        current_sources = self._source_snapshot(candidate.text)
        if current_sources == stored_sources:
            return None
        changed_sources = sorted(
            source
            for source in set(current_sources) | set(stored_sources)
            if current_sources.get(source) != stored_sources.get(source)
        )
        return {
            "path": candidate.rel_path,
            "issue": "source-changed",
            "detail": f"changed_sources={','.join(changed_sources)}",
        }

    def _source_snapshot(self, note_text: str) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for source in self._source_links_from_note(note_text):
            raw = self._normalize_lint_source(source)
            if not raw or Path(raw).is_absolute():
                continue
            if raw.startswith(SOURCE_STATE_IGNORED_PATH_PREFIXES):
                continue
            resolved = self._resolve_lint_source_path(raw)
            key = self._source_state_key(raw, resolved)
            if not key:
                continue
            if resolved is None or not resolved.exists():
                snapshot[key] = "missing"
                continue
            snapshot[key] = self._semantic_source_digest(resolved)
        return dict(sorted(snapshot.items()))

    def _source_state_key(self, raw: str, resolved: Path | None) -> str:
        if resolved is None:
            return raw
        for root in (self.vault_path, self.vault_path.parent):
            try:
                return resolved.relative_to(root).as_posix()
            except ValueError:
                continue
        return raw

    def _semantic_source_digest(self, path: Path) -> str:
        content = path.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            normalized = content
        else:
            normalized = self._semantic_source_text(text).encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()

    @staticmethod
    def _semantic_source_text(text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n")
        match = FRONTMATTER_RE.match(normalized)
        if match is not None:
            kept_lines = []
            for line in match.group(1).splitlines():
                key = line.partition(":")[0].strip()
                if key in SOURCE_STATE_IGNORED_FRONTMATTER_FIELDS:
                    continue
                kept_lines.append(line.rstrip())
            body = normalized[match.end() :]
            normalized = "---\n" + "\n".join(kept_lines) + "\n---\n" + body
        return normalized.rstrip() + "\n"

    def _record_source_state(self, rel_path: str, note_text: str) -> None:
        snapshot = self._source_snapshot(note_text)
        with self._source_state_lock():
            state = self._load_source_state_unlocked()
            state["entries"][rel_path] = {
                "evaluated_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "sources": snapshot,
            }
            self._write_source_state_unlocked(state)

    def _source_excerpt(self, source_rel_path: str, source_excerpt: str) -> str:
        clipped = self._clip(source_excerpt, MAX_SOURCE_EXCERPT_CHARS)
        if clipped:
            return clipped
        source_path = self._resolve_lint_source_path(source_rel_path)
        if source_path is None or not source_path.exists():
            return ""
        return self._clip(
            source_path.read_text(encoding="utf-8", errors="replace"),
            MAX_SOURCE_EXCERPT_CHARS,
        )

    def _daily_source_chunks(self, source_rel_path: str, source_text: str) -> list[str]:
        text = str(source_text or "").strip()
        if not text:
            return []
        if not source_rel_path.startswith("daily/"):
            return [self._clip(text, MAX_SOURCE_EXCERPT_CHARS)]

        blocks = self._daily_entry_blocks(text)
        if not blocks:
            return self._chunk_text_full(text, MAX_SOURCE_EXCERPT_CHARS)

        title = self._daily_chunk_title(source_rel_path, text)
        body_limit = max(200, MAX_SOURCE_EXCERPT_CHARS - len(title) - 2)
        chunks: list[str] = []
        for block in blocks:
            block_text = block.strip()
            if not block_text:
                continue
            body_chunks = self._chunk_text_full(block_text, body_limit)
            for piece in body_chunks:
                chunk = "\n\n".join(
                    part for part in (title, piece.strip()) if part
                ).strip()
                if chunk:
                    chunks.append(chunk)
        return chunks

    @staticmethod
    def _daily_entry_blocks(text: str) -> list[str]:
        stripped = str(text or "").strip()
        if not stripped:
            return []
        parts = DAILY_ENTRY_SPLIT_RE.split(stripped)
        blocks: list[str] = []
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("## "):
                blocks.append(candidate)
        return blocks

    @staticmethod
    def _daily_chunk_title(source_rel_path: str, source_text: str) -> str:
        first_line = (
            str(source_text or "").splitlines()[0].strip() if source_text else ""
        )
        if first_line.startswith("# "):
            return first_line
        stem = Path(source_rel_path).stem
        return f"# {stem}"

    def _chunk_text_full(self, text: str, limit: int) -> list[str]:
        source = str(text or "").strip()
        if not source:
            return []
        if limit <= 0 or len(source) <= limit:
            return [source]

        paragraphs = [
            part.strip()
            for part in re.split(r"\n{2,}", source)
            if part.strip()
        ]
        if len(paragraphs) <= 1:
            return self._chunk_lines_full(source, limit)

        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            candidate = paragraph if not current else f"{current}\n\n{paragraph}"
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(paragraph) <= limit:
                current = paragraph
                continue
            chunks.extend(self._chunk_lines_full(paragraph, limit))
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _chunk_lines_full(text: str, limit: int) -> list[str]:
        source = str(text or "").strip()
        if not source:
            return []
        if limit <= 0 or len(source) <= limit:
            return [source]

        lines = source.splitlines()
        chunks: list[str] = []
        current = ""
        for line in lines:
            clean = line.rstrip()
            candidate = clean if not current else f"{current}\n{clean}"
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(clean) <= limit:
                current = clean
                continue
            chunks.extend(CompiledBriefingService._split_long_line(clean, limit))
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _split_long_line(text: str, limit: int) -> list[str]:
        source = str(text or "").strip()
        if not source:
            return []
        if limit <= 0 or len(source) <= limit:
            return [source]

        chunks: list[str] = []
        rest = source
        while len(rest) > limit:
            window = rest[: limit + 1]
            split_at = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
            if split_at >= limit // 2:
                split_at += 1
            else:
                split_at = max(window.rfind(" "), window.rfind("\t"))
            if split_at < limit // 2:
                split_at = limit
            piece = rest[:split_at].rstrip()
            if piece:
                chunks.append(piece)
            rest = rest[split_at:].lstrip()
        if rest:
            chunks.append(rest)
        return chunks

    def _resolve_ai_cli(self, explicit_value: str | None) -> str:
        if explicit_value:
            return normalize_ai_cli(explicit_value)
        env_path = self.vault_path.parent / ".env"
        if env_path.exists():
            for raw_line in env_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() != "AI_CLI":
                    continue
                candidate = value.strip().strip('"').strip("'")
                if candidate:
                    try:
                        return normalize_ai_cli(candidate)
                    except ValueError:
                        logger.warning("Invalid AI_CLI in .env: %s", candidate)
                        break
        return "claude"

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        source = str(text or "").strip()
        if limit <= 0 or len(source) <= limit:
            return source
        return source[:limit].rstrip() + "\n...[truncated]"

    @staticmethod
    def _clean_line(value: Any) -> str:
        text = " ".join(str(value or "").split())
        return text.strip()

    @staticmethod
    def _paragraph(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return re.sub(r"\n{3,}", "\n\n", text)

    @staticmethod
    def _slugify(value: str) -> str:
        source = str(value or "").strip().lower()
        translit = source.maketrans(
            {
                "а": "a",
                "б": "b",
                "в": "v",
                "г": "g",
                "д": "d",
                "е": "e",
                "ё": "e",
                "ж": "zh",
                "з": "z",
                "и": "i",
                "й": "y",
                "к": "k",
                "л": "l",
                "м": "m",
                "н": "n",
                "о": "o",
                "п": "p",
                "р": "r",
                "с": "s",
                "т": "t",
                "у": "u",
                "ф": "f",
                "х": "h",
                "ц": "ts",
                "ч": "ch",
                "ш": "sh",
                "щ": "sch",
                "ъ": "",
                "ы": "y",
                "ь": "",
                "э": "e",
                "ю": "yu",
                "я": "ya",
            }
        )
        normalized = source.translate(translit)
        normalized = re.sub(r"[^0-9a-z]+", "-", normalized).strip("-")
        return normalized[:80]

    @staticmethod
    def _normalize_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        lines: list[str] = []
        for item in value:
            cleaned = " ".join(str(item or "").split()).strip()
            if cleaned:
                lines.append(cleaned)
        return lines[:7]

    def _normalize_paths(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        paths: list[str] = []
        project_root = self.vault_path.parent.resolve()
        for item in value:
            raw = str(item or "").strip()
            if not raw:
                continue
            if raw.startswith("/"):
                absolute_path = Path(raw)
                try:
                    raw = absolute_path.relative_to(self.vault_path).as_posix()
                except ValueError:
                    try:
                        raw = absolute_path.relative_to(project_root).as_posix()
                    except ValueError:
                        continue
            raw = self._strip_relative_prefix(raw)
            if raw.startswith("claude/"):
                raw = f".{raw}"
            if raw.startswith("vault/"):
                raw = raw[len("vault/") :]
            if raw.endswith(".md") or "/" in raw:
                paths.append(raw)
        return paths

    @staticmethod
    def _strip_relative_prefix(raw: str) -> str:
        value = str(raw or "").strip()
        while value.startswith("./"):
            value = value[2:]
        return value

    @staticmethod
    def _merge_paths(*groups: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for item in group:
                path = str(item or "").strip()
                if not path or path in seen:
                    continue
                seen.add(path)
                merged.append(path)
        return merged

    @staticmethod
    def _render_bullets(lines: list[str], *, empty: str) -> list[str]:
        if not lines:
            return [f"- {empty}"]
        return [f"- {line}" for line in lines]

    @staticmethod
    def _render_sources(paths: list[str]) -> list[str]:
        if not paths:
            return ["- (none)"]
        return [f"- [[{path}]]" for path in paths]

    @staticmethod
    def _sections_from_text(text: str) -> set[str]:
        return set(re.findall(r"^##\s+(.+?)\s*$", text or "", re.MULTILINE))

    @staticmethod
    def _source_links_from_note(text: str) -> list[str]:
        if not text:
            return []
        match = re.search(
            r"^##\s+Sources\s*$\n([\s\S]*?)(?:^##\s+|\Z)",
            text,
            re.MULTILINE,
        )
        if match is None:
            return []
        return [
            path.strip()
            for path in WIKILINK_RE.findall(match.group(1))
            if path.strip()
        ]

    @staticmethod
    def _section_text(text: str, heading: str) -> str:
        match = re.search(
            rf"^##\s+{re.escape(heading)}\s*$\n([\s\S]*?)(?:^##\s+|\Z)",
            text or "",
            re.MULTILINE,
        )
        return match.group(1).strip() if match is not None else ""

    @classmethod
    def _section_bullets(cls, text: str, heading: str) -> list[str]:
        section = cls._section_text(text, heading)
        return [
            line[2:].strip()
            for line in section.splitlines()
            if line.startswith("- ") and line[2:].strip()
        ]

    @staticmethod
    def _metadata_value(value: Any) -> str:
        raw = str(value or "").strip()
        if raw.startswith('"') and raw.endswith('"'):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return raw.strip('"')
            return str(decoded).strip()
        return raw

    @staticmethod
    def _frontmatter_fields(text: str) -> dict[str, str]:
        match = FRONTMATTER_RE.match(text or "")
        if match is None:
            return {}
        fields: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        return fields

    @staticmethod
    def _title_from_text(text: str) -> str:
        match = TITLE_RE.search(text or "")
        return str(match.group(1)).strip() if match else ""

    @staticmethod
    def _body_without_frontmatter(text: str) -> str:
        match = FRONTMATTER_RE.match(text or "")
        if match is None:
            return text
        return text[match.end() :]

    @staticmethod
    def _float_value(value: str | None, *, default: float) -> float:
        try:
            return float(str(value or "").strip())
        except ValueError:
            return default

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token.lower() for token in TOKEN_RE.findall(text or "")}

    def _merged_relevance(
        self,
        existing_meta: dict[str, str],
        signal: dict[str, Any] | None,
    ) -> float:
        existing = self._float_value(existing_meta.get("relevance"), default=0.0)
        if signal is None:
            return max(existing, 0.72)
        try:
            source_relevance = float(signal.get("relevance", 0) or 0)
        except (TypeError, ValueError):
            source_relevance = 0.0
        return max(existing, source_relevance, 0.72)

    def _merged_tier(
        self,
        existing_meta: dict[str, str],
        signal: dict[str, Any] | None,
    ) -> str:
        existing = str(existing_meta.get("tier") or "").strip().lower()
        source = str((signal or {}).get("tier") or "").strip().lower()
        existing_rank = TIER_RANK.get(existing, 0)
        source_rank = TIER_RANK.get(source, 0)
        if existing_rank >= source_rank and existing:
            return existing
        if source:
            return source
        return "active"

    def _archive_stale_notes(self, *, limit: int) -> list[str]:
        archived: list[str] = []
        if limit <= 0:
            return archived
        for candidate in self._iter_candidates():
            if len(archived) >= limit:
                break
            if candidate.freshness_state != "stale":
                continue
            status = self._frontmatter_fields(candidate.text).get("status", "")
            if status not in {"done", "inactive"}:
                continue
            archived_path = self._archive_candidate(candidate)
            if not archived_path:
                continue
            archived.append(archived_path)
        return archived

    def _backfill_freshness_notes(self, *, limit: int) -> list[str]:
        refreshed: list[str] = []
        if limit <= 0 or not self.is_available():
            return refreshed
        source_state = self._load_source_state()
        state_entries = source_state["entries"]
        attempts = 0
        for candidate in self._iter_candidates():
            if attempts >= limit:
                break
            state_entry = state_entries.get(candidate.rel_path)
            issue = self._candidate_freshness_issue(candidate, state_entry)
            if issue is None or issue["issue"] == "source-untracked":
                continue
            source_paths = self._freshness_source_paths(candidate.text, state_entry)
            if not source_paths:
                continue
            if issue["issue"] == "stale":
                source_paths = source_paths[:1]
            attempts += 1
            try:
                rel_path = self._refresh_candidate(
                    candidate,
                    source_paths=source_paths,
                )
            except (
                CliExecutionError,
                CompiledBriefingWriteConflict,
                FileNotFoundError,
                TimeoutError,
                ValueError,
            ) as exc:
                logger.warning(
                    "Compiled briefing freshness refresh failed for %s: %s",
                    candidate.rel_path,
                    exc,
                )
                continue
            if rel_path and rel_path not in refreshed:
                refreshed.append(rel_path)
        return refreshed

    def _freshness_source_paths(
        self,
        note_text: str,
        state_entry: Any,
    ) -> list[str]:
        current = self._source_snapshot(note_text)
        stored = state_entry["sources"] if state_entry is not None else {}
        changed = {
            source
            for source in set(current) | set(stored)
            if current.get(source) != stored.get(source)
        }
        existing_changed = [
            source
            for source in sorted(changed)
            if current.get(source) not in {None, "missing"}
        ]
        if existing_changed:
            return existing_changed
        return [
            source
            for source, digest in current.items()
            if digest != "missing"
        ]

    def _refresh_candidate(
        self,
        candidate: CompiledBriefingCandidate,
        *,
        source_paths: list[str],
    ) -> str:
        updated_path = ""
        for source_rel_path in source_paths:
            excerpt = self._source_excerpt(source_rel_path, "")
            if not excerpt:
                continue
            target = CompiledBriefingTarget(
                domain=candidate.domain,
                title=candidate.title,
                slug=candidate.slug,
                description=candidate.description,
                reason="semantic source snapshot changed",
                existing_path=candidate.rel_path,
            )
            updated_path = self._upsert_briefing(
                target=target,
                source_rel_path=source_rel_path,
                source_excerpt=excerpt,
                signal=self.qmd._memory_signal_for_rel_path(source_rel_path),
                record_source_state=False,
            )
        if not updated_path:
            raise ValueError("no readable source for freshness refresh")
        updated_text = (self.vault_path / updated_path).read_text(
            encoding="utf-8",
            errors="replace",
        )
        self._record_source_state(updated_path, updated_text)
        return updated_path

    def _archive_candidate(self, candidate: CompiledBriefingCandidate) -> str:
        source_path = self.vault_path / candidate.rel_path
        if not source_path.exists():
            return ""
        archive_dir = self.compiled_root / "archive" / candidate.domain
        with vault_write_lock(self.vault_path) as lock:
            source_content = source_path.read_bytes()
            document = parse_frontmatter_bytes(source_content)
            today = date.today().isoformat()
            metadata_updates = {
                key: value
                for key, value in {
                    "type": "compiled-briefing",
                    "description": candidate.description or candidate.title,
                    "last_accessed": today,
                    "relevance": 1.0,
                    "tier": "active",
                }.items()
                if document.fields.get(key) in (None, "", [])
            }
            if metadata_updates:
                source_content = patch_frontmatter_bytes(
                    source_content, metadata_updates
                )
            archive_dir.mkdir(parents=True, exist_ok=True)
            target_path = archive_dir / source_path.name
            suffix = 2
            while target_path.exists():
                target_path = (
                    archive_dir / f"{source_path.stem}-{suffix}{source_path.suffix}"
                )
                suffix += 1
            write_validated_vault_markdown(
                self.vault_path,
                target_path,
                source_content,
                manifest=self._manifest(),
                existing_lock=lock,
            )
            source_path.unlink()
        return target_path.relative_to(self.vault_path).as_posix()

    def _refresh_qmd_index(self) -> None:
        result = self.qmd.refresh_after_searchable_write()
        if not result["available"]:
            logger.warning("qmd refresh skipped: %s", " | ".join(result["errors"]))
            return
        if result["errors"]:
            logger.warning("qmd refresh failed: %s", " | ".join(result["errors"]))

    @staticmethod
    def _artifact_title(request: str) -> str:
        first = " ".join(str(request or "").split()).strip()
        if not first:
            return "Assistant Output"
        return first[:80].rstrip(" .!?") or "Assistant Output"

    def _render_output_artifact(
        self,
        *,
        title: str,
        request: str,
        output_markdown: str,
        artifact_type: str,
        created_at: datetime,
    ) -> str:
        body = self._clip(output_markdown.strip(), MAX_OUTPUT_ARTIFACT_CHARS)
        return (
            "---\n"
            f"date: {created_at.date().isoformat()}\n"
            "type: assistant-output\n"
            f"description: {json.dumps(title, ensure_ascii=False)}\n"
            f"artifact_type: {artifact_type}\n"
            f"created: {created_at.date().isoformat()}\n"
            f"updated: {created_at.date().isoformat()}\n"
            f"last_accessed: {created_at.date().isoformat()}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            f"# {title}\n\n"
            "## Request\n"
            f"{request.strip()}\n\n"
            "## Output\n"
            f"{body}\n"
        )

    def _render_batch_consolidation(
        self,
        *,
        headline: str,
        payload: dict[str, Any],
        events: list[CompiledBatchConsolidationEvent],
        created_at: datetime,
    ) -> str:
        summary = (
            self._paragraph(payload.get("summary"))
            or "No durable cross-source pattern captured yet."
        )
        themes = self._normalize_list(payload.get("themes"))
        follow_ups = self._normalize_list(payload.get("follow_ups"))
        source_paths = self._merge_paths([event.source_rel_path for event in events])
        updated_paths = self._merge_paths(
            *[list(event.updated_paths) for event in events]
        )
        themes_block = "\n".join(
            self._render_bullets(themes, empty="No strong recurring themes yet.")
        )
        follow_ups_block = "\n".join(
            self._render_bullets(
                follow_ups,
                empty="No immediate follow-ups captured.",
            )
        )
        updated_block = "\n".join(self._render_sources(updated_paths))
        sources_block = "\n".join(self._render_sources(source_paths))
        return (
            "---\n"
            f"date: {created_at.date().isoformat()}\n"
            "type: compiled-consolidation\n"
            f"description: {json.dumps(headline, ensure_ascii=False)}\n"
            "artifact_type: compiled-batch\n"
            f"created: {created_at.date().isoformat()}\n"
            f"updated: {created_at.date().isoformat()}\n"
            f"last_accessed: {created_at.date().isoformat()}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            f"# {headline}\n\n"
            "## Summary\n"
            f"{summary}\n\n"
            "## Themes\n"
            f"{themes_block}\n\n"
            "## Follow-ups\n"
            f"{follow_ups_block}\n\n"
            "## Updated Briefings\n"
            f"{updated_block}\n\n"
            "## Sources\n"
            f"{sources_block}\n"
        )
