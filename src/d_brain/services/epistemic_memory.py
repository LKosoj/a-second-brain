"""Crash-safe supersession for namespaced epistemic vault metadata.

The pair write is protected by the shared cooperative vault writer lock.  It
does not claim to defeat a non-cooperative editor: quiesce those editors before
starting a supersession or recovery operation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from d_brain.manifest import (
    ManifestValidationError,
    VaultManifest,
    load_manifest_for_vault,
)
from d_brain.services.frontmatter import (
    FrontmatterError,
    UnsafeVaultPathError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    read_vault_file_bytes,
    validate_document,
    write_validated_vault_markdown,
)
from d_brain.services.vault_lock import VaultLockError, VaultWriteLock, vault_write_lock

EPISTEMIC_CONFIDENCE_VALUES = frozenset({"verified", "inferred", "unverified"})
EPISTEMIC_STATE_VALUES = frozenset({"active", "superseded"})
_EPISTEMIC_FIELDS = frozenset(
    {
        "epistemic_confidence",
        "epistemic_scope",
        "epistemic_state",
        "epistemic_verification",
    }
)
_JOURNAL_NAME = "epistemic-supersession.json"


class EpistemicMemoryError(RuntimeError):
    """Base error for epistemic memory operations."""


class EpistemicValidationError(EpistemicMemoryError):
    """Raised when a note does not satisfy the epistemic contract."""


class EpistemicConflictError(EpistemicMemoryError):
    """Raised when content changed outside the cooperative supersession flow."""


@dataclass(frozen=True)
class EpistemicMetadata:
    """Validated namespaced metadata for one epistemic note."""

    confidence: Literal["verified", "inferred", "unverified"]
    scope: str
    state: Literal["active", "superseded"]
    verification: str | None
    supersedes: tuple[str, ...]
    superseded_by: str | None


@dataclass(frozen=True)
class SupersessionResult:
    """Result of one supersession request."""

    old_path: str
    new_path: str
    changed: bool


@dataclass(frozen=True)
class _EpistemicDocument:
    fields: Mapping[str, Any]
    header: bytes
    body: bytes
    newline: bytes


@dataclass(frozen=True)
class _JournalNote:
    path: str
    before_sha256: str
    after_sha256: str
    before_payload: bytes
    after_payload: bytes


@dataclass(frozen=True)
class _SupersessionPlan:
    old_metadata: EpistemicMetadata
    new_metadata: EpistemicMetadata
    notes: tuple[_JournalNote, _JournalNote]
    changed: bool


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalize_note_path(value: str | Path) -> str:
    path = PurePosixPath(str(value))
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or str(path) in {"", "."}
        or path.suffix.lower() != ".md"
    ):
        raise EpistemicValidationError(
            "epistemic links must be vault-relative Markdown paths"
        )
    return path.as_posix()


def _required_string(fields: Mapping[str, Any], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        raise EpistemicValidationError(f"{key} must be a non-empty one-line string")
    return value.strip()


def _parse_epistemic_document(content: bytes) -> _EpistemicDocument:
    """Parse frontmatter with WP4's strict YAML loader and preserve body bytes.

    Delegates the split to ``frontmatter.parse_frontmatter_bytes`` rather
    than repeating its delimiter rules here (code review): the hand-rolled
    copy this replaces accepted only ``\\n`` and ``\\r\\n`` and always
    demanded a newline *after* the closing ``---``, so two shapes that module
    handles were rejected outright -- a note saved with classic-Mac ``\\r``
    line endings, and a header-only note with no trailing newline. Both
    reached this function through ``supersede_notes``, which then refused a
    perfectly valid vault file.

    Errors are still raised as ``EpistemicValidationError`` (the contract
    every caller here catches), not as ``FrontmatterError``.
    """
    try:
        document = parse_frontmatter_bytes(content)
    except Exception as exc:  # FrontmatterError, or the strict YAML loader
        raise EpistemicValidationError(str(exc)) from exc
    if document.header is None:
        raise EpistemicValidationError("epistemic notes require frontmatter")
    return _EpistemicDocument(
        fields=document.fields,
        header=document.header,
        body=document.body,
        newline=document.newline,
    )


def parse_epistemic_metadata(fields: Mapping[str, Any]) -> EpistemicMetadata | None:
    """Parse only namespaced epistemic fields; compiled metadata is ignored."""
    explicit = {key for key in fields if key.startswith("epistemic_")}
    if not explicit:
        return None
    unknown = explicit - _EPISTEMIC_FIELDS
    if unknown:
        raise EpistemicValidationError(
            f"unknown epistemic field(s): {', '.join(sorted(unknown))}"
        )
    confidence = _required_string(fields, "epistemic_confidence")
    if confidence not in EPISTEMIC_CONFIDENCE_VALUES:
        raise EpistemicValidationError("epistemic_confidence is invalid")
    scope = _required_string(fields, "epistemic_scope")
    state = _required_string(fields, "epistemic_state")
    if state not in EPISTEMIC_STATE_VALUES:
        raise EpistemicValidationError("epistemic_state is invalid")

    verification_value = fields.get("epistemic_verification")
    if verification_value is None:
        verification = None
    elif (
        isinstance(verification_value, str)
        and verification_value.strip()
        and "\n" not in verification_value
    ):
        verification = verification_value.strip()
    else:
        raise EpistemicValidationError(
            "epistemic_verification must be a non-empty one-line string when set"
        )
    if confidence == "verified" and verification is None:
        raise EpistemicValidationError(
            "verified epistemic notes require epistemic_verification"
        )

    supersedes_value = fields.get("supersedes", [])
    if not isinstance(supersedes_value, list) or not all(
        isinstance(item, str) for item in supersedes_value
    ):
        raise EpistemicValidationError("supersedes must be a list of vault paths")
    supersedes = tuple(_normalize_note_path(item) for item in supersedes_value)
    if len(set(supersedes)) != len(supersedes):
        raise EpistemicValidationError("supersedes must not contain duplicates")

    superseded_by_value = fields.get("superseded_by")
    if superseded_by_value in (None, ""):
        superseded_by = None
    elif isinstance(superseded_by_value, str):
        superseded_by = _normalize_note_path(superseded_by_value)
    else:
        raise EpistemicValidationError("superseded_by must be one vault path")
    if state == "active" and superseded_by is not None:
        raise EpistemicValidationError(
            "active epistemic notes cannot have superseded_by"
        )
    if state == "superseded" and superseded_by is None:
        raise EpistemicValidationError("superseded notes require superseded_by")

    return EpistemicMetadata(
        confidence=cast(Literal["verified", "inferred", "unverified"], confidence),
        scope=scope,
        state=cast(Literal["active", "superseded"], state),
        verification=verification,
        supersedes=supersedes,
        superseded_by=superseded_by,
    )


def parse_epistemic_note(content: bytes) -> EpistemicMetadata:
    """Parse one complete epistemic note and return its typed metadata."""
    metadata = parse_epistemic_metadata(_parse_epistemic_document(content).fields)
    if metadata is None:
        raise EpistemicValidationError("note has no epistemic metadata")
    return metadata


def _patch_epistemic_payload(content: bytes, updates: Mapping[str, Any]) -> bytes:
    """Patch supported one-line fields while preserving all comments and body."""
    before = _parse_epistemic_document(content)
    try:
        candidate = patch_frontmatter_bytes(content, updates)
    except FrontmatterError as exc:
        raise EpistemicValidationError(str(exc)) from exc
    after = _parse_epistemic_document(candidate)
    if after.body != before.body:
        raise EpistemicMemoryError("epistemic update changed Markdown body")
    return candidate


def _open_child_directory(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise EpistemicValidationError(f"unsafe vault directory {name}: {exc}") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise EpistemicValidationError(f"unsafe vault directory {name}")
    return descriptor


def _open_parent_directory(lock: VaultWriteLock, relative_path: str) -> tuple[int, str]:
    parts = PurePosixPath(relative_path).parts
    descriptor = os.dup(lock.root_fd)
    try:
        for part in parts[:-1]:
            child = _open_child_directory(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def _read_regular_file(parent_fd: int, name: str) -> tuple[bytes, int]:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise EpistemicValidationError(f"unsafe vault file {name}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise EpistemicValidationError(f"vault path is not a regular file: {name}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(info.st_mode)
    finally:
        os.close(descriptor)


def _read_note(lock: VaultWriteLock, relative_path: str) -> bytes:
    parent_fd, name = _open_parent_directory(lock, relative_path)
    try:
        return _read_regular_file(parent_fd, name)[0]
    finally:
        os.close(parent_fd)


def _create_temp(parent_fd: int, prefix: str) -> tuple[int, str]:
    for _attempt in range(32):
        name = f".{prefix}.{secrets.token_hex(12)}.tmp"
        try:
            return (
                os.open(
                    name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                ),
                name,
            )
        except FileExistsError:
            continue
    raise EpistemicMemoryError("could not allocate an atomic vault temporary file")


def _replace_note(
    lock: VaultWriteLock,
    relative_path: str,
    payload: bytes,
    expected_hash: str,
    *,
    manifest: VaultManifest,
) -> None:
    """Validate and CAS-replace one note under the caller's live vault lock."""
    try:
        write_validated_vault_markdown(
            lock.root,
            lock.root / relative_path,
            payload,
            manifest=manifest,
            existing_lock=lock,
            expected_full_sha256=expected_hash,
        )
    except (OSError, UnsafeVaultPathError) as exc:
        message = str(exc)
        if "expected_full_sha256" in message or "source changed" in message:
            raise EpistemicConflictError(
                f"cannot write {relative_path}: content changed before supersession"
            ) from exc
        raise EpistemicValidationError(
            f"cannot write {relative_path} through the vault writer: {exc}"
        ) from exc
    except FrontmatterError as exc:
        raise EpistemicValidationError(
            f"cannot validate {relative_path} for supersession: {exc}"
        ) from exc


