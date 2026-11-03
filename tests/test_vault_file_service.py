"""Tests for the network vault-file service."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from d_brain.services.vault_file_service import (
    VaultFileConflict,
    VaultFileEditForbidden,
    VaultFileError,
    VaultFileForbidden,
    VaultFileService,
)


def _prepare_compiled_vault(vault: Path, write_vault_manifest) -> Path:  # type: ignore[no-untyped-def]
    vault.mkdir()
    (vault / "compiled" / "topics").mkdir(parents=True)
    write_vault_manifest(
        vault,
        overrides={
            "user_content_roots": [f"{vault.name}/compiled"],
        },
    )
    page = vault / "compiled" / "topics" / "note.md"
    page.write_text(
        "---\ntype: topic\n---\n# Note\n\n## Owner Notes\n"
        "<!-- human:start -->\nold\n<!-- human:end -->\n\n## Sources\nrow\n",
        encoding="utf-8",
    )
    return page


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
    repeated = service.resolve_conflict(record["conflict_id"], "replace")
    assert repeated is not None
    assert repeated.etag == result.etag
    with pytest.raises(VaultFileError):
        service.resolve_conflict(record["conflict_id"], "cancel")


def test_repeated_write_with_same_content_is_idempotent(
    write_vault_manifest, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    vault.mkdir()
    _prepare_vault(vault, write_vault_manifest)
    service = VaultFileService(vault)
    current = service.read_file("projects/note.md")

    service.update_file("projects/note.md", b"updated", current.etag)
    repeated = service.update_file("projects/note.md", b"updated", current.etag)

    assert repeated.etag == service.read_file("projects/note.md").etag
    assert not list(service.conflicts_path.glob("*.json"))


def test_repeated_open_conflict_reuses_record(
    write_vault_manifest, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    vault.mkdir()
    _prepare_vault(vault, write_vault_manifest)
    service = VaultFileService(vault)
    initial = service.read_file("projects/note.md")
    service.update_file("projects/note.md", b"server", initial.etag)

    with pytest.raises(VaultFileConflict) as first:
        service.update_file("projects/note.md", b"client", initial.etag)
    with pytest.raises(VaultFileConflict) as second:
        service.update_file("projects/note.md", b"client", initial.etag)

    assert first.value.record["conflict_id"] == second.value.record["conflict_id"]


def test_tree_rejects_changed_listing_between_pages(
    write_vault_manifest, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    vault.mkdir()
    _prepare_vault(vault, write_vault_manifest)
    service = VaultFileService(vault)
    (vault / "projects" / "initial.md").write_bytes(b"initial")
    first = service.list_tree(path="projects", page_size=1)
    (vault / "projects" / "another.md").write_bytes(b"another")

    with pytest.raises(VaultFileError, match="listing changed"):
        service.list_tree(
            path="projects", page_size=1, page_token=first.next_page_token
        )


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


def test_compiled_file_allows_only_owner_notes(
    write_vault_manifest, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    page = _prepare_compiled_vault(tmp_path / "vault", write_vault_manifest)
    service = VaultFileService(page.parents[2])
    current = service.read_file("compiled/topics/note.md")
    proposed = current.content.replace(b"old", b"new")

    result = service.update_file("compiled/topics/note.md", proposed, current.etag)

    assert result.etag != current.etag
    assert b"<!-- human:start -->\nnew\n<!-- human:end -->" in page.read_bytes()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda text: text.replace("# Note", "# Changed"), "outside_owner_notes"),
        (
            lambda text: text.replace("<!-- human:end -->", ""),
            "owner_notes_markers_damaged",
        ),
        (
            lambda text: text.replace(
                "<!-- human:start -->", "<!-- human:start -->\n<!-- human:start -->"
            ),
            "owner_notes_markers_damaged",
        ),
    ],
)
def test_compiled_file_rejects_outside_or_damaged_edits(
    write_vault_manifest, tmp_path: Path, change, reason: str
) -> None:  # type: ignore[no-untyped-def]
    page = _prepare_compiled_vault(tmp_path / "vault", write_vault_manifest)
    service = VaultFileService(page.parents[2])
    current = service.read_file("compiled/topics/note.md")
    proposed = change(current.content.decode("utf-8")).encode("utf-8")

    with pytest.raises(VaultFileEditForbidden) as caught:
        service.update_file("compiled/topics/note.md", proposed, current.etag)

    assert caught.value.reason == reason
    assert page.read_bytes() == current.content


def test_compiled_file_without_owner_zone_reports_no_owner_notes(
    write_vault_manifest, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    page = _prepare_compiled_vault(tmp_path / "vault", write_vault_manifest)
    page.write_text("---\ntype: topic\n---\n# Note\nbody\n", encoding="utf-8")
    service = VaultFileService(page.parents[2])
    current = service.read_file("compiled/topics/note.md")

    with pytest.raises(VaultFileEditForbidden) as caught:
        service.update_file(
            "compiled/topics/note.md", current.content + b"x", current.etag
        )

    assert caught.value.reason == "no_owner_notes"


def test_compiled_file_rejects_non_utf8_body(
    write_vault_manifest, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    page = _prepare_compiled_vault(tmp_path / "vault", write_vault_manifest)
    service = VaultFileService(page.parents[2])
    current = service.read_file("compiled/topics/note.md")

    with pytest.raises(VaultFileEditForbidden) as caught:
        service.update_file("compiled/topics/note.md", b"\xff", current.etag)

    assert caught.value.reason == "not_utf8"


def test_compiled_stale_malformed_body_does_not_create_conflict(
    write_vault_manifest, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    page = _prepare_compiled_vault(tmp_path / "vault", write_vault_manifest)
    service = VaultFileService(page.parents[2])
    current = service.read_file("compiled/topics/note.md")
    malformed = current.content.replace(b"<!-- human:end -->", b"")

    with pytest.raises(VaultFileEditForbidden) as caught:
        service.update_file("compiled/topics/note.md", malformed, "0" * 64)

    assert caught.value.reason == "owner_notes_markers_damaged"
    assert not service.conflicts_path.exists()


def test_compiled_replace_rechecks_outside_zone(
    write_vault_manifest, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    page = _prepare_compiled_vault(tmp_path / "vault", write_vault_manifest)
    service = VaultFileService(page.parents[2])
    current = service.read_file("compiled/topics/note.md")
    proposed = current.content.replace(b"old", b"new")
    page.write_bytes(current.content.replace(b"row", b"rebuilt"))

    with pytest.raises(VaultFileConflict) as caught:
        service.update_file("compiled/topics/note.md", proposed, current.etag)
    conflict_id = caught.value.record["conflict_id"]
    page.write_bytes(page.read_bytes().replace(b"rebuilt", b"rebuilt-again"))

    with pytest.raises(VaultFileEditForbidden) as rejected:
        service.resolve_conflict(conflict_id, "replace")

    assert rejected.value.reason == "outside_owner_notes"
    assert json.loads(
        (service.conflicts_path / f"{conflict_id}.json").read_text(encoding="utf-8")
    )["status"] == "open"
