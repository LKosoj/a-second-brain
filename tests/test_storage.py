from datetime import date, datetime
from pathlib import Path

import pytest
from conftest import _write_vault_manifest

from d_brain.manifest import ManifestValidationError
from d_brain.services.compiled_briefings import (
    CompiledBriefingService,
)
from d_brain.services.entry_status import (
    ENTRY_STATUS_ALREADY_PROCESSED,
    parse_daily_entry_statuses,
)
from d_brain.services.frontmatter import parse_frontmatter_bytes
from d_brain.services.memory_entries import DailyEntryMemoryStore
from d_brain.services.qmd import QmdService
from d_brain.services.source_links import (
    SourceInfo,
)
from d_brain.services.storage import ManagedBlockError, VaultStorage


@pytest.fixture(autouse=True)
def _storage_manifest(tmp_path: Path) -> None:
    _write_vault_manifest(tmp_path / "vault")


def _complex_daily_document(day: date, body: str) -> str:
    """Daily fixture with YAML structures the lossless writer must preserve."""
    return (
        "---\n"
        "type: daily\n"
        f"date: {day.isoformat()}\n"
        f"last_accessed: {day.isoformat()}\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "# standalone frontmatter comment\n"
        "tags:\n"
        "  - daily\n"
        "  - important\n"
        "context:\n"
        "  owner: alex\n"
        "  flags:\n"
        "    pinned: true\n"
        "summary: |-\n"
        "  First summary line.\n"
        "  Second summary line.\n"
        "---\n\n"
        f"# {day.isoformat()}\n\n"
        f"{body}"
    )


def _valid_daily_bytes(day: date, newline: bytes, body: bytes) -> bytes:
    """Build a valid daily document with an explicit newline convention."""
    header_lines = (
        b"---",
        b"type: daily",
        f"date: {day.isoformat()}".encode(),
        f"last_accessed: {day.isoformat()}".encode(),
        b"relevance: 1.0",
        b"tier: active",
        b"---",
    )
    return (
        newline.join(header_lines)
        + newline
        + newline
        + f"# {day.isoformat()}".encode()
        + newline
        + newline
        + body
    )


