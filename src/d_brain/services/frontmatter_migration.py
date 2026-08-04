"""Guarded, resumable frontmatter migration primitives.

Discovery is read-only. Any write requires a frozen external state, verified
backup manifest, fresh quiescence proof, and the shared cooperative vault lock.
It does not claim a filesystem-wide CAS against a non-cooperative editor.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import io
import json
import os
import stat as stat_module
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib.resources import as_file
from importlib.resources import files as resource_files
from pathlib import Path, PurePosixPath
from typing import Any

from d_brain.manifest import VaultManifest
from d_brain.services.frontmatter import (
    FrontmatterError,
    UnsafeVaultPathError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    route_profile,
    split_frontmatter_bytes,
    validate_document,
    validate_semantic_payload,
)
from d_brain.services.vault_lock import VaultLockError, vault_write_lock

STATE_VERSION = 5
WRITE_AHEAD_VERSION = 1
MECHANICAL_COMPLETION_VERSION = 1
QUIESCE_MAX_AGE_SECONDS = 300
KNOWN_WRITER_SERVICES = (
    "a-second-brain.service",
    "a-second-brain-process.service",
    "a-second-brain-plaud-sync.service",
    "a-second-brain-qmd-maintenance.service",
)
KNOWN_WRITER_TIMERS = (
    "a-second-brain-process.timer",
    "a-second-brain-plaud-sync.timer",
    "a-second-brain-qmd-maintenance.timer",
)
KNOWN_WRITER_UNITS = KNOWN_WRITER_SERVICES + KNOWN_WRITER_TIMERS


class MigrationStateError(ValueError):
    """Raised when a migration artifact is malformed, stale, or unsafe."""


class WriterQuiescenceError(MigrationStateError):
    """Raised when a writer makes the guarded migration unsafe to continue."""


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    profile: str
    source_state: str
    full_sha256: str | None
    body_sha256: str | None
    frontmatter_sha256: str | None
    has_frontmatter: bool
    missing_fields: tuple[str, ...]
    invalid_fields: tuple[str, ...]
    parse_error: str | None
    planned_updates: Mapping[str, Any]
    mechanical_provenance: Mapping[str, Mapping[str, str]]
    header_repair: str | None
    mechanical_candidate: Mapping[str, str | None] | None


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative(value: str | Path) -> str:
    path = PurePosixPath(str(value))
    if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
        raise UnsafeVaultPathError("path must be a non-empty vault-relative path")
    if path.suffix.lower() != ".md":
        raise UnsafeVaultPathError("path must name a Markdown file")
    return path.as_posix()


def _open_vault_file(
    vault_path: Path, relative_path: str | Path
) -> tuple[int, os.stat_result]:
    """Open a regular Markdown file beneath vault through no-follow dirfds."""
    relative = _safe_relative(relative_path)
    root = Path(vault_path).resolve()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    directory_fd = root_fd
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            if directory_fd != root_fd:
                os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        stat = os.fstat(file_fd)
        if not stat_module.S_ISREG(stat.st_mode):
            os.close(file_fd)
            raise UnsafeVaultPathError("path must name a regular file")
        return file_fd, stat
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeVaultPathError("symlink paths are not accepted") from exc
        raise UnsafeVaultPathError(f"cannot safely open vault path: {exc}") from exc
    finally:
        if directory_fd != root_fd:
            os.close(directory_fd)
        os.close(root_fd)


def _read_fd(file_fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def safe_read_vault_markdown(
    vault_path: Path, relative_path: str | Path
) -> tuple[bytes, os.stat_result]:
    file_fd, stat = _open_vault_file(vault_path, relative_path)
    try:
        return _read_fd(file_fd), stat
    finally:
        os.close(file_fd)


def atomic_replace_vault_markdown(
    vault_path: Path,
    relative_path: str,
    *,
    expected_full_sha256: str,
    content: bytes,
) -> os.stat_result:
    """Replace through no-follow descriptors under the cooperative lock.

    ``expected_full_sha256`` detects a change observed immediately before the
    replacement. It is not, and cannot be, a filesystem-wide CAS against a
    non-cooperative writer; callers must enforce fresh quiescence.
    """
    relative = _safe_relative(relative_path)
    root_fd = os.open(Path(vault_path).resolve(), os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = root_fd
    temporary_name: str | None = None
    source_fd: int | None = None
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
        source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        source_stat = os.fstat(source_fd)
        if not stat_module.S_ISREG(source_stat.st_mode):
            raise UnsafeVaultPathError("CAS target must be a regular file")
        if _sha256(_read_fd(source_fd)) != expected_full_sha256:
            raise MigrationStateError("source full hash changed before commit")
        temporary_name = f".{parts[-1]}.frontmatter-{os.getpid()}-{os.urandom(4).hex()}"
        temporary_fd = os.open(
            temporary_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            source_stat.st_mode & 0o7777,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(content)
            while view:
                view = view[os.write(temporary_fd, view) :]
            os.fsync(temporary_fd)
            os.fchmod(temporary_fd, source_stat.st_mode & 0o7777)
            os.fchown(temporary_fd, source_stat.st_uid, source_stat.st_gid)
        finally:
            os.close(temporary_fd)
        target_stat = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        descriptor_stat = os.fstat(source_fd)
        identity = (
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
        )
        if (
            target_stat.st_dev,
            target_stat.st_ino,
            target_stat.st_size,
            target_stat.st_mtime_ns,
        ) != identity or (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
            descriptor_stat.st_size,
            descriptor_stat.st_mtime_ns,
        ) != identity:
            raise MigrationStateError("source target changed before replacement")
        os.lseek(source_fd, 0, os.SEEK_SET)
        if _sha256(_read_fd(source_fd)) != expected_full_sha256:
            raise MigrationStateError("source descriptor changed before replacement")
        os.close(source_fd)
        source_fd = None
        os.replace(
            temporary_name, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd
        )
        temporary_name = None
        os.fsync(parent_fd)
        return source_stat
    except OSError as exc:
        raise UnsafeVaultPathError(f"cannot safely replace vault path: {exc}") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


_SEMANTIC_FIELDS = frozenset({"description", "tags", "status"})
_DATE_FIELDS = frozenset({"date", "created", "updated", "last_accessed"})
_MEMORY_FIELDS = frozenset({"last_accessed", "relevance", "tier"})
_EPISTEMIC_FIELDS = frozenset(
    {
        "epistemic_confidence",
        "epistemic_scope",
        "epistemic_state",
        "epistemic_verification",
    }
)


@lru_cache(maxsize=1)
def _memory_engine_module() -> Any:
    """Load the established memory-engine instead of copying its formulas."""
    resource = resource_files("d_brain.resources").joinpath(
        "project_template",
        "skills",
        "agent-memory",
        "scripts",
        "memory-engine.py",
    )
    with as_file(resource) as script:
        spec = importlib.util.spec_from_file_location("d_brain_memory_engine", script)
        if spec is None or spec.loader is None:
            raise MigrationStateError("cannot load memory-engine adapter")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _is_valid_date(value: object) -> bool:
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


def _date_from_filename(relative: PurePosixPath) -> date | None:
    try:
        return date.fromisoformat(relative.stem)
    except ValueError:
        return None


@lru_cache(maxsize=4096)
def _git_dates(project_root: str, relative: str) -> tuple[date, ...]:
    """Return ordered commit dates for one vault path, or no dates outside Git."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                project_root,
                "log",
                "--follow",
                "--format=%aI",
                "--",
                f"vault/{relative}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError:
        return ()
    if result.returncode != 0:
        return ()
    dates: list[date] = []
    for line in result.stdout.splitlines():
        try:
            dates.append(date.fromisoformat(line[:10]))
        except ValueError:
            continue
    return tuple(sorted(set(dates)))


def _file_dates(
    vault_path: Path,
    relative: str,
    file_stat: os.stat_result,
    *,
    use_git_dates: bool,
) -> tuple[date, date, str, str]:
    dates = _git_dates(str(vault_path.parent), relative) if use_git_dates else ()
    if dates:
        return dates[0], dates[-1], "git-earliest", "git-latest"
    fallback = datetime.fromtimestamp(file_stat.st_mtime, UTC).date()
    return fallback, fallback, "filesystem-mtime", "filesystem-mtime"


def _memory_reference_date(
    fields: Mapping[str, Any],
    latest: date,
    latest_source: str,
    file_stat: os.stat_result,
) -> tuple[date, str]:
    candidates: list[tuple[date, str]] = [(latest, latest_source)]
    for field in ("last_accessed", "updated", "created"):
        value = fields.get(field)
        if _is_valid_date(value):
            candidates.append((date.fromisoformat(str(value)), f"existing:{field}"))
    candidates.append(
        (datetime.fromtimestamp(file_stat.st_mtime, UTC).date(), "filesystem-mtime")
    )
    return max(candidates, key=lambda item: item[0])


