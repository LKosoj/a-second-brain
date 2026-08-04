from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

import pytest

from d_brain.manifest import VaultManifest
from d_brain.services import frontmatter as frontmatter_module
from d_brain.services.frontmatter import (
    DuplicateKeyError,
    FrontmatterError,
    UnsafeVaultPathError,
    atomic_write_bytes,
    move_validated_vault_markdown,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    read_vault_file_bytes,
    resolve_vault_markdown_path,
    route_profile,
    split_frontmatter_bytes,
    validate_document,
    validate_semantic_payload,
    write_validated_vault_markdown,
    write_vault_file_bytes,
)
from d_brain.services.vault_lock import (
    VaultLockError,
    VaultWriteLock,
    vault_write_lock,
)


def _manifest() -> VaultManifest:
    return VaultManifest(
        version=1,
        qmd_index="dbrain",
        context_budget_bytes=200000,
        memory_root="vault",
        user_content_roots=("vault/daily",),
        infrastructure=("vault/.qmd",),
        frontmatter_required=MappingProxyType(
            {
                "default": ("type",),
                "daily": ("type", "date"),
                "import": ("type",),
                "derived": ("type", "description"),
                "thought-card": ("type", "description", "tags", "status"),
                "reflection": ("type", "description", "tags"),
                "goal": ("type", "description"),
                "index": ("type", "description"),
                "flat-context": ("type", "description"),
                "template": ("type", "description"),
                "technical": ("type",),
                "epistemic": (
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
                ),
                "home": ("type", "description"),
            }
        ),
    )


def test_strict_parser_rejects_duplicate_keys_at_any_depth() -> None:
    with pytest.raises(DuplicateKeyError, match="duplicate YAML key"):
        parse_frontmatter_bytes(b"---\ntype: note\ntype: duplicate\n---\nbody\n")
    with pytest.raises(DuplicateKeyError, match="duplicate YAML key"):
        parse_frontmatter_bytes(
            b"---\ntype: note\nmetadata:\n  source: one\n  source: two\n---\nbody\n"
        )
    with pytest.raises(FrontmatterError, match="hashable"):
        parse_frontmatter_bytes(b"---\n? [unhashable]\n: value\n---\nbody\n")


def test_lossless_patch_preserves_comments_and_unrelated_multiline_fields() -> None:
    content = (
        b"---\n"
        b"# retained comment\n"
        b"type: note\n"
        b"description: >-\n"
        b"  Existing multiline description\n"
        b"tags:\n"
        b"  - alpha\n"
        b"  - beta\n"
        b"custom: value # retained inline comment\n"
        b"---\n"
        b"# Body\n\nExact body bytes.\n"
    )

    patched = patch_frontmatter_bytes(content, {"status": "active"})

    before = parse_frontmatter_bytes(content)
    after = parse_frontmatter_bytes(patched)
    assert after.body == before.body
    assert b"# retained comment\n" in patched
    assert b"description: >-\n  Existing multiline description\n" in patched
    assert b"custom: value # retained inline comment\n" in patched
    assert after.fields["status"] == "active"


def test_patch_preserves_top_level_comments_after_replaced_value_spans() -> None:
    content = (
        b"---\n"
        b"status: draft\n"
        b"# keep after scalar\n"
        b"description: |-\n"
        b"  old line\n"
        b"  # block scalar content\n"
        b"\n"
        b"# keep after block\n"
        b"tags:\n"
        b"  - old-tag\n"
        b"# keep after sequence\n"
        b"custom: value # keep inline on unrelated key\n"
        b"---\n"
        b"# Body\n"
    )

    patched = patch_frontmatter_bytes(
        content,
        {
            "status": "active",
            "description": "Replacement",
            "tags": ["new-tag", "second-tag"],
        },
    )

    assert b"# keep after scalar\n" in patched
    assert b"# keep after block\n" in patched
    assert b"# keep after sequence\n" in patched
    assert b"  old line\n" not in patched
    assert b"  # block scalar content\n" not in patched
    assert b"  - old-tag\n" not in patched
    assert b"custom: value # keep inline on unrelated key\n" in patched
    assert parse_frontmatter_bytes(patched).body == b"# Body\n"


def test_patch_distinguishes_standalone_and_value_owned_indented_comments() -> None:
    content = (
        b"---\n"
        b"status: draft\n"
        b"  # retain after scalar at arbitrary indentation\n"
        b"description: |-\n"
        b"  old line\n"
        b"  # block scalar content, not a standalone comment\n"
        b"tags:\n"
        b"  - old-tag\n"
        b"  # sequence-owned comment\n"
        b"  - second-tag\n"
        b"metadata:\n"
        b"  child: value\n"
        b"  # nested-map comment on an unrelated value\n"
        b"next: value\n"
        b"---\n"
        b"# Body\n"
    )

    patched = patch_frontmatter_bytes(
        content,
        {
            "status": "active",
            "description": "Replacement",
            "tags": ["new-tag", "other-tag"],
            "metadata": "Replacement",
        },
    )

    assert b"  # retain after scalar at arbitrary indentation\n" in patched
    assert b"  # block scalar content, not a standalone comment\n" not in patched
    assert b"  # sequence-owned comment\n" not in patched
    assert b"  child: value\n" not in patched
    assert b"  # nested-map comment on an unrelated value\n" not in patched
    assert parse_frontmatter_bytes(patched).body == b"# Body\n"


