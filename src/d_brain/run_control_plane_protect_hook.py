"""Fail-closed target validation for the control-plane PreToolUse hook."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path, PurePosixPath

from d_brain.manifest import ManifestValidationError, load_manifest

BLOCKED_MESSAGE = (
    "BLOCKED: control-plane asset is repo-managed. Edit it in the repo workflow, "
    "not from runtime.\n"
)
VALIDATION_ERROR = "BLOCKED: unable to validate control-plane target.\n"
_CONTROL_PLANE_REFERENCE = re.compile(
    r"(?:^|[\s'\"=:/])(?:\.claude|skills)(?:/|$)",
)
_READ_ONLY_COMMANDS = frozenset(
    {
        "cat",
        "grep",
        "head",
        "ls",
        "readlink",
        "realpath",
        "rg",
        "stat",
        "tail",
        "wc",
    }
)
_SHELL_CONTROL_MARKERS = ("&&", "||", "|", ";", ">", "<", "`", "$(")
_TRUSTED_CONTROL_PLANE_TOOLS = (
    "skills/agent-memory/scripts/memory-engine.py",
    ".claude/skills/agent-memory/scripts/memory-engine.py",
)
_READ_ONLY_SED_SCRIPT = re.compile(r"\d+(?:,\d+)?p\Z")


class TargetValidationError(ValueError):
    """Raised when a hook target cannot be proven safe."""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("file_path", "command"), required=True)
    parser.add_argument("--target", required=True)
    return parser.parse_args()


def _is_control_plane_path(relative_path: Path) -> bool:
    return relative_path.parts[:1] in {(".claude",), ("skills",)}


def _file_target_is_control_plane(
    value: str, *, project_root: Path, vault_path: Path, memory_root: str
) -> bool:
    if not value or "\x00" in value:
        raise TargetValidationError("target is empty or contains NUL")
    supplied = PurePosixPath(value)
    if not supplied.is_absolute() and ".." in supplied.parts:
        raise TargetValidationError("relative target escapes its root")
    candidate = (
        Path(value)
        if supplied.is_absolute()
        else project_root / value
        if supplied.parts[:1] in {(memory_root,), ("skills",)}
        else vault_path / value
    )
    try:
        resolved = candidate.resolve(strict=False)
        relative = resolved.relative_to(vault_path)
        return _is_control_plane_path(relative)
    except ValueError:
        try:
            resolved.relative_to(project_root / "skills")
        except (OSError, ValueError) as exc:
            raise TargetValidationError("target is outside the control plane") from exc
        return True
    except OSError as exc:
        raise TargetValidationError("target cannot be resolved") from exc


def _mentions_control_plane(value: str) -> bool:
    return bool(_CONTROL_PLANE_REFERENCE.search(value.replace("\\", "/")))


def _is_trusted_tool_path(token: str) -> bool:
    normalized = token.replace("\\", "/").removeprefix("./")
    return normalized in _TRUSTED_CONTROL_PLANE_TOOLS or normalized in {
        f"vault/{path}" for path in _TRUSTED_CONTROL_PLANE_TOOLS
    }


def _is_trusted_control_plane_tool(command: str, tokens: list[str]) -> bool:
    if any(marker in command for marker in _SHELL_CONTROL_MARKERS):
        return False
    if _is_trusted_tool_path(tokens[0]):
        return True
    executable = Path(tokens[0]).name
    if executable == "uv":
        return (
            len(tokens) >= 3 and tokens[1] == "run" and _is_trusted_tool_path(tokens[2])
        )
    if executable in {"python", "python3"}:
        return len(tokens) >= 2 and _is_trusted_tool_path(tokens[1])
    return False


def _is_read_only_sed(tokens: list[str]) -> bool:
    return (
        len(tokens) >= 4
        and tokens[1] == "-n"
        and bool(_READ_ONLY_SED_SCRIPT.fullmatch(tokens[2]))
        and all(not token.startswith("-") for token in tokens[3:])
    )


def _is_read_only_control_plane_command(command: str, tokens: list[str]) -> bool:
    if any(marker in command for marker in _SHELL_CONTROL_MARKERS):
        return False
    executable = Path(tokens[0]).name
    if executable == "sed":
        return _is_read_only_sed(tokens)
    if executable not in _READ_ONLY_COMMANDS:
        return False
    if executable == "rg" and any(
        token == "--pre" or token.startswith("--pre=") for token in tokens[1:]
    ):
        return False
    return True


def _command_targets_control_plane_write(value: str) -> bool:
    if not value or "\x00" in value:
        raise TargetValidationError("command is empty or contains NUL")
    if not _mentions_control_plane(value):
        return False
    try:
        tokens = shlex.split(value)
    except ValueError as exc:
        raise TargetValidationError("command cannot be parsed safely") from exc
    if not tokens:
        raise TargetValidationError("command is empty")
    return not (
        _is_read_only_control_plane_command(value, tokens)
        or _is_trusted_control_plane_tool(value, tokens)
    )


def evaluate_target(
    *, kind: str, target: str, project_root: Path
) -> tuple[int, str, str]:
    """Return the complete process contract without raising on hostile input."""
    try:
        manifest = load_manifest(project_root)
        vault_path = (project_root / manifest.memory_root).resolve()
        blocked = (
            _file_target_is_control_plane(
                target,
                project_root=project_root,
                vault_path=vault_path,
                memory_root=manifest.memory_root,
            )
            if kind == "file_path"
            else _command_targets_control_plane_write(target)
        )
    except (ManifestValidationError, OSError, TargetValidationError, ValueError):
        return 2, "", VALIDATION_ERROR
    if blocked:
        return 2, BLOCKED_MESSAGE, ""
    return 0, "", ""


def main() -> int:
    """Write the stable hook response for a validated tool target."""
    args = _arguments()
    code, stdout, stderr = evaluate_target(
        kind=args.kind,
        target=args.target,
        project_root=Path.cwd().resolve(),
    )
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
