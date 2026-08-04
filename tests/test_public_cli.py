from __future__ import annotations

import json
import stat
from pathlib import Path

from d_brain import run_qmd
from d_brain.cli import initialize_project, main


def test_initialize_project_creates_private_vault(tmp_path: Path) -> None:
    vault_path = initialize_project(tmp_path)

    assert vault_path == tmp_path / "vault"
    assert (vault_path / "MEMORY.md").is_file()
    assert (tmp_path / "skills/dbrain-processor/references/about.md").is_file()
    skills_path = tmp_path / "skills"
    assert {
        path.name
        for path in skills_path.iterdir()
        if path.is_dir() and path.name != "private"
    } == {
        "agent-memory",
        "anythingllm-search",
        "architecture-diagram",
        "arxiv",
        "blogwatcher",
        "content-research-writer",
        "datetime",
        "dbrain-processor",
        "decision-framework",
        "doc-coauthoring",
        "docx",
        "excalidraw",
        "graph-builder",
        "humanizer",
        "ms-project",
        "multi-agent-brainstorm",
        "negotiation-prep",
        "playwright-cli",
        "pptx",
        "ru-editor",
        "sequential-thinking",
        "todoist-ai",
        "vault-health",
        "vault-retrieval",
        "youtube-transcript",
    }
    private_path = vault_path / "skills/private"
    assert private_path.is_dir()
    assert list(private_path.iterdir()) == []
    private_alias = skills_path / "private"
    assert private_alias.is_symlink()
    assert private_alias.readlink() == Path("../vault/skills/private")
    assert (tmp_path / "vault-manifest.json").is_file()
    assert json.loads((tmp_path / "mcp-config.json").read_text())["mcpServers"]
    assert "vault/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    for name in ("CLAUDE.md", "GEMINI.md", "QWEN.md"):
        alias = tmp_path / name
        assert alias.is_symlink()
        assert alias.readlink() == Path("AGENTS.md")
        assert alias.read_bytes() == (tmp_path / "AGENTS.md").read_bytes()
    for relative in (
        ".claude/skills",
        ".agents/skills",
        ".codex/skills",
        ".qwen/skills",
    ):
        alias = tmp_path / relative
        assert alias.is_symlink()
        assert alias.readlink() == Path("../skills")
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_initialize_project_does_not_overwrite_vault(tmp_path: Path) -> None:
    vault_path = initialize_project(tmp_path)
    marker = vault_path / "MEMORY.md"
    marker.write_text("private", encoding="utf-8")

    assert main(["init", str(tmp_path)]) == 2
    assert marker.read_text(encoding="utf-8") == "private"


def test_initialize_project_keeps_existing_public_config(tmp_path: Path) -> None:
    manifest = tmp_path / "vault-manifest.json"
    manifest.write_text('{"custom": true}', encoding="utf-8")

    initialize_project(tmp_path)

    assert manifest.read_text(encoding="utf-8") == '{"custom": true}'


def test_qmd_command_forwards_arguments(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_main(args: list[str]) -> int:
        calls.append(args)
        return 7

    monkeypatch.setattr(run_qmd, "main", fake_main)

    assert main(["qmd", "recall", "weekly focus", "--limit", "3"]) == 7
    assert calls == [["recall", "weekly focus", "--limit", "3"]]


def test_packaged_project_files_match_repository_defaults() -> None:
    project_root = Path(__file__).resolve().parents[1]
    template_root = project_root / "src/d_brain/resources/project_template"

    for name in (
        "AGENTS.md",
        ".env.example",
        "mcp-config.json",
        "vault-manifest.json",
    ):
        assert (template_root / name).read_bytes() == (project_root / name).read_bytes()

    env_example = (project_root / ".env.example").read_text(encoding="utf-8")
    for name in (
        "ANYTHINGLLM_BASE_URL",
        "ANYTHINGLLM_API_KEY",
        "ANYTHINGLLM_WORKSPACE",
    ):
        assert f"{name}=\n" in env_example


def test_agent_instructions_separate_public_and_private_skills() -> None:
    project_root = Path(__file__).resolve().parents[1]
    instructions = (project_root / "AGENTS.md").read_text(encoding="utf-8")

    assert "src/d_brain/resources/project_template/skills/<name>" in instructions
    assert "vault/skills/private/<name>" in instructions
    assert "skills/<name> -> private/<name>" in instructions


def test_private_skills_are_explicitly_ignored() -> None:
    project_root = Path(__file__).resolve().parents[1]
    template_root = project_root / "src/d_brain/resources/project_template"
    rules = (
        "vault/skills/private/",
        "skills/*",
    )

    for rule in rules:
        assert rule in (project_root / ".gitignore").read_text(encoding="utf-8")
        assert rule in (template_root / ".gitignore").read_text(encoding="utf-8")
