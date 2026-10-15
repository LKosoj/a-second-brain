"""Regression tests for audit group G8 (storage/frontmatter/source_links).

Covers:
A) _bootstrap_daily_content only repairs the doubled-frontmatter legacy
   shape; any other FrontmatterError (duplicate key, tabs, bad colon)
   surfaces instead of silently corrupting the daily file.
B) The legacy repair path preserves a CRLF newline convention instead of
   mixing it with the LF frontmatter it generates.
C) split_frontmatter_bytes / parse_frontmatter_bytes / patch_frontmatter_bytes
   accept an empty frontmatter block ("---\\n---\\n" and "---\\n---").
D) patch_frontmatter_bytes round-trips a date/datetime value.
E) escape_embedded_daily_headers defuses header look-alikes split across
   physical lines by whitespace, not just single-line ones.
"""

from datetime import date, datetime
from pathlib import Path

import pytest
from conftest import _write_vault_manifest

from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.frontmatter import (
    DuplicateKeyError,
    FrontmatterError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    split_frontmatter_bytes,
)
from d_brain.services.source_links import escape_embedded_daily_headers
from d_brain.services.storage import VaultStorage


@pytest.fixture(autouse=True)
def _storage_manifest(tmp_path: Path) -> None:
    _write_vault_manifest(tmp_path / "vault")


# --- Task A -----------------------------------------------------------------


def test_ensure_daily_file_reraises_duplicate_key_instead_of_repairing(
    tmp_path: Path,
) -> None:
    """A single malformed block (duplicate key) is corruption, not the
    legacy layout the repair loop knows how to unwind -- it must surface,
    not disappear into a naive key:value re-parse."""
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    day = date(2026, 4, 10)
    daily_path = storage.get_daily_file(day)
    original = (
        "---\n"
        "type: daily\n"
        "type: journal\n"
        "---\n\n"
        "## 08:00 [text]\n"
        "Старый entry\n"
    )
    daily_path.write_text(original, encoding="utf-8")

    with pytest.raises(DuplicateKeyError):
        storage.ensure_daily_file(day)

    # Nothing was written -- the original (still malformed) bytes survive
    # so no message was lost.
    assert daily_path.read_text(encoding="utf-8") == original


