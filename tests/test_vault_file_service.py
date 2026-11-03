"""Tests for the network vault-file service."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from d_brain.services.vault_file_service import (
    VaultFileConflict,
    VaultFileError,
    VaultFileForbidden,
    VaultFileService,
)


def _prepare_vault(vault: Path, write_vault_manifest) -> None:  # type: ignore[no-untyped-def]
    for directory in ("daily", "projects", ".session"):
        (vault / directory).mkdir(parents=True, exist_ok=True)
    write_vault_manifest(vault)
    (vault / "MEMORY.md").write_bytes(b"memory")
    (vault / "projects" / "note.md").write_bytes("Привет".encode())
    (vault / ".session" / "secret.json").write_text("secret", encoding="utf-8")


def test_tree_exposes_only_user_content(write_vault_manifest, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    vault.mkdir()
    _prepare_vault(vault, write_vault_manifest)

    page = VaultFileService(vault).list_tree(page_size=10)

    paths = {entry.path for entry in page.entries}
    assert "MEMORY.md" in paths
    assert "projects" in paths
    assert ".session" not in paths


def test_read_and_conditional_write_preserve_bytes(
    write_vault_manifest,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    vault.mkdir()
    _prepare_vault(vault, write_vault_manifest)
    service = VaultFileService(vault)

    current = service.read_file("projects/note.md")
    assert current.content == "Привет".encode()
    assert current.etag == hashlib.sha256(current.content).hexdigest()

    updated = service.update_file("projects/note.md", b"updated", current.etag)
    assert service.read_file("projects/note.md").content == b"updated"
    assert updated.etag == hashlib.sha256(b"updated").hexdigest()


def test_stale_write_requires_one_shot_resolution(
    write_vault_manifest,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    vault.mkdir()
    _prepare_vault(vault, write_vault_manifest)
    service = VaultFileService(vault)
    initial = service.read_file("projects/note.md")
    service.update_file("projects/note.md", b"server", initial.etag)

    with pytest.raises(VaultFileConflict) as caught:
        service.update_file("projects/note.md", b"client", initial.etag)
    record = caught.value.record
    assert service.read_file("projects/note.md").content == b"server"

    result = service.resolve_conflict(record["conflict_id"], "replace")
    assert result is not None
    assert service.read_file("projects/note.md").content == b"client"
    with pytest.raises(VaultFileError):
        service.resolve_conflict(record["conflict_id"], "replace")


def test_invalid_and_infrastructure_paths_are_rejected(
    write_vault_manifest,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    vault.mkdir()
    _prepare_vault(vault, write_vault_manifest)
    service = VaultFileService(vault)

    with pytest.raises(VaultFileForbidden):
        service.read_file("../vault/MEMORY.md")
    with pytest.raises(VaultFileForbidden):
        service.read_file(".session/secret.json")
