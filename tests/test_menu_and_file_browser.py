from pathlib import Path

import pytest

from d_brain.bot.dashboard import (
    build_help_text,
    build_home_keyboard,
    build_stats_text,
)
from d_brain.services.file_browser import (
    FileBrowserPathError,
    FileBrowserService,
)
from d_brain.services.session import SessionStore


def test_build_help_text_mentions_menu_files_and_stats() -> None:
    text = build_help_text()

    assert "/menu" in text
    assert "/files" in text
    assert "/stats" in text
    assert "/help" in text
    assert "/status" not in text


def test_help_text_keeps_start_menu_do_at_the_top() -> None:
    text = build_help_text()

    assert text.index("/start") < text.index("/menu") < text.index("/do")


def test_home_keyboard_has_close_button_and_no_help_button() -> None:
    keyboard = build_home_keyboard()
    labels = [
        button.text
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "❌ Закрыть меню" in labels
    assert all("Помощ" not in label for label in labels)


def test_build_stats_text_uses_capture_counts_only(tmp_path: Path) -> None:
    session = SessionStore(tmp_path)
    session.append(42, "voice")
    session.append(42, "document")
    session.append(42, "command", cmd="/menu")

    text = build_stats_text(tmp_path, 42)

    assert "🎤 Голосовые: 1" in text
    assert "📄 Документы: 1" in text
    assert "Всего записей: **2**" in text
    assert "command" not in text


def test_file_browser_roots_are_curated_and_presence_based(tmp_path: Path) -> None:
    (tmp_path / "daily").mkdir()
    (tmp_path / "goals").mkdir()

    roots = FileBrowserService(tmp_path).list_roots()
    root_ids = [root.id for root in roots]

    assert root_ids == ["daily", "goals", "vault"]


def test_file_browser_skips_symlinks_and_hidden_top_level_dirs(tmp_path: Path) -> None:
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "2026-04-04.md").write_text("x", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / ".session").mkdir()
    (tmp_path / "z-file.txt").write_text("z", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "z-file.txt")

    _root, _current_dir, entries = FileBrowserService(tmp_path).list_entries(
        root_id="vault"
    )
    names = [entry.name for entry in entries]

    assert ".session" not in names
    assert "link.txt" not in names
    assert names[:2] == ["daily", "notes"]
    assert names[-1] == "z-file.txt"


def test_file_browser_prevents_path_escape(tmp_path: Path) -> None:
    (tmp_path / "daily").mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    service = FileBrowserService(tmp_path)

    with pytest.raises(FileBrowserPathError):
        service.resolve_path(root_id="daily", relative_path="../outside.txt")


def test_file_browser_rejects_hidden_internal_paths_for_full_vault(
    tmp_path: Path,
) -> None:
    (tmp_path / ".session").mkdir()

    service = FileBrowserService(tmp_path)

    with pytest.raises(FileBrowserPathError):
        service.resolve_path(root_id="vault", relative_path=".session")