def test_lf_frontmatter_with_crlf_body_keeps_one_header_and_exact_body() -> None:
    body = b"# Body\r\n\r\nExact CRLF bytes.\r\n"
    content = b"---\ntype: note\nstatus: draft\n---\n" + body

    patched = patch_frontmatter_bytes(content, {"status": "active"})
    document = parse_frontmatter_bytes(patched)

    assert patched.startswith(b"---\n")
    assert patched.count(b"---\n") == 2
    assert document.newline == b"\n"
    assert document.body == body


def test_patch_preserves_inline_comment_but_not_hash_inside_quoted_scalar() -> None:
    content = (
        b"---\n"
        b'title: "C# notes"   # exact title comment\n'
        b"single: 'value # inside' # single comment\n"
        b"plain: value#part\n"
        b"---\n"
        b"# Body\n"
    )

    patched = patch_frontmatter_bytes(
        content,
        {
            "title": "Replacement",
            "single": "Changed",
            "plain": "new",
        },
    )

    assert b'title: "Replacement"   # exact title comment\n' in patched
    assert b'single: "Changed" # single comment\n' in patched
    assert b'plain: "new"\n' in patched
    assert b"value#part" not in patched
    assert parse_frontmatter_bytes(patched).body == b"# Body\n"


def test_patch_replaces_only_named_top_level_field_and_adds_frontmatter() -> None:
    content = b"---\ntitle: old\nitems: [one, two]\n---\n# Body\n"
    patched = patch_frontmatter_bytes(content, {"title": "new"})
    assert b"items: [one, two]" in patched
    assert parse_frontmatter_bytes(patched).fields["title"] == "new"

    body_only = b"# Body\n"
    with_header = patch_frontmatter_bytes(body_only, {"type": "note"})
    assert parse_frontmatter_bytes(with_header).body == body_only


def test_patch_scalar_values_do_not_insert_yaml_document_end_marker() -> None:
    patched = patch_frontmatter_bytes(
        b"---\ntype: note\n---\n# Body\n",
        {"relevance": 1.0, "tier": "active"},
    )

    document = parse_frontmatter_bytes(patched)
    assert document.fields["relevance"] == 1.0
    assert document.fields["tier"] == "active"
    assert b"\n...\n" not in patched


def test_parser_rejects_unclosed_frontmatter() -> None:
    with pytest.raises(FrontmatterError, match="closing"):
        parse_frontmatter_bytes(b"---\ntype: note\n# Body\n")


def test_split_preserves_body_hash_material_when_yaml_is_invalid() -> None:
    header, body, _ = split_frontmatter_bytes(
        b"---\ntype: note\ntype: duplicate\n---\n# Body\n"
    )
    assert header is not None
    assert body == b"# Body\n"
    with pytest.raises(DuplicateKeyError):
        parse_frontmatter_bytes(b"---\ntype: note\ntype: duplicate\n---\n# Body\n")


def test_router_and_profile_validation_use_manifest_contract() -> None:
    manifest = _manifest()
    document = parse_frontmatter_bytes(b"---\ntype: daily\n---\n# Day\n")
    route, missing, invalid = validate_document(
        "daily/2026-07-29.md", document, manifest
    )
    assert route.name == "daily"
    assert missing == ("date",)
    assert invalid == ()
    assert route_profile("thoughts/reflections/week.md", {}, manifest).name == (
        "reflection"
    )
    assert route_profile("business/crm.md", {}, manifest).name == "flat-context"


def test_daily_profile_rejects_non_daily_type() -> None:
    manifest = _manifest()
    document = parse_frontmatter_bytes(
        b"---\ntype: note\ndate: 2026-07-29\n---\n# Day\n"
    )

    route, missing, invalid = validate_document(
        "daily/2026-07-29.md", document, manifest
    )

    assert route.name == "daily"
    assert missing == ()
    assert invalid == ("type",)


@pytest.mark.parametrize(
    ("path", "fields", "profile"),
    [
        ("imports/documents/a.md", {}, "import"),
        ("compiled/projects/a.md", {}, "derived"),
        ("thoughts/a.md", {}, "thought-card"),
        ("thoughts/reflections/a.md", {}, "reflection"),
        ("goals/3-weekly.md", {}, "goal"),
        ("MOC/index.md", {}, "index"),
        ("business/crm.md", {}, "flat-context"),
        ("templates/a.md", {}, "template"),
        ("skills/a.md", {}, "technical"),
        ("note.md", {"epistemic_state": "active"}, "epistemic"),
        ("Home.md", {}, "home"),
    ],
)
def test_router_covers_declared_profiles(
    path: str, fields: dict[str, str], profile: str
) -> None:
    assert route_profile(path, fields, _manifest()).name == profile


def test_profile_validation_checks_values_not_only_field_presence() -> None:
    manifest = _manifest()
    document = parse_frontmatter_bytes(
        b"---\ntype: note\ndescription: ok\ntags: [bad tag]\n"
        b"status: invalid\n---\n# Card\n"
    )
    _, missing, invalid = validate_document("thoughts/card.md", document, manifest)
    assert missing == ()
    assert invalid == ("tags", "status")


