import json
import os
import subprocess
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path

from _paths import PROJECT_ROOT, TEMPLATE_ROOT

from d_brain.services.context_pack import ContextPackBuilder


def _snapshot_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _create_vault(
    tmp_path: Path,
    write_vault_manifest,
    *,
    budget: int = 200_000,
) -> Path:
    vault = tmp_path / "vault"
    for directory in ("daily", "goals", "business", "projects", ".session", ".graph"):
        (vault / directory).mkdir(parents=True, exist_ok=True)
    files = {
        "MEMORY.md": "Память: важное решение.\n",
        "goals/3-weekly.md": "Неделя: завершить контекст.\n",
        "goals/2-monthly.md": "Месяц: укрепить поиск.\n",
        "goals/1-yearly-2026.md": "Год: надёжная память.\n",
        "business/_index.md": "Бизнес индекс.\n",
        "projects/_index.md": "Проекты индекс.\n",
        "daily/2026-07-29.md": "Сегодня.\n",
        "daily/2026-07-28.md": "Вчера.\n",
        ".session/handoff.md": "Передача.\n",
    }
    for relative, content in files.items():
        (vault / relative).write_text(content, encoding="utf-8")
    write_vault_manifest(
        vault,
        overrides={
            "context_budget_bytes": budget,
            "user_content_roots": [
                "vault/MEMORY.md",
                "vault/daily",
                "vault/goals",
                "vault/business",
                "vault/projects",
            ],
            "infrastructure": [
                "vault/.session",
                "vault/.graph",
                "vault/.qmd",
            ],
        },
    )
    return vault


