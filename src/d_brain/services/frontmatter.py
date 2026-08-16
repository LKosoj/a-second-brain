"""Strict, lossless frontmatter helpers for vault migration tooling.

The parser validates YAML but deliberately never serializes an entire header.
Only explicitly requested top-level fields are replaced; the Markdown body is
kept as bytes and is therefore safe to compare before and after a migration.
"""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from d_brain.manifest import VaultManifest
from d_brain.services.vault_lock import (
    VaultLockError,
    VaultWriteLock,
    duplicate_live_vault_write_lock,
    ensure_vault_root_nofollow,
    open_vault_root_nofollow,
    require_live_vault_write_lock,
    vault_write_lock,
)

_TOP_LEVEL_FIELD = re.compile(rb"^[A-Za-z][A-Za-z0-9_-]*:")
_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_SEMANTIC_FIELDS = frozenset({"description", "tags", "status"})
_EPISTEMIC_SEMANTIC_FIELDS = frozenset(
    {
        "epistemic_confidence",
        "epistemic_scope",
        "epistemic_state",
        "epistemic_verification",
    }
)
_ALLOWED_STATUSES = frozenset({"active", "draft", "pending", "done", "inactive"})
_ALLOWED_TIERS = frozenset({"core", "active", "warm", "cold", "archive"})
_DATE_FIELDS = frozenset({"date", "created", "updated", "last_accessed"})
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_LIBC = ctypes.CDLL(None, use_errno=True)


def _is_normalized_tag(value: object) -> bool:
    """Accept lowercase Unicode alphanumeric segments joined by ASCII hyphens."""
    if not isinstance(value, str) or not value or value != value.lower():
        return False
    return all(
        segment and all(character.isalnum() for character in segment)
        for segment in value.split("-")
    )


class FrontmatterError(ValueError):
    """Base error for a malformed or unsafe frontmatter operation."""


class DuplicateKeyError(FrontmatterError):
    """Raised when YAML contains a duplicated mapping key."""


class UnsafeVaultPathError(FrontmatterError):
    """Raised when a migration path escapes the vault or uses a symlink."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys at every mapping depth."""


