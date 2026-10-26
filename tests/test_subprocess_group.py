from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from d_brain.services import documents, todoist_projects
from d_brain.services.documents import DocumentArchiveService
from d_brain.services.subprocess_group import run_in_process_group
from d_brain.services.todoist_projects import TodoistProjectCatalog


def _is_dead(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state in {"Z", "X"}


def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _is_dead(pid):
            return True
        time.sleep(0.05)
    return _is_dead(pid)


def _run_with_grandchild(tmp_path: Path, script: str) -> int:
    pid_file = tmp_path / "pid"
    with pytest.raises(subprocess.TimeoutExpired):
        run_in_process_group(
            ["sh", "-c", script.format(pid=pid_file)], timeout=1
        )
    return int(pid_file.read_text())


def test_timeout_kills_grandchild(tmp_path: Path) -> None:
    pid = _run_with_grandchild(tmp_path, "sleep 300 & echo $! > {pid}; wait")
    assert _wait_dead(pid)


def test_timeout_kills_sigterm_ignoring_grandchild(tmp_path: Path) -> None:
    pid = _run_with_grandchild(
        tmp_path, "(trap '' TERM; sleep 300) & echo $! > {pid}; wait"
    )
    assert _wait_dead(pid)


def test_success_returns_completed_process() -> None:
    result = run_in_process_group(
        ["sh", "-c", "cat; echo err >&2; exit 3"], input="hello"
    )
    assert (result.stdout, result.stderr, result.returncode) == ("hello", "err\n", 3)


def test_missing_binary_raises_filenotfound() -> None:
    with pytest.raises(FileNotFoundError):
        run_in_process_group(["definitely-not-a-binary-xyz"])


def test_control_plane_doc_describes_kimi_auto_approval() -> None:
    doc = Path(__file__).resolve().parent.parent / "docs" / "control-plane.md"
    text = doc.read_text(encoding="utf-8")
    assert "Known limitation: Kimi ACP auto-approval" in text
    assert "allow_always" in text


def _fake_executable(path: Path, pid_file: Path) -> None:
    # Spawns a long-lived child, records its pid and waits, like a stuck tool.
    path.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {pid_file}\nwait\n")
    path.chmod(0o755)


def test_todoist_find_projects_timeout_kills_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "pid"
    _fake_executable(bin_dir / "mcp-cli", pid_file)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(todoist_projects, "TODOIST_FIND_PROJECTS_TIMEOUT", 1)
    vault = tmp_path / "project" / "vault"
    vault.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="timed out"):
        TodoistProjectCatalog(vault, todoist_api_key="t").refresh()

    assert _wait_dead(int(pid_file.read_text()))


def test_document_extract_timeout_kills_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "pid"
    fake_python = tmp_path / "fake-python"
    _fake_executable(fake_python, pid_file)
    monkeypatch.setattr(documents.sys, "executable", str(fake_python))
    monkeypatch.setattr(documents, "DOCUMENT_EXTRACTION_TIMEOUT_SECONDS", 1)
    vault = tmp_path / "vault"
    vault.mkdir()

    payload = DocumentArchiveService(vault)._extract_payload(
        original_path="imports/r.pdf", file_format="pdf", original_name="r.pdf"
    )

    assert "timed out" in payload["warnings"][0]
    assert _wait_dead(int(pid_file.read_text()))
