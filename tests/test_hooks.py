import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from _paths import PROJECT_ROOT, TEMPLATE_ROOT

from d_brain.manifest import load_manifest
from d_brain.run_frontmatter_hook import RACE_ERROR, validate_target

HOOKS_ROOT = TEMPLATE_ROOT / ".claude/hooks"
BLOCKED_MESSAGE = (
    "BLOCKED: control-plane asset is repo-managed. Edit it in the repo workflow, "
    "not from runtime.\n"
)
VALIDATION_ERROR = "BLOCKED: unable to validate control-plane target.\n"


def _snapshot_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_manifest(project_root: Path, *, budget: int = 200_000) -> None:
    _write(
        project_root / "vault-manifest.json",
        json.dumps(
            {
                "version": 1,
                "qmd_index": "dbrain",
                "context_budget_bytes": budget,
                "memory_root": "vault",
                "user_content_roots": [
                    "vault/MEMORY.md",
                    "vault/daily",
                    "vault/goals",
                    "vault/business",
                    "vault/projects",
                    "vault/thoughts",
                ],
                "infrastructure": [
                    "vault/.claude",
                    "vault/.session",
                    "vault/.graph",
                ],
                "frontmatter_required": {
                    "default": ["type"],
                    "daily": ["type", "date"],
                    "thought-card": ["type", "description", "tags", "status"],
                    "technical": ["type"],
                    "epistemic": [
                        "type",
                        "description",
                        "epistemic_confidence",
                        "epistemic_scope",
                        "epistemic_state",
                        "created",
                        "updated",
                        "last_accessed",
                        "relevance",
                        "tier",
                    ],
                },
            },
            ensure_ascii=False,
        ),
    )


@pytest.fixture
def hook_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "мини проект"
    vault = project_root / "vault"
    for hook_name in (
        "protect-control-plane.sh",
        "validate-frontmatter.sh",
        "context-pack.sh",
    ):
        target = vault / ".claude/hooks" / hook_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HOOKS_ROOT / hook_name, target)
    shutil.copy2(
        TEMPLATE_ROOT / ".claude/settings.json",
        vault / ".claude/settings.json",
    )
    (project_root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        PROJECT_ROOT / "scripts/setup_control_plane.sh",
        project_root / "scripts/setup_control_plane.sh",
    )
    for relative in (
        "src/d_brain/control_plane/contracts.py",
        "src/d_brain/control_plane/registry.py",
        "src/d_brain/control_plane/router.py",
        "vault/.claude/docs/prompt-source-map.md",
        "docs/control-plane.md",
        "docs/control-plane-security-profile-template.md",
    ):
        _write(project_root / relative, "placeholder\n")
    for directory in (
        "daily",
        "goals",
        "business",
        "projects",
        "thoughts",
        ".session",
        ".graph",
    ):
        (vault / directory).mkdir(parents=True, exist_ok=True)
    for relative, content in {
        "MEMORY.md": "Память.\n",
        "goals/3-weekly.md": "Неделя.\n",
        "goals/2-monthly.md": "Месяц.\n",
        "goals/1-yearly-2026.md": "Год.\n",
        "business/_index.md": "Бизнес.\n",
        "projects/_index.md": "Проекты.\n",
        "daily/2026-07-29.md": "Сегодня.\n",
        "daily/2026-07-28.md": "Вчера.\n",
        ".session/handoff.md": "Передача.\n",
    }.items():
        _write(vault / relative, content)
    _write_manifest(project_root)
    return project_root


