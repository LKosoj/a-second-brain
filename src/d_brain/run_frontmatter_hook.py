"""Validate one vault Markdown write for the PostToolUse hook."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from d_brain.manifest import ManifestValidationError, VaultManifest, load_manifest
from d_brain.services.frontmatter import (
    FrontmatterError,
    parse_frontmatter_bytes,
    resolve_vault_markdown_path,
    validate_document,
)

RACE_ERROR = "frontmatter-hook: file became unavailable during validation\n"


@dataclass(frozen=True)
class HookResult:
    """Complete PostToolUse response, including its stable output streams."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    return parser.parse_args()


def _vault_relative_markdown_path(
    value: str, *, project_root: Path, vault_path: Path
) -> tuple[Path, str] | None:
    supplied = Path(value)
    candidates = (supplied,) if supplied.is_absolute() else (
        project_root / supplied,
        vault_path / supplied,
    )
    for candidate in candidates:
        try:
            relative = candidate.relative_to(vault_path)
            resolved = resolve_vault_markdown_path(vault_path, relative)
        except (FrontmatterError, ValueError):
            continue
        if (
            resolved.suffix.lower() != ".md"
            or relative.parts[:1] == (".claude",)
        ):
            return None
        return resolved, relative.as_posix()
    return None


def _warning(path: Path, error: FrontmatterError) -> str:
    name = path.name
    message = str(error)
    if message == "frontmatter is missing a closing '---'":
        return f"WARNING: Frontmatter in {name} is missing closing '---'."
    if "found character '\\t'" in message:
        return f"WARNING: Frontmatter in {name} contains tabs; YAML requires spaces."
    if "mapping values are not allowed" in message:
        return (
            f"WARNING: Frontmatter in {name} may contain unquoted colons in values."
        )
    detail = message.splitlines()[0]
    return f"WARNING: Frontmatter in {name} is invalid: {detail}."


def validate_target(
    path: Path,
    relative_path: str,
    manifest: VaultManifest,
    *,
    read_bytes: Callable[[Path], bytes] = Path.read_bytes,
) -> HookResult:
    """Validate a resolved path while turning a post-resolve race into protocol."""
    try:
        document = parse_frontmatter_bytes(read_bytes(path))
        if not document.has_frontmatter:
            return HookResult(0)
        route, missing, invalid = validate_document(relative_path, document, manifest)
    except OSError:
        return HookResult(1, stderr=RACE_ERROR)
    except FrontmatterError as exc:
        return HookResult(1, stdout=f"{_warning(path, exc)}\n")
    if missing:
        return HookResult(
            1,
            stdout=(
                f"WARNING: Frontmatter in {path.name} is missing required fields "
                f"for {route.name}: {', '.join(missing)}.\n"
            ),
        )
    if invalid:
        return HookResult(
            1,
            stdout=(
                f"WARNING: Frontmatter in {path.name} has invalid fields for "
                f"{route.name}: {', '.join(invalid)}.\n"
            ),
        )
    return HookResult(0)


def main() -> int:
    """Return hook-compatible warnings without modifying the vault."""
    args = _arguments()
    project_root = Path.cwd().resolve()
    try:
        manifest = load_manifest(project_root)
    except ManifestValidationError as exc:
        print(f"frontmatter-hook: {exc}", file=sys.stderr)
        return 1

    vault_path = (project_root / manifest.memory_root).resolve()
    target = _vault_relative_markdown_path(
        args.file, project_root=project_root, vault_path=vault_path
    )
    if target is None:
        return 0
    path, relative_path = target
    result = validate_target(path, relative_path, manifest)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
