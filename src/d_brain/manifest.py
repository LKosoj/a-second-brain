"""Validated project-local configuration for the vault."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

MANIFEST_FILENAME = "vault-manifest.json"
MANIFEST_VERSION = 1
_MANIFEST_FIELDS = frozenset(
    {
        "version",
        "qmd_index",
        "context_budget_bytes",
        "memory_root",
        "user_content_roots",
        "infrastructure",
        "frontmatter_required",
    }
)
KNOWN_FRONTMATTER_PROFILES = frozenset(
    {
        "default",
        "daily",
        "import",
        "derived",
        "thought-card",
        "reflection",
        "goal",
        "index",
        "flat-context",
        "template",
        "technical",
        "epistemic",
        "home",
        # Kept only for older external manifests; production uses thought-card.
        "card",
    }
)
_QMD_INDEX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_FRONTMATTER_FIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


class ManifestValidationError(ValueError):
    """Raised when ``vault-manifest.json`` is absent or invalid."""


@dataclass(frozen=True)
class VaultManifest:
    """The validated configuration shared by vault-facing services."""

    version: int
    qmd_index: str
    context_budget_bytes: int
    memory_root: str
    user_content_roots: tuple[str, ...]
    infrastructure: tuple[str, ...]
    frontmatter_required: Mapping[str, tuple[str, ...]]


def _error(message: str) -> ManifestValidationError:
    return ManifestValidationError(f"{MANIFEST_FILENAME}: {message}")


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(f"{field} must be a non-empty relative path")
    if "\\" in value:
        raise _error(f"{field} must use '/' path separators")

    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise _error(f"{field} must stay inside the project root")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise _error(f"{field} must not be the project root")
    return normalized


def _path_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _error(f"{field} must be a non-empty list")

    paths = tuple(
        _relative_path(item, f"{field}[{index}]") for index, item in enumerate(value)
    )
    if len(set(paths)) != len(paths):
        raise _error(f"{field} must not contain duplicate paths")
    return paths


def _required_fields(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise _error("frontmatter_required must be an object")
    profiles = set(value)
    unknown = profiles - KNOWN_FRONTMATTER_PROFILES
    if unknown:
        raise _error(
            "frontmatter_required contains unknown profiles: "
            + ", ".join(sorted(unknown))
        )
    if "default" not in profiles:
        raise _error("frontmatter_required must define the default profile")

    result: dict[str, tuple[str, ...]] = {}
    for profile, fields in value.items():
        if not isinstance(fields, list) or not fields:
            raise _error(f"frontmatter_required.{profile} must be a non-empty list")
        validated: list[str] = []
        for index, field in enumerate(fields):
            if not isinstance(field, str) or not _FRONTMATTER_FIELD_RE.fullmatch(field):
                raise _error(
                    f"frontmatter_required.{profile}[{index}] must be a safe field name"
                )
            validated.append(field)
        if len(set(validated)) != len(validated):
            raise _error(f"frontmatter_required.{profile} must not contain duplicates")
        result[profile] = tuple(validated)
    return result


def _is_within_memory_root(path: str, memory_root: str) -> bool:
    try:
        PurePosixPath(path).relative_to(PurePosixPath(memory_root))
    except ValueError:
        return False
    return True


def _paths_overlap(left: str, right: str) -> bool:
    return _is_within_memory_root(left, right) or _is_within_memory_root(right, left)


def _parse_manifest(payload: Any) -> VaultManifest:
    if not isinstance(payload, dict):
        raise _error("top level must be an object")
    unknown_fields = set(payload) - _MANIFEST_FIELDS
    if unknown_fields:
        raise _error(
            "contains unknown top-level keys: " + ", ".join(sorted(unknown_fields))
        )

    version = payload.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != MANIFEST_VERSION
    ):
        raise _error(f"version must be {MANIFEST_VERSION}")

    qmd_index = payload.get("qmd_index")
    if not isinstance(qmd_index, str) or not _QMD_INDEX_RE.fullmatch(qmd_index):
        raise _error("qmd_index must match [A-Za-z0-9][A-Za-z0-9._-]*")

    budget = payload.get("context_budget_bytes")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise _error("context_budget_bytes must be a positive integer")

    memory_root = _relative_path(payload.get("memory_root"), "memory_root")
    user_content_roots = _path_list(
        payload.get("user_content_roots"), "user_content_roots"
    )
    infrastructure = _path_list(payload.get("infrastructure"), "infrastructure")
    all_roots = user_content_roots + infrastructure
    if len(set(all_roots)) != len(all_roots):
        raise _error("user_content_roots and infrastructure must not overlap")
    for index, root in enumerate(all_roots):
        for other_root in all_roots[index + 1 :]:
            if _paths_overlap(root, other_root):
                raise _error(
                    f"configured roots must not overlap: {root} / {other_root}"
                )
    for root in all_roots:
        if not _is_within_memory_root(root, memory_root):
            raise _error(f"root outside memory_root: {root}")

    return VaultManifest(
        version=version,
        qmd_index=qmd_index,
        context_budget_bytes=budget,
        memory_root=memory_root,
        user_content_roots=user_content_roots,
        infrastructure=infrastructure,
        frontmatter_required=MappingProxyType(
            _required_fields(payload.get("frontmatter_required"))
        ),
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _error(f"contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_resolved_roots(project_root: Path, manifest: VaultManifest) -> None:
    resolved_memory_root = (project_root / manifest.memory_root).resolve()
    try:
        resolved_memory_root.relative_to(project_root)
    except ValueError as exc:
        raise _error("memory_root resolves outside the project root") from exc
    for root in manifest.user_content_roots + manifest.infrastructure:
        resolved_root = (project_root / root).resolve()
        try:
            resolved_root.relative_to(resolved_memory_root)
        except ValueError as exc:
            raise _error(
                f"configured root resolves outside memory_root: {root}"
            ) from exc


def load_manifest(project_root: Path) -> VaultManifest:
    """Load the required manifest from an explicit project root."""
    resolved_project_root = Path(project_root).resolve()
    manifest_path = resolved_project_root / MANIFEST_FILENAME
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _error("is missing") from exc
    except OSError as exc:
        raise _error(f"cannot be read: {exc}") from exc

    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise _error(f"invalid JSON: {exc.msg}") from exc
    manifest = _parse_manifest(payload)
    _validate_resolved_roots(resolved_project_root, manifest)
    return manifest


def load_manifest_for_vault(vault_path: Path) -> VaultManifest:
    """Load the project manifest and require it to name exactly this vault."""
    resolved_vault = Path(vault_path).resolve()
    manifest = load_manifest(resolved_vault.parent)
    declared_vault = (resolved_vault.parent / manifest.memory_root).resolve()
    if declared_vault != resolved_vault:
        raise _error(
            "memory_root does not match the requested vault: "
            f"{manifest.memory_root!r} != {resolved_vault}"
        )
    return manifest