def test_context_pack_counts_utf8_bytes_and_loaded_paths(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault = _create_vault(tmp_path, write_vault_manifest)

    pack = ContextPackBuilder(vault).build(date(2026, 7, 29))

    assert pack.byte_count == len(pack.text.encode("utf-8"))
    assert pack.text.endswith("\n")
    assert "Память: важное решение." in pack.text
    assert "MEMORY.md" in pack.loaded_paths
    assert "_context injected:" in pack.text.splitlines()[-1]
    assert "collapsed: none" in pack.text


def test_context_pack_collapses_complete_sections_in_priority_order(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault = _create_vault(tmp_path, write_vault_manifest, budget=1_100)
    (vault / "daily/2026-07-28.md").write_text("y" * 2_000, encoding="utf-8")

    pack = ContextPackBuilder(vault).build(date(2026, 7, 29))

    assert pack.collapsed_sections[:2] == ("listing", "git_summary")
    assert "[vault listing omitted to fit the context budget]" in pack.text
    assert "y" * 2_000 not in pack.text
    assert (
        "[core source omitted to fit the context budget: daily/2026-07-28.md]"
        in pack.text
    )
    assert pack.byte_count == len(pack.text.encode("utf-8"))


def test_context_pack_uses_the_full_declared_collapse_order(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault = _create_vault(tmp_path, write_vault_manifest, budget=1)
    (vault / "MEMORY.md").write_text("m" * 2_000, encoding="utf-8")

    pack = ContextPackBuilder(vault).build(date(2026, 7, 29))

    assert pack.collapsed_sections == (
        "listing",
        "git_summary",
        "hygiene",
        "projects_index",
        "business_index",
        "yesterday_daily",
        "monthly_goals",
        "yearly_goals",
        "today_daily",
        "handoff",
    )


def test_context_pack_keeps_load_bearing_sections_when_minimum_exceeds_budget(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault = _create_vault(tmp_path, write_vault_manifest, budget=100)
    (vault / "MEMORY.md").write_text("m" * 2_000, encoding="utf-8")

    pack = ContextPackBuilder(vault).build(date(2026, 7, 29))

    assert pack.over_budget is True
    assert "m" * 2_000 in pack.text
    assert "memory" not in pack.collapsed_sections


def test_context_pack_uses_target_day_and_safe_optional_file_reading(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault = _create_vault(tmp_path, write_vault_manifest)
    handoff = vault / ".session/handoff.md"
    handoff.unlink()
    handoff.symlink_to(vault / "MEMORY.md")

    pack = ContextPackBuilder(vault).build(date(2026, 7, 29))

    assert "Target date: 2026-07-29" in pack.text
    assert "daily/2026-07-28.md" in pack.loaded_paths
    assert ".session/handoff.md" not in pack.loaded_paths
    assert (
        "[core source unavailable: .session/handoff.md; symlink is not allowed]"
        in pack.text
    )


def test_context_pack_safe_read_rejects_lexical_and_resolved_vault_escapes(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault = _create_vault(tmp_path, write_vault_manifest)
    builder = ContextPackBuilder(vault)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (vault / "outside-link.md").symlink_to(outside)

    assert builder._read_relative("../outside.md") == (None, "path escapes vault")
    assert builder._read_relative("outside-link.md") == (None, "path escapes vault")


def test_context_pack_hygiene_summary_uses_existing_snapshots_only(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault = _create_vault(tmp_path, write_vault_manifest)
    (vault / ".graph/vault-graph.json").write_text(
        json.dumps({"health_score": 91, "broken_links": 0, "ignored": "no"}),
        encoding="utf-8",
    )

    pack = ContextPackBuilder(vault).build(date(2026, 7, 29))

    assert "health_score=91" in pack.text
    assert "ignored=no" not in pack.text


def test_context_pack_cli_plain_and_hook_json_are_equivalent(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    _create_vault(tmp_path, write_vault_manifest)
    before = _snapshot_tree(tmp_path)
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
    }
    command = [sys.executable, "-m", "d_brain.run_context_pack", "--date", "2026-07-29"]

    plain = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )
    hook = subprocess.run(
        [*command, "--hook-json"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )

    assert plain.returncode == hook.returncode == 0
    payload = json.loads(hook.stdout)
    assert payload["hookSpecificOutput"]["additionalContext"] == plain.stdout
    assert payload["additional_context"] == plain.stdout
    assert _snapshot_tree(tmp_path) == before


def test_context_pack_cli_reports_missing_manifest_without_traceback(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "d_brain.run_context_pack"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
        },
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "context-pack: vault-manifest.json is missing\n"


def test_session_start_hook_uses_vault_cwd_and_runtime_mode_has_no_output(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault = _create_vault(tmp_path, write_vault_manifest)
    settings = json.loads(
        (TEMPLATE_ROOT / ".claude/settings.json").read_text(encoding="utf-8")
    )
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    hook = vault / ".claude/hooks/context-pack.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        (TEMPLATE_ROOT / ".claude/hooks/context-pack.sh").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    hook.chmod(0o755)
    fake_uv = tmp_path / "bin/uv"
    fake_uv.parent.mkdir()
    fake_uv.write_text(
        "#!/usr/bin/env sh\n"
        "shift\n"
        "shift\n"
        "exec \"$PYTHON_EXECUTABLE\" \"$@\"\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    environment = {
        **os.environ,
        "D_BRAIN_CONTEXT_PACK_MODE": "runtime",
        "PATH": f"{fake_uv.parent}:{os.environ['PATH']}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "PYTHON_EXECUTABLE": sys.executable,
    }
    before = _snapshot_tree(tmp_path)

    runtime = subprocess.run(
        command.split(),
        cwd=vault,
        capture_output=True,
        check=False,
        input="{}",
        text=True,
        env=environment,
    )
    manual_environment = {
        key: value
        for key, value in environment.items()
        if key != "D_BRAIN_CONTEXT_PACK_MODE"
    }
    manual = subprocess.run(
        command.split(),
        cwd=vault,
        capture_output=True,
        check=False,
        input="{}",
        text=True,
        env=manual_environment,
    )

    assert command == "bash .claude/hooks/context-pack.sh"
    assert runtime.returncode == 0
    assert runtime.stdout == runtime.stderr == ""
    assert manual.returncode == 0
    assert manual.stderr == ""
    payload = json.loads(manual.stdout)
    assert payload["hookSpecificOutput"]["additionalContext"].startswith(
        "=== INJECTED CORE CONTEXT ===\n"
    )
    assert _snapshot_tree(tmp_path) == before