_UniqueKeyLoader.yaml_implicit_resolvers = {
    initial: [
        (tag, expression)
        for tag, expression in resolvers
        if tag != "tag:yaml.org,2002:timestamp"
    ]
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise FrontmatterError(
                "YAML mapping keys must be hashable scalars"
            ) from exc
        if duplicate:
            raise DuplicateKeyError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


@dataclass(frozen=True)
class FrontmatterDocument:
    """A Markdown document split without changing its body bytes."""

    content: bytes
    header: bytes | None
    body: bytes
    fields: Mapping[str, Any]
    newline: bytes

    @property
    def has_frontmatter(self) -> bool:
        return self.header is not None

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def full_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def frontmatter_sha256(self) -> str | None:
        if self.header is None:
            return None
        return hashlib.sha256(self.header).hexdigest()


@dataclass(frozen=True)
class ProfileRoute:
    """Resolved validation profile for one vault-relative Markdown path."""

    name: str
    required_fields: tuple[str, ...]


_UTF8_BOM = b"\xef\xbb\xbf"


def _strip_bom(content: bytes) -> bytes:
    """Drop a leading UTF-8 BOM so '---' delimiter checks still match it.

    A BOM is a harmless marker some editors (e.g. Notepad "Save As UTF-8")
    prepend to a file. It carries no meaning for our Markdown/YAML content, so
    once a header is being rewritten wholesale we simply do not reproduce it.
    """
    if content.startswith(_UTF8_BOM):
        return content[len(_UTF8_BOM) :]
    return content


def _detect_newline(content: bytes) -> bytes:
    content = _strip_bom(content)
    if content.startswith(b"---\r\n"):
        return b"\r\n"
    if content.startswith(b"---\n"):
        return b"\n"
    if content.startswith(b"---\r"):
        return b"\r"
    first_newline = content.find(b"\n")
    if first_newline > 0 and content[first_newline - 1 : first_newline] == b"\r":
        return b"\r\n"
    if first_newline == -1 and b"\r" in content:
        return b"\r"
    return b"\n"


def _parse_yaml(header: bytes) -> Mapping[str, Any]:
    try:
        text = header.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrontmatterError("frontmatter is not UTF-8") from exc
    try:
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except DuplicateKeyError:
        raise
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FrontmatterError("frontmatter must be a YAML mapping")
    if not all(isinstance(key, str) for key in value):
        raise FrontmatterError("frontmatter keys must be strings")
    return value


def split_frontmatter_bytes(content: bytes) -> tuple[bytes | None, bytes, bytes]:
    """Split Markdown into raw header/body bytes before YAML validation.

    A document without a leading delimiter is valid Markdown with no header.
    A leading delimiter without a matching close delimiter is an error. A
    leading UTF-8 BOM is skipped when looking for the delimiter and is not
    carried into the parsed header or body; a document with no delimiter at
    all keeps its BOM as part of the returned body, unchanged.
    """
    newline = _detect_newline(content)
    unmarked = _strip_bom(content)
    opening = b"---" + newline
    if not unmarked.startswith(opening):
        return None, content, newline

    offset = len(opening)
    closing = newline + b"---" + newline
    closing_index = unmarked.find(closing, offset)
    if closing_index == -1:
        trailing = newline + b"---"
        if unmarked.endswith(trailing):
            closing_index = len(unmarked) - len(trailing)
            header = unmarked[offset:closing_index]
            body = b""
        else:
            raise FrontmatterError("frontmatter is missing a closing '---'")
    else:
        header = unmarked[offset:closing_index]
        body = unmarked[closing_index + len(closing) :]
    return header, body, newline


def parse_frontmatter_bytes(content: bytes) -> FrontmatterDocument:
    """Split a Markdown byte string and strictly parse its frontmatter."""
    header, body, newline = split_frontmatter_bytes(content)
    fields = {} if header is None else _parse_yaml(header)
    return FrontmatterDocument(content, header, body, fields, newline)


def read_frontmatter(path: Path) -> FrontmatterDocument:
    """Read one Markdown file as bytes so body equality is meaningful."""
    try:
        return parse_frontmatter_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise FrontmatterError(f"cannot read {path}: {exc}") from exc


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            raise FrontmatterError("top-level string values must be one line")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        rendered = str(yaml.safe_dump(value, default_flow_style=True).strip())
        # PyYAML adds an explicit document-end marker for scalar documents.
        # It is valid only at the end of a YAML stream, not between fields.
        return rendered.removesuffix("\n...")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return str(
            yaml.safe_dump(
                value,
                allow_unicode=True,
                default_flow_style=True,
                sort_keys=False,
                width=1000,
            ).strip()
        )
    raise FrontmatterError("top-level patch values must be scalar or list[str]")


def _inline_comment_suffix(line: bytes) -> bytes:
    """Return exact spacing/comment suffix, ignoring hashes inside quotes."""
    colon = line.find(b":")
    if colon < 0:
        return b""
    end = len(line.rstrip(b"\r\n"))
    single_quoted = False
    double_quoted = False
    index = colon + 1
    while index < end:
        character = line[index]
        if single_quoted:
            if character == ord("'"):
                if index + 1 < end and line[index + 1] == ord("'"):
                    index += 2
                    continue
                single_quoted = False
        elif double_quoted:
            if character == ord("\\"):
                index += 2
                continue
            if character == ord('"'):
                double_quoted = False
        elif character == ord("'"):
            single_quoted = True
        elif character == ord('"'):
            double_quoted = True
        elif character == ord("#") and (
            index == colon + 1 or line[index - 1] in b" \t"
        ):
            suffix_start = index
            while suffix_start > colon + 1 and line[suffix_start - 1] in b" \t":
                suffix_start -= 1
            return line[suffix_start:end]
        index += 1
    return b""


def _field_value_end_lines(header: bytes) -> dict[str, int]:
    """Return YAML value end lines so value-owned comments are not detached."""
    try:
        root = yaml.compose(header.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(root, yaml.nodes.MappingNode):
        raise FrontmatterError("frontmatter must be a YAML mapping")
    result: dict[str, int] = {}
    for key_node, value_node in root.value:
        if isinstance(key_node.value, str):
            result[key_node.value] = value_node.end_mark.line
    return result


def _field_spans(header: bytes) -> dict[str, tuple[int, int, bytes, bytes]]:
    lines = header.splitlines(keepends=True)
    value_end_lines = _field_value_end_lines(header)
    starts: list[tuple[str, int, int]] = []
    offset = 0
    for line_index, line in enumerate(lines):
        match = _TOP_LEVEL_FIELD.match(line)
        if match:
            field = line.split(b":", 1)[0].decode("ascii")
            starts.append((field, offset, line_index))
        offset += len(line)
    spans: dict[str, tuple[int, int, bytes, bytes]] = {}
    for index, (field, start, line_index) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else len(header)
        next_line_index = (
            starts[index + 1][2] if index + 1 < len(starts) else len(lines)
        )
        comment_start = max(
            line_index + 1,
            value_end_lines.get(field, line_index + 1),
        )
        preserved_lines: list[bytes] = []
        for candidate_index in range(line_index + 1, next_line_index):
            line = lines[candidate_index]
            top_level_comment = line.startswith(b"#")
            after_value = candidate_index >= comment_start
            if top_level_comment or (
                after_value
                and (not line.strip() or line.lstrip(b" \t").startswith(b"#"))
            ):
                preserved_lines.append(line)
        preserved = b"".join(preserved_lines)
        spans[field] = (
            start,
            end,
            preserved,
            _inline_comment_suffix(lines[line_index]),
        )
    return spans


def patch_frontmatter_bytes(content: bytes, updates: Mapping[str, Any]) -> bytes:
    """Patch named top-level fields without reformatting unrelated YAML/body."""
    if not updates:
        return content
    for field in updates:
        if not _FIELD_NAME.fullmatch(field):
            raise FrontmatterError(f"unsafe frontmatter field name: {field!r}")

    document = parse_frontmatter_bytes(content)
    newline = document.newline
    rendered = {
        field: f"{field}: {_render_value(value)}".encode()
        for field, value in sorted(updates.items())
    }
    if document.header is None:
        header = b"".join(rendered[field] + newline for field in sorted(rendered))
        # ``split_frontmatter_bytes`` deliberately leaves a BOM in the body
        # of a document that has no delimiter at all -- it is a lossless
        # split, and there is no header for the BOM to sit in front of.
        # Here there is one, and prepending it in front of a body that still
        # carries the marker moved that marker into the middle of the file,
        # right behind the closing ``---``, where it shows up as a stray
        # character on the note's first line. ``_strip_bom``'s own docstring
        # already states the rule this branch was missing: once a header is
        # being written wholesale, the marker is simply not reproduced.
        return (
            b"---" + newline + header + b"---" + newline + _strip_bom(document.body)
        )

    spans = _field_spans(document.header)
    header = document.header
    replacements: list[tuple[int, int, bytes]] = []
    for field, value in rendered.items():
        span = spans.get(field)
        if span is None:
            suffix = b"" if not header or header.endswith(newline) else newline
            header += suffix + value + newline
        else:
            replacements.append((span[0], span[1], value + span[3] + newline + span[2]))
    for start, end, value in sorted(replacements, reverse=True):
        header = header[:start] + value + header[end:]
    separator = b"" if not header or header.endswith(newline) else newline
    candidate = b"---" + newline + header + separator + b"---" + newline + document.body
    reparsed = parse_frontmatter_bytes(candidate)
    for field, value in updates.items():
        if reparsed.fields.get(field) != value:
            raise FrontmatterError(f"patched field did not round-trip: {field}")
    if reparsed.body != document.body:
        raise FrontmatterError("frontmatter patch changed Markdown body")
    return candidate


def resolve_vault_markdown_path(vault_path: Path, relative_path: str | Path) -> Path:
    """Return an existing regular Markdown file strictly inside ``vault_path``."""
    root = Path(vault_path).resolve()
    relative = PurePosixPath(str(relative_path))
    if relative.is_absolute() or ".." in relative.parts or str(relative) in {"", "."}:
        raise UnsafeVaultPathError("path must be a non-empty vault-relative path")
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeVaultPathError("path escapes vault") from exc
    if candidate.suffix.lower() != ".md":
        raise UnsafeVaultPathError("path must name a Markdown file")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeVaultPathError("symlink paths are not accepted")
    if not candidate.is_file():
        raise UnsafeVaultPathError("path must name an existing regular file")
    if candidate.resolve().parent != root and root not in candidate.resolve().parents:
        raise UnsafeVaultPathError("resolved path escapes vault")
    return candidate


def _read_all_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _inode_identity(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _read_regular_at(
    parent_fd: int, name: str
) -> tuple[bytes, tuple[int, int, int, int], str]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeVaultPathError("vault file target is not a regular file")
        content = _read_all_descriptor(descriptor)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise UnsafeVaultPathError("vault file target changed while reading")
        return content, _stat_identity(after), hashlib.sha256(content).hexdigest()
    finally:
        os.close(descriptor)


def _verify_regular_at(
    parent_fd: int,
    source_name: str,
    expected_identity: tuple[int, int, int, int],
    expected_hash: str,
) -> None:
    source_stat = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(source_stat.st_mode)
        or _stat_identity(source_stat) != expected_identity
    ):
        raise UnsafeVaultPathError("write source changed before commit")
    source_fd = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        opened_stat = os.fstat(source_fd)
        if _stat_identity(opened_stat) != expected_identity:
            raise UnsafeVaultPathError("write source changed before commit")
        source_hash = hashlib.sha256(_read_all_descriptor(source_fd)).hexdigest()
        if (
            _stat_identity(os.fstat(source_fd)) != expected_identity
            or source_hash != expected_hash
        ):
            raise UnsafeVaultPathError("write source changed before commit")
    finally:
        os.close(source_fd)


def _verify_open_descriptor(
    candidate_fd: int,
    expected_identity: tuple[int, int, int, int],
    expected_hash: str,
    *,
    label: str,
) -> None:
    if _stat_identity(os.fstat(candidate_fd)) != expected_identity:
        raise UnsafeVaultPathError(f"{label} descriptor changed before commit")
    os.lseek(candidate_fd, 0, os.SEEK_SET)
    candidate_hash = hashlib.sha256(_read_all_descriptor(candidate_fd)).hexdigest()
    if (
        _stat_identity(os.fstat(candidate_fd)) != expected_identity
        or candidate_hash != expected_hash
    ):
        raise UnsafeVaultPathError(f"{label} descriptor changed before commit")


def _verify_candidate_descriptor(
    candidate_fd: int,
    expected_identity: tuple[int, int, int, int],
    expected_hash: str,
) -> None:
    _verify_open_descriptor(
        candidate_fd,
        expected_identity,
        expected_hash,
        label="write candidate",
    )


def _link_name_noreplace(
    source_fd: int, source_name: str, destination_fd: int, destination_name: str
) -> None:
    """Hard-link one existing name into another directory without replacing.

    Publication used to link the open descriptor itself through
    ``linkat(..., AT_EMPTY_PATH)``. That form needs ``CAP_DAC_READ_SEARCH``, so
    every vault write raised ``ENOENT`` for an unprivileged process -- including
    the systemd *user* unit this project ships and documents. Linking by name
    needs no capability. It reopens a name the caller already resolved, so each
    call site closes that window by comparing the published inode against the
    identity it verified through its own descriptor.
    """
    os.link(
        source_name,
        destination_name,
        src_dir_fd=source_fd,
        dst_dir_fd=destination_fd,
        follow_symlinks=False,
    )


def _rename_noreplace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    renameat2 = getattr(_LIBC, "renameat2", None)
    if renameat2 is None and sys.platform != "darwin":
        # On Linux without glibc >= 2.28 the non-atomic fallback is unsafe --
        # silently taking it would mean data loss on stale-NFS-style failures.
        # Fail closed and let the operator know the kernel/libc is too old.
        raise UnsafeVaultPathError(
            "renameat2 is unavailable on this platform and the non-atomic "
            f"fallback is only supported on Darwin; sys.platform={sys.platform!r}"
        )
    if renameat2 is not None:
        ctypes.set_errno(0)
        result = renameat2(
            source_fd,
            ctypes.c_char_p(os.fsencode(source_name)),
            destination_fd,
            ctypes.c_char_p(os.fsencode(destination_name)),
            _RENAME_NOREPLACE,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), destination_name)
        return

    # macOS / BSD fallback: ``rename(2)`` overwrites the destination on POSIX,
    # so ``os.rename`` cannot give us no-replace semantics on its own. The
    # standard portable dance is to hard-link the source to the destination
    # (which fails with EEXIST if the destination already exists) and then
    # unlink the source. ``os.link`` with ``src_dir_fd``/``dst_dir_fd``
    # surfaces ``FileExistsError`` on macOS, matching the Linux RENAME_NOREPLACE
    # contract.
    os.link(
        source_name,
        destination_name,
        src_dir_fd=source_fd,
        dst_dir_fd=destination_fd,
        follow_symlinks=False,
    )
    os.unlink(source_name, dir_fd=source_fd)


def _rename_exchange(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    renameat2 = getattr(_LIBC, "renameat2", None)
    if renameat2 is None and sys.platform != "darwin":
        raise UnsafeVaultPathError(
            "renameat2 is unavailable on this platform and the non-atomic "
            f"fallback is only supported on Darwin; sys.platform={sys.platform!r}"
        )
    if renameat2 is not None:
        ctypes.set_errno(0)
        result = renameat2(
            source_fd,
            ctypes.c_char_p(os.fsencode(source_name)),
            destination_fd,
            ctypes.c_char_p(os.fsencode(destination_name)),
            _RENAME_EXCHANGE,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), destination_name)
        return

    # macOS / BSD fallback: POSIX has no RENAME_EXCHANGE equivalent, so simulate
    # it with three renames through a temporary name in the staging directory.
    # The sequence is **not** atomic. If ``os.rename`` raises mid-sequence, the
    # ``except BaseException`` block tries to undo the first swap by renaming
    # ``swap_name`` back over ``destination_name``; if that recovery rename
    # itself fails (a narrow window, but a real double I/O failure in one
    # directory), the target can be lost from this directory. The writer
    # additionally keeps a recovery hard link for the original source
    # (``recovery_name``, set up at ``frontmatter.py:897`` before this function
    # is called); on a crash window between steps, ``_rollback_publication``
    # re-publishes the target from that guard link, but only when
    # ``published and not durable_commit``. Crash-window safety therefore
    # depends on the directory's I/O being healthy enough to surface any
    # mid-sequence failure as a raised exception, which is the common case on
    # local filesystems.
    #
    # Pre-state:
    #   source_fd / source_name      -> new content (candidate)
    #   destination_fd / destination_name -> old content (target)
    # Post-state:
    #   destination_fd / destination_name -> new content
    #   source_fd / source_name      -> old content
    swap_name = f"swap-{os.urandom(8).hex()}"
    os.rename(
        destination_name,
        swap_name,
        src_dir_fd=destination_fd,
        dst_dir_fd=source_fd,
    )
    try:
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
        )
    except BaseException:
        try:
            os.rename(
                swap_name,
                destination_name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
            )
        except OSError:
            pass
        raise
    os.rename(
        swap_name,
        source_name,
        src_dir_fd=source_fd,
        dst_dir_fd=source_fd,
    )


