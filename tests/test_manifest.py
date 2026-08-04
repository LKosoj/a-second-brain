import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from d_brain.manifest import (
    ManifestValidationError,
    VaultManifest,
    load_manifest,
    load_manifest_for_vault,
)

ManifestWriter = Callable[..., Path]


def test_load_manifest_reads_production_contract() -> None:
    manifest = load_manifest(Path(__file__).resolve().parents[1])

    assert isinstance(manifest, VaultManifest)
    assert manifest.qmd_index == "dbrain"
    assert manifest.context_budget_bytes == 200000
    assert manifest.memory_root == "vault"
    assert manifest.frontmatter_required["default"] == (
        "type",
        "last_accessed",
        "relevance",
        "tier",
    )
    with pytest.raises(TypeError):
        manifest.frontmatter_required["default"] = ("changed",)  # type: ignore[index]
    assert {
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
    } <= set(manifest.frontmatter_required)


def test_load_manifest_requires_file_without_legacy_fallback(tmp_path: Path) -> None:
    with pytest.raises(
        ManifestValidationError, match="vault-manifest.json: is missing"
    ):
        load_manifest(tmp_path)


def test_load_manifest_rejects_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "vault-manifest.json").write_text("{", encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="invalid JSON"):
        load_manifest(tmp_path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"version": True}, "version must be 1"),
        ({"context_budget_bytes": True}, "positive integer"),
        ({"context_budget_bytes": 0}, "positive integer"),
        ({"qmd_index": "../../unsafe"}, "qmd_index"),
        ({"memory_root": "/vault"}, "memory_root must stay inside"),
        ({"user_content_roots": ["vault/daily", "vault/./daily"]}, "duplicate"),
        ({"infrastructure": ["vault/daily/cache"]}, "must not overlap"),
        (
            {"user_content_roots": ["vault/daily", "vault/daily/archive"]},
            "must not overlap",
        ),
        (
            {"infrastructure": ["vault/.qmd", "vault/.qmd/cache"]},
            "must not overlap",
        ),
        ({"infrastructure": ["outside"]}, "root outside memory_root"),
        ({"extra": "field"}, "unknown top-level keys"),
        ({"frontmatter_required": {"unknown": ["type"]}}, "unknown profiles"),
        ({"frontmatter_required": {"daily": ["type"]}}, "default profile"),
    ],
)
def test_load_manifest_rejects_invalid_contracts(
    tmp_path: Path,
    write_vault_manifest: ManifestWriter,
    overrides: dict[str, Any],
    message: str,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(vault_path, overrides=overrides)

    with pytest.raises(ManifestValidationError, match=message):
        load_manifest(tmp_path)


def test_load_manifest_for_vault_requires_exact_memory_root(
    tmp_path: Path, write_vault_manifest: ManifestWriter
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(
        vault_path,
        overrides={
            "memory_root": "other-vault",
            "user_content_roots": ["other-vault/daily"],
            "infrastructure": ["other-vault/.qmd"],
        },
    )

    with pytest.raises(
        ManifestValidationError, match="does not match the requested vault"
    ):
        load_manifest_for_vault(vault_path)


def test_manifest_normalizes_paths_and_profiles(
    tmp_path: Path, write_vault_manifest: ManifestWriter
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(
        vault_path,
        overrides={
            "user_content_roots": ["vault/daily/"],
            "infrastructure": ["vault/.qmd/"],
            "frontmatter_required": {
                "default": ["type"],
                "epistemic": ["type", "epistemic_confidence"],
            },
        },
    )

    manifest = load_manifest_for_vault(vault_path)

    assert manifest.user_content_roots == ("vault/daily",)
    assert manifest.infrastructure == ("vault/.qmd",)
    assert manifest.frontmatter_required["epistemic"] == (
        "type",
        "epistemic_confidence",
    )


def test_load_manifest_rejects_non_object_payload(tmp_path: Path) -> None:
    (tmp_path / "vault-manifest.json").write_text(
        json.dumps(["not", "an", "object"]), encoding="utf-8"
    )

    with pytest.raises(ManifestValidationError, match="top level must be an object"):
        load_manifest(tmp_path)


def test_load_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    (tmp_path / "vault-manifest.json").write_text(
        '{"version": 1, "version": 1}', encoding="utf-8"
    )

    with pytest.raises(ManifestValidationError, match="duplicate JSON key: version"):
        load_manifest(tmp_path)


def test_load_manifest_allows_missing_future_root(
    tmp_path: Path, write_vault_manifest: ManifestWriter
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(
        vault_path,
        overrides={"user_content_roots": ["vault/future-root"]},
    )

    assert load_manifest(tmp_path).user_content_roots == ("vault/future-root",)


def test_load_manifest_rejects_configured_symlink_escape(
    tmp_path: Path, write_vault_manifest: ManifestWriter
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    external_root = tmp_path / "external"
    external_root.mkdir()
    (vault_path / "external-link").symlink_to(external_root, target_is_directory=True)
    write_vault_manifest(
        vault_path,
        overrides={"user_content_roots": ["vault/external-link"]},
    )

    with pytest.raises(ManifestValidationError, match="resolves outside memory_root"):
        load_manifest(tmp_path)


def test_load_manifest_rejects_memory_root_symlink_escape(
    tmp_path: Path, write_vault_manifest: ManifestWriter
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    external_root = tmp_path / "external-vault"
    external_root.mkdir()
    vault_path = project_root / "vault"
    vault_path.symlink_to(external_root, target_is_directory=True)
    write_vault_manifest(vault_path)

    with pytest.raises(
        ManifestValidationError, match="memory_root resolves outside the project root"
    ):
        load_manifest(project_root)