@pytest.fixture
def hook_environment(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = _write(
        bin_dir / "uv",
        "#!/usr/bin/env sh\n"
        '[ "$1" = run ] || exit 64\n'
        "shift\n"
        'while [ "${1:-}" = --frozen ] || [ "${1:-}" = --no-dev ]; do\n'
        "  shift\n"
        "done\n"
        '[ "$1" = python ] || exit 64\n'
        "shift\n"
        'exec "$PYTHON_EXECUTABLE" "$@"\n',
    )
    jq = _write(
        bin_dir / "jq",
        f'#!/usr/bin/env sh\nexec {shutil.which("jq")} "$@"\n',
    )
    uv.chmod(0o755)
    jq.chmod(0o755)
    dirname = bin_dir / "dirname"
    shutil.copy2(shutil.which("dirname"), dirname)
    dirname.chmod(0o755)
    return {
        **os.environ,
        "LC_ALL": "C.UTF-8",
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "PYTHON_EXECUTABLE": sys.executable,
    }


def _payload(**tool_input: Any) -> str:
    return json.dumps({"tool_input": tool_input}, ensure_ascii=False)


def _run_hook(
    project_root: Path,
    hook_name: str,
    payload: str,
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    foreign_cwd = project_root.parent
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(project_root / "vault/.claude/hooks" / hook_name)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        cwd=foreign_cwd,
        env=environment,
    )


@pytest.mark.parametrize(
    ("payload_key", "target"),
    [
        ("file_path", "vault/.claude/CLAUDE.md"),
        ("file_path", ".claude/CLAUDE.md"),
        ("file_path", "vault/.claude/settings.json"),
        ("file_path", "vault/.claude/docs/документ с пробелом.md"),
        ("file_path", "vault/.claude/hooks/hook.sh"),
        ("file_path", "vault/.claude/skills/skill.md"),
        ("file_path", "skills/public-skill/SKILL.md"),
        ("file_path", "vault/skills/private/private-skill/SKILL.md"),
        ("file_path", "vault/.claude/agents/agent.md"),
        ("command", "vault/.claude/rules/rule.md"),
    ],
)
def test_protect_hook_blocks_each_control_plane_root(
    hook_project: Path,
    hook_environment: dict[str, str],
    payload_key: str,
    target: str,
) -> None:
    result = _run_hook(
        hook_project,
        "protect-control-plane.sh",
        _payload(**{payload_key: target}),
        environment=hook_environment,
    )

    assert result.returncode == 2
    assert result.stdout == BLOCKED_MESSAGE
    assert result.stderr == ""


@pytest.mark.parametrize(
    "payload",
    ("", "{", "{}", _payload(file_path=["x"]), _payload(file_path="line\nbreak")),
)
def test_protect_hook_fails_closed_for_unparseable_or_ambiguous_payloads(
    hook_project: Path,
    hook_environment: dict[str, str],
    payload: str,
) -> None:
    result = _run_hook(
        hook_project,
        "protect-control-plane.sh",
        payload,
        environment=hook_environment,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == VALIDATION_ERROR


def test_protect_hook_fails_closed_when_jq_is_missing(
    hook_project: Path,
    hook_environment: dict[str, str],
) -> None:
    bin_dir = Path(hook_environment["PATH"].split(":", maxsplit=1)[0])
    (bin_dir / "jq").unlink()
    result = _run_hook(
        hook_project,
        "protect-control-plane.sh",
        _payload(file_path="daily/note.md"),
        environment={**hook_environment, "PATH": str(bin_dir)},
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == VALIDATION_ERROR


def test_protect_hook_allows_user_owned_unicode_note(
    hook_project: Path,
    hook_environment: dict[str, str],
) -> None:
    result = _run_hook(
        hook_project,
        "protect-control-plane.sh",
        _payload(file_path="vault/thoughts/идея с пробелом.md"),
        environment=hook_environment,
    )

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    ("payload", "stdout", "stderr"),
    [
        (_payload(file_path="vault/daily/control-route/new.md"), BLOCKED_MESSAGE, ""),
        (_payload(file_path="vault/daily/outside-route/new.md"), "", VALIDATION_ERROR),
        (_payload(file_path="vault/daily/../../outside.md"), "", VALIDATION_ERROR),
    ],
)
def test_protect_hook_canonicalizes_symlinks_and_traversal(
    hook_project: Path,
    hook_environment: dict[str, str],
    payload: str,
    stdout: str,
    stderr: str,
) -> None:
    outside = hook_project.parent / "outside"
    outside.mkdir()
    (hook_project / "vault/daily/control-route").symlink_to(
        hook_project / "vault/.claude", target_is_directory=True
    )
    (hook_project / "vault/daily/outside-route").symlink_to(
        outside, target_is_directory=True
    )
    result = _run_hook(
        hook_project,
        "protect-control-plane.sh",
        payload,
        environment=hook_environment,
    )

    assert result.returncode == 2
    assert result.stdout == stdout
    assert result.stderr == stderr


@pytest.mark.parametrize(
    "command",
    (
        "echo ordinary command",
        "cat .claude/settings.json",
        "rg settings .claude",
        "sed -n '1,20p' .claude/settings.json",
        "a-second-brain qmd recall test",
        ("uv run skills/agent-memory/scripts/memory-engine.py touch MEMORY.md"),
    ),
)
def test_protect_hook_allows_ordinary_and_read_only_commands(
    hook_project: Path,
    hook_environment: dict[str, str],
    command: str,
) -> None:
    result = _run_hook(
        hook_project,
        "protect-control-plane.sh",
        _payload(command=command),
        environment=hook_environment,
    )

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    "command",
    (
        "printf changed > .claude/settings.json",
        "sed -i 's/a/b/' .claude/settings.json",
        "rm -f vault/.claude/settings.json",
        "python -c \"open('.claude/settings.json', 'w').write('x')\"",
        ("python -c pass skills/agent-memory/scripts/memory-engine.py"),
        "rg --pre rm pattern .claude",
        "sed -n '1w .claude/copy.json' .claude/settings.json",
        "sed -e 'e touch /tmp/control-plane-bypass' .claude/settings.json",
        "cat .claude/settings.json | tee .claude/copy.json",
        "printf changed\n> .claude/settings.json",
    ),
)
def test_protect_hook_blocks_direct_bash_mutations(
    hook_project: Path,
    hook_environment: dict[str, str],
    command: str,
) -> None:
    result = _run_hook(
        hook_project,
        "protect-control-plane.sh",
        _payload(command=command),
        environment=hook_environment,
    )

    assert result.returncode == 2
    assert result.stdout == BLOCKED_MESSAGE
    assert result.stderr == ""


@pytest.mark.parametrize("payload", ("", "{", "{}"))
def test_validate_frontmatter_hook_ignores_empty_or_malformed_payload(
    hook_project: Path,
    hook_environment: dict[str, str],
    payload: str,
) -> None:
    result = _run_hook(
        hook_project,
        "validate-frontmatter.sh",
        payload,
        environment=hook_environment,
    )

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    ("relative", "content", "expected"),
    [
        ("daily/no-close.md", "---\ntype: daily\n", "is missing closing '---'.\n"),
        (
            "daily/tabs.md",
            "---\ntype:\t daily\n---\n",
            "contains tabs; YAML requires spaces.\n",
        ),
        (
            "daily/colon.md",
            "---\ndescription: first: second\n---\n",
            "may contain unquoted colons in values.\n",
        ),
        (
            "daily/required.md",
            "---\ntype: daily\n---\n",
            "is missing required fields for daily: date.\n",
        ),
        (
            "thoughts/карточка с пробелом.md",
            (
                "---\ntype: note\ndescription: Поиск\ntags: [Проект, тест]\n"
                "status: active\n---\n"
            ),
            "has invalid fields for thought-card: tags.\n",
        ),
    ],
)
def test_validate_frontmatter_hook_reports_exact_warnings(
    hook_project: Path,
    hook_environment: dict[str, str],
    relative: str,
    content: str,
    expected: str,
) -> None:
    path = _write(hook_project / "vault" / relative, content)
    result = _run_hook(
        hook_project,
        "validate-frontmatter.sh",
        _payload(file_path=path.relative_to(hook_project).as_posix()),
        environment=hook_environment,
    )

    assert result.returncode == 1
    assert result.stdout == f"WARNING: Frontmatter in {path.name} {expected}"
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("daily/valid.md", "---\ntype: daily\ndate: 2026-07-29\n---\n"),
        (
            "thoughts/valid.md",
            (
                "---\ntype: note\ndescription: Поиск\ntags: [project, test]\n"
                "status: active\n---\n"
            ),
        ),
        (
            "thoughts/карточка с пробелом.md",
            (
                "---\ntype: note\ndescription: Поиск\ntags: [ии-агенты, тест]\n"
                "status: active\n---\n"
            ),
        ),
        (".session/generated.md", "---\ntype: generated\n---\n"),
        ("daily/no-frontmatter.md", "body\n"),
        ("daily/not-markdown.txt", "---\ntype: daily\n---\n"),
    ],
)
def test_validate_frontmatter_hook_accepts_valid_or_non_applicable_files(
    hook_project: Path,
    hook_environment: dict[str, str],
    relative: str,
    content: str,
) -> None:
    path = _write(hook_project / "vault" / relative, content)
    result = _run_hook(
        hook_project,
        "validate-frontmatter.sh",
        _payload(file_path=path.relative_to(hook_project).as_posix()),
        environment=hook_environment,
    )

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_validate_frontmatter_hook_ignores_outside_vault_and_claude(
    hook_project: Path,
    hook_environment: dict[str, str],
) -> None:
    outside = _write(hook_project.parent / "outside.md", "---\ntype: note\n")
    claude = _write(hook_project / "vault/.claude/ignored.md", "---\ntype: note\n")
    symlink = hook_project / "vault/daily/outside-link.md"
    symlink.symlink_to(outside)

    for path in (outside, claude, symlink):
        result = _run_hook(
            hook_project,
            "validate-frontmatter.sh",
            _payload(file_path=str(path)),
            environment=hook_environment,
        )
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""