def _profile_type(profile: str) -> str:
    return {
        "daily": "daily",
        "import": "import",
        "derived": "derived",
        "thought-card": "note",
        "reflection": "reflection",
        "goal": "goal",
        "index": "index",
        "flat-context": "note",
        "template": "template",
        "technical": "technical",
        "epistemic": "epistemic",
        "home": "home",
        "default": "note",
    }[profile]


def _known_header_repair(relative: str, content: bytes) -> tuple[bytes, str] | None:
    """Repair the audited CRM template header without touching its body."""
    crm_description = (
        "[One-line summary: industry, key deal, what makes this client notable]"
    )
    if relative == "templates/crm-template.md":
        header, body, newline = split_frontmatter_bytes(content)
        marker = crm_description.encode()
        if (
            header is None
            or header.count(b"description: >-\n") != 1
            or header.count(marker) != 1
        ):
            return None
        repaired_header = header.replace(marker, b"").replace(
            b"description: >-\n",
            f"description: {json.dumps(crm_description)}\n".encode(),
        )
        candidate = b"---" + newline + repaired_header + b"---" + newline + body
        parse_frontmatter_bytes(candidate)
        return candidate, "crm-template-description-placeholder"

    return None


def _plan_mechanical_updates(
    vault_path: Path,
    relative: str,
    fields: Mapping[str, Any],
    manifest: VaultManifest,
    required_fields: tuple[str, ...],
    invalid_fields: tuple[str, ...],
    file_stat: os.stat_result,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Plan deterministic metadata only; semantic fields are deliberately absent."""
    required = set(required_fields)
    invalid = set(invalid_fields)
    missing_or_invalid = {
        field
        for field in required
        if fields.get(field) in (None, "", []) or field in invalid
    }
    profile = route_profile(relative, fields, manifest).name
    updates: dict[str, Any] = {}
    provenance: dict[str, dict[str, str]] = {}
    if "type" in missing_or_invalid:
        updates["type"] = _profile_type(profile)
        provenance["type"] = {"source": "profile-router", "value": updates["type"]}

    memory_missing = _MEMORY_FIELDS & missing_or_invalid
    dated_fields = {"created", "updated", "date"} & missing_or_invalid
    if not dated_fields and not memory_missing:
        return updates, provenance

    engine = _memory_engine_module()
    config = engine.load_config(vault_path)
    earliest, latest, created_source, updated_source = _file_dates(
        vault_path,
        relative,
        file_stat,
        use_git_dates=bool(config["use_git_dates"]),
    )
    for field, value, source in (
        ("created", earliest, created_source),
        ("updated", latest, updated_source),
    ):
        if field in missing_or_invalid:
            updates[field] = value.isoformat()
            provenance[field] = {"source": source, "value": updates[field]}

    if "date" in missing_or_invalid:
        filename_date = _date_from_filename(PurePosixPath(relative))
        if profile == "daily" and filename_date is not None:
            value, source = filename_date, "daily-filename"
        elif profile == "reflection" and filename_date is not None:
            value, source = filename_date, "reflection-filename"
        else:
            value, source = earliest, created_source
        updates["date"] = value.isoformat()
        provenance["date"] = {"source": source, "value": updates["date"]}

    if memory_missing:
        reference, reference_source = _memory_reference_date(
            fields, latest, updated_source, file_stat
        )
        days = max(0, (engine.TODAY - reference).days)
        if "last_accessed" in memory_missing:
            updates["last_accessed"] = reference.isoformat()
            provenance["last_accessed"] = {
                "source": f"memory-engine:{reference_source}",
                "value": updates["last_accessed"],
            }
        if "relevance" in memory_missing:
            updates["relevance"] = engine.calc_relevance(
                days, config["decay_rate"], config["relevance_floor"]
            )
            provenance["relevance"] = {
                "source": "memory-engine:calc_relevance",
                "value": str(updates["relevance"]),
            }
        if "tier" in memory_missing:
            updates["tier"] = engine.calc_tier(
                days, config["tiers"], str(fields.get("tier", ""))
            )
            provenance["tier"] = {
                "source": "memory-engine:calc_tier",
                "value": str(updates["tier"]),
            }
    return updates, provenance


def _iter_inventory_paths(vault_path: Path) -> tuple[list[str], list[str]]:
    root = Path(vault_path).resolve()
    markdown: list[str] = []
    symlink_directories: list[str] = []
    for current_root, directories, files in os.walk(root, followlinks=False):
        current = Path(current_root)
        kept: list[str] = []
        for name in sorted(directories):
            path = current / name
            if path.is_symlink():
                symlink_directories.append(path.relative_to(root).as_posix())
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            path = current / name
            if path.is_symlink():
                symlink_directories.append(path.relative_to(root).as_posix())
            if path.suffix.lower() == ".md":
                markdown.append(path.relative_to(root).as_posix())
    return markdown, sorted(symlink_directories)


def _configured_infrastructure_roots(
    vault_path: Path, manifest: VaultManifest
) -> tuple[tuple[str, Path], ...]:
    """Return manifest infrastructure roots as lexical paths below this vault.

    The lexical path is intentional: ``vault/.codex`` is an alias and resolves
    to ``vault/.claude``.  Alias classification must retain that distinction.
    """
    root = Path(vault_path).resolve()
    project_root = root.parent
    roots: list[tuple[str, Path]] = []
    for configured in manifest.infrastructure:
        candidate = project_root / configured
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        roots.append((configured, candidate))
    return tuple(roots)


def _root_containing(
    candidate: Path, roots: Iterable[tuple[str, Path]]
) -> tuple[str, Path] | None:
    for configured, root in roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return configured, root
    return None


def _canonical_subtree_is_included(
    vault_path: Path, target: Path, discovered_markdown: set[str]
) -> bool:
    """Prove regular Markdown below a canonical target was discovered once."""
    root = Path(vault_path).resolve()
    try:
        for current_root, _, files in os.walk(target, followlinks=False):
            current = Path(current_root)
            for name in files:
                path = current / name
                if path.suffix.lower() != ".md":
                    continue
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    return False
                if relative not in discovered_markdown:
                    return False
    except OSError:
        return False
    return True


def _blocking_symlink_record(relative: str, reason: str) -> dict[str, str]:
    return {"path": relative, "classification": "blocking", "reason": reason}


def _classify_symlink_directory(
    vault_path: Path,
    manifest: VaultManifest,
    relative: str,
    discovered_markdown: set[str],
) -> dict[str, str]:
    """Classify a non-traversed directory symlink without granting it access.

    Only an infrastructure alias to an already scanned canonical
    infrastructure subtree is safe. Every other directory symlink blocks a
    frozen inventory.
    """
    root = Path(vault_path).resolve()
    alias = root / relative
    roots = _configured_infrastructure_roots(root, manifest)
    source_root = _root_containing(alias, roots)
    if source_root is None:
        return _blocking_symlink_record(relative, "alias_not_in_infrastructure")
    try:
        target = alias.resolve(strict=True)
    except (OSError, RuntimeError):
        return _blocking_symlink_record(relative, "dangling_or_cyclic")
    if not target.is_dir():
        return _blocking_symlink_record(relative, "target_not_directory")
    try:
        target.relative_to(root)
    except ValueError:
        return _blocking_symlink_record(relative, "external_target")
    target_root = _root_containing(target, roots)
    if target_root is None:
        return _blocking_symlink_record(relative, "target_not_other_infrastructure")
    if target_root[0] == source_root[0]:
        return _blocking_symlink_record(relative, "target_not_other_infrastructure")
    if not _canonical_subtree_is_included(root, target, discovered_markdown):
        return _blocking_symlink_record(relative, "target_not_covered")
    return {
        "path": relative,
        "classification": "safe_accounted_alias",
        "target": target.relative_to(root).as_posix(),
        "infrastructure_root": source_root[0],
        "target_infrastructure_root": target_root[0],
    }


def _inventory_entry(
    vault_path: Path, relative: str, manifest: VaultManifest
) -> InventoryEntry:
    try:
        content, file_stat = safe_read_vault_markdown(vault_path, relative)
    except UnsafeVaultPathError as exc:
        message = str(exc)
        state = (
            "symlink"
            if "symlink" in message
            else "racing"
            if "No such file" in message or "Errno 2" in message
            else "unreadable"
        )
        return InventoryEntry(
            relative,
            "unknown",
            state,
            None,
            None,
            None,
            False,
            (),
            (),
            str(exc),
            {},
            {},
            None,
            None,
        )
    try:
        document = parse_frontmatter_bytes(content)
    except FrontmatterError as exc:
        repaired = _known_header_repair(relative, content)
        if repaired is None:
            try:
                header, body, _ = split_frontmatter_bytes(content)
            except FrontmatterError:
                header, body = None, None
            route = route_profile(relative, {}, manifest)
            return InventoryEntry(
                relative,
                route.name,
                "regular",
                _sha256(content),
                _sha256(body) if body is not None else None,
                _sha256(header) if header is not None else None,
                content.startswith(b"---"),
                route.required_fields,
                (),
                str(exc),
                {},
                {},
                None,
                None,
            )
        repaired_content, repair_id = repaired
        document = parse_frontmatter_bytes(repaired_content)
        header_repair: str | None = repair_id
    else:
        header_repair = None
    route, missing, invalid = validate_document(relative, document, manifest)
    updates, provenance = _plan_mechanical_updates(
        vault_path,
        relative,
        document.fields,
        manifest,
        route.required_fields,
        invalid,
        file_stat,
    )
    mechanical_candidate: dict[str, str | None] | None = None
    if updates or header_repair is not None:
        candidate = patch_frontmatter_bytes(document.content, updates)
        candidate_document = parse_frontmatter_bytes(candidate)
        if candidate_document.body_sha256 != document.body_sha256:
            raise MigrationStateError("mechanical candidate changed Markdown body")
        mechanical_candidate = {
            "full_sha256": candidate_document.full_sha256,
            "body_sha256": candidate_document.body_sha256,
            "frontmatter_sha256": candidate_document.frontmatter_sha256,
        }
    raw_header, _, _ = split_frontmatter_bytes(content)
    raw_frontmatter_hash = _sha256(raw_header) if raw_header is not None else None
    return InventoryEntry(
        relative,
        route.name,
        "regular",
        _sha256(content),
        document.body_sha256,
        (
            raw_frontmatter_hash
            if header_repair is not None
            else document.frontmatter_sha256
        ),
        document.has_frontmatter,
        missing,
        invalid,
        None,
        updates,
        provenance,
        header_repair,
        mechanical_candidate,
    )


def inventory_vault(vault_path: Path, manifest: VaultManifest) -> dict[str, Any]:
    """Read every discovered Markdown candidate and account for every outcome."""
    root = Path(vault_path).resolve()
    if not root.is_dir():
        raise MigrationStateError(f"vault does not exist: {root}")
    paths, symlink_directories = _iter_inventory_paths(root)
    symlink_records = [
        _classify_symlink_directory(root, manifest, relative, set(paths))
        for relative in symlink_directories
    ]
    safe_aliases = [
        record
        for record in symlink_records
        if record["classification"] == "safe_accounted_alias"
    ]
    blocking_aliases = [
        record for record in symlink_records if record["classification"] == "blocking"
    ]
    entries = [_inventory_entry(root, path, manifest) for path in paths]
    counts = {
        state: sum(item.source_state == state for item in entries)
        for state in ("regular", "symlink", "unreadable", "racing")
    }
    profiles: dict[str, int] = {}
    for entry in entries:
        profiles[entry.profile] = profiles.get(entry.profile, 0) + 1
    return {
        "version": STATE_VERSION,
        "created_at": _utc_now(),
        "vault_path": str(root),
        "source_count": len(entries),
        "discovered_markdown_count": len(paths),
        "coverage_complete": len(entries) == len(paths) and not blocking_aliases,
        "source_state_counts": counts,
        "symlink_directories": symlink_directories,
        "symlink_directory_count": len(symlink_directories),
        "safe_accounted_aliases": safe_aliases,
        "safe_accounted_alias_count": len(safe_aliases),
        "blocking_symlink_directories": blocking_aliases,
        "blocking_symlink_directory_count": len(blocking_aliases),
        "profile_counts": dict(sorted(profiles.items())),
        "entries": [asdict(entry) for entry in entries],
    }


def _ensure_external(vault_path: Path, *paths: Path) -> tuple[Path, ...]:
    root = Path(vault_path).resolve()
    resolved = tuple(Path(path).resolve() for path in paths)
    if any(path == root or root in path.parents for path in resolved):
        raise MigrationStateError(
            "state, report, proof, backup, and archive must stay outside vault"
        )
    return resolved


def _atomic_external_write(path: Path, content: bytes) -> None:
    target = Path(path)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(temporary_fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _source_set_hash(entries: Iterable[Mapping[str, Any]]) -> str:
    canonical = [
        {
            "path": entry.get("path"),
            "full_sha256": entry.get("frozen_full_sha256", entry.get("full_sha256")),
            "body_sha256": entry.get("frozen_body_sha256", entry.get("body_sha256")),
            "frontmatter_sha256": entry.get(
                "frozen_frontmatter_sha256", entry.get("frontmatter_sha256")
            ),
        }
        for entry in entries
    ]
    return _sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    )


def _expected_set_hash(entries: Iterable[Mapping[str, Any]]) -> str:
    canonical = [
        {
            "path": entry.get("path"),
            "full_sha256": entry.get("expected_full_sha256"),
            "body_sha256": entry.get("expected_body_sha256"),
            "frontmatter_sha256": entry.get("expected_frontmatter_sha256"),
        }
        for entry in entries
    ]
    return _sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    )


def _mechanical_plan_hash(entries: Iterable[Mapping[str, Any]]) -> str:
    """Hash immutable plan/policy data for accidental tamper detection.

    This is a consistency binding across state, backup, and proof, not a
    cryptographic defense against an actor able to rewrite every artifact.
    """
    canonical = [
        {
            "path": entry.get("path"),
            "profile": entry.get("profile"),
            "mechanical_plan": entry.get("mechanical_plan"),
            "semantic_requested_fields": entry.get("semantic", {}).get(
                "requested_fields"
            ),
        }
        for entry in entries
    ]
    return _sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    )


def _wal_set_hash(entries: Iterable[Mapping[str, Any]]) -> str:
    canonical = [
        {"path": entry.get("path"), "write_ahead": entry.get("write_ahead")}
        for entry in entries
        if entry.get("write_ahead") is not None
    ]
    return _sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    )


def _mapping_identity(value: object) -> tuple[object, object, object] | None:
    if not isinstance(value, Mapping):
        return None
    return (
        value.get("full_sha256"),
        value.get("body_sha256"),
        value.get("frontmatter_sha256"),
    )


def _validate_state_entries(value: object) -> list[Mapping[str, Any]]:
    """Reject malformed entry containers before hashing or nested ``get`` calls."""
    if not isinstance(value, list):
        raise MigrationStateError("migration state entries must be a list")
    entries: list[Mapping[str, Any]] = []
    for index, raw_entry in enumerate(value):
        if not isinstance(raw_entry, Mapping):
            raise MigrationStateError(
                f"migration state entry {index} must be an object"
            )
        for field in ("mechanical_plan", "semantic"):
            if not isinstance(raw_entry.get(field), Mapping):
                raise MigrationStateError(
                    f"migration state entry {index}.{field} must be an object"
                )
        write_ahead = raw_entry.get("write_ahead")
        if write_ahead is not None and not isinstance(write_ahead, Mapping):
            raise MigrationStateError(
                f"migration state entry {index}.write_ahead must be an object"
            )
        completion = raw_entry.get("mechanical_completion")
        if completion is not None:
            if not isinstance(completion, Mapping):
                raise MigrationStateError(
                    "migration state entry "
                    f"{index}.mechanical_completion must be an object"
                )
            completion_wal = completion.get("wal")
            if completion_wal is not None and not isinstance(completion_wal, Mapping):
                raise MigrationStateError(
                    "migration state entry "
                    f"{index}.mechanical_completion.wal must be an object"
                )
        entries.append(raw_entry)
    return entries


def _validate_state_top_level(
    state: object,
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    """Validate only top-level fields used before deeper state verification."""
    if not isinstance(state, Mapping):
        raise MigrationStateError("migration state must be an object")
    version = state.get("version")
    if type(version) is not int or version != STATE_VERSION:
        raise MigrationStateError("unsupported migration state version")
    vault_path = state.get("vault_path")
    if (
        type(vault_path) is not str
        or not vault_path.strip()
        or "\x00" in vault_path
        or not os.path.isabs(vault_path)
    ):
        raise MigrationStateError(
            "migration state vault_path must be a non-empty absolute string"
        )
    entries = _validate_state_entries(state.get("entries"))
    source_count = state.get("source_count")
    if (
        type(source_count) is not int
        or source_count < 0
        or source_count != len(entries)
    ):
        raise MigrationStateError(
            "migration state source_count must equal entries length"
        )
    for field in ("source_set_hash", "plan_hash"):
        value = state.get(field)
        if type(value) is not str or not value:
            raise MigrationStateError(
                f"migration state {field} must be a non-empty string"
            )
    return state, entries


def _state_vault_path(state: Mapping[str, Any]) -> Path:
    validated, _ = _validate_state_top_level(state)
    vault_path = validated["vault_path"]
    assert isinstance(vault_path, str)
    return Path(vault_path).resolve()


def _mechanical_wal_is_allowed(
    record: object,
    *,
    plan: Mapping[str, Any],
    entry: Mapping[str, Any],
    plan_hash: str,
) -> bool:
    if not isinstance(record, Mapping):
        return False
    old_identity = (
        entry.get("frozen_full_sha256"),
        entry.get("frozen_body_sha256"),
        entry.get("frozen_frontmatter_sha256"),
    )
    candidate_identity = _mapping_identity(plan.get("candidate"))
    return (
        record.get("version") == WRITE_AHEAD_VERSION
        and record.get("plan_hash") == plan_hash
        and record.get("kind") == "mechanical"
        and record.get("updates") == plan.get("updates")
        and record.get("header_repair") == plan.get("header_repair")
        and _journal_identity(record, "old") == old_identity
        and _journal_identity(record, "candidate") == candidate_identity
    )


def _validate_plan_state(state: Mapping[str, Any]) -> str:
    _, entries = _validate_state_top_level(state)
    plan_hash = _mechanical_plan_hash(entries)
    if state.get("plan_hash") != plan_hash:
        raise MigrationStateError("immutable mechanical plan hash is invalid")
    for entry in entries:
        plan = entry.get("mechanical_plan")
        if not isinstance(plan, dict):
            raise MigrationStateError("immutable mechanical plan is missing")
        applied = entry.get("mechanical_plan_applied")
        if not isinstance(applied, bool):
            raise MigrationStateError("mechanical plan progress is invalid")
        has_mechanical_work = bool(plan.get("updates")) or (
            plan.get("header_repair") is not None
        )
        planned_candidate = _mapping_identity(plan.get("candidate"))
        if (
            has_mechanical_work
            and (
                planned_candidate is None
                or planned_candidate[0] is None
                or planned_candidate[1] != entry.get("frozen_body_sha256")
            )
        ) or (not has_mechanical_work and plan.get("candidate") is not None):
            raise MigrationStateError("immutable mechanical candidate is invalid")
        if entry.get("mechanical_provenance") != plan.get("provenance"):
            raise MigrationStateError("mechanical plan provenance was modified")
        completion = entry.get("mechanical_completion")
        if applied:
            if entry.get("updates") != {} or entry.get("header_repair") is not None:
                raise MigrationStateError("applied mechanical plan was modified")
            if has_mechanical_work:
                if (
                    not isinstance(completion, Mapping)
                    or completion.get("version") != MECHANICAL_COMPLETION_VERSION
                    or not _mechanical_wal_is_allowed(
                        completion.get("wal"),
                        plan=plan,
                        entry=entry,
                        plan_hash=plan_hash,
                    )
                ):
                    raise MigrationStateError(
                        "mechanical completion evidence is invalid"
                    )
                semantic = entry.get("semantic")
                if (
                    not isinstance(semantic, Mapping)
                    or (
                        semantic.get("frozen_full_sha256"),
                        entry.get("frozen_body_sha256"),
                        semantic.get("frozen_frontmatter_sha256"),
                    )
                    != planned_candidate
                ):
                    raise MigrationStateError(
                        "mechanical completion candidate is not frozen"
                    )
                if (
                    semantic.get("status") != "complete"
                    and (
                        entry.get("expected_full_sha256"),
                        entry.get("expected_body_sha256"),
                        entry.get("expected_frontmatter_sha256"),
                    )
                    != planned_candidate
                ):
                    raise MigrationStateError(
                        "mechanical completion does not match expected candidate"
                    )
            elif completion is not None:
                raise MigrationStateError(
                    "empty mechanical plan cannot have completion evidence"
                )
        elif entry.get("updates") != plan.get("updates") or entry.get(
            "header_repair"
        ) != plan.get("header_repair"):
            raise MigrationStateError("pending mechanical plan was modified")
        elif completion is not None:
            raise MigrationStateError(
                "pending mechanical plan cannot have completion evidence"
            )
        record = entry.get("write_ahead")
        if record is None:
            continue
        if not isinstance(record, dict) or record.get("plan_hash") != plan_hash:
            raise MigrationStateError("write-ahead plan binding is invalid")
        if record.get("candidate_body_sha256") != entry.get("frozen_body_sha256"):
            raise MigrationStateError("write-ahead candidate changed frozen body")
        if record.get("kind") == "mechanical":
            if applied or not _mechanical_wal_is_allowed(
                record,
                plan=plan,
                entry=entry,
                plan_hash=plan_hash,
            ):
                raise MigrationStateError("mechanical WAL candidate is not allowed")
        elif record.get("kind") == "semantic":
            if not applied or record.get("header_repair") is not None:
                raise MigrationStateError("semantic WAL candidate is not allowed")
            try:
                validate_semantic_payload(
                    record.get("updates", {}),
                    entry.get("semantic", {}).get("requested_fields", ()),
                )
            except FrontmatterError as exc:
                raise MigrationStateError(
                    "semantic WAL candidate is not allowed"
                ) from exc
        else:
            raise MigrationStateError("write-ahead journal kind is invalid")
    return plan_hash


def write_inventory_report(
    inventory: Mapping[str, Any],
    output_dir: Path,
    *,
    state: Mapping[str, Any] | None = None,
) -> Path:
    root = Path(str(inventory["vault_path"])).resolve()
    destination = _ensure_external(root, output_dir)[0]
    destination.mkdir(parents=True, exist_ok=True)
    report = destination / "inventory.json"
    report_payload = dict(inventory)
    if state is not None:
        report_payload["projected"] = projected_migration_summary(state)
    elif not inventory["coverage_complete"]:
        report_payload["projected"] = {
            "available": False,
            "reason": "coverage_incomplete",
        }
    _atomic_external_write(
        report,
        json.dumps(
            report_payload, ensure_ascii=False, indent=2, sort_keys=True
        ).encode()
        + b"\n",
    )
    summary = "\n".join(
        (
            "# Frontmatter inventory",
            "",
            f"- source_count: {inventory['source_count']}",
            f"- coverage_complete: {inventory['coverage_complete']}",
            (
                "- projected unserviceable: "
                f"{len(report_payload.get('projected', {}).get('unserviceable', []))}"
            ),
            (f"- symlink directories: {inventory.get('symlink_directory_count', 0)}"),
            (
                "- safe accounted aliases: "
                f"{inventory.get('safe_accounted_alias_count', 0)}"
            ),
            (
                "- blocking symlink directories: "
                f"{inventory.get('blocking_symlink_directory_count', 0)}"
            ),
            "",
        )
    )
    _atomic_external_write(destination / "inventory.md", summary.encode())
    return report


def build_migration_state(inventory: Mapping[str, Any]) -> dict[str, Any]:
    if inventory.get("version") != STATE_VERSION or not inventory.get(
        "coverage_complete"
    ):
        raise MigrationStateError("inventory is incomplete or unsupported")
    entries: list[dict[str, Any]] = []
    for source in inventory["entries"]:
        unresolved = set(source["missing_fields"]) | set(source["invalid_fields"])
        semantic = sorted(unresolved & _SEMANTIC_FIELDS)
        epistemic = sorted(unresolved & _EPISTEMIC_FIELDS)
        if "epistemic_metadata" in unresolved:
            epistemic = sorted(_EPISTEMIC_FIELDS)
        semantic_requested = tuple(dict.fromkeys((*semantic, *epistemic)))
        mechanical = set(source["planned_updates"])
        classified = {field: "mechanical" for field in sorted(mechanical & unresolved)}
        classified.update({field: "semantic" for field in semantic_requested})
        unserviceable = sorted(unresolved - set(classified))
        if source["parse_error"]:
            unserviceable.append("frontmatter")
        ready = (
            source["source_state"] == "regular"
            and not source["parse_error"]
            and not unresolved
            and not source["planned_updates"]
            and not source["header_repair"]
        )
        planned_updates = dict(source["planned_updates"])
        header_repair = source["header_repair"]
        mechanical_provenance = deepcopy(source["mechanical_provenance"])
        entries.append(
            {
                "path": source["path"],
                "profile": source["profile"],
                "expected_full_sha256": source["full_sha256"],
                "expected_body_sha256": source["body_sha256"],
                "expected_frontmatter_sha256": source["frontmatter_sha256"],
                "frozen_full_sha256": source["full_sha256"],
                "frozen_body_sha256": source["body_sha256"],
                "frozen_frontmatter_sha256": source["frontmatter_sha256"],
                "updates": dict(planned_updates),
                "mechanical_provenance": deepcopy(mechanical_provenance),
                "header_repair": header_repair,
                "mechanical_plan": {
                    "updates": dict(planned_updates),
                    "header_repair": header_repair,
                    "provenance": mechanical_provenance,
                    "candidate": deepcopy(source["mechanical_candidate"]),
                },
                "mechanical_plan_applied": not planned_updates
                and header_repair is None,
                "mechanical_completion": None,
                "field_classification": classified,
                "unserviceable_fields": unserviceable,
                "status": "complete" if ready else "pending",
                "applied_full_sha256": None,
                "candidate_full_sha256": None,
                "error": source["parse_error"],
                "write_ahead": None,
                "semantic": {
                    "requested_fields": semantic_requested,
                    "status": "pending" if semantic_requested else "not_required",
                    "full_sha256": source["full_sha256"],
                    "frontmatter_sha256": source["frontmatter_sha256"],
                    "frozen_full_sha256": source["full_sha256"],
                    "frozen_frontmatter_sha256": source["frontmatter_sha256"],
                    "error": None,
                },
            }
        )
    state = {
        "version": STATE_VERSION,
        "created_at": _utc_now(),
        "vault_path": inventory["vault_path"],
        "source_count": inventory["source_count"],
        "source_set_hash": _source_set_hash(entries),
        "entries": entries,
    }
    state["plan_hash"] = _mechanical_plan_hash(entries)
    return state


def projected_migration_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    """Report every unresolved field as mechanical or semantic before any write."""
    _, entries = _validate_state_top_level(state)
    classifications = {"mechanical": 0, "semantic": 0}
    unserviceable: list[dict[str, Any]] = []
    for entry in entries:
        field_classification = entry.get("field_classification")
        if not isinstance(field_classification, Mapping):
            raise MigrationStateError(
                "migration state field_classification must be an object"
            )
        for kind in field_classification.values():
            classifications[kind] = classifications.get(kind, 0) + 1
        if entry.get("unserviceable_fields"):
            unserviceable.append(
                {"path": entry["path"], "fields": entry["unserviceable_fields"]}
            )
    return {
        "entries": len(entries),
        "pending_entries": sum(entry["status"] == "pending" for entry in entries),
        "mechanical_field_count": classifications["mechanical"],
        "semantic_field_count": classifications["semantic"],
        "unserviceable": unserviceable,
    }


def load_migration_state(state_path: Path) -> dict[str, Any]:
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationStateError(f"cannot load migration state: {exc}") from exc
    if not isinstance(state, dict):
        raise MigrationStateError("migration state must be an object")
    _, entries = _validate_state_top_level(state)
    vault_path = _state_vault_path(state)
    _ensure_external(vault_path, state_path)
    if state.get("source_set_hash") != _source_set_hash(entries):
        raise MigrationStateError("state source set hash is invalid")
    _validate_plan_state(state)
    return state


def save_migration_state(state_path: Path, state: Mapping[str, Any]) -> None:
    _validate_plan_state(state)
    root = _state_vault_path(state)
    path = _ensure_external(root, state_path)[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_external_write(
        path,
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode()
        + b"\n",
    )


def _write_ahead_record(
    entry: Mapping[str, Any],
    *,
    plan_hash: str,
    kind: str,
    candidate: Any,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "version": WRITE_AHEAD_VERSION,
        "plan_hash": plan_hash,
        "kind": kind,
        "old_full_sha256": entry["expected_full_sha256"],
        "old_body_sha256": entry["expected_body_sha256"],
        "old_frontmatter_sha256": entry["expected_frontmatter_sha256"],
        "candidate_full_sha256": candidate.full_sha256,
        "candidate_body_sha256": candidate.body_sha256,
        "candidate_frontmatter_sha256": candidate.frontmatter_sha256,
        "updates": dict(updates),
        "header_repair": entry.get("header_repair"),
    }


def _stage_write_ahead(
    state: dict[str, Any],
    entry: dict[str, Any],
    *,
    kind: str,
    candidate: Any,
    updates: Mapping[str, Any],
    state_path: Path,
    backup_manifest_path: Path,
    proof_path: Path,
    writer_states: Callable[[], Mapping[str, str]],
    writer_processes: Callable[[Path], Iterable[int]],
) -> Mapping[str, Any]:
    plan_hash = _validate_plan_state(state)
    record = _write_ahead_record(
        entry,
        plan_hash=plan_hash,
        kind=kind,
        candidate=candidate,
        updates=updates,
    )
    existing = entry.get("write_ahead")
    if existing is not None:
        if not isinstance(existing, dict):
            raise MigrationStateError("write-ahead journal is invalid")
        if existing != record:
            raise MigrationStateError("write-ahead candidate does not match retry")
        return existing
    entry["write_ahead"] = record
    _validate_plan_state(state)
    refresh_backup_gate(
        state,
        backup_manifest_path=backup_manifest_path,
        proof_path=proof_path,
        writer_states=writer_states,
        writer_processes=writer_processes,
    )
    save_migration_state(state_path, state)
    return record


def _journal_identity(record: Mapping[str, Any], prefix: str) -> tuple[Any, Any, Any]:
    return (
        record.get(f"{prefix}_full_sha256"),
        record.get(f"{prefix}_body_sha256"),
        record.get(f"{prefix}_frontmatter_sha256"),
    )


def _content_identity(content: bytes) -> tuple[str, str | None, str | None]:
    try:
        header, body, _ = split_frontmatter_bytes(content)
    except FrontmatterError:
        header, body = None, None
    return (
        _sha256(content),
        _sha256(body) if body is not None else None,
        _sha256(header) if header is not None else None,
    )


def _finalize_write_ahead(
    entry: dict[str, Any],
    record: Mapping[str, Any],
) -> None:
    candidate_full, candidate_body, candidate_frontmatter = _journal_identity(
        record, "candidate"
    )
    entry["expected_full_sha256"] = candidate_full
    entry["expected_body_sha256"] = candidate_body
    entry["expected_frontmatter_sha256"] = candidate_frontmatter
    entry["applied_full_sha256"] = candidate_full
    entry["status"], entry["error"] = "applied", None
    semantic = entry["semantic"]
    semantic["full_sha256"] = candidate_full
    semantic["frontmatter_sha256"] = candidate_frontmatter
    if record["kind"] == "mechanical":
        entry["updates"] = {}
        entry["header_repair"] = None
        entry["mechanical_plan_applied"] = True
        entry["mechanical_completion"] = {
            "version": MECHANICAL_COMPLETION_VERSION,
            "wal": deepcopy(dict(record)),
        }
        semantic["frozen_full_sha256"] = candidate_full
        semantic["frozen_frontmatter_sha256"] = candidate_frontmatter
    elif record["kind"] == "semantic":
        semantic["status"], semantic["error"] = "complete", None
    else:
        raise MigrationStateError("write-ahead journal kind is invalid")
    entry["write_ahead"] = None


def reconcile_migration_state(
    state: dict[str, Any],
    *,
    state_path: Path,
    manifest: VaultManifest,
    backup_manifest_path: Path | None = None,
    proof_path: Path | None = None,
    writer_states: Callable[[], Mapping[str, str]] | None = None,
    writer_processes: Callable[[Path], Iterable[int]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Reconcile durable intent with current Markdown after an interrupted commit."""
    root = _state_vault_path(state)
    plan_hash = _validate_plan_state(state)
    retried: list[str] = []
    finalized: list[str] = []
    has_journal = any(
        entry.get("write_ahead") is not None for entry in state["entries"]
    )
    if has_journal:
        if backup_manifest_path is None or proof_path is None:
            raise MigrationStateError(
                "write-ahead reconciliation requires backup manifest and proof"
            )
        states_reader = writer_states or _writer_states
        process_reader = writer_processes or _project_writer_processes
        _assert_writers_quiesced(
            states_reader(),
            process_reader(root.parent),
        )
        _validate_frozen_backup(state, backup_manifest_path)
        proof = _load_json_external(root, proof_path, "quiesce proof")
        if (
            proof.get("source_set_hash") != state.get("source_set_hash")
            or proof.get("expected_set_hash") != _expected_set_hash(state["entries"])
            or proof.get("plan_hash") != plan_hash
        ):
            raise MigrationStateError(
                "write-ahead proof does not match migration plan/state"
            )
        if proof.get("wal_set_hash") != _wal_set_hash(state["entries"]):
            raise MigrationStateError(
                "write-ahead candidate is not authorized by quiesce proof"
            )
    with vault_write_lock(root):
        for entry in state["entries"]:
            record = entry.get("write_ahead")
            if record is None:
                continue
            if (
                not isinstance(record, dict)
                or record.get("version") != WRITE_AHEAD_VERSION
                or record.get("kind") not in {"mechanical", "semantic"}
                or _journal_identity(record, "old")
                != (
                    entry.get("expected_full_sha256"),
                    entry.get("expected_body_sha256"),
                    entry.get("expected_frontmatter_sha256"),
                )
            ):
                raise MigrationStateError("write-ahead journal is invalid")
            content, _ = safe_read_vault_markdown(root, entry["path"])
            identity = _content_identity(content)
            if identity == _journal_identity(record, "old"):
                entry["status"], entry["error"] = "pending", None
                save_migration_state(state_path, state)
                retried.append(entry["path"])
                continue
            if identity != _journal_identity(record, "candidate"):
                entry["status"], entry["error"] = (
                    "stale",
                    "current Markdown matches neither journal old nor candidate hash",
                )
                save_migration_state(state_path, state)
                raise MigrationStateError(entry["error"])
            document = parse_frontmatter_bytes(content)
            if document.body_sha256 != entry.get("frozen_body_sha256"):
                raise MigrationStateError(
                    "write-ahead candidate violates frozen body invariant"
                )
            if record["kind"] == "semantic":
                route, missing, invalid = validate_document(
                    entry["path"], document, manifest
                )
                if route.name != entry["profile"] or missing or invalid:
                    raise MigrationStateError(
                        "write-ahead semantic candidate is not profile-valid"
                    )
            _finalize_write_ahead(entry, record)
            save_migration_state(state_path, state)
            finalized.append(entry["path"])
    return {"retried": tuple(retried), "finalized": tuple(finalized)}


def _systemctl_user_environment() -> dict[str, str] | None:
    """Return an explicit user-bus environment, or fail closed when absent."""
    runtime_dir = Path(f"/run/user/{os.getuid()}")
    bus_path = runtime_dir / "bus"
    if not runtime_dir.is_dir() or not bus_path.exists():
        return None
    environment = os.environ.copy()
    environment["XDG_RUNTIME_DIR"] = str(runtime_dir)
    environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
    return environment


def _writer_states() -> dict[str, str]:
    """Read unit states through the correct user bus; unavailable is blocking."""
    environment = _systemctl_user_environment()
    if environment is None:
        return {unit: "bus-unavailable" for unit in KNOWN_WRITER_UNITS}
    states: dict[str, str] = {}
    for unit in KNOWN_WRITER_UNITS:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=ActiveState", "--value"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=environment,
        )
        state = result.stdout.strip()
        states[unit] = state if result.returncode == 0 and state else "systemctl-error"
    return states