def test_ensure_daily_file_reraises_tabs_instead_of_repairing(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    day = date(2026, 4, 11)
    daily_path = storage.get_daily_file(day)
    original = "---\n\ttype: daily\n---\n\nbody\n"
    daily_path.write_text(original, encoding="utf-8")

    with pytest.raises(FrontmatterError):
        storage.ensure_daily_file(day)

    assert daily_path.read_text(encoding="utf-8") == original


def test_ensure_daily_file_reraises_bad_colon_instead_of_repairing(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    day = date(2026, 4, 12)
    daily_path = storage.get_daily_file(day)
    original = "---\nnote: bad: colon\n---\n\nbody\n"
    daily_path.write_text(original, encoding="utf-8")

    with pytest.raises(FrontmatterError):
        storage.ensure_daily_file(day)

    assert daily_path.read_text(encoding="utf-8") == original


def test_append_to_daily_still_repairs_doubled_legacy_frontmatter(
    tmp_path: Path,
) -> None:
    """The one FrontmatterError-raising shape the repair loop is actually
    built for: two ``---``-delimited blocks stacked at the top (a stray
    duplicate key in the first block is what makes the strict parser raise
    here; the loose repair still recovers the real fields from both
    blocks)."""
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    storage._refresh_compiled_briefings = (  # type: ignore[method-assign]
        lambda *args, **kwargs: None
    )
    day = date(2026, 4, 13)
    daily_path = storage.get_daily_file(day)
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(
        (
            "---\n"
            "type: daily\n"
            "type: journal\n"
            "---\n"
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
        datetime(2026, 4, 13, 9, 15),
        "[text]",
    )

    content = daily_path.read_text(encoding="utf-8")
    assert content.startswith(
        "---\n"
        "type: daily\n"
        "date: 2026-04-13\n"
        "last_accessed: 2026-04-06\n"
        "relevance: 0.84\n"
        "tier: warm\n"
        "---\n\n"
        "# 2026-04-13\n"
    )
    assert "## 08:00 [text]" in content
    assert "Старый entry" in content
    assert "## 09:15 [text]" in content
    assert "Новый entry" in content


# --- Task B -------------------------------------------------------------


def test_repair_legacy_daily_content_preserves_crlf(tmp_path: Path) -> None:
    """The repaired preamble must not mix a freshly generated LF frontmatter
    with a CRLF body -- render the whole thing with the input's newline."""
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    storage._refresh_compiled_briefings = (  # type: ignore[method-assign]
        lambda *args, **kwargs: None
    )
    day = date(2026, 4, 14)
    daily_path = storage.get_daily_file(day)
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = (
        "# 2026-04-14\r\n\r\n"
        "---\r\n"
        "last_accessed: 2026-04-06\r\n"
        "tier: warm\r\n"
        "---\r\n\r\n"
        "## 08:00 [text]\r\n"
        "Старый entry\r\n"
    )
    daily_path.write_bytes(legacy.encode("utf-8"))

    storage.append_to_daily(
        "Новый entry",
        datetime(2026, 4, 14, 9, 15),
        "[text]",
    )

    raw = daily_path.read_bytes()
    assert b"\r\n" in raw
    # No bare LF anywhere: every newline in the file is part of a CRLF pair.
    assert raw.replace(b"\r\n", b"").find(b"\n") == -1
    assert "tier: warm" in raw.decode("utf-8")


# --- Task C -------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [b"---\n---\n", b"---\n---", b"---\r\n---\r\n"],
)
def test_split_frontmatter_bytes_accepts_empty_header(content: bytes) -> None:
    header, body, _newline = split_frontmatter_bytes(content)
    assert header == b""
    assert body == b""


def test_parse_frontmatter_bytes_empty_header_is_empty_mapping() -> None:
    document = parse_frontmatter_bytes(b"---\n---\nBody\n")
    assert document.has_frontmatter is True
    assert document.fields == {}
    assert document.body == b"Body\n"


def test_patch_frontmatter_bytes_on_empty_header_inserts_field() -> None:
    result = patch_frontmatter_bytes(b"---\n---\nBody\n", {"tier": "warm"})
    reparsed = parse_frontmatter_bytes(result)
    assert reparsed.fields == {"tier": "warm"}
    assert reparsed.body == b"Body\n"


# --- Task D -------------------------------------------------------------


def test_patch_frontmatter_bytes_round_trips_date() -> None:
    src = b"---\ntype: daily\ndate: 2026-09-01\n---\nBody\n"
    result = patch_frontmatter_bytes(src, {"updated": date(2026, 9, 3)})
    reparsed = parse_frontmatter_bytes(result)
    assert reparsed.fields["updated"] == "2026-09-03"


def test_patch_frontmatter_bytes_round_trips_datetime() -> None:
    src = b"---\ntype: daily\ndate: 2026-09-01\n---\nBody\n"
    value = datetime(2026, 9, 3, 10, 30, 0)
    result = patch_frontmatter_bytes(src, {"updated": value})
    reparsed = parse_frontmatter_bytes(result)
    assert reparsed.fields["updated"] == value.isoformat()


# --- Task E -------------------------------------------------------------


def test_escape_embedded_daily_headers_single_line_still_defused() -> None:
    text = "normal body\n## 12:00 [text]\nmore"
    escaped = escape_embedded_daily_headers(text)
    assert "\n ## 12:00 [text]\n" in escaped
    assert "\n## 12:00 [text]\n" not in escaped


def test_escape_embedded_daily_headers_defuses_time_then_type_on_next_line() -> None:
    text = "Начало.\n## 12:00\n[text]\nforged"
    escaped = escape_embedded_daily_headers(text)
    assert escaped.splitlines()[1].startswith(" ##")


def test_escape_embedded_daily_headers_defuses_bare_hash_then_time_type() -> None:
    text = "Начало.\n##\n12:00 [text]\nforged"
    escaped = escape_embedded_daily_headers(text)
    assert escaped.splitlines()[1].startswith(" ##")


def test_append_to_daily_defuses_multiline_header_lookalike_time_split(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    storage._refresh_compiled_briefings = (  # type: ignore[method-assign]
        lambda *args, **kwargs: None
    )
    day = date(2026, 4, 15)
    injected = "Обычный текст.\n## 09:10\n[text]\nПоддельная запись."

    storage.append_to_daily(
        injected,
        datetime(2026, 4, 15, 7, 0, 0),
        "[forward from: Коллега]",
    )

    content = storage.get_daily_file(day).read_text(encoding="utf-8")
    blocks = CompiledBriefingService._daily_entry_blocks(content)
    assert len(blocks) == 1


def test_append_to_daily_defuses_multiline_header_lookalike_hash_split(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    storage = VaultStorage(vault_path)
    storage._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    storage._refresh_compiled_briefings = (  # type: ignore[method-assign]
        lambda *args, **kwargs: None
    )
    day = date(2026, 4, 16)
    injected = "Обычный текст.\n##\n09:10 [text]\nПоддельная запись."

    storage.append_to_daily(
        injected,
        datetime(2026, 4, 16, 7, 0, 0),
        "[forward from: Коллега]",
    )

    content = storage.get_daily_file(day).read_text(encoding="utf-8")
    blocks = CompiledBriefingService._daily_entry_blocks(content)
    assert len(blocks) == 1