@pytest.mark.parametrize(
    ("tags", "is_valid"),
    [
        (["ии-агенты", "2026"], True),
        (["ИИ-агенты", "2026"], False),
        (["ии_агенты", "2026"], False),
        (["ии агенты", "2026"], False),
    ],
)
def test_tag_contract_accepts_lowercase_unicode_alnum_segments(
    tags: list[str], is_valid: bool
) -> None:
    payload = {"tags": tags}

    if is_valid:
        assert validate_semantic_payload(payload, ("tags",)) == payload
    else:
        with pytest.raises(FrontmatterError, match="tags"):
            validate_semantic_payload(payload, ("tags",))


@pytest.mark.parametrize("tier", ["core", "active", "warm", "cold", "archive"])
def test_profile_validation_accepts_memory_engine_tiers(tier: str) -> None:
    document = parse_frontmatter_bytes(
        (f"---\ntype: note\ntier: {tier}\n---\n# Card\n").encode()
    )

    _, missing, invalid = validate_document("note.md", document, _manifest())

    assert missing == ()
    assert invalid == ()


@pytest.mark.parametrize(
    ("replacement", "expected_missing"),
    [
        (b"epistemic_confidence: high", ()),
        (b'epistemic_scope: ""', ("epistemic_scope",)),
        (b"epistemic_state: retired", ()),
        (b"", ()),
    ],
)
def test_epistemic_profile_uses_wp3_metadata_contract(
    replacement: bytes, expected_missing: tuple[str, ...]
) -> None:
    content = (
        b"---\n"
        b"type: epistemic\n"
        b"description: Durable fact\n"
        b"epistemic_confidence: verified\n"
        b'epistemic_scope: "project:test"\n'
        b"epistemic_state: active\n"
        b'epistemic_verification: "test evidence"\n'
        + b"created: 2026-07-29\nupdated: 2026-07-29\n"
        b"last_accessed: 2026-07-29\nrelevance: 0.8\ntier: warm\n---\n# Note\n"
    )
    if replacement:
        if replacement.startswith(b"epistemic_confidence"):
            content = content.replace(b"epistemic_confidence: verified", replacement)
        elif replacement.startswith(b"epistemic_scope"):
            content = content.replace(b'epistemic_scope: "project:test"', replacement)
        else:
            content = content.replace(b"epistemic_state: active", replacement)
    else:
        content = content.replace(b'epistemic_verification: "test evidence"\n', b"")
    document = parse_frontmatter_bytes(content)

    route, missing, invalid = validate_document(
        "thoughts/fact.md", document, _manifest()
    )

    assert route.name == "epistemic"
    assert missing == expected_missing
    assert invalid == ("epistemic_metadata",)


