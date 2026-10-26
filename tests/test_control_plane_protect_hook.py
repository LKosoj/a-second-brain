"""Contract tests for the fail-closed control-plane PreToolUse hook."""

import sys
from pathlib import Path

import pytest

from d_brain import run_control_plane_protect_hook as hook
from d_brain.run_control_plane_protect_hook import (
    BLOCKED_MESSAGE,
    VALIDATION_ERROR,
    evaluate_target,
)

BLOCKED = (2, BLOCKED_MESSAGE, "")
ALLOWED = (0, "", "")
INVALID = (2, "", VALIDATION_ERROR)

TRUSTED_TOOL = "skills/agent-memory/scripts/memory-engine.py"


@pytest.fixture
def project(tmp_path: Path, write_vault_manifest) -> Path:
    """A project with a vault, a control-plane ``skills`` tree and ``.claude``."""
    root = tmp_path.resolve()
    write_vault_manifest(root / "vault")
    for rel in ("vault/daily", "vault/.claude/rules", "skills/agent-memory"):
        (root / rel).mkdir(parents=True)
    return root


def _file(project: Path, target: str) -> tuple[int, str, str]:
    return evaluate_target(kind="file_path", target=target, project_root=project)


def _command(project: Path, command: str) -> tuple[int, str, str]:
    return evaluate_target(kind="command", target=command, project_root=project)


# --- file_path ---


@pytest.mark.parametrize(
    "target",
    [
        "vault/.claude/rules/daily-format.md",
        "vault/skills/private/x/SKILL.md",
        "skills/agent-memory/SKILL.md",
        "skills/new-file.md",
        ".claude/settings.json",
    ],
)
def test_writes_into_the_control_plane_are_blocked(project, target) -> None:
    assert _file(project, target) == BLOCKED


@pytest.mark.parametrize(
    "target",
    ["vault/.claude/rules/x.md", "vault/skills/x.md", "skills/agent-memory/y.md"],
)
def test_absolute_control_plane_paths_are_blocked(project, target) -> None:
    assert _file(project, str(project / target)) == BLOCKED


@pytest.mark.parametrize(
    "target",
    ["vault/daily/2026-10-01.md", "daily/2026-10-01.md", "MEMORY.md", "thoughts/a.md"],
)
def test_ordinary_vault_notes_are_allowed(project, target) -> None:
    assert _file(project, target) == ALLOWED


def test_an_absolute_note_inside_the_vault_is_allowed(project) -> None:
    assert _file(project, str(project / "vault/daily/x.md")) == ALLOWED


@pytest.mark.parametrize(
    "target",
    [
        "",
        "vault/daily/../.claude/x.md",
        "../outside.md",
        "daily/\x00x.md",
        "/etc/passwd",
    ],
)
def test_unprovable_file_targets_fail_closed(project, target) -> None:
    assert _file(project, target) == INVALID