def test_frontmatter_hook_turns_post_resolve_read_race_into_protocol(
    hook_project: Path,
) -> None:
    path = _write(hook_project / "vault/daily/race.md", "---\ntype: daily\n---\n")

    def missing_reader(_: Path) -> bytes:
        raise FileNotFoundError("removed after resolve")

    result = validate_target(
        path,
        "daily/race.md",
        load_manifest(hook_project),
        read_bytes=missing_reader,
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == RACE_ERROR


def test_validate_frontmatter_hook_rejects_invalid_epistemic_metadata(
    hook_project: Path,
    hook_environment: dict[str, str],
) -> None:
    path = _write(
        hook_project / "vault/thoughts/epistemic.md",
        "---\n"
        "type: epistemic\n"
        "description: Durable fact\n"
        "epistemic_confidence: high\n"
        'epistemic_scope: "project:test"\n'
        "epistemic_state: active\n"
        "created: 2026-07-29\n"
        "updated: 2026-07-29\n"
        "last_accessed: 2026-07-29\n"
        "relevance: 0.8\n"
        "tier: warm\n"
        "---\n",
    )
    result = _run_hook(
        hook_project,
        "validate-frontmatter.sh",
        _payload(file_path=path.relative_to(hook_project).as_posix()),
        environment=hook_environment,
    )

    assert result.returncode == 1
    assert result.stdout == (
        "WARNING: Frontmatter in epistemic.md has invalid fields for epistemic: "
        "epistemic_metadata.\n"
    )
    assert result.stderr == ""


def test_context_hook_is_payload_independent_and_read_only(
    hook_project: Path,
    hook_environment: dict[str, str],
) -> None:
    (hook_project / "vault/.session/handoff.md").unlink()
    before = _snapshot_tree(hook_project)
    outputs = []
    for payload in (_payload(session_id="startup"), "", "{"):
        result = _run_hook(
            hook_project,
            "context-pack.sh",
            payload,
            environment=hook_environment,
        )
        assert result.returncode == 0
        assert result.stderr == ""
        outputs.append(result.stdout)

    assert outputs[0] == outputs[1] == outputs[2]
    context = json.loads(outputs[0])["hookSpecificOutput"]["additionalContext"]
    assert "=== CORE: handoff ===" in context
    assert context.splitlines()[-1].startswith("_context injected: ")
    assert "/ 200000B budget" in context.splitlines()[-1]
    assert _snapshot_tree(hook_project) == before


def test_context_hook_runtime_suppression_and_missing_manifest_contract(
    hook_project: Path,
    hook_environment: dict[str, str],
) -> None:
    runtime = _run_hook(
        hook_project,
        "context-pack.sh",
        _payload(),
        environment={**hook_environment, "D_BRAIN_CONTEXT_PACK_MODE": "runtime"},
    )
    assert runtime.returncode == 0
    assert runtime.stdout == runtime.stderr == ""

    (hook_project / "vault-manifest.json").unlink()
    missing = _run_hook(
        hook_project,
        "context-pack.sh",
        _payload(),
        environment=hook_environment,
    )
    assert missing.returncode == 1
    assert missing.stdout == ""
    assert missing.stderr == "context-pack: vault-manifest.json is missing\n"


def test_actual_settings_commands_run_from_vault_cwd(
    hook_project: Path,
    hook_environment: dict[str, str],
) -> None:
    settings = json.loads(
        (TEMPLATE_ROOT / ".claude/settings.json").read_text(encoding="utf-8")
    )
    _write(
        hook_project / "vault/daily/from-settings.md",
        "---\ntype: daily\ndate: 2026-07-29\n---\n",
    )
    expected_payloads = {
        "SessionStart": _payload(session_id="startup"),
        "PreToolUse": _payload(file_path="daily/from-settings.md"),
        "PostToolUse": _payload(file_path="daily/from-settings.md"),
    }
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Edit|Write|Bash"
    assert settings["hooks"]["PostToolUse"][0]["matcher"] == "Edit|Write"

    for event, payload in expected_payloads.items():
        command = settings["hooks"][event][0]["hooks"][0]["command"]
        result = subprocess.run(  # noqa: S603
            shlex.split(command),
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            cwd=hook_project / "vault",
            env=hook_environment,
        )
        assert result.returncode == 0, event
        assert result.stderr == "", event
        if event == "SessionStart":
            assert json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"] == (
                "SessionStart"
            )
        else:
            assert result.stdout == "", event

    protect_command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    blocked_bash = subprocess.run(  # noqa: S603
        shlex.split(protect_command),
        input=_payload(command="printf changed > .claude/settings.json"),
        text=True,
        capture_output=True,
        check=False,
        cwd=hook_project / "vault",
        env=hook_environment,
    )
    assert blocked_bash.returncode == 2
    assert blocked_bash.stdout == BLOCKED_MESSAGE
    assert blocked_bash.stderr == ""