def _is_project_writer_command(command: bytes) -> bool:
    """Return whether a project-rooted process can mutate vault content."""
    if b"run_frontmatter_migration" in command:
        return False
    arguments = set(command.replace(b"\0", b" ").split())
    if b"--dry-run" in arguments:
        return False
    if b"d_brain" in command:
        return True
    if b"memory-engine.py" not in command:
        return False
    return any(
        command_name in arguments
        for command_name in (
            b"init",
            b"daily",
            b"touch",
            b"decay",
            b"supersede",
            b"recover-supersession",
        )
    )


def _project_writer_processes(project_root: Path) -> tuple[int, ...]:
    """Find non-migration vault writers rooted in this checkout via ``/proc``."""
    root = Path(project_root).resolve()
    found: list[int] = []
    for candidate in Path("/proc").iterdir():
        if not candidate.name.isdecimal():
            continue
        pid = int(candidate.name)
        if pid == os.getpid():
            continue
        try:
            cwd = Path(os.readlink(candidate / "cwd")).resolve()
            command = (candidate / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if cwd not in {root, root / "vault"} or not _is_project_writer_command(command):
            continue
        found.append(pid)
    return tuple(sorted(found))


def _assert_writers_quiesced(
    states: Mapping[str, str], writer_pids: Iterable[int] = ()
) -> None:
    blocked = [unit for unit in KNOWN_WRITER_UNITS if states.get(unit) != "inactive"]
    blocked.extend(f"pid:{pid}" for pid in writer_pids)
    if blocked:
        raise WriterQuiescenceError(
            "known vault writers are active or unverifiable: " + ", ".join(blocked)
        )


def _safe_archive_member_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise MigrationStateError("backup archive contains an unsafe member path")
    return path.as_posix()


def _tar_member_mtime_ns(member: tarfile.TarInfo) -> int:
    raw = member.pax_headers.get("mtime")
    if raw is None:
        return int(member.mtime) * 1_000_000_000
    try:
        return int(Decimal(raw) * 1_000_000_000)
    except (InvalidOperation, ValueError) as exc:
        raise MigrationStateError("backup archive member timestamp is invalid") from exc


def _validate_backup_archive(
    archive: Path, records: Iterable[Mapping[str, Any]]
) -> None:
    """Verify a durable archive covers exactly the frozen regular-file records."""
    record_list = list(records)
    expected = {str(record["path"]): record for record in record_list}
    if len(expected) != len(record_list):
        raise MigrationStateError("backup archive records contain duplicate paths")
    try:
        with tarfile.open(archive, "r") as tar:
            members = tar.getmembers()
            if len(members) != len(expected):
                raise MigrationStateError("backup archive member count is invalid")
            actual = {
                _safe_archive_member_path(member.name): member for member in members
            }
            if set(actual) != set(expected):
                raise MigrationStateError("backup archive member paths are invalid")
            for path, member in actual.items():
                if not member.isreg():
                    raise MigrationStateError(
                        "backup archive contains a non-regular member"
                    )
                record = expected[path]
                metadata = {
                    "mode": member.mode,
                    "uid": member.uid,
                    "gid": member.gid,
                    "mtime_ns": _tar_member_mtime_ns(member),
                }
                if any(metadata[field] != record.get(field) for field in metadata):
                    raise MigrationStateError(
                        "backup archive member metadata is invalid"
                    )
                source = tar.extractfile(member)
                if source is None or _sha256(source.read()) != record["full_sha256"]:
                    raise MigrationStateError("backup archive member hash is invalid")
    except (OSError, tarfile.TarError) as exc:
        raise MigrationStateError(f"backup archive is unreadable: {exc}") from exc


def _replace_durable_archive(temporary: Path, archive: Path) -> None:
    os.replace(temporary, archive)
    directory_fd = os.open(archive.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_quiesce_proof(
    state: Mapping[str, Any],
    proof_path: Path,
    *,
    states: Mapping[str, str],
    processes: Iterable[int],
) -> None:
    plan_hash = _validate_plan_state(state)
    proof_data = {
        "version": STATE_VERSION,
        "created_at": _utc_now(),
        "vault_path": str(_state_vault_path(state)),
        "source_set_hash": state["source_set_hash"],
        "expected_set_hash": _expected_set_hash(state["entries"]),
        "plan_hash": plan_hash,
        "wal_set_hash": _wal_set_hash(state["entries"]),
        "writers": dict(states),
        "writer_pids": tuple(processes),
    }
    _atomic_external_write(
        proof_path,
        json.dumps(proof_data, ensure_ascii=False, indent=2, sort_keys=True).encode()
        + b"\n",
    )


def create_backup_gate(
    state: Mapping[str, Any],
    *,
    backup_dir: Path,
    proof_path: Path,
    writer_states: Callable[[], Mapping[str, str]] = _writer_states,
    writer_processes: Callable[[Path], Iterable[int]] = _project_writer_processes,
) -> tuple[Path, Path]:
    """Create the immutable backup, or refresh its proof for resumed state."""
    root = _state_vault_path(state)
    destination, proof = _ensure_external(root, backup_dir, proof_path)
    manifest_path = destination / "backup-manifest.json"
    if manifest_path.exists():
        return refresh_backup_gate(
            state,
            backup_manifest_path=manifest_path,
            proof_path=proof,
            writer_states=writer_states,
            writer_processes=writer_processes,
        )
    states = dict(writer_states())
    processes = tuple(writer_processes(root.parent))
    _assert_writers_quiesced(states, processes)
    plan_hash = _validate_plan_state(state)
    if any(
        entry.get("expected_full_sha256") != entry.get("frozen_full_sha256")
        or entry.get("expected_body_sha256") != entry.get("frozen_body_sha256")
        or entry.get("expected_frontmatter_sha256")
        != entry.get("frozen_frontmatter_sha256")
        for entry in state["entries"]
    ):
        raise MigrationStateError(
            "partial state requires its existing immutable backup manifest"
        )
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "vault-frontmatter-source.tar"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.unlink(missing_ok=True)
    records: list[dict[str, Any]] = []
    temporary = destination / f".vault-frontmatter-source.{os.getpid()}.tar.tmp"
    try:
        with temporary.open("xb") as handle:
            with tarfile.open(
                fileobj=handle, mode="w", format=tarfile.PAX_FORMAT
            ) as tar:
                for entry in state["entries"]:
                    content, file_stat = safe_read_vault_markdown(root, entry["path"])
                    header, body, _ = split_frontmatter_bytes(content)
                    header_hash = _sha256(header) if header is not None else None
                    if (
                        _sha256(content) != entry["frozen_full_sha256"]
                        or _sha256(body) != entry["frozen_body_sha256"]
                        or header_hash != entry["frozen_frontmatter_sha256"]
                    ):
                        raise MigrationStateError("source changed before backup")
                    info = tarfile.TarInfo(entry["path"])
                    mtime_seconds, mtime_nanoseconds = divmod(
                        file_stat.st_mtime_ns, 1_000_000_000
                    )
                    info.size, info.mode, info.uid, info.gid, info.mtime = (
                        len(content),
                        file_stat.st_mode & 0o7777,
                        file_stat.st_uid,
                        file_stat.st_gid,
                        mtime_seconds,
                    )
                    info.pax_headers = {
                        "mtime": f"{mtime_seconds}.{mtime_nanoseconds:09d}"
                    }
                    tar.addfile(info, io.BytesIO(content))
                    records.append(
                        {
                            "path": entry["path"],
                            "full_sha256": _sha256(content),
                            "body_sha256": _sha256(body),
                            "frontmatter_sha256": header_hash,
                            "mode": info.mode,
                            "uid": info.uid,
                            "gid": info.gid,
                            "mtime_ns": file_stat.st_mtime_ns,
                        }
                    )
            handle.flush()
            os.fsync(handle.fileno())
        _validate_backup_archive(temporary, records)
        _replace_durable_archive(temporary, archive)
    except Exception:
        temporary.unlink(missing_ok=True)
        proof.unlink(missing_ok=True)
        raise
    archive_hash = _sha256(archive.read_bytes())
    backup = {
        "version": STATE_VERSION,
        "created_at": _utc_now(),
        "vault_path": str(root),
        "source_set_hash": state["source_set_hash"],
        "plan_hash": plan_hash,
        "archive": str(archive),
        "archive_sha256": archive_hash,
        "entries": records,
    }
    _atomic_external_write(
        manifest_path,
        json.dumps(backup, ensure_ascii=False, indent=2, sort_keys=True).encode()
        + b"\n",
    )
    _write_quiesce_proof(
        state,
        proof,
        states=states,
        processes=processes,
    )
    return manifest_path, proof


def _load_json_external(vault_path: Path, path: Path, label: str) -> dict[str, Any]:
    resolved = _ensure_external(vault_path, path)[0]
    try:
        result = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationStateError(f"cannot load {label}: {exc}") from exc
    if not isinstance(result, dict):
        raise MigrationStateError(f"{label} must be an object")
    return result


def _validate_frozen_backup(
    state: Mapping[str, Any], backup_manifest_path: Path
) -> dict[str, Any]:
    root = _state_vault_path(state)
    backup = _load_json_external(root, backup_manifest_path, "backup manifest")
    if (
        backup.get("version") != STATE_VERSION
        or backup.get("vault_path") != str(root)
        or backup.get("source_set_hash") != state.get("source_set_hash")
        or backup.get("plan_hash") != _validate_plan_state(state)
    ):
        raise MigrationStateError("backup does not match frozen migration state")
    archive = _ensure_external(root, Path(str(backup.get("archive", ""))))[0]
    if not archive.is_file() or _sha256(archive.read_bytes()) != backup.get(
        "archive_sha256"
    ):
        raise MigrationStateError("backup archive is missing or invalid")
    records = backup.get("entries")
    if not isinstance(records, list):
        raise MigrationStateError("backup manifest entries are invalid")
    _validate_backup_archive(archive, records)
    backup_entries = {
        entry.get("path"): entry for entry in records if isinstance(entry, dict)
    }
    if len(backup_entries) != state.get("source_count"):
        raise MigrationStateError("backup does not cover exact frozen source set")
    for entry in state["entries"]:
        saved = backup_entries.get(entry["path"])
        if (
            not saved
            or saved.get("full_sha256") != entry["frozen_full_sha256"]
            or saved.get("body_sha256") != entry["frozen_body_sha256"]
            or saved.get("frontmatter_sha256") != entry["frozen_frontmatter_sha256"]
        ):
            raise MigrationStateError("backup hashes do not match frozen state")
    return backup


def _validate_expected_sources(state: Mapping[str, Any]) -> None:
    root = _state_vault_path(state)
    for entry in state["entries"]:
        if entry.get("expected_body_sha256") != entry.get("frozen_body_sha256"):
            raise MigrationStateError("immutable frozen body hash changed in state")
        content, _ = safe_read_vault_markdown(root, entry["path"])
        try:
            header, body, _ = split_frontmatter_bytes(content)
        except FrontmatterError:
            header, body = None, None
        if (
            _sha256(content) != entry["expected_full_sha256"]
            or (_sha256(body) if body is not None else None)
            != entry["expected_body_sha256"]
            or (_sha256(header) if header is not None else None)
            != entry["expected_frontmatter_sha256"]
        ):
            raise MigrationStateError(
                "vault source changed after backup/current expected state"
            )


def refresh_backup_gate(
    state: Mapping[str, Any],
    *,
    backup_manifest_path: Path,
    proof_path: Path,
    writer_states: Callable[[], Mapping[str, str]] = _writer_states,
    writer_processes: Callable[[Path], Iterable[int]] = _project_writer_processes,
) -> tuple[Path, Path]:
    """Revalidate immutable backup/current progress and issue a fresh proof."""
    root = _state_vault_path(state)
    backup_path, proof = _ensure_external(root, backup_manifest_path, proof_path)
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.unlink(missing_ok=True)
    states = dict(writer_states())
    processes = tuple(writer_processes(root.parent))
    _assert_writers_quiesced(states, processes)
    _validate_frozen_backup(state, backup_path)
    _validate_expected_sources(state)
    _write_quiesce_proof(
        state,
        proof,
        states=states,
        processes=processes,
    )
    return backup_path, proof


def verify_backup_gate(
    state: Mapping[str, Any],
    *,
    backup_manifest_path: Path,
    proof_path: Path,
    writer_states: Callable[[], Mapping[str, str]] = _writer_states,
    writer_processes: Callable[[Path], Iterable[int]] = _project_writer_processes,
) -> None:
    root = _state_vault_path(state)
    proof = _load_json_external(root, proof_path, "quiesce proof")
    _validate_frozen_backup(state, backup_manifest_path)
    if proof.get("source_set_hash") != state.get("source_set_hash"):
        raise MigrationStateError("backup/proof source set does not match state")
    if proof.get("plan_hash") != _validate_plan_state(state):
        raise MigrationStateError("backup/proof mechanical plan does not match state")
    if proof.get("wal_set_hash") != _wal_set_hash(state["entries"]):
        raise MigrationStateError("quiesce proof does not authorize current WAL")
    if proof.get("expected_set_hash") != _expected_set_hash(state["entries"]):
        raise MigrationStateError("quiesce proof does not match expected state")
    try:
        age = (
            datetime.now(UTC) - datetime.fromisoformat(str(proof["created_at"]))
        ).total_seconds()
    except (KeyError, ValueError) as exc:
        raise MigrationStateError("quiesce proof timestamp is invalid") from exc
    if age < 0 or age > QUIESCE_MAX_AGE_SECONDS:
        raise MigrationStateError("quiesce proof is stale")
    _assert_writers_quiesced(proof.get("writers", {}), proof.get("writer_pids", ()))
    _assert_writers_quiesced(writer_states(), writer_processes(root.parent))
    _validate_expected_sources(state)


def _entry_complete(
    entry: Mapping[str, Any], manifest: VaultManifest, vault_path: Path
) -> bool:
    if (
        entry.get("status") not in {"complete", "applied"}
        or entry.get("updates")
        or entry.get("header_repair")
        or entry.get("unserviceable_fields")
        or entry["semantic"]["status"] == "pending"
    ):
        return False
    try:
        content, _ = safe_read_vault_markdown(vault_path, entry["path"])
        route, missing, invalid = validate_document(
            entry["path"], parse_frontmatter_bytes(content), manifest
        )
    except FrontmatterError:
        return False
    return route.name == entry["profile"] and not missing and not invalid


def _recheck_before_commit(
    state: Mapping[str, Any],
    *,
    backup_manifest_path: Path,
    proof_path: Path,
    writer_states: Callable[[], Mapping[str, str]],
    writer_processes: Callable[[Path], Iterable[int]],
) -> None:
    """Revalidate the external safety gate as the last step before a write."""
    verify_backup_gate(
        state,
        backup_manifest_path=backup_manifest_path,
        proof_path=proof_path,
        writer_states=writer_states,
        writer_processes=writer_processes,
    )


def _recheck_after_commit(
    vault_path: Path,
    *,
    writer_states: Callable[[], Mapping[str, str]],
    writer_processes: Callable[[Path], Iterable[int]],
) -> None:
    """Abort after a commit if a writer appeared; never roll back blindly."""
    _assert_writers_quiesced(writer_states(), writer_processes(vault_path.parent))


def apply_migration_state(
    state: dict[str, Any],
    *,
    manifest: VaultManifest,
    apply: bool,
    state_path: Path | None = None,
    backup_manifest_path: Path | None = None,
    proof_path: Path | None = None,
    writer_states: Callable[[], Mapping[str, str]] = _writer_states,
    writer_processes: Callable[[Path], Iterable[int]] = _project_writer_processes,
) -> dict[str, int]:
    root = _state_vault_path(state)
    if apply:
        if state_path is None or backup_manifest_path is None or proof_path is None:
            raise MigrationStateError(
                "apply requires external state, backup manifest, and quiesce proof"
            )
        assert backup_manifest_path is not None
        assert proof_path is not None
        assert state_path is not None
        reconciled = reconcile_migration_state(
            state,
            state_path=state_path,
            manifest=manifest,
            backup_manifest_path=backup_manifest_path,
            proof_path=proof_path,
            writer_states=writer_states,
            writer_processes=writer_processes,
        )
        if reconciled["finalized"]:
            refresh_backup_gate(
                state,
                backup_manifest_path=backup_manifest_path,
                proof_path=proof_path,
                writer_states=writer_states,
                writer_processes=writer_processes,
            )
        verify_backup_gate(
            state,
            backup_manifest_path=backup_manifest_path,
            proof_path=proof_path,
            writer_states=writer_states,
            writer_processes=writer_processes,
        )
    summary = {
        "planned": 0,
        "applied": 0,
        "complete": 0,
        "pending": 0,
        "stale": 0,
        "errors": 0,
        "malformed": 0,
    }

    def checkpoint() -> None:
        if apply and state_path is not None:
            save_migration_state(state_path, state)

    with vault_write_lock(root) if apply else _null_lock():
        for entry in state["entries"]:
            if entry.get("error") and entry.get("expected_full_sha256") is None:
                summary["malformed"] += 1
                continue
            try:
                content, _ = safe_read_vault_markdown(root, entry["path"])
                expected = entry["expected_full_sha256"]
                if _sha256(content) != expected:
                    entry["status"], entry["error"] = (
                        "stale",
                        "source full hash changed after inventory",
                    )
                    summary["stale"] += 1
                    checkpoint()
                    continue
                updates = entry["updates"]
                header_repair = entry.get("header_repair")
                candidate_base = content
                if header_repair:
                    repaired = _known_header_repair(entry["path"], content)
                    if repaired is None or repaired[1] != header_repair:
                        raise MigrationStateError(
                            "known header repair no longer matches"
                        )
                    candidate_base = repaired[0]
                document = parse_frontmatter_bytes(candidate_base)
                if updates or header_repair:
                    summary["planned"] += 1
                    if apply:
                        assert backup_manifest_path is not None
                        assert proof_path is not None
                        candidate = patch_frontmatter_bytes(candidate_base, updates)
                        candidate_document = parse_frontmatter_bytes(candidate)
                        if candidate_document.body_sha256 != document.body_sha256:
                            raise MigrationStateError("candidate changed Markdown body")
                        assert state_path is not None
                        _stage_write_ahead(
                            state,
                            entry,
                            kind="mechanical",
                            candidate=candidate_document,
                            updates=updates,
                            state_path=state_path,
                            backup_manifest_path=backup_manifest_path,
                            proof_path=proof_path,
                            writer_states=writer_states,
                            writer_processes=writer_processes,
                        )
                        _recheck_before_commit(
                            state,
                            backup_manifest_path=backup_manifest_path,
                            proof_path=proof_path,
                            writer_states=writer_states,
                            writer_processes=writer_processes,
                        )
                        atomic_replace_vault_markdown(
                            root,
                            entry["path"],
                            expected_full_sha256=expected,
                            content=candidate,
                        )
                        _recheck_after_commit(
                            root,
                            writer_states=writer_states,
                            writer_processes=writer_processes,
                        )
                        record = entry["write_ahead"]
                        assert isinstance(record, dict)
                        _finalize_write_ahead(entry, record)
                        summary["applied"] += 1
                        checkpoint()
                        refresh_backup_gate(
                            state,
                            backup_manifest_path=backup_manifest_path,
                            proof_path=proof_path,
                            writer_states=writer_states,
                            writer_processes=writer_processes,
                        )
                if _entry_complete(entry, manifest, root):
                    entry["status"] = "complete"
                    summary["complete"] += 1
                    checkpoint()
                else:
                    entry["status"] = "pending"
                    summary["pending"] += 1
                    checkpoint()
            except WriterQuiescenceError as exc:
                if entry.get("write_ahead") is not None:
                    entry["status"], entry["error"] = "pending", None
                else:
                    entry["status"], entry["error"] = "error", str(exc)
                summary["errors"] += 1
                checkpoint()
                raise
            except (
                FrontmatterError,
                MigrationStateError,
                UnsafeVaultPathError,
                OSError,
                VaultLockError,
            ) as exc:
                entry["status"], entry["error"] = "error", str(exc)
                summary["errors"] += 1
                checkpoint()
    return summary


@contextmanager
def _null_lock() -> Iterator[None]:
    yield


def apply_semantic_result(
    state: dict[str, Any],
    *,
    manifest: VaultManifest,
    relative_path: str,
    payload: Mapping[str, Any],
    state_path: Path | None = None,
    backup_manifest_path: Path | None = None,
    proof_path: Path | None = None,
    writer_states: Callable[[], Mapping[str, str]] = _writer_states,
    writer_processes: Callable[[Path], Iterable[int]] = _project_writer_processes,
) -> None:
    if state_path is None or backup_manifest_path is None or proof_path is None:
        raise MigrationStateError(
            "semantic apply requires external state, backup manifest, and quiesce proof"
        )
    root = _state_vault_path(state)
    reconciled = reconcile_migration_state(
        state,
        state_path=state_path,
        manifest=manifest,
        backup_manifest_path=backup_manifest_path,
        proof_path=proof_path,
        writer_states=writer_states,
        writer_processes=writer_processes,
    )
    if reconciled["finalized"]:
        refresh_backup_gate(
            state,
            backup_manifest_path=backup_manifest_path,
            proof_path=proof_path,
            writer_states=writer_states,
            writer_processes=writer_processes,
        )
    if relative_path in reconciled["finalized"]:
        return
    verify_backup_gate(
        state,
        backup_manifest_path=backup_manifest_path,
        proof_path=proof_path,
        writer_states=writer_states,
        writer_processes=writer_processes,
    )
    matches = [entry for entry in state["entries"] if entry["path"] == relative_path]
    if len(matches) != 1:
        raise MigrationStateError("semantic result does not identify one state entry")
    entry = matches[0]
    semantic = entry["semantic"]
    if entry.get("status") != "pending" or semantic.get("status") != "pending":
        raise MigrationStateError("semantic result entry is not pending")
    values = validate_semantic_payload(payload, semantic["requested_fields"])
    with vault_write_lock(root):
        content, _ = safe_read_vault_markdown(root, relative_path)
        document = parse_frontmatter_bytes(content)
        if (
            document.full_sha256 != semantic["full_sha256"]
            or document.frontmatter_sha256 != semantic["frontmatter_sha256"]
        ):
            semantic["status"], semantic["error"] = (
                "stale",
                "source full/frontmatter hash changed before semantic apply",
            )
            save_migration_state(state_path, state)
            return
        candidate = patch_frontmatter_bytes(content, values)
        candidate_document = parse_frontmatter_bytes(candidate)
        route, missing, invalid = validate_document(
            relative_path, candidate_document, manifest
        )
        if route.name != entry["profile"] or missing or invalid:
            raise MigrationStateError(
                "semantic result does not produce a valid complete frontmatter document"
            )
        _stage_write_ahead(
            state,
            entry,
            kind="semantic",
            candidate=candidate_document,
            updates=values,
            state_path=state_path,
            backup_manifest_path=backup_manifest_path,
            proof_path=proof_path,
            writer_states=writer_states,
            writer_processes=writer_processes,
        )
        _recheck_before_commit(
            state,
            backup_manifest_path=backup_manifest_path,
            proof_path=proof_path,
            writer_states=writer_states,
            writer_processes=writer_processes,
        )
        atomic_replace_vault_markdown(
            root,
            relative_path,
            expected_full_sha256=document.full_sha256,
            content=candidate,
        )
        _recheck_after_commit(
            root,
            writer_states=writer_states,
            writer_processes=writer_processes,
        )
        record = entry["write_ahead"]
        assert isinstance(record, dict)
        _finalize_write_ahead(entry, record)
        save_migration_state(state_path, state)
        refresh_backup_gate(
            state,
            backup_manifest_path=backup_manifest_path,
            proof_path=proof_path,
            writer_states=writer_states,
            writer_processes=writer_processes,
        )


def validate_vault(vault_path: Path, manifest: VaultManifest) -> dict[str, Any]:
    inventory = inventory_vault(vault_path, manifest)
    entries = inventory["entries"]
    return {
        "source_count": inventory["source_count"],
        "coverage_complete": inventory["coverage_complete"],
        "blocking_symlink_directory_count": inventory[
            "blocking_symlink_directory_count"
        ],
        "blocking_symlink_directories": inventory["blocking_symlink_directories"],
        "valid": sum(
            not entry["parse_error"]
            and not entry["missing_fields"]
            and not entry["invalid_fields"]
            for entry in entries
        ),
        "malformed": sum(bool(entry["parse_error"]) for entry in entries),
        "missing": sum(bool(entry["missing_fields"]) for entry in entries),
        "invalid": sum(bool(entry["invalid_fields"]) for entry in entries),
        "unreadable": inventory["source_state_counts"]["unreadable"],
        "symlink": inventory["source_state_counts"]["symlink"],
        "racing": inventory["source_state_counts"]["racing"],
        "profile_counts": inventory["profile_counts"],
        "entries": entries,
    }