@dataclass(frozen=True)
class _VaultWriteCapability:
    source: VaultWriteLock
    root_fd: int
    locks_fd: int


def _validate_lock_namespace(capability: _VaultWriteCapability) -> None:
    try:
        require_live_vault_write_lock(capability.source)
        supplied_root = os.fstat(capability.root_fd)
        supplied_locks = os.fstat(capability.locks_fd)
        if not stat.S_ISDIR(supplied_root.st_mode) or not stat.S_ISDIR(
            supplied_locks.st_mode
        ):
            raise UnsafeVaultPathError(
                "vault-write capability descriptors are not directories"
            )
        canonical_locks_fd = os.open(
            ".locks",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=capability.root_fd,
        )
        try:
            if _inode_identity(os.fstat(canonical_locks_fd)) != _inode_identity(
                supplied_locks
            ):
                raise UnsafeVaultPathError(
                    "canonical .locks changed while writer lock was held"
                )
        finally:
            os.close(canonical_locks_fd)
    except VaultLockError as exc:
        raise UnsafeVaultPathError(str(exc)) from exc
    except OSError as exc:
        raise UnsafeVaultPathError(
            f"cannot validate vault-write lock namespace: {exc}"
        ) from exc


def _name_inode(parent_fd: int, name: str) -> tuple[int, int] | None:
    try:
        value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _inode_identity(value)


def _unlink_name_if_inode(
    parent_fd: int, name: str, expected_inode: tuple[int, int]
) -> bool:
    if _name_inode(parent_fd, name) != expected_inode:
        return False
    os.unlink(name, dir_fd=parent_fd)
    return True