def _validate_manifest_payloads(
    manifest: VaultManifest, notes: tuple[_JournalNote, _JournalNote]
) -> None:
    """Reject either candidate before a supersession journal can be created."""
    for note in notes:
        try:
            document = parse_frontmatter_bytes(note.after_payload)
            _route, missing, invalid = validate_document(note.path, document, manifest)
        except FrontmatterError as exc:
            raise EpistemicValidationError(
                f"manifest validation failed for {note.path}: {exc}"
            ) from exc
        if missing or invalid:
            raise EpistemicValidationError(
                f"manifest validation failed for {note.path}: "
                f"missing={','.join(missing) or '-'} "
                f"invalid={','.join(invalid) or '-'}"
            )


def _write_journal(
    lock: VaultWriteLock, notes: tuple[_JournalNote, _JournalNote]
) -> None:
    try:
        os.stat(_JOURNAL_NAME, dir_fd=lock.locks_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise EpistemicConflictError("a pending epistemic journal already exists")
    data = {
        "version": 1,
        "notes": [
            {
                "path": note.path,
                "before_sha256": note.before_sha256,
                "after_sha256": note.after_sha256,
                "before_payload_b64": base64.b64encode(note.before_payload).decode(
                    "ascii"
                ),
                "after_payload_b64": base64.b64encode(note.after_payload).decode(
                    "ascii"
                ),
            }
            for note in notes
        ],
    }
    descriptor, temporary_name = _create_temp(lock.locks_fd, _JOURNAL_NAME)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True).encode())
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            _JOURNAL_NAME,
            src_dir_fd=lock.locks_fd,
            dst_dir_fd=lock.locks_fd,
        )
        os.fsync(lock.locks_fd)
    except Exception:
        try:
            os.unlink(temporary_name, dir_fd=lock.locks_fd)
        except FileNotFoundError:
            pass
        raise


