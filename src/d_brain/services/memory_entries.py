"""Entry-level memory metadata for daily notes."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

DAILY_HEADING_RE = re.compile(r"^##\s+(\d{2}:\d{2})\s+(\[[^\]]+\])\s*$")
ENTRY_STORE_NAME = ".memory-entries.json"
DEFAULT_MEMORY_CONFIG = {
    "tiers": {"active": 7, "warm": 21, "cold": 60},
    "decay_rate": 0.015,
    "relevance_floor": 0.1,
}
TIER_RANK = {
    "archive": 0,
    "cold": 1,
    "warm": 2,
    "active": 3,
    "core": 4,
}


def calc_relevance(days: int, rate: float, floor: float) -> float:
    """Floor-adjusted exponential forgetting curve."""
    import math

    return round(floor + (1.0 - floor) * math.exp(-rate * days), 2)


def calc_tier(days: int, tiers: dict[str, int], current_tier: str = "") -> str:
    """Assign memory tier based on days since last access."""
    if current_tier == "core":
        return "core"
    for tier_name, threshold in sorted(tiers.items(), key=lambda item: item[1]):
        if days <= threshold:
            return tier_name
    return "archive"


def promote_tier(
    current_tier: str,
    *,
    today: date,
    tiers: dict[str, int],
    decay_rate: float,
    relevance_floor: float,
) -> tuple[str, str, float]:
    """Promote one tier up, mirroring the spaced-repetition touch rules."""
    warm_threshold = tiers.get("warm", 21)
    cold_threshold = tiers.get("cold", 60)
    active_threshold = tiers.get("active", 7)

    if current_tier == "core":
        return today.isoformat(), "core", 1.0
    if current_tier == "archive":
        target_days = (warm_threshold + cold_threshold) // 2
        new_tier = "cold"
    elif current_tier == "cold":
        target_days = (active_threshold + warm_threshold) // 2
        new_tier = "warm"
    elif current_tier == "warm":
        target_days = active_threshold // 2
        new_tier = "active"
    else:
        target_days = 0
        new_tier = "active"

    target_date = today - timedelta(days=target_days)
    return (
        target_date.isoformat(),
        new_tier,
        calc_relevance(target_days, decay_rate, relevance_floor),
    )


@dataclass
class DailyEntrySignal:
    """Best memory signal derived from daily entry metadata."""

    tier: str
    relevance: float
    last_accessed: str
    entries: int


class DailyEntryMemoryStore:
    """Persist entry-level memory for daily note sections."""

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = Path(vault_path).resolve()
        self._store_path = self.vault_path / ENTRY_STORE_NAME
        self._config = self._load_config()

    @property
    def _lock_path(self) -> Path:
        return self.vault_path / ".locks" / "memory-entries.lock"

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _shared_lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_config(self) -> dict[str, Any]:
        config_path = self.vault_path / ".memory-config.json"
        if not config_path.exists():
            return DEFAULT_MEMORY_CONFIG.copy()
        raw = config_path.read_text(encoding="utf-8")
        if raw.startswith("---"):
            raw = re.sub(r"\A---\n.*?\n---\n*", "", raw, count=1, flags=re.DOTALL)
        try:
            user = json.loads(raw)
        except json.JSONDecodeError:
            return DEFAULT_MEMORY_CONFIG.copy()
        merged = {**DEFAULT_MEMORY_CONFIG, **user}
        default_tiers = cast(dict[str, int], DEFAULT_MEMORY_CONFIG["tiers"])
        merged["tiers"] = {**default_tiers, **user.get("tiers", {})}
        return merged

    def _load(self) -> dict[str, Any]:
        if not self._store_path.exists():
            return {"version": 1, "entries": {}}
        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"version": 1, "entries": {}}
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        return {"version": 1, "entries": entries}

    def _write(self, payload: dict[str, Any]) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self._store_path.parent,
            delete=False,
        ) as temp_file:
            temp_file.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            temp_path = Path(temp_file.name)
        os.replace(temp_path, self._store_path)

    @staticmethod
    def _parse_entries(rel_path: str, content: str) -> list[dict[str, Any]]:
        lines = content.splitlines()
        headings: list[tuple[int, str, str]] = []
        for line_no, line in enumerate(lines, start=1):
            match = DAILY_HEADING_RE.match(line.strip())
            if match:
                headings.append((line_no, match.group(1), match.group(2)))

        entries: list[dict[str, Any]] = []
        file_date = Path(rel_path).stem
        for index, (line_no, time_label, kind) in enumerate(headings, start=1):
            end_line = headings[index][0] - 1 if index < len(headings) else len(lines)
            body = "\n".join(lines[line_no:end_line]).strip()
            preview = ""
            for chunk in body.splitlines():
                chunk = chunk.strip()
                if chunk:
                    preview = chunk
                    break
            entry_id = f"{rel_path}::{index:04d}"
            entries.append(
                {
                    "id": entry_id,
                    "path": rel_path,
                    "ordinal": index,
                    "heading": f"## {time_label} {kind}",
                    "line": line_no,
                    "event_date": file_date,
                    "kind": kind,
                    "preview": preview,
                }
            )
        return entries

    def sync_daily_file(self, file_path: Path, *, today: date | None = None) -> None:
        """Upsert entry-level memory for one daily file."""
        today = today or date.today()
        resolved = file_path.resolve()
        rel_path = resolved.relative_to(self.vault_path).as_posix()
        parsed = self._parse_entries(rel_path, resolved.read_text(encoding="utf-8"))
        parsed_ids = {entry["id"] for entry in parsed}

        with self._exclusive_lock():
            payload = self._load()
            entries = payload["entries"]

            for stale_id in [
                entry_id
                for entry_id, meta in entries.items()
                if meta.get("path") == rel_path and entry_id not in parsed_ids
            ]:
                entries.pop(stale_id, None)

            for entry in parsed:
                existing = entries.get(entry["id"], {})
                last_accessed = existing.get("last_accessed", today.isoformat())
                try:
                    last_accessed_date = date.fromisoformat(str(last_accessed)[:10])
                except ValueError:
                    last_accessed_date = today
                days = max(0, (today - last_accessed_date).days)
                tier = calc_tier(
                    days,
                    self._config["tiers"],
                    str(existing.get("tier", "")),
                )
                relevance = calc_relevance(
                    days,
                    float(self._config["decay_rate"]),
                    float(self._config["relevance_floor"]),
                )
                entries[entry["id"]] = {
                    **entry,
                    "first_seen": existing.get("first_seen", today.isoformat()),
                    "last_accessed": last_accessed_date.isoformat(),
                    "tier": tier,
                    "relevance": relevance,
                }

            self._write(payload)

    def best_signal_for_path(self, rel_path: str) -> DailyEntrySignal | None:
        """Return the hottest signal for one daily file."""
        with self._shared_lock():
            payload = self._load()
        matched = [
            meta for meta in payload["entries"].values() if meta.get("path") == rel_path
        ]
        if not matched:
            return None
        best = max(
            matched,
            key=lambda item: (
                TIER_RANK.get(str(item.get("tier", "archive")), -1),
                float(item.get("relevance", 0.0)),
            ),
        )
        return DailyEntrySignal(
            tier=str(best.get("tier", "archive")),
            relevance=float(best.get("relevance", 0.0)),
            last_accessed=str(best.get("last_accessed", "")),
            entries=len(matched),
        )

    def all_entries(self) -> list[dict[str, Any]]:
        """Return all tracked daily entry memories."""
        with self._shared_lock():
            payload = self._load()
        return list(payload["entries"].values())

    def touch_daily_file(self, rel_path: str, *, today: date | None = None) -> int:
        """Promote all entry memories for one daily file."""
        today = today or date.today()
        with self._exclusive_lock():
            payload = self._load()
            entries = payload["entries"]
            changed = 0
            for meta in entries.values():
                if meta.get("path") != rel_path:
                    continue
                last_accessed, new_tier, new_relevance = promote_tier(
                    str(meta.get("tier", "archive")),
                    today=today,
                    tiers=self._config["tiers"],
                    decay_rate=float(self._config["decay_rate"]),
                    relevance_floor=float(self._config["relevance_floor"]),
                )
                meta["last_accessed"] = last_accessed
                meta["tier"] = new_tier
                meta["relevance"] = new_relevance
                changed += 1
            if changed:
                self._write(payload)
            return changed