def test_path_checks_reject_symlink_outside_and_traversal(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# Note\n", encoding="utf-8")
    assert resolve_vault_markdown_path(vault, "note.md") == note
    with pytest.raises(UnsafeVaultPathError):
        resolve_vault_markdown_path(vault, "../outside.md")
    with pytest.raises(UnsafeVaultPathError):
        resolve_vault_markdown_path(vault, tmp_path / "outside.md")

    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    link = vault / "link.md"
    link.symlink_to(outside)
    with pytest.raises(UnsafeVaultPathError, match="symlink"):
        resolve_vault_markdown_path(vault, "link.md")


def test_atomic_write_preserves_mode_and_replaces_bytes(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    note.chmod(0o640)

    atomic_write_bytes(note, b"new")

    assert note.read_bytes() == b"new"
    assert os.stat(note).st_mode & 0o777 == 0o640


def test_validated_write_rejects_symlink_vault_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    vault = tmp_path / "vault"
    outside.mkdir()
    vault.symlink_to(outside, target_is_directory=True)
    content = b"---\ntype: note\n---\n# Secret\n"

    with pytest.raises(UnsafeVaultPathError, match="following symlinks"):
        write_validated_vault_markdown(vault, vault / "note.md", content)

    assert not (outside / "note.md").exists()


def test_writer_and_lock_reject_symlink_in_vault_parent_path(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external_vault = external / "vault"
    alias = tmp_path / "alias"
    external_vault.mkdir(parents=True)
    alias.symlink_to(external, target_is_directory=True)
    vault = alias / "vault"
    content = b"---\ntype: note\n---\n# Secret\n"

    with pytest.raises(UnsafeVaultPathError, match="following symlinks"):
        write_validated_vault_markdown(vault, vault / "note.md", content)
    with pytest.raises(VaultLockError, match="following symlinks"):
        with vault_write_lock(vault):
            pytest.fail("symlinked parent must not yield a lock")

    assert not (external_vault / "note.md").exists()
    assert not (external_vault / ".locks").exists()


def test_vault_file_writer_keeps_pinned_root_after_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    pinned = tmp_path / "vault-pinned"
    external = tmp_path / "external"
    vault.mkdir()
    external.mkdir()
    target = vault / "imports" / "demo" / "payload.bin"
    original_lock = frontmatter_module.vault_write_lock
    swapped = False

    @contextmanager
    def replace_root_before_lock(path: Path, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if not swapped:
            vault.rename(pinned)
            vault.symlink_to(external, target_is_directory=True)
            swapped = True
        with original_lock(path, **kwargs) as lock:
            yield lock

    monkeypatch.setattr(
        frontmatter_module,
        "vault_write_lock",
        replace_root_before_lock,
    )

    write_vault_file_bytes(
        vault,
        target,
        b"\x00descriptor-safe\xff",
        require_absent=True,
    )

    assert swapped is True
    assert (pinned / "imports" / "demo" / "payload.bin").read_bytes() == (
        b"\x00descriptor-safe\xff"
    )
    assert not (external / "imports").exists()


def test_vault_file_writer_create_only_preserves_existing_target(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target = vault / "imports" / "demo" / "payload.txt"

    write_vault_file_bytes(vault, target, b"first", require_absent=True)

    with pytest.raises(UnsafeVaultPathError, match="already exists"):
        write_vault_file_bytes(vault, target, b"second", require_absent=True)

    assert read_vault_file_bytes(vault, target) == b"first"


def test_validated_write_keeps_single_open_root_when_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    pinned = tmp_path / "vault-pinned"
    vault.mkdir()
    content = b"---\ntype: note\n---\n# Pinned root\n"
    original_lock = frontmatter_module.vault_write_lock
    swapped = False

    @contextmanager
    def replace_root_before_lock(path: Path, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if not swapped:
            vault.rename(pinned)
            vault.mkdir()
            swapped = True
        with original_lock(path, **kwargs) as lock:
            yield lock

    monkeypatch.setattr(
        frontmatter_module, "vault_write_lock", replace_root_before_lock
    )

    write_validated_vault_markdown(vault, vault / "note.md", content)

    assert swapped is True
    assert not (vault / "note.md").exists()
    assert (pinned / "note.md").read_bytes() == content


def test_existing_lock_rejects_replaced_root_identity(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    pinned = tmp_path / "vault-pinned"
    vault.mkdir()
    content = b"---\ntype: note\n---\n# Pinned root\n"

    with vault_write_lock(vault) as lock:
        vault.rename(pinned)
        vault.mkdir()
        with pytest.raises(UnsafeVaultPathError, match="does not match vault"):
            write_validated_vault_markdown(
                vault,
                vault / "note.md",
                content,
                existing_lock=lock,
            )

    assert not (vault / "note.md").exists()
    assert not (pinned / "note.md").exists()


def test_validated_write_rejects_forged_existing_lock_descriptors(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (vault / ".locks").mkdir()
    (outside / ".locks").mkdir()
    content = b"---\ntype: note\n---\n# Secret\n"
    outside_root_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
    outside_locks_fd = os.open(outside / ".locks", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(VaultLockError, match="cannot be constructed"):
            VaultWriteLock(
                root=vault.absolute(),
                root_fd=outside_root_fd,
                locks_fd=outside_locks_fd,
            )
    finally:
        os.close(outside_locks_fd)
        os.close(outside_root_fd)

    assert not (vault / "note.md").exists()
    assert not (outside / "note.md").exists()

    with vault_write_lock(vault) as real_lock:
        outside_root_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
        outside_locks_fd = os.open(outside / ".locks", os.O_RDONLY | os.O_DIRECTORY)
        forged = object.__new__(VaultWriteLock)
        object.__setattr__(forged, "root", vault.absolute())
        object.__setattr__(forged, "root_fd", outside_root_fd)
        object.__setattr__(forged, "locks_fd", outside_locks_fd)
        object.__setattr__(forged, "_lease", real_lock._lease)
        try:
            with pytest.raises(UnsafeVaultPathError, match="not active"):
                write_validated_vault_markdown(
                    vault,
                    vault / "note.md",
                    content,
                    existing_lock=forged,
                )
        finally:
            os.close(outside_locks_fd)
            os.close(outside_root_fd)

        vault_root_fd = os.open(vault, os.O_RDONLY | os.O_DIRECTORY)
        outside_locks_fd = os.open(outside / ".locks", os.O_RDONLY | os.O_DIRECTORY)
        forged_locks = object.__new__(VaultWriteLock)
        object.__setattr__(forged_locks, "root", vault.absolute())
        object.__setattr__(forged_locks, "root_fd", vault_root_fd)
        object.__setattr__(forged_locks, "locks_fd", outside_locks_fd)
        object.__setattr__(forged_locks, "_lease", real_lock._lease)
        try:
            with pytest.raises(UnsafeVaultPathError, match="not active"):
                write_validated_vault_markdown(
                    vault,
                    vault / "note.md",
                    content,
                    existing_lock=forged_locks,
                )
        finally:
            os.close(outside_locks_fd)
            os.close(vault_root_fd)

        fake_proof = object.__new__(VaultWriteLock)
        object.__setattr__(fake_proof, "root", vault.absolute())
        object.__setattr__(fake_proof, "root_fd", real_lock.root_fd)
        object.__setattr__(fake_proof, "locks_fd", real_lock.locks_fd)
        object.__setattr__(fake_proof, "_lease", object())
        with pytest.raises(UnsafeVaultPathError, match="not active"):
            write_validated_vault_markdown(
                vault,
                vault / "note.md",
                content,
                existing_lock=fake_proof,
            )

    assert not (vault / "note.md").exists()
    assert not (outside / "note.md").exists()


def test_hidden_root_lease_survives_public_fd_reuse_and_locks_swap(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    content = b"---\ntype: note\n---\n# Secret\n"
    first = vault / "first.md"
    second = vault / "second.md"
    second_started = threading.Event()
    second_done = threading.Event()
    second_errors: list[BaseException] = []

    def run_second_writer() -> None:
        second_started.set()
        try:
            write_validated_vault_markdown(vault, second, content)
        except BaseException as exc:
            second_errors.append(exc)
        finally:
            second_done.set()

    worker: threading.Thread
    with vault_write_lock(vault) as lock:
        same_inode_fd = os.open(vault, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.dup2(same_inode_fd, lock.root_fd)
        finally:
            if same_inode_fd != lock.root_fd:
                os.close(same_inode_fd)
        os.rename(vault / ".locks", vault / ".locks-original")
        (vault / ".locks").mkdir()
        worker = threading.Thread(target=run_second_writer)
        worker.start()
        assert second_started.wait(1)
        assert not second_done.wait(0.1)
        with pytest.raises(UnsafeVaultPathError, match="directory descriptor"):
            write_validated_vault_markdown(
                vault,
                first,
                content,
                existing_lock=lock,
            )
        assert not first.exists()

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert second_errors == []
    assert second.read_bytes() == content


def test_vault_lock_cleanup_ignores_closed_public_descriptors(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    with vault_write_lock(vault) as lock:
        os.close(lock.locks_fd)
        os.close(lock.root_fd)

    with vault_write_lock(vault):
        pass


def test_vault_lock_cleanup_does_not_close_reused_public_descriptor(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    marker_path = tmp_path / "marker"

    with vault_write_lock(vault) as lock:
        exposed_root_fd = lock.root_fd
        os.close(exposed_root_fd)
        marker_fd = os.open(marker_path, os.O_CREAT | os.O_RDWR, 0o600)
        assert marker_fd == exposed_root_fd

    try:
        os.fstat(marker_fd)
    finally:
        os.close(marker_fd)


def test_validated_write_rejects_closed_existing_lock(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    content = b"---\ntype: note\n---\n# Secret\n"
    with vault_write_lock(vault) as lock:
        stale_lock = lock

    with pytest.raises(UnsafeVaultPathError, match="not active"):
        write_validated_vault_markdown(
            vault,
            vault / "note.md",
            content,
            existing_lock=stale_lock,
        )

    assert not (vault / "note.md").exists()


def test_validated_write_uses_verified_unnamed_source_at_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    content = b"---\ntype: note\n---\n# Sensitive candidate\n"
    original_link = frontmatter_module._link_descriptor_noreplace
    attacked = False

    def inject_old_visible_source(
        source_fd: int, destination_fd: int, destination_name: str
    ) -> None:
        nonlocal attacked
        stage = next(
            child
            for child in vault.iterdir()
            if child.name.startswith(".dbrain-write-")
        )
        assert stat.S_IMODE(os.stat(stage).st_mode) == 0o700
        (stage / "content").write_bytes(b"attacker-controlled")
        attacked = True
        original_link(source_fd, destination_fd, destination_name)

    monkeypatch.setattr(
        frontmatter_module,
        "_link_descriptor_noreplace",
        inject_old_visible_source,
    )

    write_validated_vault_markdown(vault, note, content)

    assert attacked is True
    assert note.read_bytes() == content
    assert not any(child.name.startswith(".dbrain-write-") for child in vault.iterdir())


def test_validated_write_checks_expected_full_hash_inside_commit(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    original = b"---\ntype: note\n---\n# Original\n"
    updated = b"---\ntype: note\n---\n# Updated\n"
    note.write_bytes(original)

    write_validated_vault_markdown(
        vault,
        note,
        updated,
        expected_full_sha256=hashlib.sha256(original).hexdigest(),
    )

    assert note.read_bytes() == updated
    with pytest.raises(UnsafeVaultPathError, match="expected_full_sha256"):
        write_validated_vault_markdown(
            vault,
            note,
            original,
            expected_full_sha256=hashlib.sha256(original).hexdigest(),
        )
    assert note.read_bytes() == updated


def test_validated_write_require_absent_rejects_existing_markdown(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    original = b"---\ntype: note\n---\n# Original\n"
    note.write_bytes(original)

    with pytest.raises(UnsafeVaultPathError, match="already exists"):
        write_validated_vault_markdown(
            vault,
            note,
            b"---\ntype: note\n---\n# Replacement\n",
            require_absent=True,
        )

    assert note.read_bytes() == original


def test_validated_move_refuses_target_appearing_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    source = vault / "imports" / "source.md"
    target = vault / "imports" / "target.md"
    source.parent.mkdir(parents=True)
    source_bytes = b"---\ntype: note\n---\n# Source\n"
    target_bytes = b"---\ntype: note\n---\n# Racer\n"
    source.write_bytes(source_bytes)
    original_link = frontmatter_module._link_descriptor_noreplace

    def create_target(
        source_fd: int, destination_fd: int, destination_name: str
    ) -> None:
        if destination_name == target.name:
            target.write_bytes(target_bytes)
        original_link(source_fd, destination_fd, destination_name)

    monkeypatch.setattr(frontmatter_module, "_link_descriptor_noreplace", create_target)

    with pytest.raises(FrontmatterError, match="target appeared"):
        move_validated_vault_markdown(vault, source, target, manifest=_manifest())

    assert source.read_bytes() == source_bytes
    assert target.read_bytes() == target_bytes


def test_validated_write_creates_new_markdown_without_following_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "created.md"
    content = b"---\ntype: note\n---\n# Created\n"

    write_validated_vault_markdown(vault, note, content)

    assert note.read_bytes() == content

    swapped = vault / "swapped.md"
    original_link = frontmatter_module._link_descriptor_noreplace

    def create_racer_before_publication(
        source_fd: int, destination_fd: int, destination_name: str
    ) -> None:
        if destination_name == "swapped.md":
            swapped.write_bytes(b"RACER")
        original_link(source_fd, destination_fd, destination_name)

    monkeypatch.setattr(
        frontmatter_module,
        "_link_descriptor_noreplace",
        create_racer_before_publication,
    )
    with pytest.raises(UnsafeVaultPathError, match="no-replace publication"):
        write_validated_vault_markdown(vault, swapped, content)
    assert swapped.read_bytes() == b"RACER"
    assert not any(child.name.startswith(".dbrain-write-") for child in vault.iterdir())


def test_expected_hash_target_race_restores_original_and_preserves_racer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    original = b"---\ntype: note\n---\n# Original\n"
    candidate = b"---\ntype: note\n---\n# Candidate\n"
    racer = b"RACER"
    note.write_bytes(original)
    original_exchange = frontmatter_module._rename_exchange
    attacked = False

    def race_target_before_exchange(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal attacked
        replacement = vault / "racer.tmp"
        replacement.write_bytes(racer)
        os.replace(replacement, note)
        attacked = True
        original_exchange(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
        )

    monkeypatch.setattr(
        frontmatter_module, "_rename_exchange", race_target_before_exchange
    )

    with pytest.raises(UnsafeVaultPathError, match="conflicting entry preserved"):
        write_validated_vault_markdown(
            vault,
            note,
            candidate,
            expected_full_sha256=hashlib.sha256(original).hexdigest(),
        )

    assert attacked is True
    assert note.read_bytes() == original
    stages = [
        child for child in vault.iterdir() if child.name.startswith(".dbrain-write-")
    ]
    assert len(stages) == 1
    staged_bytes = [child.read_bytes() for child in stages[0].iterdir()]
    assert racer in staged_bytes
    assert original in staged_bytes


@pytest.mark.parametrize("replace_existing", [False, True])
def test_subprocess_candidate_mutation_rolls_back_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_existing: bool,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    original = b"---\ntype: note\n---\n# Original\n"
    candidate = b"---\ntype: note\n---\n# Candidate\n"
    if replace_existing:
        note.write_bytes(original)
    original_link = frontmatter_module._link_descriptor_noreplace
    original_exchange = frontmatter_module._rename_exchange
    candidate_fd: int | None = None

    def mutate_from_child(descriptor: int) -> None:
        script = (
            "import os,sys\n"
            "fd=os.open(f'/proc/{sys.argv[1]}/fd/{sys.argv[2]}',os.O_WRONLY)\n"
            "os.ftruncate(fd,0)\n"
            "os.write(fd,b'ATTACKER')\n"
            "os.close(fd)\n"
        )
        subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(os.getpid()),
                str(descriptor),
            ],
            check=True,
        )

    def remember_or_mutate_candidate(
        source_fd: int, destination_fd: int, destination_name: str
    ) -> None:
        nonlocal candidate_fd
        if replace_existing and destination_name.startswith("candidate-"):
            candidate_fd = source_fd
        elif not replace_existing and destination_name == "note.md":
            mutate_from_child(source_fd)
        original_link(source_fd, destination_fd, destination_name)

    def mutate_after_verify_before_exchange(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        assert candidate_fd is not None
        mutate_from_child(candidate_fd)
        original_exchange(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
        )

    monkeypatch.setattr(
        frontmatter_module,
        "_link_descriptor_noreplace",
        remember_or_mutate_candidate,
    )
    if replace_existing:
        monkeypatch.setattr(
            frontmatter_module,
            "_rename_exchange",
            mutate_after_verify_before_exchange,
        )

    with pytest.raises(UnsafeVaultPathError, match="source changed"):
        write_validated_vault_markdown(
            vault,
            note,
            candidate,
            expected_full_sha256=(
                hashlib.sha256(original).hexdigest() if replace_existing else None
            ),
        )

    if replace_existing:
        assert note.read_bytes() == original
    else:
        assert not note.exists()
    assert not any(child.name.startswith(".dbrain-write-") for child in vault.iterdir())


def test_swapped_guard_slot_restores_original_from_held_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    original = b"---\ntype: note\n---\n# Original\n"
    candidate = b"---\ntype: note\n---\n# Candidate\n"
    note.write_bytes(original)
    original_exchange = frontmatter_module._rename_exchange
    attacked = False

    def swap_guard_slot_after_exchange(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal attacked
        original_exchange(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
        )
        os.unlink(source_name, dir_fd=source_fd)
        attacker_fd = os.open(
            source_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=source_fd,
        )
        try:
            os.write(attacker_fd, b"ATTACKER")
        finally:
            os.close(attacker_fd)
        attacked = True

    monkeypatch.setattr(
        frontmatter_module,
        "_rename_exchange",
        swap_guard_slot_after_exchange,
    )

    with pytest.raises(UnsafeVaultPathError, match="conflicting entry preserved"):
        write_validated_vault_markdown(
            vault,
            note,
            candidate,
            expected_full_sha256=hashlib.sha256(original).hexdigest(),
        )

    assert attacked is True
    assert note.read_bytes() == original
    stages = [
        child for child in vault.iterdir() if child.name.startswith(".dbrain-write-")
    ]
    assert len(stages) == 1
    staged_bytes = [child.read_bytes() for child in stages[0].iterdir()]
    assert b"ATTACKER" in staged_bytes
    assert original in staged_bytes
    assert candidate not in staged_bytes


@pytest.mark.parametrize("directory_fsync_call", [1, 2, 3])
def test_replace_fsync_fault_restores_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory_fsync_call: int,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    original = b"---\ntype: note\n---\n# Original\n"
    candidate = b"---\ntype: note\n---\n# Candidate\n"
    note.write_bytes(original)
    original_fsync = frontmatter_module.os.fsync
    directory_calls = 0
    injected = False

    def fail_one_directory_fsync(descriptor: int) -> None:
        nonlocal directory_calls, injected
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_calls += 1
            if directory_calls == directory_fsync_call and not injected:
                injected = True
                raise OSError(errno.EIO, "injected directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(frontmatter_module.os, "fsync", fail_one_directory_fsync)

    with pytest.raises(UnsafeVaultPathError, match="injected"):
        write_validated_vault_markdown(
            vault,
            note,
            candidate,
            expected_full_sha256=hashlib.sha256(original).hexdigest(),
        )

    assert injected is True
    assert note.read_bytes() == original
    assert not any(child.name.startswith(".dbrain-write-") for child in vault.iterdir())


def test_create_parent_fsync_fault_removes_published_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    candidate = b"---\ntype: note\n---\n# Candidate\n"
    original_fsync = frontmatter_module.os.fsync
    injected = False

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal injected
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not injected:
            injected = True
            raise OSError(errno.EIO, "injected parent fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(frontmatter_module.os, "fsync", fail_first_directory_fsync)

    with pytest.raises(UnsafeVaultPathError, match="injected"):
        write_validated_vault_markdown(vault, note, candidate)

    assert injected is True
    assert not note.exists()
    assert not any(child.name.startswith(".dbrain-write-") for child in vault.iterdir())


def test_postcommit_cleanup_fsync_failure_does_not_report_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    original = b"---\ntype: note\n---\n# Original\n"
    candidate = b"---\ntype: note\n---\n# Candidate\n"
    note.write_bytes(original)
    original_fsync = frontmatter_module.os.fsync
    directory_calls = 0
    injected = False

    def fail_fourth_directory_fsync(descriptor: int) -> None:
        nonlocal directory_calls, injected
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_calls += 1
            if directory_calls == 4 and not injected:
                injected = True
                raise OSError(errno.EIO, "injected postcommit cleanup failure")
        original_fsync(descriptor)

    monkeypatch.setattr(frontmatter_module.os, "fsync", fail_fourth_directory_fsync)

    write_validated_vault_markdown(
        vault,
        note,
        candidate,
        expected_full_sha256=hashlib.sha256(original).hexdigest(),
    )

    assert injected is True
    assert note.read_bytes() == candidate
    assert (
        len(
            [
                child
                for child in vault.iterdir()
                if child.name.startswith(".dbrain-write-")
            ]
        )
        == 1
    )


def test_partial_postcommit_cleanup_preserves_recovery_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    original = b"---\ntype: note\n---\n# Original\n"
    candidate = b"---\ntype: note\n---\n# Candidate\n"
    note.write_bytes(original)
    original_unlink = frontmatter_module._unlink_name_if_inode
    injected = False

    def fail_first_guard_unlink(
        parent_fd: int, name: str, expected_inode: tuple[int, int]
    ) -> bool:
        nonlocal injected
        if name.startswith("candidate-") and not injected:
            injected = True
            raise OSError(errno.EIO, "injected guard cleanup failure")
        return original_unlink(parent_fd, name, expected_inode)

    monkeypatch.setattr(
        frontmatter_module,
        "_unlink_name_if_inode",
        fail_first_guard_unlink,
    )

    write_validated_vault_markdown(
        vault,
        note,
        candidate,
        expected_full_sha256=hashlib.sha256(original).hexdigest(),
    )

    stages = [
        child for child in vault.iterdir() if child.name.startswith(".dbrain-write-")
    ]
    assert injected is True
    assert note.read_bytes() == candidate
    assert len(stages) == 1
    staged_bytes = [child.read_bytes() for child in stages[0].iterdir()]
    assert staged_bytes.count(original) == 2


def test_locks_directory_swap_blocks_second_writer_and_aborts_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    first = vault / "first.md"
    second = vault / "second.md"
    content = b"---\ntype: note\n---\n# Content\n"
    original_validate = frontmatter_module._validate_lock_namespace
    swapped = False
    second_started = threading.Event()
    second_done = threading.Event()
    second_errors: list[BaseException] = []
    worker: threading.Thread | None = None

    def run_second_writer() -> None:
        second_started.set()
        try:
            write_validated_vault_markdown(vault, second, content)
        except BaseException as exc:
            second_errors.append(exc)
        finally:
            second_done.set()

    def swap_locks_once(capability) -> None:  # type: ignore[no-untyped-def]
        nonlocal swapped, worker
        if not swapped:
            swapped = True
            os.rename(vault / ".locks", vault / ".locks-original")
            (vault / ".locks").mkdir()
            worker = threading.Thread(target=run_second_writer)
            worker.start()
            assert second_started.wait(1)
            assert not second_done.wait(0.1)
        original_validate(capability)

    monkeypatch.setattr(frontmatter_module, "_validate_lock_namespace", swap_locks_once)

    with pytest.raises(UnsafeVaultPathError, match="canonical .locks changed"):
        write_validated_vault_markdown(vault, first, content)

    assert worker is not None
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert second_errors == []
    assert not first.exists()
    assert second.read_bytes() == content


def test_cleanup_never_touches_swapped_stage_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    victim = vault / "victim"
    victim.mkdir()
    victim_file = victim / "keep.txt"
    victim_file.write_text("keep", encoding="utf-8")
    content = b"---\ntype: note\n---\n# Sensitive candidate\n"
    moved_stage = vault / "original-stage"
    swapped_stage: Path | None = None

    def swap_stage_and_fail(
        source_fd: int, destination_fd: int, destination_name: str
    ) -> None:
        del source_fd, destination_fd, destination_name
        nonlocal swapped_stage
        stage = next(
            child
            for child in vault.iterdir()
            if child.name.startswith(".dbrain-write-")
        )
        stage.rename(moved_stage)
        victim.rename(stage)
        swapped_stage = stage
        raise UnsafeVaultPathError("injected publication failure")

    monkeypatch.setattr(
        frontmatter_module, "_link_descriptor_noreplace", swap_stage_and_fail
    )

    with pytest.raises(UnsafeVaultPathError, match="injected"):
        write_validated_vault_markdown(vault, note, content)

    assert swapped_stage is not None
    assert (swapped_stage / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert list(moved_stage.iterdir()) == []
    assert not note.exists()


def test_validated_writer_preserves_mode_and_does_not_leak_fds(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_bytes(b"---\ntype: note\n---\n# 0\n")
    note.chmod(0o640)
    before = len(os.listdir("/proc/self/fd"))

    for index in range(1, 6):
        current = note.read_bytes()
        candidate = f"---\ntype: note\n---\n# {index}\n".encode()
        write_validated_vault_markdown(
            vault,
            note,
            candidate,
            expected_full_sha256=hashlib.sha256(current).hexdigest(),
        )

    after = len(os.listdir("/proc/self/fd"))
    assert after == before
    assert stat.S_IMODE(note.stat().st_mode) == 0o640
    assert not any(child.name.startswith(".dbrain-write-") for child in vault.iterdir())


def test_validated_write_creates_missing_nested_parents(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "new" / "nested" / "note.md"
    content = b"---\ntype: note\n---\n# Created\n"

    write_validated_vault_markdown(vault, note, content)

    assert note.read_bytes() == content


def test_validated_write_rejects_existing_intermediate_symlink(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (vault / "linked").symlink_to(outside, target_is_directory=True)
    content = b"---\ntype: note\n---\n# Created\n"

    with pytest.raises(UnsafeVaultPathError, match="safely"):
        write_validated_vault_markdown(
            vault, vault / "linked" / "nested" / "note.md", content
        )

    assert not (outside / "nested" / "note.md").exists()


def test_validated_write_rejects_intermediate_directory_symlink_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    content = b"---\ntype: note\n---\n# Created\n"
    original_mkdir = os.mkdir
    raced = False

    def swap_created_directory(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal raced
        original_mkdir(path, mode, dir_fd=dir_fd)
        if path == "raced" and dir_fd is not None:
            os.rmdir(vault / "raced")
            (vault / "raced").symlink_to(outside, target_is_directory=True)
            raced = True

    monkeypatch.setattr("d_brain.services.frontmatter.os.mkdir", swap_created_directory)

    with pytest.raises(UnsafeVaultPathError, match="safely"):
        write_validated_vault_markdown(
            vault, vault / "raced" / "nested" / "note.md", content
        )

    assert raced is True
    assert not (outside / "nested" / "note.md").exists()


def test_semantic_adapter_accepts_only_requested_valid_values() -> None:
    assert validate_semantic_payload(
        {
            "description": "Search-ready note",
            "tags": ["project-alpha", "planning"],
            "status": "active",
        },
        ("description", "tags", "status"),
    ) == {
        "description": "Search-ready note",
        "tags": ["project-alpha", "planning"],
        "status": "active",
    }
    with pytest.raises(FrontmatterError, match="tags"):
        validate_semantic_payload({"tags": ["Bad Tag"]}, ("tags",))
    with pytest.raises(FrontmatterError, match="exactly"):
        validate_semantic_payload({"description": "extra"}, ("tags",))


def test_semantic_adapter_validates_namespaced_epistemic_values() -> None:
    payload = {
        "epistemic_confidence": "inferred",
        "epistemic_scope": "project:planner",
        "epistemic_state": "active",
        "epistemic_verification": "Reviewed source",
    }
    assert validate_semantic_payload(payload, tuple(payload)) == payload
    for field, value in (
        ("epistemic_confidence", "certain"),
        ("epistemic_scope", ""),
        ("epistemic_state", "retired"),
        ("epistemic_verification", ""),
    ):
        invalid = dict(payload)
        invalid[field] = value
        with pytest.raises(FrontmatterError, match=field):
            validate_semantic_payload(invalid, tuple(invalid))