def test_storage_append_to_daily_syncs_entry_memory(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    storage.append_to_daily(
        "Идея про memory-aware retrieval",
        datetime(2026, 4, 4, 16, 20),
        "[text]",
    )

    signal = DailyEntryMemoryStore(vault_path).best_signal_for_path(
        "daily/2026-04-04.md"
    )

    assert signal is not None
    assert signal.entries == 1
    assert signal.tier == "active"
    assert signal.relevance == 1.0


def test_storage_daily_writer_requires_manifest(tmp_path: Path) -> None:
    (tmp_path / "vault-manifest.json").unlink()

    with pytest.raises(ManifestValidationError, match="is missing"):
        VaultStorage(tmp_path / "vault").ensure_daily_file(date(2026, 4, 4))


def test_storage_append_to_daily_refreshes_compiled_briefings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    calls: list[tuple[str, str]] = []
    spawns: list[bool] = []

    def fake_enqueue(self, *, source_path, source_excerpt="", max_updates=3, debounce_seconds=45):  # noqa: ANN001,E501
        del self, max_updates, debounce_seconds
        calls.append((str(source_path), source_excerpt))
        return {"queued": True, "errors": []}

    def fake_spawn(self):  # noqa: ANN001
        del self
        spawns.append(True)
        return True

    monkeypatch.setattr(CompiledBriefingService, "enqueue_refresh", fake_enqueue)
    monkeypatch.setattr(CompiledBriefingService, "spawn_background_drain", fake_spawn)

    storage.append_to_daily(
        "Идея про memory-aware retrieval",
        datetime(2026, 4, 4, 16, 20),
        "[text]",
    )

    assert calls == [
        (
            str(vault_path / "daily" / "2026-04-04.md"),
            "Идея про memory-aware retrieval",
        )
    ]
    assert spawns == [True]


def test_storage_append_to_daily_ignores_compiled_refresh_exception(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    def fail_enqueue(self, **kwargs):  # noqa: ANN001, ANN003
        del self, kwargs
        raise RuntimeError("queue broken")

    monkeypatch.setattr(CompiledBriefingService, "enqueue_refresh", fail_enqueue)

    storage.append_to_daily(
        "Идея про memory-aware retrieval",
        datetime(2026, 4, 4, 16, 20),
        "[text]",
    )

    daily = vault_path / "daily" / "2026-04-04.md"
    assert "Идея про memory-aware retrieval" in daily.read_text(encoding="utf-8")


def test_upsert_daily_block_skips_compiled_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    calls: list[object] = []

    def fake_enqueue(self, *, source_path, source_excerpt="", max_updates=3, debounce_seconds=45):  # noqa: ANN001,E501
        del self, source_path, source_excerpt, max_updates, debounce_seconds
        calls.append(True)
        return {"queued": True, "errors": []}

    monkeypatch.setattr(CompiledBriefingService, "enqueue_refresh", fake_enqueue)

    storage.upsert_daily_block(
        day=date(2026, 4, 4),
        start_marker="<!-- demo:start -->",
        end_marker="<!-- demo:end -->",
        block="\n## 16:20 [plaud]\n<!-- demo:start -->\ntext\n<!-- demo:end -->\n",
        refresh_qmd=False,
    )

    assert calls == []


def test_append_to_daily_bootstraps_daily_header(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    storage.append_to_daily(
        "Первый entry дня",
        datetime(2026, 4, 5, 9, 15),
        "[text]",
    )

    content = (vault_path / "daily" / "2026-04-05.md").read_text(encoding="utf-8")

    assert content.startswith(
        "---\n"
        "type: daily\n"
        "date: 2026-04-05\n"
        "last_accessed: 2026-04-05\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        "# 2026-04-05\n"
    )
    assert "## 09:15 [text]" in content


def test_ensure_daily_file_creates_header_only_file(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)

    daily_file = storage.ensure_daily_file(date(2026, 4, 6))

    assert daily_file.read_text(encoding="utf-8") == (
        "---\n"
        "type: daily\n"
        "date: 2026-04-06\n"
        "last_accessed: 2026-04-06\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        "# 2026-04-06\n"
    )


def test_append_to_daily_repairs_header_before_frontmatter(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    daily_path = storage.get_daily_file(date(2026, 4, 7))
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(
        (
            "# 2026-04-07\n\n"
            "---\n"
            "last_accessed: 2026-04-06\n"
            "relevance: 0.84\n"
            "tier: warm\n"
            "---\n\n"
            "## 08:00 [text]\n"
            "Старый entry\n"
        ),
        encoding="utf-8",
    )

    storage.append_to_daily(
        "Новый entry",
        datetime(2026, 4, 7, 9, 15),
        "[text]",
    )

    content = daily_path.read_text(encoding="utf-8")

    assert content.startswith(
        "---\n"
        "type: daily\n"
        "date: 2026-04-07\n"
        "last_accessed: 2026-04-06\n"
        "relevance: 0.84\n"
        "tier: warm\n"
        "---\n\n"
        "# 2026-04-07\n"
    )
    assert content.count("# 2026-04-07") == 1
    assert "## 08:00 [text]" in content
    assert "## 09:15 [text]" in content


def test_append_to_daily_preserves_complex_valid_frontmatter_bytes(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 8)
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    storage._refresh_compiled_briefings = lambda *args, **kwargs: None  # type: ignore[method-assign]
    daily_path = storage.get_daily_file(day)
    original = _complex_daily_document(
        day,
        "## 08:00 [text]\nExisting body.\n",
    ).replace("\n", "\r\n")
    daily_path.write_text(original, encoding="utf-8")
    original_header = parse_frontmatter_bytes(original.encode("utf-8")).header

    storage.append_to_daily(
        "Appended body.",
        datetime(2026, 4, 8, 9, 15),
        "[text]",
        refresh_qmd=False,
    )

    content = daily_path.read_bytes().decode("utf-8")
    document = parse_frontmatter_bytes(content.encode("utf-8"))
    assert document.header == original_header
    normalized = content.replace("\r\n", "\n")
    assert "  - daily\n  - important" in normalized
    assert "  owner: alex\n  flags:\n    pinned: true" in normalized
    assert "# standalone frontmatter comment" in normalized
    assert "summary: |-\n  First summary line.\n  Second summary line." in normalized
    assert "## 08:00 [text]\nExisting body." in normalized
    assert "## 09:15 [text]\nAppended body." in normalized


@pytest.mark.parametrize(
    ("newline", "tail", "separator"),
    [
        (b"\n", b"LF trailing spaces  \t", b"\n\n"),
        (b"\n", b"LF keeps blank lines\n\n\n", b""),
        (b"\r\n", b"mixed CRLF\r\nand LF\n", b"\r\n"),
        (b"\r\n", b"mixed CRLF\r\nand LF\nno final newline", b"\r\n\r\n"),
    ],
)
def test_append_to_daily_preserves_existing_bytes_and_adds_minimal_separator(
    tmp_path: Path,
    newline: bytes,
    tail: bytes,
    separator: bytes,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 9)
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    storage._refresh_compiled_briefings = lambda *args, **kwargs: None  # type: ignore[method-assign]
    daily_path = storage.get_daily_file(day)
    original = _valid_daily_bytes(day, newline, tail)
    daily_path.write_bytes(original)

    storage.append_to_daily(
        "Appended body.",
        datetime(2026, 4, 9, 9, 15),
        "[text]",
        refresh_qmd=False,
    )

    expected_entry = (
        b"## 09:15 [text]" + newline + b"Appended body." + newline
    )
    assert daily_path.read_bytes() == original + separator + expected_entry


def test_save_attachment_uses_name_hint_to_avoid_collisions(tmp_path: Path) -> None:
    storage = VaultStorage(tmp_path)
    timestamp = datetime(2026, 4, 4, 12, 30, 45)

    first = storage.save_attachment(b"one", timestamp.date(), timestamp, "jpg", "100")
    second = storage.save_attachment(b"two", timestamp.date(), timestamp, "jpg", "101")

    assert first != second
    assert (tmp_path / first).read_bytes() == b"one"
    assert (tmp_path / second).read_bytes() == b"two"


def test_append_to_daily_refreshes_qmd_index(tmp_path: Path, monkeypatch) -> None:
    calls: list[bool] = []
    _write_vault_manifest(tmp_path)

    def fake_refresh(self):  # noqa: ANN001
        calls.append(True)
        return {
            "available": True,
            "updated": True,
            "embedded": False,
            "errors": [],
        }

    monkeypatch.setattr(QmdService, "refresh_after_searchable_write", fake_refresh)
    storage = VaultStorage(tmp_path)

    storage.append_to_daily(
        "hello",
        datetime(2026, 4, 4, 12, 30, 45),
        "[text]",
    )

    assert calls == [True]
    assert "hello" in (tmp_path / "daily" / "2026-04-04.md").read_text(encoding="utf-8")


def test_append_to_daily_writes_source_block(tmp_path: Path, monkeypatch) -> None:
    _write_vault_manifest(tmp_path)
    monkeypatch.setattr(
        QmdService,
        "refresh_after_searchable_write",
        lambda self: {  # noqa: ANN001
            "available": True,
            "updated": True,
            "embedded": False,
            "errors": [],
        },
    )
    storage = VaultStorage(tmp_path)

    storage.append_to_daily(
        "hello",
        datetime(2026, 4, 4, 12, 30, 45),
        "[text]",
        source=SourceInfo(
            kind="telegram",
            ref="telegram:1:2",
            url="https://t.me/test/2",
            label="Open Telegram message",
        ),
    )

    content = (tmp_path / "daily" / "2026-04-04.md").read_text(encoding="utf-8")
    assert "Источник: [Open Telegram message](https://t.me/test/2)" in content
    assert "Идентификатор источника: `telegram:1:2`" in content


def test_append_to_daily_writes_entry_status_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_vault_manifest(tmp_path)
    monkeypatch.setattr(
        QmdService,
        "refresh_after_searchable_write",
        lambda self: {  # noqa: ANN001
            "available": True,
            "updated": True,
            "embedded": False,
            "errors": [],
        },
    )
    storage = VaultStorage(tmp_path)

    storage.append_to_daily(
        "hello",
        datetime(2026, 4, 4, 12, 30, 45),
        "[text]",
        entry_statuses=[ENTRY_STATUS_ALREADY_PROCESSED],
    )

    content = (tmp_path / "daily" / "2026-04-04.md").read_text(encoding="utf-8")
    assert "<!-- d-brain:entry-status: already_processed -->" in content
    assert parse_daily_entry_statuses(content)[0].statuses == (
        ENTRY_STATUS_ALREADY_PROCESSED,
    )


def test_upsert_daily_block_replaces_only_target_block_with_inner_headings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        QmdService,
        "refresh_after_searchable_write",
        lambda self: {  # noqa: ANN001
            "available": True,
            "updated": True,
            "embedded": False,
            "errors": [],
        },
    )
    storage = VaultStorage(tmp_path / "vault")
    day = date(2026, 4, 4)
    daily_path = storage.get_daily_file(day)
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(
        (
            "# 2026-04-04\n\n"
            "## 09:00 [plaud]\n"
            "<!-- plaud:first:start -->\n"
            "PLAUD: [[note-a]]\n"
            "Саммари: Первая секция\n"
            "## Внутренний заголовок\n"
            "Детали первой записи\n"
            "<!-- plaud:first:end -->\n\n"
            "## 10:00 [plaud]\n"
            "<!-- plaud:second:start -->\n"
            "PLAUD: [[note-b]]\n"
            "Саммари: Старая вторая запись\n"
            "<!-- plaud:second:end -->\n\n"
            "## 11:00 [text]\n"
            "Ручная заметка\n"
        ),
        encoding="utf-8",
    )

    storage.upsert_daily_block(
        day=day,
        start_marker="<!-- plaud:second:start -->",
        end_marker="<!-- plaud:second:end -->",
        block=(
            "\n## 10:00 [plaud]\n"
            "<!-- plaud:second:start -->\n"
            "PLAUD: [[note-b]]\n"
            "Саммари: Обновлённая вторая запись\n"
            "<!-- plaud:second:end -->\n"
        ),
        refresh_qmd=False,
    )

    content = daily_path.read_text(encoding="utf-8")
    assert "<!-- plaud:first:start -->" in content
    assert "## Внутренний заголовок" in content
    assert "Саммари: Обновлённая вторая запись" in content
    assert "Ручная заметка" in content
    assert content.count("<!-- plaud:first:start -->") == 1
    assert content.count("<!-- plaud:second:start -->") == 1


def test_upsert_daily_block_appends_new_block_without_clobbering_existing_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        QmdService,
        "refresh_after_searchable_write",
        lambda self: {  # noqa: ANN001
            "available": True,
            "updated": True,
            "embedded": False,
            "errors": [],
        },
    )
    storage = VaultStorage(tmp_path / "vault")
    day = date(2026, 4, 4)
    daily_path = storage.get_daily_file(day)
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(
        (
            "# 2026-04-04\n\n"
            "## 09:00 [plaud]\n"
            "<!-- plaud:first:start -->\n"
            "PLAUD: [[note-a]]\n"
            "Саммари: Первая секция\n"
            "## Внутренний заголовок\n"
            "Детали первой записи\n"
            "<!-- plaud:first:end -->\n"
        ),
        encoding="utf-8",
    )

    storage.upsert_daily_block(
        day=day,
        start_marker="<!-- plaud:second:start -->",
        end_marker="<!-- plaud:second:end -->",
        block=(
            "\n## 10:00 [plaud]\n"
            "<!-- plaud:second:start -->\n"
            "PLAUD: [[note-b]]\n"
            "Саммари: Новая вторая запись\n"
            "<!-- plaud:second:end -->\n"
        ),
        refresh_qmd=False,
    )

    content = daily_path.read_text(encoding="utf-8")
    assert "<!-- plaud:first:start -->" in content
    assert "<!-- plaud:second:start -->" in content
    assert "Детали первой записи" in content
    assert "Саммари: Новая вторая запись" in content
    assert content.index("<!-- plaud:first:start -->") < content.index(
        "<!-- plaud:second:start -->"
    )


def test_upsert_daily_block_preserves_complex_valid_frontmatter_bytes(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 8)
    storage = VaultStorage(vault_path)
    daily_path = storage.get_daily_file(day)
    original = _complex_daily_document(
        day,
        (
            "## 08:00 [plaud]\n"
            "<!-- demo:start -->\n"
            "Old managed body.\n"
            "<!-- demo:end -->\n\n"
            "## 08:30 [text]\n"
            "Unrelated body.\n"
        ),
    )
    daily_path.write_text(original, encoding="utf-8")
    original_header = parse_frontmatter_bytes(original.encode("utf-8")).header

    storage.upsert_daily_block(
        day=day,
        start_marker="<!-- demo:start -->",
        end_marker="<!-- demo:end -->",
        block=(
            "\n## 08:00 [plaud]\n"
            "<!-- demo:start -->\n"
            "New managed body.\n"
            "<!-- demo:end -->\n"
        ),
        refresh_qmd=False,
    )

    content = daily_path.read_text(encoding="utf-8")
    document = parse_frontmatter_bytes(content.encode("utf-8"))
    assert document.header == original_header
    assert "  - daily\n  - important" in content
    assert "  owner: alex\n  flags:\n    pinned: true" in content
    assert "# standalone frontmatter comment" in content
    assert "summary: |-\n  First summary line.\n  Second summary line." in content
    assert "Old managed body." not in content
    assert "New managed body." in content
    assert "## 08:30 [text]\nUnrelated body." in content


def test_upsert_daily_block_replaces_only_exact_range_bytes(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 9)
    storage = VaultStorage(vault_path)
    daily_path = storage.get_daily_file(day)
    prefix = _valid_daily_bytes(
        day,
        b"\r\n",
        b"PREFIX sentinel with spaces  \t\n\n",
    )
    old_range = (
        b"## 08:00 [plaud]\r\n"
        b"<!-- demo:start -->\r\n"
        b"Old managed body.\n"
        b"<!-- demo:end -->\r\n"
    )
    suffix = (
        b"SUFFIX sentinel with spaces  \t\n"
        b"\n"
        b"## 08:30 [text]\r\n"
        b"Unrelated body without final newline"
    )
    daily_path.write_bytes(prefix + old_range + suffix)

    storage.upsert_daily_block(
        day=day,
        start_marker="<!-- demo:start -->",
        end_marker="<!-- demo:end -->",
        block=(
            "\n## 08:00 [plaud]\n"
            "<!-- demo:start -->\n"
            "New managed body.  \n"
            "<!-- demo:end -->\n"
        ),
        refresh_qmd=False,
    )

    new_range = (
        b"## 08:00 [plaud]\r\n"
        b"<!-- demo:start -->\r\n"
        b"New managed body.  \r\n"
        b"<!-- demo:end -->\r\n"
    )
    result = daily_path.read_bytes()
    assert result == prefix + new_range + suffix
    assert result[: len(prefix)] == prefix
    assert result[-len(suffix) :] == suffix


def test_upsert_daily_block_preserves_user_text_before_start_marker(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 9)
    storage = VaultStorage(vault_path)
    daily_path = storage.get_daily_file(day)
    original = _valid_daily_bytes(
        day,
        b"\n",
        (
            b"## 08:00 [plaud]\n"
            b"User annotation that is not managed.\n"
            b"<!-- demo:start -->\n"
            b"Old managed body.\n"
            b"<!-- demo:end -->\n"
        ),
    )
    daily_path.write_bytes(original)

    storage.upsert_daily_block(
        day=day,
        start_marker="<!-- demo:start -->",
        end_marker="<!-- demo:end -->",
        block=(
            "\n## 08:00 [plaud]\n"
            "<!-- demo:start -->\n"
            "New managed body.\n"
            "<!-- demo:end -->\n"
        ),
        refresh_qmd=False,
    )

    result = daily_path.read_bytes()
    assert b"User annotation that is not managed.\n" in result
    assert result.count(b"## 08:00 [plaud]\n") == 1
    assert b"Old managed body." not in result
    assert b"New managed body." in result


@pytest.mark.parametrize(
    ("newline", "tail", "separator"),
    [
        (b"\n", b"LF insertion trailing spaces  \t", b"\n\n"),
        (b"\r\n", b"mixed insertion\r\nends in LF\n", b"\r\n"),
        (b"\r\n", b"keeps all blank lines\r\n\r\n\r\n", b""),
    ],
)
def test_upsert_daily_block_insertion_preserves_bytes_and_uses_minimal_separator(
    tmp_path: Path,
    newline: bytes,
    tail: bytes,
    separator: bytes,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 10)
    storage = VaultStorage(vault_path)
    daily_path = storage.get_daily_file(day)
    original = _valid_daily_bytes(day, newline, tail)
    daily_path.write_bytes(original)

    storage.upsert_daily_block(
        day=day,
        start_marker="<!-- demo:start -->",
        end_marker="<!-- demo:end -->",
        block=(
            "\n## 08:00 [plaud]\n"
            "<!-- demo:start -->\n"
            "Managed body.\n"
            "<!-- demo:end -->\n"
        ),
        refresh_qmd=False,
    )

    rendered_block = (
        b"## 08:00 [plaud]"
        + newline
        + b"<!-- demo:start -->"
        + newline
        + b"Managed body."
        + newline
        + b"<!-- demo:end -->"
        + newline
    )
    assert daily_path.read_bytes() == original + separator + rendered_block


def test_upsert_daily_block_ignores_header_markers_and_replaces_body_pair(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 11)
    storage = VaultStorage(vault_path)
    daily_path = storage.get_daily_file(day)
    original = _valid_daily_bytes(
        day,
        b"\n",
        (
            b"## 08:00 [plaud]\n"
            b"<!-- demo:start -->\n"
            b"Old managed body.\n"
            b"<!-- demo:end -->\n"
        ),
    )
    original = original.replace(
        b"tier: active\n",
        (
            b"tier: active\n"
            b'header_start: "<!-- demo:start -->"\n'
            b'header_end: "<!-- demo:end -->"\n'
        ),
        1,
    )
    original_header = parse_frontmatter_bytes(original).header
    daily_path.write_bytes(original)

    storage.upsert_daily_block(
        day=day,
        start_marker="<!-- demo:start -->",
        end_marker="<!-- demo:end -->",
        block=(
            "\n## 08:00 [plaud]\n"
            "<!-- demo:start -->\n"
            "New managed body.\n"
            "<!-- demo:end -->\n"
        ),
        refresh_qmd=False,
    )

    result = daily_path.read_bytes()
    assert parse_frontmatter_bytes(result).header == original_header
    assert b"Old managed body." not in result
    assert b"New managed body." in result


def test_upsert_daily_block_ignores_prose_and_marker_substrings_when_inserting(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 12)
    storage = VaultStorage(vault_path)
    daily_path = storage.get_daily_file(day)
    original = _valid_daily_bytes(
        day,
        b"\r\n",
        (
            b"Prose mentions <!-- demo:start --> without owning a line.\r\n"
            b"<!-- demo:end --> has a suffix and is not exact.\n"
            b"prefix <!-- demo:start -->\r\n"
        ),
    )
    daily_path.write_bytes(original)

    storage.upsert_daily_block(
        day=day,
        start_marker="<!-- demo:start -->",
        end_marker="<!-- demo:end -->",
        block=(
            "\n## 08:00 [plaud]\n"
            "<!-- demo:start -->\n"
            "Managed body.\n"
            "<!-- demo:end -->\n"
        ),
        refresh_qmd=False,
    )

    result = daily_path.read_bytes()
    assert result.startswith(original)
    assert result.count(b"\n<!-- demo:start -->\r\n") == 1
    assert result.count(b"\r\n<!-- demo:end -->\r\n") == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            (
                b"## 08:00 [plaud]\n"
                b"<!-- demo:start -->\n"
                b"First.\n"
                b"<!-- demo:end -->\n\n"
                b"## 09:00 [plaud]\n"
                b"<!-- demo:start -->\n"
                b"Second.\n"
                b"<!-- demo:end -->\n"
            ),
            "start=2 end=2",
        ),
        (b"<!-- demo:start -->\nOrphan.\n", "start=1 end=0"),
        (b"Orphan.\n<!-- demo:end -->\n", "start=0 end=1"),
        (
            b"<!-- demo:end -->\nReverse.\n<!-- demo:start -->\n",
            "end marker must follow",
        ),
        (
            (
                b"<!-- demo:start -->\n"
                b"Duplicate start.\n"
                b"<!-- demo:start -->\n"
                b"<!-- demo:end -->\n"
            ),
            "start=2 end=1",
        ),
    ],
)
def test_upsert_daily_block_rejects_ambiguous_markers_without_writing(
    tmp_path: Path,
    body: bytes,
    message: str,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 13)
    storage = VaultStorage(vault_path)
    daily_path = storage.get_daily_file(day)
    original = _valid_daily_bytes(day, b"\n", body)
    daily_path.write_bytes(original)

    with pytest.raises(ManagedBlockError, match=message):
        storage.upsert_daily_block(
            day=day,
            start_marker="<!-- demo:start -->",
            end_marker="<!-- demo:end -->",
            block=(
                "\n## 10:00 [plaud]\n"
                "<!-- demo:start -->\n"
                "Replacement.\n"
                "<!-- demo:end -->\n"
            ),
            refresh_qmd=False,
        )

    assert daily_path.read_bytes() == original