def _rollback_publication(
    parent_fd: int,
    target_name: str,
    stage_fd: int,
    candidate_name: str | None,
    recovery_name: str | None,
    candidate_fd: int,
    guard_fd: int | None,
    guard_identity: tuple[int, int, int, int] | None,
    guard_hash: str | None,
) -> tuple[bool, bool]:
    """Restore only from held descriptors; return ``(restored, preserve_stage)``."""
    candidate_inode = _inode_identity(os.fstat(candidate_fd))
    if _name_inode(parent_fd, target_name) != candidate_inode:
        return False, True
    guard_inode = _inode_identity(os.fstat(guard_fd)) if guard_fd is not None else None
    preserve_stage = False
    restore_name: str | None = None
    if guard_inode is not None:
        preserve_stage = any(
            name is None or _name_inode(stage_fd, name) != guard_inode
            for name in (candidate_name, recovery_name)
        )
        restore_name = next(
            (
                name
                for name in (recovery_name, candidate_name)
                if name is not None and _name_inode(stage_fd, name) == guard_inode
            ),
            None,
        )
        if restore_name is None:
            # Restoring now means linking a name, not a bare descriptor, and
            # the guard is reachable by name only from staging. Both staging
            # names losing the guard inode means the staging directory was
            # rewritten mid-transaction, so leave the published file in place
            # and report IN_DOUBT rather than unlinking with nothing to put
            # back. ``recovery_name`` is created before publication and removed
            # only after ``durable_commit``, so an untampered rollback always
            # finds it.
            return False, True
    try:
        os.unlink(target_name, dir_fd=parent_fd)
        if guard_fd is not None:
            assert guard_identity is not None
            assert guard_hash is not None
            assert restore_name is not None
            _verify_open_descriptor(
                guard_fd,
                guard_identity,
                guard_hash,
                label="write guard",
            )
            _link_name_noreplace(stage_fd, restore_name, parent_fd, target_name)
            _verify_regular_at(parent_fd, target_name, guard_identity, guard_hash)
        os.fsync(stage_fd)
        os.fsync(parent_fd)
        return True, preserve_stage
    except (OSError, UnsafeVaultPathError):
        return False, True


