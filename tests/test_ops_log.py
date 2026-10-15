"""Tests for the shared operations journal (T2, ``services/ops_log.py``)."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytest

from d_brain.services.ops_log import append_ops_log


def test_append_ops_log_collapses_multiline_summary_to_one_line(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    now = datetime(2026, 9, 25, 9, 5)

    append_ops_log(
        vault_path, "ingest", "Первая строка\n  Вторая строка\tвопроса", now=now
    )

    log_path = vault_path / ".session" / "log.md"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["- 2026-09-25 09:05 [ingest] Первая строка Вторая строка вопроса"]


def test_append_ops_log_creates_session_log_and_appends_in_order(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    now = datetime(2026, 9, 25, 9, 5)

    append_ops_log(vault_path, "ingest", "Первая запись", now=now)
    append_ops_log(vault_path, "compile", "Вторая запись", now=now)

    log_path = vault_path / ".session" / "log.md"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "- 2026-09-25 09:05 [ingest] Первая запись",
        "- 2026-09-25 09:05 [compile] Вторая запись",
    ]


def test_append_ops_log_clips_long_summary_with_ellipsis(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    now = datetime(2026, 9, 25, 9, 5)

    append_ops_log(vault_path, "nightly", "x" * 400, now=now)

    log_path = vault_path / ".session" / "log.md"
    line = log_path.read_text(encoding="utf-8").splitlines()[0]
    summary = line.split("] ", 1)[1]
    assert len(summary) == 300
    assert summary.endswith("…")
    assert summary[:-1] == "x" * 299


def test_append_ops_log_uses_explicit_now_verbatim(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    now = datetime(2020, 1, 2, 3, 4, 5)

    append_ops_log(vault_path, "answer", "Проверка формата времени", now=now)

    log_path = vault_path / ".session" / "log.md"
    line = log_path.read_text(encoding="utf-8").splitlines()[0]
    assert line == "- 2020-01-02 03:04 [answer] Проверка формата времени"


def test_append_ops_log_does_not_raise_on_surrogate_summary(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    now = datetime(2026, 9, 25, 9, 5)
    # A lone surrogate (e.g. from a mis-decoded transcript) cannot be
    # encoded as UTF-8 under the default "strict" error handler -- this
    # best-effort write must not raise ``UnicodeEncodeError`` over it.
    summary_with_surrogate = "плохая строка \udcff конец"

    append_ops_log(vault_path, "ingest", summary_with_surrogate, now=now)

    log_path = vault_path / ".session" / "log.md"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    assert lines[0].startswith("- 2026-09-25 09:05 [ingest] плохая строка")


def test_append_ops_log_swallows_oserror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    # ``.session`` exists as a plain file, so ``mkdir(parents=True,
    # exist_ok=True)`` for ``.session/log.md``'s parent fails with an
    # ``OSError`` -- best-effort: the caller's own write must not fail.
    (vault_path / ".session").write_text("not a directory", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        append_ops_log(vault_path, "ingest", "Не должно упасть")

    assert "Failed to append ops log entry" in caplog.text
