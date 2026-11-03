"""Safe file access for the private network API."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from d_brain.manifest import load_manifest_for_vault
from d_brain.services.atomic_io import atomic_write_file_bytes
from d_brain.services.vault_lock import vault_write_lock

MAX_FILE_BYTES = 16 * 1024 * 1024
_CONFLICT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class VaultFileError(ValueError):
    """Base error for a network vault-file operation."""


class VaultFileNotFound(VaultFileError):
    """The requested allowlisted path does not exist."""


class VaultFileForbidden(VaultFileError):
    """The requested path is outside the user-content allowlist."""


class VaultFileConflict(VaultFileError):
    """The file changed since the client read it."""

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__("file changed since it was read")
        self.record = record


@dataclass(frozen=True, slots=True)
class VaultFileEntry:
    path: str
    name: str
    kind: str
    size: int | None
    mtime_ns: int | None
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class VaultTreePage:
    path: str
    depth: int
    entries: tuple[VaultFileEntry, ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class VaultFileRead:
    path: str
    content: bytes
    size: int
    mtime_ns: int
    etag: str
    mime_type: str


@dataclass(frozen=True, slots=True)
class VaultWriteResult:
    path: str
    size: int
    mtime_ns: int
    etag: str


class VaultFileService:
    """Read and conditionally update user-content files in one vault."""

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = Path(vault_path).resolve()
        self.conflicts_path = self.vault_path / ".session" / "vault-file-conflicts"

    def list_tree(
        self,
        path: str = "",
        *,
        depth: int = 1,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> VaultTreePage:
        if depth < 0 or depth > 8:
            raise VaultFileError("depth must be between 0 and 8")
        if page_size < 1 or page_size > 1000:
            raise VaultFileError("page_size must be between 1 and 1000")
        relative = self._normalize_path(path, allow_root=True)
        target = self._resolve_existing(relative)
        if not target.is_dir():
            raise VaultFileError("tree path is not a directory")
        offset = self._decode_page_token(page_token, relative, depth)
        children = self._children(target, relative)
        page_children = children[offset : offset + page_size]
        entries: list[VaultFileEntry] = []
        for child in page_children:
            entries.append(child)
            if child.kind == "directory" and depth:
                entries.extend(self._nested(child.path, depth - 1))
        next_offset = offset + len(page_children)
        next_token = (
            self._encode_page_token(relative, depth, next_offset)
            if next_offset < len(children)
            else None
        )
        return VaultTreePage(relative, depth, tuple(entries), next_token)

    def read_file(self, path: str) -> VaultFileRead:
        relative = self._normalize_path(path)
        target = self._resolve_existing(relative)
        self._require_regular(target)
        with target.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
            info = os.fstat(stream.fileno())
        if len(content) > MAX_FILE_BYTES:
            raise VaultFileError("file is too large")
        return VaultFileRead(
            relative,
            content,
            len(content),
            info.st_mtime_ns,
            self._etag(content),
            mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        )

    def update_file(
        self, path: str, content: bytes, expected_etag: str
    ) -> VaultWriteResult:
        if len(content) > MAX_FILE_BYTES:
            raise VaultFileError("file is too large")
        relative = self._normalize_path(path)
        target = self._resolve_existing(relative)
        self._require_regular(target)
        with vault_write_lock(self.vault_path):
            current = self.read_file(relative)
            if current.etag != expected_etag:
                raise VaultFileConflict(
                    self._create_conflict(
                        relative,
                        expected_etag,
                        current.etag,
                        content,
                    )
                )
            self._atomic_replace(target, content)
            result = self.read_file(relative)
        return VaultWriteResult(result.path, result.size, result.mtime_ns, result.etag)

    @staticmethod
    def _atomic_replace(path: Path, content: bytes) -> None:
        atomic_write_file_bytes(path, content)

    def resolve_conflict(
        self, conflict_id: str, decision: str
    ) -> VaultWriteResult | None:
        if _CONFLICT_ID_RE.fullmatch(conflict_id) is None:
            raise VaultFileNotFound("conflict not found")
        if decision not in {"replace", "cancel"}:
            raise VaultFileError("decision must be replace or cancel")
        metadata_path = self.conflicts_path / f"{conflict_id}.json"
        if not metadata_path.is_file():
            raise VaultFileNotFound("conflict not found")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("status") != "open":
            raise VaultFileError("conflict was already resolved")
        if decision == "cancel":
            metadata["status"] = "cancelled"
            self._write_metadata(metadata_path, metadata)
            payload_path = self.conflicts_path / f"{conflict_id}.bin"
            payload_path.unlink(missing_ok=True)
            return None
        payload_path = self.conflicts_path / f"{conflict_id}.bin"
        content = payload_path.read_bytes()
        if self._etag(content) != metadata.get("proposed_etag"):
            raise VaultFileError("conflict payload is corrupted")
        with vault_write_lock(self.vault_path):
            current = self.read_file(str(metadata["path"]))
            if current.etag != metadata.get("current_etag"):
                raise VaultFileConflict(
                    self._create_conflict(
                        str(metadata["path"]),
                        str(metadata["current_etag"]),
                        current.etag,
                        content,
                    )
                )
            target = self._resolve_existing(str(metadata["path"]))
            self._require_regular(target)
            self._atomic_replace(target, content)
            result = self.read_file(str(metadata["path"]))
        metadata["status"] = "replaced"
        self._write_metadata(metadata_path, metadata)
        payload_path.unlink(missing_ok=True)
        return VaultWriteResult(result.path, result.size, result.mtime_ns, result.etag)

    def _create_conflict(
        self,
        path: str,
        expected_etag: str,
        current_etag: str,
        content: bytes,
    ) -> dict[str, Any]:
        self.conflicts_path.mkdir(parents=True, exist_ok=True)
        conflict_id = uuid.uuid4().hex
        payload_path = self.conflicts_path / f"{conflict_id}.bin"
        metadata_path = self.conflicts_path / f"{conflict_id}.json"
        proposed_etag = self._etag(content)
        atomic_write_file_bytes(payload_path, content, new_file_mode=0o600)
        record: dict[str, Any] = {
            "conflict_id": conflict_id,
            "path": path,
            "expected_etag": expected_etag,
            "current_etag": current_etag,
            "proposed_etag": proposed_etag,
            "status": "open",
        }
        self._write_metadata(metadata_path, record)
        return record

    @staticmethod
    def _write_metadata(path: Path, value: dict[str, Any]) -> None:
        atomic_write_file_bytes(
            path,
            (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(),
            new_file_mode=0o600,
        )

    def _children(self, target: Path, relative: str) -> list[VaultFileEntry]:
        entries: list[VaultFileEntry] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name.casefold()):
            child_relative = f"{relative}/{child.name}" if relative else child.name
            if child.name.startswith(".") or not self._is_visible(child_relative):
                continue
            try:
                info = child.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                entries.append(
                    VaultFileEntry(
                        child_relative, child.name, "directory", None, info.st_mtime_ns
                    )
                )
            elif stat.S_ISREG(info.st_mode):
                entries.append(
                    VaultFileEntry(
                        child_relative,
                        child.name,
                        "file",
                        info.st_size,
                        info.st_mtime_ns,
                        mimetypes.guess_type(child.name)[0]
                        or "application/octet-stream",
                    )
                )
        return entries

    def _nested(self, relative: str, depth: int) -> list[VaultFileEntry]:
        if depth <= 0:
            return []
        target = self._resolve_existing(relative)
        return [
            entry
            for child in self._children(target, relative)
            for entry in (
                [child]
                + (
                    self._nested(child.path, depth - 1)
                    if child.kind == "directory"
                    else []
                )
            )
        ]

    def _resolve_existing(self, relative: str) -> Path:
        target = self.vault_path / relative
        if not target.exists() or target.is_symlink():
            raise VaultFileNotFound("file not found")
        try:
            target.relative_to(self.vault_path)
        except ValueError as exc:
            raise VaultFileForbidden("path is outside vault") from exc
        if not self._is_visible(relative):
            raise VaultFileForbidden("path is not user content")
        current = self.vault_path
        for component in PurePosixPath(relative).parts:
            current = current / component
            if current.is_symlink():
                raise VaultFileForbidden("symlink path is not allowed")
        return target

    def _is_visible(self, relative: str) -> bool:
        if relative == "":
            return True
        manifest = load_manifest_for_vault(self.vault_path)
        roots = {
            PurePosixPath(root)
            .relative_to(PurePosixPath(manifest.memory_root))
            .as_posix()
            for root in manifest.user_content_roots
        }
        path = PurePosixPath(relative)
        return any(
            path == PurePosixPath(root)
            or PurePosixPath(root) in path.parents
            or path in PurePosixPath(root).parents
            for root in roots
        )

    @staticmethod
    def _require_regular(path: Path) -> None:
        if not path.is_file() or path.is_symlink():
            raise VaultFileNotFound("file not found")

    @staticmethod
    def _normalize_path(value: str, *, allow_root: bool = False) -> str:
        if "\x00" in value or "\\" in value:
            raise VaultFileForbidden("unsafe path")
        if not value and allow_root:
            return ""
        path = PurePosixPath(value)
        if not value or path.is_absolute() or "." in path.parts or ".." in path.parts:
            raise VaultFileForbidden("unsafe path")
        return path.as_posix()

    @staticmethod
    def _etag(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _encode_page_token(path: str, depth: int, offset: int) -> str:
        payload = json.dumps(
            {"path": path, "depth": depth, "offset": offset}, separators=(",", ":")
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @staticmethod
    def _decode_page_token(token: str | None, path: str, depth: int) -> int:
        if not token:
            return 0
        try:
            padded = token + "=" * (-len(token) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
            if value["path"] != path or value["depth"] != depth:
                raise ValueError
            offset = int(value["offset"])
            if offset < 0:
                raise ValueError
            return offset
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise VaultFileError("invalid page_token") from None


__all__ = [
    "MAX_FILE_BYTES",
    "VaultFileConflict",
    "VaultFileEntry",
    "VaultFileError",
    "VaultFileForbidden",
    "VaultFileNotFound",
    "VaultFileRead",
    "VaultFileService",
    "VaultTreePage",
    "VaultWriteResult",
]