def _cleanup_staging_directory(
    parent_fd: int,
    stage_name: str | None,
    stage_fd: int | None,
    stage_identity: tuple[int, int] | None,
    *,
    preserve_contents: bool,
) -> None:
    if stage_fd is not None:
        try:
            opened_stage = os.fstat(stage_fd)
            if (
                stage_identity is not None
                and stat.S_ISDIR(opened_stage.st_mode)
                and _inode_identity(opened_stage) == stage_identity
                and not preserve_contents
            ):
                for entry in os.listdir(stage_fd):
                    try:
                        os.unlink(entry, dir_fd=stage_fd)
                    except IsADirectoryError:
                        try:
                            os.rmdir(entry, dir_fd=stage_fd)
                        except OSError:
                            pass
                    except OSError:
                        pass
            elif preserve_contents:
                os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
    if preserve_contents or stage_name is None or stage_identity is None:
        return
    try:
        current = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(current.st_mode) and _inode_identity(current) == stage_identity:
            os.rmdir(stage_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError:
        pass


def _atomic_write_at(
    parent_fd: int,
    target_name: str,
    content: bytes,
    *,
    expected_source: tuple[tuple[int, int, int, int], str] | None = None,
    expected_full_sha256: str | None = None,
    require_absent: bool = False,
    capability: _VaultWriteCapability | None = None,
) -> None:
    stage_name: str | None = None
    stage_fd: int | None = None
    stage_identity: tuple[int, int] | None = None
    candidate_fd: int | None = None
    source_fd: int | None = None
    source_identity: tuple[int, int, int, int] | None = None
    source_hash: str | None = None
    candidate_name: str | None = None
    recovery_name: str | None = None
    published = False
    durable_commit = False
    preserve_stage_contents = False
    mode = 0o600
    # The candidate below is created inside a private staging directory whose
    # setgid bit is deliberately cleared, so it is born with the *writing
    # process's* group rather than the group the target directory hands out.
    # A file created directly in the target directory would inherit that
    # group, and on a shared vault (setgid directory, group-readable notes)
    # losing it means a root-owned service publishes notes the vault's own
    # group can no longer read. Carry the group across explicitly: from the
    # file being replaced when there is one, from the target directory
    # otherwise -- the same source ``mode`` comes from.
    target_gid = os.fstat(parent_fd).st_gid
    try:
        try:
            source_stat = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            source_stat = None
        if source_stat is not None:
            if require_absent:
                raise UnsafeVaultPathError("atomic write target already exists")
            if not stat.S_ISREG(source_stat.st_mode):
                raise UnsafeVaultPathError("atomic writes require a regular target")
            source_fd = os.open(
                target_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            opened_stat = os.fstat(source_fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise UnsafeVaultPathError("atomic writes require a regular target")
            source_identity = _stat_identity(opened_stat)
            if _stat_identity(source_stat) != source_identity:
                raise UnsafeVaultPathError("atomic write target changed before reading")
            source_bytes = _read_all_descriptor(source_fd)
            final_opened_stat = os.fstat(source_fd)
            if _stat_identity(final_opened_stat) != source_identity:
                raise UnsafeVaultPathError("atomic write target changed while reading")
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            mode = opened_stat.st_mode & 0o7777
            target_gid = opened_stat.st_gid
        if expected_source is not None and (
            source_identity != expected_source[0] or source_hash != expected_source[1]
        ):
            raise UnsafeVaultPathError("atomic write source changed before commit")
        if expected_full_sha256 is not None and source_hash != expected_full_sha256:
            raise UnsafeVaultPathError(
                "atomic write source does not match expected_full_sha256"
            )
        stage_name = f".dbrain-write-{os.getpid()}-{os.urandom(16).hex()}.stage"
        os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
        stage_stat = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(stage_stat.st_mode):
            raise UnsafeVaultPathError("write staging path is not a directory")
        stage_identity = _inode_identity(stage_stat)
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        opened_stage_stat = os.fstat(stage_fd)
        if (
            not stat.S_ISDIR(opened_stage_stat.st_mode)
            or _inode_identity(opened_stage_stat) != stage_identity
        ):
            raise UnsafeVaultPathError("write staging directory changed after create")
        os.fchmod(stage_fd, 0o700)
        if stat.S_IMODE(os.fstat(stage_fd).st_mode) != 0o700:
            raise UnsafeVaultPathError("write staging directory is not private")
        # Staged under a name from the start, rather than as an ``O_TMPFILE``
        # anonymous file linked in later. The anonymous form bought invisibility
        # that this writer never had anyway -- the ``.dbrain-write-*`` staging
        # directory below is a real directory entry -- and it could only be
        # published through the privileged ``AT_EMPTY_PATH`` link. The staging
        # directory is private (0700) and checked for that on every commit.
        candidate_name = f"candidate-{os.urandom(8).hex()}"
        candidate_fd = os.open(
            candidate_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=stage_fd,
        )
        view = memoryview(content)
        while view:
            view = view[os.write(candidate_fd, view) :]
        if os.fstat(candidate_fd).st_gid != target_gid:
            try:
                os.fchown(candidate_fd, -1, target_gid)
            except OSError:
                # A writer that is not a member of that group cannot hand the
                # file over; the write itself must not fail over a permission
                # detail, so the candidate keeps its own group.
                pass
        # After ``fchown``, which clears setuid/setgid on the file.
        os.fchmod(candidate_fd, mode)
        os.fsync(candidate_fd)
        candidate_identity = _stat_identity(os.fstat(candidate_fd))
        candidate_hash = hashlib.sha256(content).hexdigest()
        if capability is not None:
            _validate_lock_namespace(capability)
        current_stage_stat = os.fstat(stage_fd)
        if (
            not stat.S_ISDIR(current_stage_stat.st_mode)
            or _inode_identity(current_stage_stat) != stage_identity
            or stat.S_IMODE(current_stage_stat.st_mode) != 0o700
        ):
            raise UnsafeVaultPathError("write staging directory changed before commit")
        if source_identity is not None:
            assert source_fd is not None
            assert source_hash is not None
            recovery_name = f"recovery-{os.urandom(8).hex()}"
            _link_name_noreplace(parent_fd, target_name, stage_fd, recovery_name)
            # The guard was linked by name, so prove the name still pointed at
            # the descriptor this call already read and hashed.
            if _name_inode(stage_fd, recovery_name) != source_identity[:2]:
                raise UnsafeVaultPathError("write guard link does not match the target")
            os.fsync(stage_fd)
            _verify_candidate_descriptor(
                candidate_fd, candidate_identity, candidate_hash
            )
            _verify_open_descriptor(
                source_fd,
                source_identity,
                source_hash,
                label="write guard",
            )
            _verify_regular_at(parent_fd, target_name, source_identity, source_hash)
            if capability is not None:
                _validate_lock_namespace(capability)
            _rename_exchange(stage_fd, candidate_name, parent_fd, target_name)
            published = True
            _verify_regular_at(
                parent_fd, target_name, candidate_identity, candidate_hash
            )
            _verify_regular_at(stage_fd, candidate_name, source_identity, source_hash)
            _verify_open_descriptor(
                source_fd,
                source_identity,
                source_hash,
                label="write guard",
            )
            if capability is not None:
                _validate_lock_namespace(capability)
            os.fsync(stage_fd)
            os.fsync(parent_fd)
            durable_commit = True
            try:
                source_inode = _inode_identity(os.fstat(source_fd))
                removed_candidate_slot = _unlink_name_if_inode(
                    stage_fd, candidate_name, source_inode
                )
                if not removed_candidate_slot:
                    preserve_stage_contents = True
                else:
                    removed_recovery = _unlink_name_if_inode(
                        stage_fd, recovery_name, source_inode
                    )
                    if not removed_recovery:
                        preserve_stage_contents = True
                if not preserve_stage_contents:
                    os.fsync(stage_fd)
            except (OSError, UnsafeVaultPathError):
                preserve_stage_contents = True
        else:
            if capability is not None:
                _validate_lock_namespace(capability)
            _verify_candidate_descriptor(
                candidate_fd, candidate_identity, candidate_hash
            )
            try:
                _rename_noreplace(stage_fd, candidate_name, parent_fd, target_name)
            except FileExistsError as exc:
                raise UnsafeVaultPathError(
                    "target appeared before no-replace publication"
                ) from exc
            published = True
            _verify_regular_at(
                parent_fd, target_name, candidate_identity, candidate_hash
            )
            if capability is not None:
                _validate_lock_namespace(capability)
            os.fsync(parent_fd)
            durable_commit = True
    except BaseException as exc:
        if published and not durable_commit:
            assert stage_fd is not None
            assert candidate_fd is not None
            restored, preserve_stage = _rollback_publication(
                parent_fd,
                target_name,
                stage_fd,
                candidate_name,
                recovery_name,
                candidate_fd,
                source_fd,
                source_identity,
                source_hash,
            )
            preserve_stage_contents = preserve_stage_contents or preserve_stage
            if not restored:
                preserve_stage_contents = True
                raise UnsafeVaultPathError(
                    f"write transaction IN_DOUBT at {stage_name}"
                ) from exc
            if preserve_stage_contents:
                raise UnsafeVaultPathError(
                    f"write rolled back; conflicting entry preserved at {stage_name}"
                ) from exc
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if candidate_fd is not None:
            os.close(candidate_fd)
        try:
            _cleanup_staging_directory(
                parent_fd,
                stage_name,
                stage_fd,
                stage_identity,
                preserve_contents=preserve_stage_contents,
            )
        except OSError:
            if not durable_commit:
                raise


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Publish one regular file through descriptor-based fail-closed writes.

    New targets use ``RENAME_NOREPLACE`` publication. Existing targets use
    ``RENAME_EXCHANGE`` with a staged recovery link, and retain that guard
    until candidate verification plus directory fsyncs complete.

    This protects cooperative writers and accidental namespace races. It is not
    a security boundary against arbitrary same-UID/root processes, which can
    mutate open files directly and therefore must be quiesced. A detected
    post-publication mismatch is rolled back or reported as ``IN_DOUBT``.
    """
    target = Path(path)
    parent = target.parent
    parent_fd: int | None = None
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _atomic_write_at(parent_fd, target.name, content)
    except OSError as exc:
        raise UnsafeVaultPathError(
            f"cannot atomically write vault path: {exc}"
        ) from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


@dataclass(frozen=True)
class _OpenedVaultRoot:
    path: Path
    fd: int
    identity: tuple[int, int]


@contextmanager
def _open_vault_root_once(
    vault_path: Path,
    *,
    create: bool = False,
) -> Iterator[_OpenedVaultRoot]:
    root_fd: int | None = None
    try:
        opener = ensure_vault_root_nofollow if create else open_vault_root_nofollow
        root, root_fd = opener(vault_path)
        opened_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(opened_stat.st_mode):
            raise UnsafeVaultPathError("vault root must be a directory")
    except VaultLockError as exc:
        raise UnsafeVaultPathError(str(exc)) from exc
    except OSError as exc:
        raise UnsafeVaultPathError(f"cannot open vault root safely: {exc}") from exc
    assert root_fd is not None
    try:
        yield _OpenedVaultRoot(
            path=root,
            fd=root_fd,
            identity=_inode_identity(opened_stat),
        )
    finally:
        os.close(root_fd)


def require_safe_vault_root(vault_path: Path) -> Path:
    """Reject lexical vault paths containing a symlink before any side effect."""
    root_fd: int | None = None
    try:
        root, root_fd = ensure_vault_root_nofollow(vault_path)
        return root
    except VaultLockError as exc:
        raise UnsafeVaultPathError(str(exc)) from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _vault_relative_file_path(vault_path: Path, path: Path) -> str:
    root = Path(vault_path).absolute()
    target = Path(path)
    if ".." in target.parts:
        raise UnsafeVaultPathError("path escapes vault")
    if target.is_absolute():
        candidate = target
    else:
        cwd_resolved = target.absolute()
        try:
            cwd_resolved.relative_to(root)
            candidate = cwd_resolved
        except ValueError:
            candidate = root / target
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeVaultPathError("path escapes vault") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise UnsafeVaultPathError("path must name a vault-relative file")
    return relative.as_posix()


@contextmanager
def _open_vault_file_parent(
    root_fd: int,
    relative_path: str,
    *,
    create: bool,
) -> Iterator[tuple[int, str]]:
    relative = PurePosixPath(relative_path)
    parts = relative.parts
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or relative.is_absolute()
    ):
        raise UnsafeVaultPathError("path must be a non-empty vault-relative path")
    current_fd = os.dup(root_fd)
    try:
        try:
            for part in parts[:-1]:
                try:
                    child_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current_fd,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    created = False
                    try:
                        os.mkdir(part, 0o777, dir_fd=current_fd)
                        created = True
                    except FileExistsError:
                        pass
                    if created:
                        os.fsync(current_fd)
                    child_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current_fd,
                    )
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    os.close(child_fd)
                    raise UnsafeVaultPathError("vault file parent is not a directory")
                os.close(current_fd)
                current_fd = child_fd
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise UnsafeVaultPathError(
                f"cannot access vault file path safely: {exc}"
            ) from exc
        yield current_fd, parts[-1]
    finally:
        os.close(current_fd)


def _vault_relative_markdown_path(vault_path: Path, path: Path) -> str:
    relative = _vault_relative_file_path(vault_path, path)
    if PurePosixPath(relative).suffix.lower() != ".md":
        raise UnsafeVaultPathError("path must name a Markdown file")
    return relative


@contextmanager
def _open_vault_markdown_parent(
    lock: _VaultWriteCapability,
    relative_path: str,
    *,
    create: bool,
) -> Iterator[tuple[int, str]]:
    try:
        with _open_vault_file_parent(
            lock.root_fd,
            relative_path,
            create=create,
        ) as opened:
            yield opened
    except FileNotFoundError:
        raise UnsafeVaultPathError("vault Markdown parent does not exist") from None
    except OSError as exc:
        raise UnsafeVaultPathError(
            f"cannot access vault Markdown path safely: {exc}"
        ) from exc


def _duplicate_validated_existing_lock(
    root: _OpenedVaultRoot, existing_lock: VaultWriteLock
) -> _VaultWriteCapability:
    root_fd: int | None = None
    locks_fd: int | None = None
    expected_locks_fd: int | None = None
    try:
        root_fd, locks_fd = duplicate_live_vault_write_lock(existing_lock)
        supplied_root_stat = os.fstat(root_fd)
        supplied_locks_stat = os.fstat(locks_fd)
        if not stat.S_ISDIR(supplied_root_stat.st_mode) or not stat.S_ISDIR(
            supplied_locks_stat.st_mode
        ):
            raise UnsafeVaultPathError(
                "existing vault lock descriptors must be directories"
            )
        if _inode_identity(supplied_root_stat) != root.identity:
            raise UnsafeVaultPathError(
                "existing vault lock root descriptor does not match vault"
            )
        expected_locks_fd = os.open(
            ".locks",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root.fd,
        )
        expected_locks_stat = os.fstat(expected_locks_fd)
        supplied_locks_stat = os.fstat(locks_fd)
        if not stat.S_ISDIR(supplied_locks_stat.st_mode) or _inode_identity(
            supplied_locks_stat
        ) != _inode_identity(expected_locks_stat):
            raise UnsafeVaultPathError(
                "existing vault lock directory descriptor does not match vault"
            )
        validated = _VaultWriteCapability(
            source=existing_lock,
            root_fd=root_fd,
            locks_fd=locks_fd,
        )
        root_fd = None
        locks_fd = None
        return validated
    except VaultLockError as exc:
        raise UnsafeVaultPathError(str(exc)) from exc
    except OSError as exc:
        raise UnsafeVaultPathError(
            f"existing vault lock descriptor is invalid: {exc}"
        ) from exc
    finally:
        for descriptor in (
            expected_locks_fd,
            root_fd,
            locks_fd,
        ):
            if descriptor is not None:
                os.close(descriptor)


@contextmanager
def vault_markdown_write_lock(
    root: _OpenedVaultRoot, existing_lock: VaultWriteLock | None = None
) -> Iterator[_VaultWriteCapability]:
    """Enter the shared cooperative lock, or reuse the caller's lock."""
    if existing_lock is not None:
        if existing_lock.root != root.path:
            raise UnsafeVaultPathError("existing vault lock belongs to another vault")
        validated_lock = _duplicate_validated_existing_lock(root, existing_lock)
        try:
            yield validated_lock
        finally:
            os.close(validated_lock.locks_fd)
            os.close(validated_lock.root_fd)
        return
    with vault_write_lock(root.path, _root_fd=root.fd) as lock:
        validated_lock = _duplicate_validated_existing_lock(root, lock)
        try:
            yield validated_lock
        finally:
            os.close(validated_lock.locks_fd)
            os.close(validated_lock.root_fd)


def write_vault_file_bytes(
    vault_path: Path,
    path: Path,
    content: bytes,
    *,
    expected_full_sha256: str | None = None,
    require_absent: bool = False,
) -> None:
    """Atomically write one arbitrary vault file through a pinned root fd."""
    try:
        with _open_vault_root_once(vault_path, create=True) as root:
            relative = _vault_relative_file_path(root.path, path)
            with vault_markdown_write_lock(root) as lock:
                with _open_vault_file_parent(
                    lock.root_fd,
                    relative,
                    create=True,
                ) as (parent_fd, target_name):
                    _atomic_write_at(
                        parent_fd,
                        target_name,
                        content,
                        expected_full_sha256=expected_full_sha256,
                        require_absent=require_absent,
                        capability=lock,
                    )
    except UnsafeVaultPathError:
        raise
    except OSError as exc:
        raise UnsafeVaultPathError(f"cannot write vault file safely: {exc}") from exc


def write_vault_file_text(
    vault_path: Path,
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    expected_full_sha256: str | None = None,
    require_absent: bool = False,
) -> None:
    """Encode and atomically write one arbitrary text file inside the vault."""
    write_vault_file_bytes(
        vault_path,
        path,
        content.encode(encoding),
        expected_full_sha256=expected_full_sha256,
        require_absent=require_absent,
    )


def read_vault_file_bytes(vault_path: Path, path: Path) -> bytes:
    """Read one regular vault file through a pinned root fd without symlinks."""
    with _open_vault_root_once(vault_path) as root:
        relative = _vault_relative_file_path(root.path, path)
        with _open_vault_file_parent(
            root.fd,
            relative,
            create=False,
        ) as (parent_fd, target_name):
            content, _identity, _content_hash = _read_regular_at(
                parent_fd,
                target_name,
            )
            return content


def read_vault_file_text(
    vault_path: Path,
    path: Path,
    *,
    encoding: str = "utf-8",
) -> str:
    """Decode one descriptor-safe regular file inside the vault."""
    return read_vault_file_bytes(vault_path, path).decode(encoding)


@contextmanager
def vault_relative_file_lock(
    vault_path: Path,
    path: Path,
    *,
    blocking: bool = True,
) -> Iterator[None]:
    """Hold an advisory lock file opened relative to one pinned vault root."""
    with _open_vault_root_once(vault_path, create=True) as root:
        relative = _vault_relative_file_path(root.path, path)
        with _open_vault_file_parent(
            root.fd,
            relative,
            create=True,
        ) as (parent_fd, target_name):
            try:
                descriptor = os.open(
                    target_name,
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise UnsafeVaultPathError(
                    f"cannot open vault lock file safely: {exc}"
                ) from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise UnsafeVaultPathError(
                        "vault lock target is not a regular file"
                    )
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(descriptor, flags)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def write_validated_vault_markdown(
    vault_path: Path,
    path: Path,
    content: bytes,
    *,
    manifest: VaultManifest | None = None,
    existing_lock: VaultWriteLock | None = None,
    preserve_existing_body: bool = False,
    expected_full_sha256: str | None = None,
    require_absent: bool = False,
) -> FrontmatterDocument:
    """Atomically write one validated Markdown note under the shared lock.

    The caller prepares ``content`` outside the lock when expensive work is
    involved. This helper takes the lock for the final read/validation/write;
    callers that replace only frontmatter can require an unchanged body.
    """
    with _open_vault_root_once(vault_path) as root:
        relative = _vault_relative_markdown_path(root.path, path)
        document = parse_frontmatter_bytes(content)
        if manifest is not None:
            _route, missing, invalid = validate_document(relative, document, manifest)
            if missing or invalid:
                raise FrontmatterError(
                    "generated frontmatter is invalid: "
                    f"missing={','.join(missing) or '-'} "
                    f"invalid={','.join(invalid) or '-'}"
                )
        with vault_markdown_write_lock(root, existing_lock) as lock:
            with _open_vault_markdown_parent(lock, relative, create=True) as (
                parent_fd,
                target_name,
            ):
                expected_source = None
                if preserve_existing_body:
                    try:
                        existing_bytes, identity, existing_hash = _read_regular_at(
                            parent_fd, target_name
                        )
                    except FileNotFoundError:
                        raise FrontmatterError(
                            "cannot preserve body of a new Markdown note"
                        ) from None
                    existing = parse_frontmatter_bytes(existing_bytes)
                    if existing.body != document.body:
                        raise FrontmatterError(
                            "frontmatter write changed Markdown body"
                        )
                    expected_source = (identity, existing_hash)
                _atomic_write_at(
                    parent_fd,
                    target_name,
                    content,
                    expected_source=expected_source,
                    expected_full_sha256=expected_full_sha256,
                    require_absent=require_absent,
                    capability=lock,
                )
    return document


def _fresh_memory_fields() -> dict[str, Any]:
    """Return the memory-engine values for a note accessed at creation time."""
    return {
        "last_accessed": date.today().isoformat(),
        "relevance": 1.0,
        "tier": "active",
    }


def write_import_vault_markdown(
    vault_path: Path,
    path: Path,
    content: str | bytes,
    *,
    manifest: VaultManifest,
    expected_full_sha256: str | None = None,
    require_absent: bool = True,
) -> FrontmatterDocument:
    """Write one validated import note with fresh memory-engine fields."""
    payload = content.encode("utf-8") if isinstance(content, str) else content
    return write_validated_vault_markdown(
        vault_path,
        path,
        patch_frontmatter_bytes(payload, _fresh_memory_fields()),
        manifest=manifest,
        expected_full_sha256=expected_full_sha256,
        require_absent=require_absent,
    )


def move_validated_vault_markdown(
    vault_path: Path,
    source_path: Path,
    target_path: Path,
    *,
    manifest: VaultManifest,
) -> FrontmatterDocument:
    """Move a valid Markdown note under the shared cooperative lock.

    The source is validated and read only after acquiring the lock.  A matching
    pre-existing destination is deduplicated; a conflicting destination fails
    without changing either note.
    """
    with _open_vault_root_once(vault_path) as root:
        source_relative = _vault_relative_markdown_path(root.path, source_path)
        target_relative = _vault_relative_markdown_path(root.path, target_path)
        if source_relative == target_relative:
            raise UnsafeVaultPathError("Markdown move source and target must differ")
        with vault_markdown_write_lock(root) as lock:
            with _open_vault_markdown_parent(lock, source_relative, create=False) as (
                source_parent_fd,
                source_name,
            ):
                source_bytes, source_identity, source_hash = _read_regular_at(
                    source_parent_fd, source_name
                )
                document = parse_frontmatter_bytes(source_bytes)
                for relative in (source_relative, target_relative):
                    _route, missing, invalid = validate_document(
                        relative, document, manifest
                    )
                    if missing or invalid:
                        raise FrontmatterError(
                            "moved frontmatter is invalid: "
                            f"missing={','.join(missing) or '-'} "
                            f"invalid={','.join(invalid) or '-'}"
                        )
                with _open_vault_markdown_parent(
                    lock, target_relative, create=True
                ) as (target_parent_fd, target_name):
                    try:
                        target_bytes, _target_identity, target_hash = _read_regular_at(
                            target_parent_fd, target_name
                        )
                    except FileNotFoundError:
                        source_fd = os.open(
                            source_name,
                            os.O_RDONLY | os.O_NOFOLLOW,
                            dir_fd=source_parent_fd,
                        )
                        try:
                            _verify_open_descriptor(
                                source_fd,
                                source_identity,
                                source_hash,
                                label="move source",
                            )
                            try:
                                _link_name_noreplace(
                                    source_parent_fd,
                                    source_name,
                                    target_parent_fd,
                                    target_name,
                                )
                            except FileExistsError as exc:
                                raise FrontmatterError(
                                    "Markdown move target appeared before publication"
                                ) from exc
                            # Linked by name, so prove the name still pointed at
                            # the descriptor verified just above before the
                            # source entry is unlinked below.
                            if (
                                _name_inode(target_parent_fd, target_name)
                                != source_identity[:2]
                            ):
                                raise UnsafeVaultPathError(
                                    "Markdown move source changed before publication"
                                )
                        finally:
                            os.close(source_fd)
                        os.fsync(target_parent_fd)
                        if not _unlink_name_if_inode(
                            source_parent_fd,
                            source_name,
                            source_identity[:2],
                        ):
                            raise UnsafeVaultPathError(
                                "Markdown move source changed after publication"
                            ) from None
                    else:
                        if target_hash != source_hash or target_bytes != source_bytes:
                            raise FrontmatterError(
                                "Markdown move target conflicts with source"
                            )
                        if not _unlink_name_if_inode(
                            source_parent_fd,
                            source_name,
                            source_identity[:2],
                        ):
                            raise UnsafeVaultPathError(
                                "Markdown move source changed before deduplication"
                            )
                    os.fsync(source_parent_fd)
                    if _inode_identity(os.fstat(source_parent_fd)) != _inode_identity(
                        os.fstat(target_parent_fd)
                    ):
                        os.fsync(target_parent_fd)
    return document


def patch_validated_vault_frontmatter(
    vault_path: Path,
    path: Path,
    updates: Mapping[str, Any],
    *,
    manifest: VaultManifest | None = None,
    existing_lock: VaultWriteLock | None = None,
) -> FrontmatterDocument:
    """Losslessly patch top-level frontmatter while keeping body bytes stable."""
    with _open_vault_root_once(vault_path) as root:
        relative = _vault_relative_markdown_path(root.path, path)
        with vault_markdown_write_lock(root, existing_lock) as lock:
            with _open_vault_markdown_parent(lock, relative, create=False) as (
                parent_fd,
                target_name,
            ):
                try:
                    current, identity, current_hash = _read_regular_at(
                        parent_fd, target_name
                    )
                except FileNotFoundError:
                    raise UnsafeVaultPathError(
                        "frontmatter patch target does not exist"
                    ) from None
                candidate = patch_frontmatter_bytes(current, updates)
                document = parse_frontmatter_bytes(candidate)
                if manifest is not None:
                    _route, missing, invalid = validate_document(
                        relative, document, manifest
                    )
                    if missing or invalid:
                        raise FrontmatterError(
                            "generated frontmatter is invalid: "
                            f"missing={','.join(missing) or '-'} "
                            f"invalid={','.join(invalid) or '-'}"
                        )
                _atomic_write_at(
                    parent_fd,
                    target_name,
                    candidate,
                    expected_source=(identity, current_hash),
                    capability=lock,
                )
                return document


def route_profile(
    relative_path: str | Path,
    fields: Mapping[str, Any],
    manifest: VaultManifest,
) -> ProfileRoute:
    """Route a note deterministically using path first, then explicit metadata."""
    parts = PurePosixPath(str(relative_path)).parts
    explicit_epistemic = any(key.startswith("epistemic_") for key in fields)
    if explicit_epistemic or fields.get("type") == "epistemic":
        candidate = "epistemic"
    elif parts == ("Home.md",):
        candidate = "home"
    elif parts and parts[0] == "daily":
        candidate = "daily"
    elif parts and parts[0] == "imports":
        candidate = "import"
    elif parts and parts[0] in {"compiled", "summaries"}:
        candidate = "derived"
    elif (parts and parts[0] == "thoughts" and "reflections" in parts) or fields.get(
        "type"
    ) == "reflection":
        candidate = "reflection"
    elif parts and parts[0] == "thoughts":
        candidate = "thought-card"
    elif parts and parts[0] == "goals":
        candidate = "goal"
    elif PurePosixPath(str(relative_path)).name in {"MEMORY.md", "_index.md"} or (
        parts and parts[0] == "MOC"
    ):
        candidate = "index"
    elif parts and parts[0] in {"business", "projects"}:
        candidate = "flat-context"
    elif parts and parts[0] == "templates":
        candidate = "template"
    elif parts and (parts[0].startswith(".") or parts[0] in {"blog", "skills"}):
        candidate = "technical"
    else:
        candidate = "default"
    if candidate not in manifest.frontmatter_required:
        raise FrontmatterError(f"manifest does not define profile: {candidate}")
    return ProfileRoute(candidate, manifest.frontmatter_required[candidate])


def missing_required_fields(
    document: FrontmatterDocument,
    route: ProfileRoute,
) -> tuple[str, ...]:
    """Return required fields that are absent, null, empty, or empty lists."""
    missing: list[str] = []
    for field in route.required_fields:
        value = document.fields.get(field)
        if value is None or value == "" or value == []:
            missing.append(field)
    return tuple(missing)


def validate_document(
    relative_path: str | Path,
    document: FrontmatterDocument,
    manifest: VaultManifest,
) -> tuple[ProfileRoute, tuple[str, ...], tuple[str, ...]]:
    """Validate profile coverage without mutating a note."""
    route = route_profile(relative_path, document.fields, manifest)
    if not document.has_frontmatter:
        return route, route.required_fields, ()
    missing = missing_required_fields(document, route)
    invalid: list[str] = []
    for field in route.required_fields:
        value = document.fields.get(field)
        if field in missing:
            continue
        if field == "type":
            if (
                not isinstance(value, str)
                or not value.strip()
                or (route.name == "daily" and value != "daily")
            ):
                invalid.append(field)
        elif field == "description" and (
            not isinstance(value, str) or not value.strip() or "\n" in value
        ):
            invalid.append(field)
        elif field == "tags" and (
            not isinstance(value, list)
            or not 2 <= len(value) <= 5
            or not all(_is_normalized_tag(tag) for tag in value)
        ):
            invalid.append(field)
        elif (
            field == "status"
            and route.name in {"thought-card", "card"}
            and (value not in _ALLOWED_STATUSES)
        ):
            invalid.append(field)
        elif field == "tier" and value not in _ALLOWED_TIERS:
            invalid.append(field)
        elif field == "relevance" and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
        ):
            invalid.append(field)
        elif field in _DATE_FIELDS:
            try:
                date.fromisoformat(str(value))
            except ValueError:
                invalid.append(field)
    if route.name == "epistemic":
        # Keep the migration validator on the same contract as WP3's writer.
        # This import is intentionally local: epistemic_memory reuses this
        # module's strict YAML parser.
        from d_brain.services.epistemic_memory import (
            EpistemicValidationError,
            parse_epistemic_metadata,
        )

        try:
            parse_epistemic_metadata(document.fields)
        except EpistemicValidationError:
            invalid.append("epistemic_metadata")
    return route, tuple(dict.fromkeys(missing)), tuple(dict.fromkeys(invalid))


def validate_semantic_payload(
    payload: Mapping[str, Any],
    requested_fields: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Validate a bounded enrichment response without inventing any values."""
    requested = tuple(requested_fields)
    supported = _SEMANTIC_FIELDS | _EPISTEMIC_SEMANTIC_FIELDS
    if not requested or any(field not in supported for field in requested):
        raise FrontmatterError("requested semantic fields must be a non-empty subset")
    if set(payload) != set(requested):
        raise FrontmatterError("semantic payload must contain exactly requested fields")
    result: dict[str, Any] = {}
    if "description" in requested:
        value = payload["description"]
        if not isinstance(value, str) or not value.strip() or "\n" in value:
            raise FrontmatterError("description must be a non-empty one-line string")
        result["description"] = value.strip()
    if "tags" in requested:
        value = payload["tags"]
        if (
            not isinstance(value, list)
            or not 2 <= len(value) <= 5
            or not all(_is_normalized_tag(tag) for tag in value)
        ):
            raise FrontmatterError("tags must be 2-5 lowercase hyphenated strings")
        result["tags"] = value
    if "status" in requested:
        value = payload["status"]
        if value not in _ALLOWED_STATUSES:
            raise FrontmatterError("status is not an allowed card status")
        result["status"] = value
    if _EPISTEMIC_SEMANTIC_FIELDS & set(requested):
        from d_brain.services.epistemic_memory import (
            EPISTEMIC_CONFIDENCE_VALUES,
            EPISTEMIC_STATE_VALUES,
        )

        if "epistemic_confidence" in requested:
            value = payload["epistemic_confidence"]
            if value not in EPISTEMIC_CONFIDENCE_VALUES:
                raise FrontmatterError("epistemic_confidence is invalid")
            result["epistemic_confidence"] = value
        if "epistemic_state" in requested:
            value = payload["epistemic_state"]
            if value not in EPISTEMIC_STATE_VALUES:
                raise FrontmatterError("epistemic_state is invalid")
            result["epistemic_state"] = value
        for field in ("epistemic_scope", "epistemic_verification"):
            if field in requested:
                value = payload[field]
                if not isinstance(value, str) or not value.strip() or "\n" in value:
                    raise FrontmatterError(
                        f"{field} must be a non-empty one-line string"
                    )
                result[field] = value.strip()
    return result