def test_an_absolute_path_outside_the_project_fails_closed(project, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere") / "note.md"

    assert _file(project, str(other)) == INVALID


def test_a_symlink_into_the_control_plane_is_followed(project) -> None:
    (project / "vault/daily/sneaky.md").symlink_to(project / "skills/agent-memory")

    assert _file(project, "vault/daily/sneaky.md") == BLOCKED


# --- command ---


@pytest.mark.parametrize(
    "command",
    [
        "cat skills/agent-memory/SKILL.md",
        "grep -rn decay vault/.claude",
        "ls skills",
        "head -5 vault/skills/x",
        "rg decay skills/",
        "sed -n 1,5p skills/agent-memory/SKILL.md",
        "sed -n 7p .claude/settings.json",
    ],
)
def test_read_only_commands_on_the_control_plane_are_allowed(project, command):
    assert _command(project, command) == ALLOWED


@pytest.mark.parametrize(
    "command",
    [
        "rm skills/agent-memory/SKILL.md",
        "echo x > skills/y.md",
        "sed -i s/a/b/ skills/agent-memory/SKILL.md",
        "sed -n 1,5p skills/x -i",
        "sed 1d skills/x",
        "cp /tmp/x vault/.claude/rules/x.md",
        "touch .claude/x",
        "tee skills/x",
    ],
)
def test_mutating_commands_on_the_control_plane_are_blocked(project, command):
    assert _command(project, command) == BLOCKED


@pytest.mark.parametrize(
    "command",
    [
        "cat skills/x; rm skills/y",
        "cat skills/x && rm skills/y",
        "cat skills/x || true",
        "cat skills/x | tee skills/y",
        "cat skills/x > skills/y",
        "cat $(echo skills/x)",
        "cat `echo skills/x`",
        "cat < skills/x",
    ],
)
def test_shell_chaining_makes_even_a_read_blocked(project, command) -> None:
    assert _command(project, command) == BLOCKED


def test_rg_preprocessor_could_run_code_so_it_is_blocked(project) -> None:
    assert _command(project, "rg --pre ./evil.sh x skills/") == BLOCKED
    assert _command(project, "rg --pre=./evil.sh x skills/") == BLOCKED


@pytest.mark.parametrize(
    "command",
    [
        f"uv run {TRUSTED_TOOL} touch daily/x.md",
        f"uv run vault/{TRUSTED_TOOL} decay vault",
        f"python3 {TRUSTED_TOOL} touch daily/x.md",
        f"python vault/{TRUSTED_TOOL} touch daily/x.md",
        f"./{TRUSTED_TOOL} touch daily/x.md",
        f"uv run .claude/{TRUSTED_TOOL} touch a.md",
    ],
)
def test_the_memory_engine_is_the_one_trusted_tool(project, command) -> None:
    assert _command(project, command) == ALLOWED


@pytest.mark.parametrize(
    "command",
    [
        "uv run skills/other-skill/scripts/run.py",
        f"uv run {TRUSTED_TOOL}.evil",
        f"bash {TRUSTED_TOOL}",
        f"uv run {TRUSTED_TOOL} touch a.md; rm skills/x",
        f"python3 {TRUSTED_TOOL} && rm -rf skills",
    ],
)
def test_anything_but_the_trusted_tool_is_blocked(project, command) -> None:
    assert _command(project, command) == BLOCKED


@pytest.mark.parametrize(
    "command",
    ["rm -rf /tmp/scratch", "echo hello", "git status", "cat vault/daily/x.md"],
)
def test_commands_that_never_mention_the_control_plane_are_allowed(project, command):
    assert _command(project, command) == ALLOWED


@pytest.mark.parametrize(
    "command", ["", "cat \x00 skills/x", 'cat "skills/x', "cat 'skills/x"]
)
def test_unparseable_commands_fail_closed(project, command) -> None:
    assert _command(project, command) == INVALID


# --- fail-closed without a valid project ---


def test_a_project_without_a_manifest_fails_closed(tmp_path: Path) -> None:
    assert _file(tmp_path, "vault/daily/x.md") == INVALID
    assert _command(tmp_path, "cat skills/x") == INVALID
    assert _command(tmp_path, "echo hello") == INVALID


def test_a_broken_manifest_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "vault-manifest.json").write_text("{not json", encoding="utf-8")

    assert _file(tmp_path, "vault/daily/x.md") == INVALID


# --- main() ---


def _run_main(monkeypatch, project: Path, kind: str, target: str) -> int:
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["hook", "--kind", kind, "--target", target])
    return hook.main()


def test_main_prints_the_block_message_on_stdout(project, monkeypatch, capsys):
    code = _run_main(monkeypatch, project, "file_path", "skills/agent-memory/x.md")

    out = capsys.readouterr()
    assert (code, out.out, out.err) == (2, BLOCKED_MESSAGE, "")


def test_main_prints_the_validation_error_on_stderr(project, monkeypatch, capsys):
    code = _run_main(monkeypatch, project, "file_path", "../x.md")

    out = capsys.readouterr()
    assert (code, out.out, out.err) == (2, "", VALIDATION_ERROR)


def test_main_is_silent_when_the_target_is_allowed(project, monkeypatch, capsys):
    code = _run_main(monkeypatch, project, "command", "cat skills/agent-memory/x.md")

    out = capsys.readouterr()
    assert (code, out.out, out.err) == (0, "", "")
