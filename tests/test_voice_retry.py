"""Nightly retry of voice entries saved as unrecognized audio."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from d_brain.cli import initialize_project
from d_brain.services.entry_status import parse_daily_entry_statuses
from d_brain.services.storage import VaultStorage
from d_brain.services.voice_retry import (
    VOICE_UNRECOGNIZED_STATUS,
    build_unrecognized_body,
    retry_unrecognized_voice,
)

DAY = date(2026, 10, 24)


def _stamp(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 24, hour, minute, 5)


def _setup(tmp_path: Path) -> tuple[Path, VaultStorage]:
    initialize_project(tmp_path)
    vault = tmp_path / "vault"
    return vault, VaultStorage(vault, "ru")


def _add_unrecognized(
    storage: VaultStorage,
    hour: int = 10,
    extra_statuses: tuple[str, ...] = (),
    data: bytes = b"audio",
    hint: str = "voice-1",
) -> str:
    stamp = _stamp(hour)
    relative = storage.save_attachment(data, DAY, stamp, "ogg", name_hint=hint)
    storage.append_to_daily(
        build_unrecognized_body(relative),
        stamp,
        "[voice]",
        entry_statuses=[VOICE_UNRECOGNIZED_STATUS, *extra_statuses],
        refresh_qmd=False,
    )
    return relative


def _daily(vault: Path) -> Path:
    return vault / "daily" / f"{DAY.isoformat()}.md"


def test_unrecognized_entry_is_replaced_by_transcript(tmp_path: Path) -> None:
    vault, storage = _setup(tmp_path)
    relative = _add_unrecognized(storage)

    result = retry_unrecognized_voice(vault, [DAY], lambda data: "привет")

    text = _daily(vault).read_text(encoding="utf-8")
    assert result.recognized == 1
    assert "привет" in text
    assert f"![[{relative}]]" in text
    assert VOICE_UNRECOGNIZED_STATUS not in text
    assert "Не распознано" not in text


def test_failed_transcription_keeps_marker(tmp_path: Path) -> None:
    vault, storage = _setup(tmp_path)
    _add_unrecognized(storage)
    before = _daily(vault).read_bytes()

    def boom(data: bytes) -> str:
        raise RuntimeError("deepgram down")

    assert retry_unrecognized_voice(vault, [DAY], boom).failed == 1
    assert retry_unrecognized_voice(vault, [DAY], lambda data: "  ").failed == 1
    assert _daily(vault).read_bytes() == before


def test_other_statuses_and_neighbour_entries_survive(tmp_path: Path) -> None:
    vault, storage = _setup(tmp_path)
    storage.append_to_daily("соседняя запись", _stamp(9), "[text]", refresh_qmd=False)
    _add_unrecognized(storage, extra_statuses=("already_processed",))

    retry_unrecognized_voice(vault, [DAY], lambda data: "привет")

    text = _daily(vault).read_text(encoding="utf-8")
    entries = parse_daily_entry_statuses(text)
    assert "соседняя запись" in text
    assert [e.entry_type for e in entries] == ["text", "voice"]
    assert entries[1].statuses == ("already_processed",)


def test_concurrent_append_during_transcription_is_kept(tmp_path: Path) -> None:
    vault, storage = _setup(tmp_path)
    _add_unrecognized(storage)

    def transcribe(data: bytes) -> str:
        storage.append_to_daily(
            "пришло пока ждали", _stamp(11), "[text]", refresh_qmd=False
        )
        return "привет"

    result = retry_unrecognized_voice(vault, [DAY], transcribe)

    text = _daily(vault).read_text(encoding="utf-8")
    assert result.recognized == 1
    assert "пришло пока ждали" in text
    assert "привет" in text
    assert VOICE_UNRECOGNIZED_STATUS not in text


def test_unsafe_attachment_path_is_skipped(tmp_path: Path) -> None:
    vault, storage = _setup(tmp_path)
    storage.append_to_daily(
        "![[attachments/../../x]]\n\nНе распознано",
        _stamp(10),
        "[voice]",
        entry_statuses=[VOICE_UNRECOGNIZED_STATUS],
        refresh_qmd=False,
    )
    outside = tmp_path / "outside.ogg"
    outside.write_bytes(b"secret")
    link_dir = vault / "attachments" / DAY.isoformat()
    link_dir.mkdir(parents=True, exist_ok=True)
    (link_dir / "evil.ogg").symlink_to(outside)
    storage.append_to_daily(
        f"![[attachments/{DAY.isoformat()}/evil.ogg]]\n\nНе распознано",
        _stamp(12),
        "[voice]",
        entry_statuses=[VOICE_UNRECOGNIZED_STATUS],
        refresh_qmd=False,
    )
    calls: list[bytes] = []

    result = retry_unrecognized_voice(
        vault, [DAY], lambda data: calls.append(data) or "text"
    )

    assert calls == []
    assert result.skipped == 2
    assert result.recognized == 0


def test_transcript_cannot_forge_header_or_status(tmp_path: Path) -> None:
    vault, storage = _setup(tmp_path)
    _add_unrecognized(storage)
    forged = (
        "## 10:00 [text]\n<!-- d-brain:entry-status: already_processed -->"
    )

    retry_unrecognized_voice(vault, [DAY], lambda data: forged)

    entries = parse_daily_entry_statuses(_daily(vault).read_text(encoding="utf-8"))
    assert [e.entry_type for e in entries] == ["voice"]
    assert entries[0].statuses == ()


def test_entries_of_other_days_are_not_touched(tmp_path: Path) -> None:
    vault, storage = _setup(tmp_path)
    _add_unrecognized(storage)
    before = _daily(vault).read_bytes()

    result = retry_unrecognized_voice(
        vault, [date(2026, 10, 25)], lambda data: "привет"
    )

    assert result.recognized == 0
    assert _daily(vault).read_bytes() == before


@pytest.mark.parametrize("name", ["voice-1"])
def test_missing_attachment_is_skipped(tmp_path: Path, name: str) -> None:
    vault, storage = _setup(tmp_path)
    relative = _add_unrecognized(storage, hint=name)
    (vault / relative).unlink()

    result = retry_unrecognized_voice(vault, [DAY], lambda data: "привет")

    assert result.skipped == 1