def _load_journal(lock: VaultWriteLock) -> tuple[_JournalNote, _JournalNote] | None:
    try:
        info = os.stat(_JOURNAL_NAME, dir_fd=lock.locks_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise EpistemicMemoryError("epistemic journal is not a regular file")
    raw, _mode = _read_regular_file(lock.locks_fd, _JOURNAL_NAME)
    try:
        payload = json.loads(raw.decode("utf-8"))
        entries = payload["notes"]
        if (
            payload.get("version") != 1
            or not isinstance(entries, list)
            or len(entries) != 2
        ):
            raise ValueError("unexpected journal shape")
        notes = tuple(
            _JournalNote(
                path=_normalize_note_path(entry["path"]),
                before_sha256=str(entry["before_sha256"]),
                after_sha256=str(entry["after_sha256"]),
                before_payload=base64.b64decode(
                    entry["before_payload_b64"], validate=True
                ),
                after_payload=base64.b64decode(
                    entry["after_payload_b64"], validate=True
                ),
            )
            for entry in entries
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EpistemicMemoryError("epistemic journal is malformed") from exc
    if any(
        _sha256(note.before_payload) != note.before_sha256
        or _sha256(note.after_payload) != note.after_sha256
        for note in notes
    ):
        raise EpistemicMemoryError("epistemic journal payload hash mismatch")
    return cast(tuple[_JournalNote, _JournalNote], notes)


def _remove_journal(lock: VaultWriteLock) -> None:
    try:
        info = os.stat(_JOURNAL_NAME, dir_fd=lock.locks_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise EpistemicMemoryError("epistemic journal is not a regular file")
    os.unlink(_JOURNAL_NAME, dir_fd=lock.locks_fd)
    os.fsync(lock.locks_fd)


def _recover_locked(lock: VaultWriteLock, manifest: VaultManifest) -> bool:
    notes = _load_journal(lock)
    if notes is None:
        return False
    _validate_manifest_payloads(manifest, notes)
    for note in notes:
        current = _read_note(lock, note.path)
        digest = _sha256(current)
        if digest == note.after_sha256:
            continue
        if digest != note.before_sha256:
            raise EpistemicConflictError(
                f"cannot recover {note.path}: content differs from journal hashes"
            )
        _replace_note(
            lock,
            note.path,
            note.after_payload,
            note.before_sha256,
            manifest=manifest,
        )
    for note in notes:
        if _sha256(_read_note(lock, note.path)) != note.after_sha256:
            raise EpistemicMemoryError(
                f"epistemic recovery did not persist {note.path}"
            )
    _remove_journal(lock)
    return True


def _metadata_for(lock: VaultWriteLock, relative_path: str) -> EpistemicMetadata:
    return parse_epistemic_note(_read_note(lock, relative_path))


def _walk_supersedes(
    lock: VaultWriteLock,
    start: str,
    *,
    target: str | None,
    visited: set[str],
    active: set[str],
) -> bool:
    if start in active:
        raise EpistemicValidationError(f"supersedes graph contains a cycle at {start}")
    if start in visited:
        return False
    visited.add(start)
    active.add(start)
    metadata = _metadata_for(lock, start)
    for predecessor in metadata.supersedes:
        if predecessor == target:
            active.remove(start)
            return True
        if _walk_supersedes(
            lock,
            predecessor,
            target=target,
            visited=visited,
            active=active,
        ):
            active.remove(start)
            return True
    active.remove(start)
    return False


def _validate_supersession_graph(
    lock: VaultWriteLock,
    old_path: str,
    new_path: str,
    old_metadata: EpistemicMetadata,
    new_metadata: EpistemicMetadata,
) -> None:
    if old_metadata.scope != new_metadata.scope:
        raise EpistemicValidationError(
            "old and successor notes must have the same epistemic_scope"
        )
    if _walk_supersedes(
        lock,
        old_path,
        target=new_path,
        visited=set(),
        active=set(),
    ):
        raise EpistemicValidationError("supersession would create a cycle")
    has_direct_old = old_path in new_metadata.supersedes
    if not has_direct_old and _walk_supersedes(
        lock,
        new_path,
        target=old_path,
        visited=set(),
        active=set(),
    ):
        raise EpistemicValidationError(
            "successor already reaches the old note through supersedes"
        )
    _walk_supersedes(lock, new_path, target=None, visited=set(), active=set())


def _prepare_supersession_plan(
    old_path: str,
    new_path: str,
    old_before: bytes,
    new_before: bytes,
) -> _SupersessionPlan:
    """Build both reciprocal candidates without modifying the vault."""
    old_metadata = parse_epistemic_note(old_before)
    new_metadata = parse_epistemic_note(new_before)
    if new_metadata.state != "active" or new_metadata.superseded_by is not None:
        raise EpistemicValidationError("successor note must be epistemically active")
    if old_metadata.state == "superseded":
        if (
            old_metadata.superseded_by != new_path
            or old_path not in new_metadata.supersedes
        ):
            raise EpistemicConflictError(
                "old note is already superseded by another note"
            )
        return _SupersessionPlan(
            old_metadata,
            new_metadata,
            (
                _JournalNote(
                    old_path,
                    _sha256(old_before),
                    _sha256(old_before),
                    old_before,
                    old_before,
                ),
                _JournalNote(
                    new_path,
                    _sha256(new_before),
                    _sha256(new_before),
                    new_before,
                    new_before,
                ),
            ),
            changed=False,
        )
    if old_metadata.superseded_by is not None:
        raise EpistemicConflictError("active old note already names a successor")

    supersedes = new_metadata.supersedes
    if old_path not in supersedes:
        supersedes = (*supersedes, old_path)
    old_after = _patch_epistemic_payload(
        old_before,
        {"epistemic_state": "superseded", "superseded_by": new_path},
    )
    new_after = _patch_epistemic_payload(
        new_before,
        {"supersedes": list(supersedes)},
    )
    updated_old = parse_epistemic_note(old_after)
    updated_new = parse_epistemic_note(new_after)
    if updated_old.superseded_by != new_path or old_path not in updated_new.supersedes:
        raise EpistemicMemoryError("supersession metadata did not round-trip")
    return _SupersessionPlan(
        old_metadata,
        new_metadata,
        (
            _JournalNote(
                old_path,
                _sha256(old_before),
                _sha256(old_after),
                old_before,
                old_after,
            ),
            _JournalNote(
                new_path,
                _sha256(new_before),
                _sha256(new_after),
                new_before,
                new_after,
            ),
        ),
        changed=True,
    )


def _read_unlocked_note(vault_path: Path, relative_path: str) -> bytes:
    """Read one preflight note through the common no-symlink reader."""
    try:
        return read_vault_file_bytes(vault_path, vault_path / relative_path)
    except (OSError, UnsafeVaultPathError) as exc:
        raise EpistemicValidationError(
            f"cannot safely preflight {relative_path}: {exc}"
        ) from exc


def _has_pending_journal(vault_path: Path) -> bool:
    """Avoid a preflight write when no prior interrupted transaction exists."""
    try:
        read_vault_file_bytes(vault_path, vault_path / ".locks" / _JOURNAL_NAME)
    except FileNotFoundError:
        return False
    except (OSError, UnsafeVaultPathError) as exc:
        raise EpistemicValidationError(
            f"cannot inspect epistemic journal: {exc}"
        ) from exc
    return True


def _load_vault_manifest(vault_path: Path) -> VaultManifest:
    try:
        return load_manifest_for_vault(vault_path)
    except ManifestValidationError as exc:
        raise EpistemicValidationError(str(exc)) from exc


def recover_pending_supersession(vault_path: Path) -> bool:
    """Roll a durable but unfinished supersession forward, if one exists.

    Non-cooperative editors must be quiesced by the caller before recovery.
    """
    manifest = _load_vault_manifest(vault_path)
    try:
        with vault_write_lock(vault_path) as lock:
            return _recover_locked(lock, manifest)
    except VaultLockError as exc:
        raise EpistemicMemoryError(str(exc)) from exc


def supersede_notes(
    vault_path: Path,
    old_relative_path: str | Path,
    new_relative_path: str | Path,
) -> SupersessionResult:
    """Link a correction pair under the cooperative vault writer lock.

    External non-cooperative editors must be quiesced before this operation;
    descriptor-based full-hash checks protect the cooperative write boundary.
    A detected compare-and-swap conflict raises ``EpistemicConflictError`` and
    retains the journal as recovery evidence.
    """
    old_path = _normalize_note_path(old_relative_path)
    new_path = _normalize_note_path(new_relative_path)
    if old_path == new_path:
        raise EpistemicValidationError("a note cannot supersede itself")
    manifest = _load_vault_manifest(vault_path)
    if not _has_pending_journal(vault_path):
        preflight = _prepare_supersession_plan(
            old_path,
            new_path,
            _read_unlocked_note(vault_path, old_path),
            _read_unlocked_note(vault_path, new_path),
        )
        _validate_manifest_payloads(manifest, preflight.notes)
    try:
        with vault_write_lock(vault_path) as lock:
            _recover_locked(lock, manifest)
            old_before = _read_note(lock, old_path)
            new_before = _read_note(lock, new_path)
            plan = _prepare_supersession_plan(
                old_path, new_path, old_before, new_before
            )
            _validate_supersession_graph(
                lock,
                old_path,
                new_path,
                plan.old_metadata,
                plan.new_metadata,
            )
            _validate_manifest_payloads(manifest, plan.notes)
            if not plan.changed:
                return SupersessionResult(old_path, new_path, changed=False)
            _write_journal(lock, plan.notes)
            for note in plan.notes:
                _replace_note(
                    lock,
                    note.path,
                    note.after_payload,
                    note.before_sha256,
                    manifest=manifest,
                )
            for note in plan.notes:
                if _sha256(_read_note(lock, note.path)) != note.after_sha256:
                    raise EpistemicMemoryError(
                        f"epistemic write did not persist {note.path}"
                    )
            _remove_journal(lock)
    except VaultLockError as exc:
        raise EpistemicMemoryError(str(exc)) from exc
    return SupersessionResult(old_path, new_path, changed=True)