def test_setup_checks_assets_prerequisites_manifest_and_executability(
    hook_project: Path,
    hook_environment: dict[str, str],
) -> None:
    setup = hook_project / "scripts/setup_control_plane.sh"
    result = subprocess.run(  # noqa: S603
        ["bash", str(setup)],
        cwd=hook_project.parent,
        text=True,
        capture_output=True,
        check=False,
        env=hook_environment,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith("Control-plane assets are present.\n")
    for name in (
        "protect-control-plane.sh",
        "validate-frontmatter.sh",
        "context-pack.sh",
    ):
        assert (
            hook_project / "vault/.claude/hooks" / name
        ).stat().st_mode & stat.S_IXUSR


def test_setup_reports_missing_context_hook_before_prerequisites(
    hook_project: Path,
    hook_environment: dict[str, str],
) -> None:
    (hook_project / "vault/.claude/hooks/context-pack.sh").unlink()
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(hook_project / "scripts/setup_control_plane.sh")],
        cwd=hook_project.parent,
        text=True,
        capture_output=True,
        check=False,
        env=hook_environment,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "Missing control-plane asset: vault/.claude/hooks/context-pack.sh\n"
    )


@pytest.mark.parametrize("missing", ("jq", "uv"))
def test_setup_reports_missing_prerequisite(
    hook_project: Path,
    hook_environment: dict[str, str],
    missing: str,
) -> None:
    bin_dir = Path(hook_environment["PATH"].split(":", maxsplit=1)[0])
    (bin_dir / missing).unlink()
    environment = {**hook_environment, "PATH": str(bin_dir)}
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(hook_project / "scripts/setup_control_plane.sh")],
        cwd=hook_project.parent,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"Missing required command: {missing}\n"
