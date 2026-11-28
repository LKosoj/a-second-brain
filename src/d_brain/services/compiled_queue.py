"""File-backed queue, lock, PID liveness, and worker journal helpers for compile-enrich.

Extracted from ``compiled_briefings.py`` so low-level queue persistence,
cooperative file locking, worker state/heartbeat checks, worker/crash
journals, and dropped-source bookkeeping live in one cohesive module.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from d_brain.services.atomic_io import atomic_write_file_text

if TYPE_CHECKING:
    import threading

    from d_brain.services.compiled_briefings import (
        CompiledBriefingCandidate,
        CompileEnrichPass,
    )

logger = logging.getLogger(__name__)
QueueLockResult = TypeVar("QueueLockResult")


class CompiledSourceStateError(ValueError):
    """Raised by ``load_source_state_unlocked`` when
    ``.compiled/source-state.json`` is corrupt or in an unsupported shape.
    """


SOURCE_STATE_VERSION = 1
DEFAULT_WORKER_STALE_SECONDS = 90
DEFAULT_QUEUE_CLAIM_STALE_SECONDS = 900
QUEUE_WORKER_HISTORY_LIMIT = 10
QUEUE_STARVATION_SKIP_LIMIT = 5
QUEUE_TIER_RANK = {
    "core": 5,
    "active": 4,
    "warm": 3,
    "cold": 2,
    "archive": 1,
}


@contextmanager
def file_lock(
    lock_path: Path,
    *,
    ensure_dir: Callable[[], None],
    blocking: bool,
) -> Iterator[bool]:
    """Acquire an exclusive ``flock`` on ``lock_path``."""
    ensure_dir()
    with lock_path.open("a+", encoding="utf-8") as handle:
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


def load_source_state_unlocked(source_state_path: Path) -> dict[str, Any]:
    if not source_state_path.exists():
        return {"version": SOURCE_STATE_VERSION, "entries": {}}
    try:
        payload = json.loads(source_state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompiledSourceStateError(
            f"invalid compiled source state: {source_state_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise CompiledSourceStateError(
            "compiled source state must be a JSON object"
        )
    if payload.get("version") != SOURCE_STATE_VERSION:
        raise CompiledSourceStateError(
            "unsupported compiled source state version"
        )
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise CompiledSourceStateError(
            "compiled source state entries must be a JSON object"
        )
    for rel_path, entry in entries.items():
        if not isinstance(rel_path, str) or not isinstance(entry, dict):
            raise CompiledSourceStateError("invalid compiled source state entry")
        if not isinstance(entry.get("sources"), dict):
            raise CompiledSourceStateError(
                f"invalid compiled source state entry: {rel_path}"
            )
    return payload


def write_source_state_unlocked(
    source_state_path: Path,
    state: dict[str, Any],
    *,
    write_text: Callable[[Path, str], None] = atomic_write_file_text,
) -> None:
    write_text(
        source_state_path,
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def load_worker_state(worker_state_path: Path) -> dict[str, Any]:
    if not worker_state_path.exists():
        return {}
    try:
        payload = json.loads(worker_state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning(
            "Invalid compiled worker state payload at %s",
            worker_state_path,
        )
        return {}
    return payload if isinstance(payload, dict) else {}


def save_worker_state(
    worker_state_path: Path,
    payload: dict[str, Any],
    *,
    write_text: Callable[[Path, str], None] = atomic_write_file_text,
) -> None:
    write_text(
        worker_state_path,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def parse_iso_datetime(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def rotate_queue_worker_journals(
    queue_worker_history_root: Path,
    *,
    limit: int = QUEUE_WORKER_HISTORY_LIMIT,
) -> None:
    journals = sorted(
        queue_worker_history_root.glob("*.json"),
        key=lambda path: path.name,
        reverse=True,
    )
    for path in journals[limit:]:
        path.unlink()


def write_queue_worker_crash_journal(
    vault_path: Path,
    *,
    pid: int,
    started_at: str,
    exc: Exception,
    now_iso: str | None = None,
    write_text: Callable[[Path, str], None] = atomic_write_file_text,
) -> None:
    """Persist an owner-visible trace of an unexpected ``run_queue_worker`` crash."""
    crashed_at = (
        now_iso
        if now_iso is not None
        else datetime.now().astimezone().isoformat(timespec="seconds")
    )
    payload = {
        "pid": pid,
        "started_at": started_at,
        "crashed_at": crashed_at,
        "error": {"type": type(exc).__name__, "message": str(exc)},
        "traceback": traceback.format_exc(),
    }
    journal_path = vault_path / ".session" / "compile-queue-worker.json"
    try:
        write_text(
            journal_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
    except Exception as journal_exc:  # noqa: BLE001 - best-effort trace
        logger.warning(
            "Failed to write queue worker crash journal (original "
            "crash: %r): %s",
            exc,
            journal_exc,
        )


def dropped_sources_journal_path(vault_path: Path) -> Path:
    return vault_path / ".session" / "compile-dropped-sources.json"


def load_dropped_queue_sources(vault_path: Path) -> list[dict[str, Any]]:
    """Read the give-up journal, fail-safe like ``load_queue``."""
    journal_path = dropped_sources_journal_path(vault_path)
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Invalid dropped-sources journal at %s", journal_path)
        return []
    if not isinstance(payload, dict):
        return []
    entries = payload.get("sources")
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def load_queue(queue_path: Path) -> list[dict[str, Any]]:
    if not queue_path.exists():
        return []
    try:
        payload = json.loads(queue_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Invalid compiled queue payload at %s", queue_path)
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def save_queue(
    queue_path: Path,
    payload: list[dict[str, Any]],
    *,
    write_text: Callable[[Path, str], None] = atomic_write_file_text,
) -> None:
    write_text(
        queue_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


class CompiledQueueMixin:
    """Queue, lock, PID liveness, worker journal, and dropped-source methods."""

    if TYPE_CHECKING:
        vault_path: Path
        launcher_lock_path: Path
        worker_lock_path: Path
        state_lock_path: Path
        source_state_path: Path
        worker_state_path: Path
        queue_path: Path
        queue_worker_history_root: Path
        _state_lock_depth: threading.local
        _active_queue_worker_journal: dict[str, Any] | None
        _active_queue_worker_journal_path: Path | None
        _active_pass: CompileEnrichPass | None

        def _ensure_state_dirs(self) -> None: ...
        def _now(self) -> datetime: ...
        def _write_text_atomic(self, path: Path, content: str) -> None: ...
        def _iter_candidates(self) -> list[CompiledBriefingCandidate]: ...

    @contextmanager
    def _launcher_lock(
        self, *, blocking: bool
    ) -> Iterator[bool]:
        with file_lock(
            self.launcher_lock_path,
            ensure_dir=self._ensure_state_dirs,
            blocking=blocking,
        ) as acquired:
            yield acquired

    @contextmanager
    def _worker_lock(
        self, *, blocking: bool
    ) -> Iterator[bool]:
        with file_lock(
            self.worker_lock_path,
            ensure_dir=self._ensure_state_dirs,
            blocking=blocking,
        ) as acquired:
            yield acquired

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        """Re-entrant per-thread lock over ``.compiled``/give-up state files."""
        depth = getattr(self._state_lock_depth, "value", 0)
        if depth:
            self._state_lock_depth.value = depth + 1
            try:
                yield
            finally:
                self._state_lock_depth.value = depth
            return
        self._ensure_state_dirs()
        with self.state_lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._state_lock_depth.value = 1
            try:
                yield
            finally:
                self._state_lock_depth.value = 0
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _with_queue_lock(
        self,
        callback: Callable[[], QueueLockResult],
    ) -> QueueLockResult:
        with self._state_lock():
            return callback()

    def _load_source_state(self) -> dict[str, Any]:
        with self._state_lock():
            return self._load_source_state_unlocked()

    def _load_source_state_unlocked(
        self,
    ) -> dict[str, Any]:
        return load_source_state_unlocked(self.source_state_path)

    def _write_source_state_unlocked(
        self, state: dict[str, Any]
    ) -> None:
        write_source_state_unlocked(
            self.source_state_path, state, write_text=self._write_text_atomic
        )

    def _load_worker_state(self) -> dict[str, Any]:
        return load_worker_state(self.worker_state_path)

    def _save_worker_state(
        self, payload: dict[str, Any]
    ) -> None:
        save_worker_state(
            self.worker_state_path, payload, write_text=self._write_text_atomic
        )

    def _clear_worker_state_unlocked(self) -> None:
        self.worker_state_path.unlink(missing_ok=True)

    def _parse_iso_datetime(
        self, raw: str
    ) -> datetime | None:
        return parse_iso_datetime(raw)

    def _pid_is_alive(self, pid: int) -> bool:
        return pid_is_alive(pid)

    def _worker_state_is_live(
        self,
        state: dict[str, Any],
        *,
        stale_after_seconds: int = DEFAULT_WORKER_STALE_SECONDS,
    ) -> bool:
        pid = int(state.get("pid") or 0)
        heartbeat_raw = str(
            state.get("heartbeat_at") or state.get("started_at") or ""
        )
        heartbeat_at = self._parse_iso_datetime(heartbeat_raw)
        if not self._pid_is_alive(pid):
            return False
        if heartbeat_at is None:
            return True
        age_seconds = (
            self._now() - heartbeat_at.astimezone()
        ).total_seconds()
        return age_seconds <= stale_after_seconds

    def _write_worker_state(
        self,
        *,
        pid: int,
        status: str,
        started_at: str | None = None,
    ) -> None:
        now = self._now().isoformat()

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

    def _clear_worker_state(
        self, *, pid: int | None = None
    ) -> None:
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

    def _start_queue_worker_journal(
        self,
        *,
        pid: int,
        started_at: str,
        force: bool,
        max_events: int,
        refresh_qmd: bool,
        initial_queue_size: int,
    ) -> None:
        started = datetime.fromisoformat(started_at)
        filename = f"{started.strftime('%Y-%m-%d-%H%M%S')}-{pid}.json"
        self.queue_worker_history_root.mkdir(parents=True, exist_ok=True)
        self._active_queue_worker_journal_path = (
            self.queue_worker_history_root / filename
        )
        self._active_queue_worker_journal = {
            "pid": pid,
            "status": "running",
            "started_at": started_at,
            "parameters": {
                "force": force,
                "max_events": max_events,
                "refresh_qmd": refresh_qmd,
            },
            "initial_queue_size": initial_queue_size,
            "events": [],
        }
        self._write_active_queue_worker_journal()
        self._rotate_queue_worker_journals()

    def _write_active_queue_worker_journal(
        self,
    ) -> None:
        if (
            self._active_queue_worker_journal is None
            or self._active_queue_worker_journal_path is None
        ):
            return
        self._write_text_atomic(
            self._active_queue_worker_journal_path,
            json.dumps(
                self._active_queue_worker_journal,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def _record_queue_worker_event(
        self,
        event: dict[str, Any],
        *,
        outcome: str,
        updated: list[str],
        errors: list[str],
        attempts: int | None = None,
    ) -> None:
        if self._active_queue_worker_journal is None:
            return
        source_path = str(event.get("source_path") or "")
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", source_path)
        self._active_queue_worker_journal["events"].append(
            {
                "source_path": source_path,
                "source_date": date_match.group(0) if date_match else None,
                "enqueued_at": event.get("enqueued_at"),
                "last_enqueued_at": event.get("last_enqueued_at"),
                "claimed_at": event.get("claimed_at"),
                "finished_at": self._now().isoformat(),
                "attempts": (
                    attempts
                    if attempts is not None
                    else int(event.get("attempts") or 0)
                ),
                "outcome": outcome,
                "updated": list(updated),
                "errors": list(errors),
            }
        )
        self._write_active_queue_worker_journal()

    def _finish_queue_worker_journal(
        self,
        *,
        status: str,
        remaining_queue_size: int,
        totals: dict[str, int] | None = None,
        error: dict[str, str] | None = None,
    ) -> None:
        if self._active_queue_worker_journal is None:
            return
        self._active_queue_worker_journal["status"] = status
        self._active_queue_worker_journal["finished_at"] = (
            self._now().isoformat()
        )
        self._active_queue_worker_journal["remaining_queue_size"] = (
            remaining_queue_size
        )
        if totals is not None:
            self._active_queue_worker_journal["totals"] = totals
        if error is not None:
            self._active_queue_worker_journal["error"] = error
        self._write_active_queue_worker_journal()
        self._rotate_queue_worker_journals()
        self._active_queue_worker_journal = None
        self._active_queue_worker_journal_path = None

    def _rotate_queue_worker_journals(self) -> None:
        rotate_queue_worker_journals(
            self.queue_worker_history_root, limit=QUEUE_WORKER_HISTORY_LIMIT
        )

    def _write_queue_worker_crash_journal(
        self,
        *,
        pid: int,
        started_at: str,
        exc: Exception,
    ) -> None:
        write_queue_worker_crash_journal(
            self.vault_path,
            pid=pid,
            started_at=started_at,
            exc=exc,
            now_iso=self._now().isoformat(timespec="seconds"),
            write_text=self._write_text_atomic,
        )

    def _dropped_sources_journal_path(self) -> Path:
        return dropped_sources_journal_path(self.vault_path)

    def _load_dropped_queue_sources(
        self,
    ) -> list[dict[str, Any]]:
        return load_dropped_queue_sources(self.vault_path)

    def _record_dropped_queue_source(
        self,
        *,
        source_rel_path: str,
        errors: list[str],
        attempts: int,
    ) -> None:
        normalized = source_rel_path.strip()
        if not normalized:
            return
        try:
            with self._state_lock():
                entries = [
                    item
                    for item in self._load_dropped_queue_sources()
                    if str(item.get("source_path") or "") != normalized
                ]
                entries.append(
                    {
                        "source_path": normalized,
                        "dropped_at": self._now().isoformat(timespec="seconds"),
                        "attempts": attempts,
                        "errors": [str(item) for item in errors],
                    }
                )
                self._write_text_atomic(
                    self._dropped_sources_journal_path(),
                    json.dumps({"sources": entries}, ensure_ascii=False, indent=2)
                    + "\n",
                )
        except Exception as exc:  # noqa: BLE001 - best-effort trace
            logger.warning(
                "Failed to record dropped compiled source %s: %s", normalized, exc
            )

    def _clear_dropped_queue_source(
        self,
        source_rel_path: str,
        updated_paths: tuple[str, ...] = (),
    ) -> None:
        normalized = source_rel_path.strip()
        if not normalized:
            return
        if self._active_pass is not None:
            pending = self._active_pass.dropped_sources_cleared
            for index, (existing_source, existing_pages) in enumerate(pending):
                if existing_source != normalized:
                    continue
                merged = tuple(dict.fromkeys((*existing_pages, *updated_paths)))
                pending[index] = (normalized, merged)
                return
            pending.append((normalized, tuple(dict.fromkeys(updated_paths))))
            return
        self._forget_dropped_queue_source(normalized)

    def _forget_dropped_queue_source(
        self, normalized: str
    ) -> None:
        try:
            with self._state_lock():
                entries = self._load_dropped_queue_sources()
                remaining = [
                    item
                    for item in entries
                    if str(item.get("source_path") or "") != normalized
                ]
                if len(remaining) == len(entries):
                    return
                self._write_text_atomic(
                    self._dropped_sources_journal_path(),
                    json.dumps(
                        {"sources": remaining}, ensure_ascii=False, indent=2
                    )
                    + "\n",
                )
        except Exception as exc:  # noqa: BLE001 - best-effort bookkeeping
            logger.warning(
                "Failed to clear dropped compiled source %s: %s", normalized, exc
            )

    def _load_queue(self) -> list[dict[str, Any]]:
        return load_queue(self.queue_path)

    def _save_queue(
        self, payload: list[dict[str, Any]]
    ) -> None:
        save_queue(self.queue_path, payload, write_text=self._write_text_atomic)

    def _recover_stale_claims(
        self,
        queue: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        now = self._now()
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
            claimed_at = self._parse_iso_datetime(
                str(event.get("claimed_at") or "")
            )
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

    def _source_tier_ranks(self) -> dict[str, int]:
        """Best-known memory-tier rank for each source path."""
        page_ranks = {
            candidate.rel_path: QUEUE_TIER_RANK.get(candidate.tier, 0)
            for candidate in self._iter_candidates()
        }
        ranks: dict[str, int] = {}
        try:
            state = self._load_source_state_unlocked()
        except CompiledSourceStateError:
            logger.warning(
                "Повреждён файл состояния источников %s -- приоритет "
                "очереди по уровню памяти временно отключён, события "
                "разбираются в порядке поступления",
                self.source_state_path,
            )
            return ranks
        for page_rel_path, entry in state.get("entries", {}).items():
            if not isinstance(entry, dict):
                continue
            sources = entry.get("sources")
            if not isinstance(sources, dict):
                continue
            rank = page_ranks.get(page_rel_path, 0)
            for source_rel_path in sources:
                if rank > ranks.get(source_rel_path, -1):
                    ranks[source_rel_path] = rank
        return ranks

    def _claim_ready_queue_events(
        self,
        *,
        force: bool,
        max_events: int,
    ) -> list[dict[str, Any]]:
        now = self._now()
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
            skipped_updated = False
            if len(ready_indices) > 1:
                default_rank = QUEUE_TIER_RANK["active"]
                source_ranks = self._source_tier_ranks()
                ready_indices.sort(
                    key=lambda index: -source_ranks.get(
                        str(queue[index].get("source_path") or ""),
                        default_rank,
                    )
                )
                starved = [
                    index
                    for index in ready_indices
                    if int(queue[index].get("skipped_cycles") or 0)
                    >= QUEUE_STARVATION_SKIP_LIMIT
                ]
                if starved:
                    rest = [index for index in ready_indices if index not in starved]
                    ready_indices = starved + rest
            claimed_indices = set(ready_indices[:max_events])
            selected: list[dict[str, Any]] = []
            for index in ready_indices[:max_events]:
                event = queue[index]
                claim_token = uuid4().hex
                event["state"] = "in_flight"
                event["claim_token"] = claim_token
                event["claimed_at"] = now.isoformat()
                event["claimed_pid"] = os.getpid()
                selected.append(dict(event))
            if len(ready_indices) > 1:
                for index in ready_indices:
                    if index in claimed_indices:
                        continue
                    queue[index]["skipped_cycles"] = (
                        int(queue[index].get("skipped_cycles") or 0) + 1
                    )
                    skipped_updated = True
            if recovered or selected or skipped_updated:
                self._save_queue(queue)
            return selected

        return self._with_queue_lock(claim_events)

    def _ack_claimed_queue_event(
        self, event: dict[str, Any]
    ) -> None:
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
        backoff: bool = False,
    ) -> None:
        """Release a claimed event back to ``pending``."""
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
                item["backoff"] = backoff
                item["state"] = "pending"
                item["claim_token"] = ""
                item["claimed_at"] = ""
                item["claimed_pid"] = 0
                updated = True
                break
            if updated:
                self._save_queue(queue)

        self._with_queue_lock(release_event)
