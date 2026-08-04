"""Cooperative, vault-local writer lock shared by vault mutation services.

All cooperating writers must take this lock.  External editors that do not
cooperate must be quiesced before a multi-file vault mutation starts.
"""

from __future__ import annotations

import fcntl
import os
import stat
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class VaultLockError(RuntimeError):
    """Raised when a vault or its cooperative lock cannot be opened safely."""


def _open_directory_at(parent_fd: int, name: str) -> int:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )
    try:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
    except BaseException:
        os.close(descriptor)
        raise
    if not is_directory:
        os.close(descriptor)
        raise VaultLockError(f"{name} is not a directory")
    return descriptor


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    value = os.fstat(descriptor)
    return value.st_dev, value.st_ino


def _close_if_identity_matches(
    descriptor: int | None,
    expected_identity: tuple[int, int] | None,
) -> None:
    """Close an exposed descriptor only while it still names our inode."""
    if descriptor is None or expected_identity is None:
        return
    try:
        if _descriptor_identity(descriptor) == expected_identity:
            os.close(descriptor)
    except OSError:
        return


def open_vault_root_nofollow(vault_path: Path) -> tuple[Path, int]:
    """Open a lexical absolute directory path without following any symlink."""
    supplied = Path(vault_path)
    root = supplied if supplied.is_absolute() else Path.cwd() / supplied
    if ".." in root.parts:
        raise VaultLockError("vault root path cannot contain '..'")
    current_fd = os.open(
        "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        for component in root.parts[1:]:
            next_fd = _open_directory_at(current_fd, component)
            os.close(current_fd)
            current_fd = next_fd
        return root, current_fd
    except (OSError, VaultLockError) as exc:
        os.close(current_fd)
        raise VaultLockError(
            "cannot open vault root safely without following symlinks"
        ) from exc


def ensure_vault_root_nofollow(vault_path: Path) -> tuple[Path, int]:
    """Open a lexical vault path, creating missing directory components safely."""
    supplied = Path(vault_path)
    root = supplied if supplied.is_absolute() else Path.cwd() / supplied
    if ".." in root.parts:
        raise VaultLockError("vault root path cannot contain '..'")
    current_fd = os.open(
        "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        for component in root.parts[1:]:
            try:
                next_fd = _open_directory_at(current_fd, component)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(current_fd)
                next_fd = _open_directory_at(current_fd, component)
            os.close(current_fd)
            current_fd = next_fd
        return root, current_fd
    except (OSError, VaultLockError) as exc:
        os.close(current_fd)
        raise VaultLockError(
            "cannot create vault root safely without following symlinks"
        ) from exc


_CAPABILITY_TOKEN = object()


@dataclass
class _LockLease:
    root_fd: int
    locks_fd: int
    active: bool = True


@dataclass(frozen=True, init=False, eq=False)
class VaultWriteLock:
    """Module-managed capability for one live cooperative critical section.

    This coordinates cooperating project writers.  It is not a security
    boundary against arbitrary same-UID or root code, which must be quiesced.
    """

    root: Path
    root_fd: int
    locks_fd: int
    _lease: _LockLease

    def __init__(
        self,
        root: Path,
        root_fd: int,
        locks_fd: int,
        *,
        _token: object | None = None,
        _lease: _LockLease | None = None,
    ) -> None:
        if _token is not _CAPABILITY_TOKEN or _lease is None:
            raise VaultLockError("VaultWriteLock cannot be constructed directly")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "root_fd", root_fd)
        object.__setattr__(self, "locks_fd", locks_fd)
        object.__setattr__(self, "_lease", _lease)


_LIVE_CAPABILITIES: weakref.WeakSet[VaultWriteLock] = weakref.WeakSet()


def require_live_vault_write_lock(lock: VaultWriteLock) -> None:
    """Reject capabilities not issued by a currently active lock context."""
    if not isinstance(lock, VaultWriteLock):
        raise VaultLockError("invalid vault-write lock capability")
    lease = getattr(lock, "_lease", None)
    if (
        not isinstance(lease, _LockLease)
        or not lease.active
        or lock not in _LIVE_CAPABILITIES
    ):
        raise VaultLockError("vault-write lock capability is not active")


def duplicate_live_vault_write_lock(lock: VaultWriteLock) -> tuple[int, int]:
    """Duplicate hidden lease descriptors without trusting exposed fd numbers."""
    require_live_vault_write_lock(lock)
    lease = lock._lease
    root_fd: int | None = None
    try:
        root_fd = os.dup(lease.root_fd)
        locks_fd = os.dup(lease.locks_fd)
        return root_fd, locks_fd
    except OSError as exc:
        if root_fd is not None:
            os.close(root_fd)
        raise VaultLockError(f"cannot duplicate vault-write capability: {exc}") from exc


@contextmanager
def vault_write_lock(
    vault_path: Path, *, _root_fd: int | None = None
) -> Iterator[VaultWriteLock]:
    """Serialize cooperating writes under ``vault/.locks/vault-write.lock``.

    The hidden root descriptor retains the directory flock even if a caller
    closes or reuses the exposed ``root_fd``.  This is cooperative locking, not
    isolation from arbitrary same-UID/root processes; those must be quiesced.
    """
    try:
        supplied = Path(vault_path)
        root = supplied if supplied.is_absolute() else Path.cwd() / supplied
        if ".." in root.parts:
            raise VaultLockError("vault root path cannot contain '..'")
        if _root_fd is None:
            root, lease_root_fd = open_vault_root_nofollow(root)
        else:
            lease_root_fd = os.dup(_root_fd)
            if not stat.S_ISDIR(os.fstat(lease_root_fd).st_mode):
                os.close(lease_root_fd)
                raise VaultLockError("provided vault root fd is not a directory")
    except OSError as exc:
        raise VaultLockError(f"cannot open vault root: {exc}") from exc
    lease_locks_fd: int | None = None
    lock_fd: int | None = None
    lease: _LockLease | None = None
    capability: VaultWriteLock | None = None
    public_root_fd: int | None = None
    public_locks_fd: int | None = None
    public_root_identity: tuple[int, int] | None = None
    public_locks_identity: tuple[int, int] | None = None
    root_locked = False
    try:
        fcntl.flock(lease_root_fd, fcntl.LOCK_EX)
        root_locked = True
        try:
            os.mkdir(".locks", 0o700, dir_fd=lease_root_fd)
        except FileExistsError:
            pass
        lease_locks_fd = _open_directory_at(lease_root_fd, ".locks")
        lock_fd = os.open(
            "vault-write.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=lease_locks_fd,
        )
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise VaultLockError("vault-write.lock is not a regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        public_root_fd = os.dup(lease_root_fd)
        public_locks_fd = os.dup(lease_locks_fd)
        public_root_identity = _descriptor_identity(public_root_fd)
        public_locks_identity = _descriptor_identity(public_locks_fd)
        lease = _LockLease(root_fd=lease_root_fd, locks_fd=lease_locks_fd)
        capability = VaultWriteLock(
            root=root,
            root_fd=public_root_fd,
            locks_fd=public_locks_fd,
            _token=_CAPABILITY_TOKEN,
            _lease=lease,
        )
        _LIVE_CAPABILITIES.add(capability)
        yield capability
    finally:
        if capability is not None:
            _LIVE_CAPABILITIES.discard(capability)
        if lease is not None:
            lease.active = False
        _close_if_identity_matches(public_locks_fd, public_locks_identity)
        _close_if_identity_matches(public_root_fd, public_root_identity)
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        if lease_locks_fd is not None:
            os.close(lease_locks_fd)
        if root_locked:
            fcntl.flock(lease_root_fd, fcntl.LOCK_UN)
        os.close(lease_root_fd)
