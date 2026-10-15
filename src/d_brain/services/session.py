"""Session persistence service.

Stores all bot interactions in JSONL format for history and analytics.
Inspired by Clawdbot's session persistence pattern.
"""

import fcntl
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SessionStore:
    """Persistent session storage in JSONL format.

    Each user gets their own session file at vault/.sessions/{user_id}.jsonl.
    Entries are append-only for reliability and simplicity.
    """

    def __init__(self, vault_path: Path | str) -> None:
        self.sessions_dir = Path(vault_path) / ".sessions"
        self.sessions_dir.mkdir(exist_ok=True)

    def _get_user_dir(self, user_id: int) -> Path:
        user_dir = self.sessions_dir / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    @staticmethod
    def _local_today() -> date:
        return datetime.now().astimezone().date()

    def _get_session_file(self, user_id: int, day: date | None = None) -> Path:
        if day is None:
            day = self._local_today()
        return self._get_user_dir(user_id) / f"{day.isoformat()}.jsonl"

    @contextmanager
    def _session_lock(self, path: Path, *, exclusive: bool) -> Iterator[None]:
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            lock_mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_file.fileno(), lock_mode)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def append(self, user_id: int, entry_type: str, **data: Any) -> None:
        """Append entry to user's session file.

        Args:
            user_id: Telegram user ID
            entry_type: Type of entry (voice, text, photo, forward, command, etc.)
            **data: Additional data to store (text, duration, msg_id, etc.)
        """
        now = datetime.now().astimezone()
        entry = {
            "ts": now.isoformat(),
            "type": entry_type,
            **data,
        }
        path = self._get_session_file(user_id, now.date())
        with self._session_lock(path, exclusive=True):
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _read_entries(self, path: Path) -> list[dict[str, Any]]:
        """Read one session file under a shared lock with warning on corruption."""
        entries: list[dict[str, Any]] = []
        with self._session_lock(path, exclusive=False):
            with path.open("r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping corrupted session entry in %s at line %s",
                            path,
                            line_number,
                        )
        return entries

    def get_recent(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent session entries.

        Args:
            user_id: Telegram user ID
            limit: Maximum number of entries to return

        Returns:
            List of session entries, most recent last
        """
        user_dir = self._get_user_dir(user_id)
        if not user_dir.exists():
            return []

        recent_reversed: list[dict[str, Any]] = []
        session_files = sorted(user_dir.glob("*.jsonl"), reverse=True)
        for path in session_files:
            day_entries = self._read_entries(path)
            for entry in reversed(day_entries):
                recent_reversed.append(entry)
                if len(recent_reversed) >= limit:
                    return list(reversed(recent_reversed))

        return list(reversed(recent_reversed))

    def get_today(self, user_id: int) -> list[dict[str, Any]]:
        """Get today's session entries.

        Args:
            user_id: Telegram user ID

        Returns:
            List of today's entries
        """
        path = self._get_session_file(user_id, self._local_today())
        if not path.exists():
            return []
        return self._read_entries(path)

    def get_stats(self, user_id: int, days: int = 7) -> dict[str, int]:
        """Get usage statistics for the last N days.

        Args:
            user_id: Telegram user ID
            days: Number of days to analyze

        Returns:
            Dict with counts by entry type
        """
        stats: dict[str, int] = {}
        today = self._local_today()
        for offset in range(days):
            day = today - timedelta(days=offset)
            path = self._get_session_file(user_id, day)
            if not path.exists():
                continue
            for entry in self._read_entries(path):
                entry_type = entry.get("type", "unknown")
                stats[entry_type] = stats.get(entry_type, 0) + 1

        return stats

    def save_answer(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
        question: str,
        answer: str,
        *,
        parent_id: int | None = None,
        quoted_parent: str = "",
    ) -> None:
        """Store one delivered answer and its link to the preceding answer."""
        directory = self._get_user_dir(user_id) / "conversations"
        directory.mkdir(exist_ok=True)
        path = directory / f"{chat_id}.jsonl"
        entry = {
            "ts": datetime.now().astimezone().isoformat(),
            "message_id": message_id,
            "parent_id": parent_id,
            "question": question,
            "answer": answer,
            "quoted_parent": quoted_parent,
        }
        with self._session_lock(path, exclusive=True):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_conversation(
        self, user_id: int, chat_id: int, message_id: int
    ) -> list[dict[str, str]]:
        """Read only the chain leading to the selected answer, across days."""
        path = self._get_user_dir(user_id) / "conversations" / f"{chat_id}.jsonl"
        if not path.exists():
            return []
        answers = {entry["message_id"]: entry for entry in self._read_entries(path)}
        turns: list[dict[str, str]] = []
        current = answers.get(message_id)
        while current is not None:
            turns.append({"role": "assistant", "text": current["answer"]})
            if current["question"]:
                turns.append({"role": "user", "text": current["question"]})
            if current["quoted_parent"]:
                turns.append({"role": "assistant", "text": current["quoted_parent"]})
            current = answers.get(current["parent_id"])
        return list(reversed(turns))

    def get_conversation_entries(
        self, user_id: int, chat_id: int
    ) -> list[dict[str, Any]]:
        """Return raw saved records for one user and chat without creating paths."""
        path = self.sessions_dir / str(user_id) / "conversations" / f"{chat_id}.jsonl"
        if not path.exists():
            return []
        return self._read_entries(path)

    @staticmethod
    def _conversation_turns(entry: dict[str, Any]) -> list[dict[str, Any]]:
        """Render one saved record as ordered Telegram turns."""
        message_id = entry.get("message_id")
        timestamp = str(entry.get("ts") or "")
        turns: list[dict[str, Any]] = []
        quoted_parent = str(entry.get("quoted_parent") or "")
        question = str(entry.get("question") or "")
        answer = str(entry.get("answer") or "")
        if quoted_parent:
            turns.append(
                {
                    "message_id": message_id,
                    "role": "assistant",
                    "text": quoted_parent,
                    "ts": timestamp,
                }
            )
        if question:
            turns.append(
                {
                    "message_id": message_id,
                    "role": "user",
                    "text": question,
                    "ts": timestamp,
                }
            )
        if answer:
            turns.append(
                {
                    "message_id": message_id,
                    "role": "assistant",
                    "text": answer,
                    "ts": timestamp,
                }
            )
        return turns

    def search_conversation_turns(
        self,
        user_id: int,
        query: str,
        *,
        before: int = 2,
        after: int = 2,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Find matching saved turns with bounded context from one branch.

        A context window follows parent links backwards and follows a child only
        while it is unique. It therefore stops at a fork instead of mixing two
        Telegram reply branches.
        """
        needle = query.casefold().strip()
        if not needle or limit <= 0:
            return []
        before = max(0, before)
        after = max(0, after)
        directory = self.sessions_dir / str(user_id) / "conversations"
        if not directory.exists():
            return []

        results: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.jsonl")):
            try:
                chat_id = int(path.stem)
            except ValueError:
                continue
            entries = [
                entry
                for entry in self._read_entries(path)
                if isinstance(entry.get("message_id"), int)
            ]
            by_message_id = {entry["message_id"]: entry for entry in entries}
            children: dict[int, list[dict[str, Any]]] = {}
            for entry in entries:
                parent_id = entry.get("parent_id")
                if isinstance(parent_id, int) and parent_id in by_message_id:
                    children.setdefault(parent_id, []).append(entry)

            for entry in reversed(entries):
                matched = next(
                    (
                        turn
                        for turn in self._conversation_turns(entry)
                        if needle in str(turn["text"]).casefold()
                    ),
                    None,
                )
                if matched is None:
                    continue

                earlier: list[dict[str, Any]] = []
                current = entry
                for _ in range(before):
                    parent_id = current.get("parent_id")
                    parent = by_message_id.get(parent_id)
                    if parent is None:
                        break
                    earlier.append(parent)
                    current = parent
                earlier.reverse()

                later: list[dict[str, Any]] = []
                current = entry
                for _ in range(after):
                    successors = children.get(current["message_id"], [])
                    if len(successors) != 1:
                        break
                    current = successors[0]
                    later.append(current)

                turns: list[dict[str, Any]] = []
                for context_entry in [*earlier, entry, *later]:
                    turns.extend(self._conversation_turns(context_entry))
                results.append(
                    {
                        "chat_id": chat_id,
                        "source_path": path.relative_to(
                            self.sessions_dir.parent
                        ).as_posix(),
                        "matched": matched,
                        "turns": turns,
                    }
                )
                if len(results) >= limit:
                    return results
        return results
