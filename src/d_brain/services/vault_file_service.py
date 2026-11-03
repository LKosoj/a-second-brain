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
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from d_brain.manifest import load_manifest_for_vault
from d_brain.services.atomic_io import atomic_write_file_bytes
from d_brain.services.vault_lock import vault_write_lock

MAX_FILE_BYTES = 16 * 1024 * 1024
_CONFLICT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_OPEN_CONFLICT_RETENTION = timedelta(days=7)
_RESOLVED_CONFLICT_RETENTION = timedelta(days=1)


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


class VaultFileConflictResolved(VaultFileError):
    """Raised when a resolved conflict receives a different decision."""

    def __init__(self, status: str) -> None:
        super().__init__("conflict already resolved")
        self.status = status


class VaultFileEditForbidden(VaultFileError):
    """Raised when a compiled page edit leaves the owner-notes zone."""

    def __init__(self, reason: str) -> None:
        super().__init__("only owner notes may be edited")
        self.reason = reason


class VaultFileListingChanged(VaultFileError):
    """Raised when a paginated directory changed between requests."""

    def __init__(self) -> None:
        super().__init__("listing changed")


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
    listing_complete: bool


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
        children = self._children(target, relative)
        listing_etag = self._listing_etag(children)
        offset = self._decode_page_token(
            page_token, relative, depth, listing_etag=listing_etag
        )
        page_children = children[offset : offset + page_size]
        entries: list[VaultFileEntry] = []
        for child in page_children:
            entries.append(child)
            if child.kind == "directory" and depth:
                entries.extend(self._nested(child.path, depth - 1))
        next_offset = offset + len(page_children)
        next_token = (
            self._encode_page_token(relative, depth, next_offset, listing_etag)
            if next_offset < len(children)
            else None
        )
        return VaultTreePage(
            relative,
            depth,
            tuple(entries),
            next_token,
            listing_complete=next_token is None,
        )

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
            if current.etag == self._etag(content):
                return VaultWriteResult(
                    current.path, current.size, current.mtime_ns, current.etag
                )
            if self._is_owner_notes_file(relative):
                self._validate_owner_notes_edit(
                    current.content,
                    content,
                    check_outside=current.etag == expected_etag,
                )
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

    @staticmethod
    def _is_owner_notes_file(relative: str) -> bool:
        path = PurePosixPath(relative)
        return (
            len(path.parts) >= 2
            and path.parts[0] == "compiled"
            and path.suffix == ".md"
        )

    @staticmethod
    def _validate_owner_notes_edit(
        current: bytes,
        proposed: bytes,
        *,
        check_outside: bool = True,
    ) -> None:
        try:
            proposed_text = proposed.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VaultFileEditForbidden("not_utf8") from exc

        current_span: tuple[int, int] | None = None
        current_text = ""
        if check_outside:
            try:
                current_text = current.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise VaultFileEditForbidden("not_utf8") from exc
            current_span = VaultFileService._owner_notes_span(current_text)
            if current_span is None:
                raise VaultFileEditForbidden("no_owner_notes")
        proposed_span = VaultFileService._owner_notes_span(proposed_text)
        if proposed_span is None:
            raise VaultFileEditForbidden("owner_notes_markers_damaged")
        if not check_outside:
            return
        assert current_span is not None
        current_start, current_end = current_span
        proposed_start, proposed_end = proposed_span
        if (
            current_text[:current_start] != proposed_text[:proposed_start]
            or current_text[current_end:] != proposed_text[proposed_end:]
        ):
            raise VaultFileEditForbidden("outside_owner_notes")

    @staticmethod
    def _owner_notes_span(text: str) -> tuple[int, int] | None:
        from d_brain.services import compiled_page

        span = compiled_page.human_zone_span(text)
        if span == (-1, -1):
            raise VaultFileEditForbidden("owner_notes_markers_damaged")
        if span is None:
            if compiled_page.human_zone_markers_look_corrupted(text):
                raise VaultFileEditForbidden("owner_notes_markers_damaged")
            return None
        return span

    def resolve_conflict(
        self, conflict_id: str, decision: str
    ) -> VaultWriteResult | None:
        if _CONFLICT_ID_RE.fullmatch(conflict_id) is None:
            raise VaultFileNotFound("conflict not found")
        if decision not in {"replace", "cancel"}:
            raise VaultFileError("decision must be replace or cancel")
        metadata_path = self.conflicts_path / f"{conflict_id}.json"
        with vault_write_lock(self.vault_path):
            self._cleanup_conflicts()
            if not metadata_path.is_file():
                raise VaultFileNotFound("conflict not found")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            status = str(metadata.get("status", ""))
            if status != "open":
                expected_status = "cancelled" if decision == "cancel" else "replaced"
                if status == expected_status:
                    return (
                        None
                        if status == "cancelled"
                        else self._resolved_result(metadata)
                    )
                raise VaultFileConflictResolved(status)
            if decision == "cancel":
                current = self.read_file(str(metadata["path"]))
                now = self._now()
                metadata.update(
                    {
                        "status": "cancelled",
                        "resolved_at": now,
                        "updated_at": now,
                        "resolved_etag": current.etag,
                        "resolved_size": current.size,
                        "resolved_mtime_ns": current.mtime_ns,
                    }
                )
                self._write_metadata(metadata_path, metadata)
                payload_path = self.conflicts_path / f"{conflict_id}.bin"
                payload_path.unlink(missing_ok=True)
                return None
            payload_path = self.conflicts_path / f"{conflict_id}.bin"
            content = payload_path.read_bytes()
            if self._etag(content) != metadata.get("proposed_etag"):
                raise VaultFileError("conflict payload is corrupted")
            current = self.read_file(str(metadata["path"]))
            if self._is_owner_notes_file(str(metadata["path"])):
                self._validate_owner_notes_edit(current.content, content)
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
            now = self._now()
            metadata.update(
                {
                    "status": "replaced",
                    "resolved_at": now,
                    "updated_at": now,
                    "resolved_etag": result.etag,
                    "resolved_size": result.size,
                    "resolved_mtime_ns": result.mtime_ns,
                }
            )
            self._write_metadata(metadata_path, metadata)
            payload_path.unlink(missing_ok=True)
            return VaultWriteResult(
                result.path, result.size, result.mtime_ns, result.etag
            )

    def _create_conflict(
        self,
        path: str,
        expected_etag: str,
        current_etag: str,
        content: bytes,
    ) -> dict[str, Any]:
        self.conflicts_path.mkdir(parents=True, exist_ok=True)
        proposed_etag = self._etag(content)
        self._cleanup_conflicts()
        for metadata_path in self.conflicts_path.glob("*.json"):
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                existing.get("status") == "open"
                and existing.get("path") == path
                and existing.get("proposed_etag") == proposed_etag
            ):
                return dict(existing)

        conflict_id = uuid.uuid4().hex
        payload_path = self.conflicts_path / f"{conflict_id}.bin"
        metadata_path = self.conflicts_path / f"{conflict_id}.json"
        now = self._now()
        atomic_write_file_bytes(payload_path, content, new_file_mode=0o600)
        record: dict[str, Any] = {
            "conflict_id": conflict_id,
            "path": path,
            "expected_etag": expected_etag,
            "current_etag": current_etag,
            "proposed_etag": proposed_etag,
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
        self._write_metadata(metadata_path, record)
        return record

    def _cleanup_conflicts(self) -> None:
        if not self.conflicts_path.is_dir():
            return
        now = datetime.now(UTC)
        for metadata_path in self.conflicts_path.glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = metadata.get("status")
            if status not in {"open", "cancelled", "replaced"}:
                continue
            field = "created_at" if status == "open" else "resolved_at"
            timestamp = self._parse_timestamp(metadata.get(field))
            if timestamp is None:
                try:
                    timestamp = datetime.fromtimestamp(
                        metadata_path.stat().st_mtime, tz=UTC
                    )
                except OSError:
                    continue
            retention = (
                _OPEN_CONFLICT_RETENTION
                if status == "open"
                else _RESOLVED_CONFLICT_RETENTION
            )
            if now - timestamp <= retention:
                continue
            metadata_path.unlink(missing_ok=True)
            metadata_path.with_suffix(".bin").unlink(missing_ok=True)

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _resolved_result(metadata: dict[str, Any]) -> VaultWriteResult:
        if metadata.get("status") != "replaced":
            raise VaultFileError("cancelled conflict has no write result")
        return VaultWriteResult(
            str(metadata["path"]),
            int(metadata["resolved_size"]),
            int(metadata["resolved_mtime_ns"]),
            str(metadata["resolved_etag"]),
        )

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
    def _encode_page_token(
        path: str, depth: int, offset: int, listing_etag: str | None = None
    ) -> str:
        value: dict[str, object] = {"path": path, "depth": depth, "offset": offset}
        if listing_etag is not None:
            value["listing_etag"] = listing_etag
        payload = json.dumps(
            value, separators=(",", ":")
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @staticmethod
    def _decode_page_token(
        token: str | None,
        path: str,
        depth: int,
        *,
        listing_etag: str | None = None,
    ) -> int:
        if not token:
            return 0
        try:
            padded = token + "=" * (-len(token) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
            if value["path"] != path or value["depth"] != depth:
                raise ValueError
            token_listing_etag = value.get("listing_etag")
            if (
                listing_etag is not None
                and token_listing_etag is not None
                and token_listing_etag != listing_etag
            ):
                raise VaultFileListingChanged
            offset = int(value["offset"])
            if offset < 0:
                raise ValueError
            return offset
        except VaultFileListingChanged:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise VaultFileError("invalid page_token") from None

    @staticmethod
    def _listing_etag(children: list[VaultFileEntry]) -> str:
        value = [(entry.name, entry.kind) for entry in children]
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


__all__ = [
    "MAX_FILE_BYTES",
    "VaultFileConflict",
    "VaultFileConflictResolved",
    "VaultFileEditForbidden",
    "VaultFileEntry",
    "VaultFileError",
    "VaultFileForbidden",
    "VaultFileNotFound",
    "VaultFileListingChanged",
    "VaultFileRead",
    "VaultFileService",
    "VaultTreePage",
    "VaultWriteResult",
]
