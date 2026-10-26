"""Shared "temp file + rename" writer for state and journal files.

Every service that used to carry its own mkstemp + os.replace copy goes
through here, so the file keeps its mode and group, the data and the
directory entry are fsynced, and a symlink at the target is refused instead
of being replaced. Vault notes have a stricter protocol of their own
(``frontmatter.write_vault_file_*``); this module is for everything else.

Deliberately imports nothing from ``d_brain`` so any service can use it.
It takes no locks: callers hold their own, as before.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# Some filesystems cannot fsync a directory; the file itself is already synced.
_DIR_FSYNC_IGNORED = {errno.EINVAL, errno.ENOTSUP, errno.EBADF}


def copy_owner_group(fd: int, *, gid: int, uid: int | None = None) -> None:
    """Give ``fd`` the wanted group (and owner when running as root).

    Best effort: without root the owner cannot be preserved, and a user who is
    not a member of ``gid`` gets EPERM. The write must not fail over that, so
    the file keeps what it was born with (mode and group stay readable for the
    rest of the group).
    """
    current = os.fstat(fd)
    wanted_uid = uid if uid is not None and os.geteuid() == 0 else -1
    if current.st_gid == gid and (wanted_uid == -1 or current.st_uid == wanted_uid):
        return
    try:
        os.fchown(fd, wanted_uid, gid)
    except OSError as exc:
        logger.warning(
            "cannot copy owner/group to %s: %s", _describe_fd(fd), exc
        )


def _describe_fd(fd: int) -> str:
    # /proc is Linux-only; elsewhere the number is all we can name.
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return f"fd {fd}"


_NO_HARDLINK_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP})


def _fsync_dir(dir_fd: int) -> None:
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if exc.errno not in _DIR_FSYNC_IGNORED:
            raise


def _write_new_in_place(
    dir_fd: int, name: str, data: bytes, mode: int, gid: int
) -> None:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        copy_owner_group(fd, gid=gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def atomic_write_file_at(
    dir_fd: int,
    name: str,
    data: bytes,
    *,
    new_file_mode: int = 0o660,
    require_absent: bool = False,
) -> None:
    """Atomically write ``data`` to ``name`` inside the directory ``dir_fd``.

    An existing regular file keeps its mode and group; a new one gets
    ``new_file_mode`` and the directory's group. A symlink at ``name`` raises
    ``ELOOP`` and is neither followed nor replaced. With ``require_absent`` an
    existing ``name`` raises ``FileExistsError`` (published by hard link, which
    never overwrites, so two writers cannot clobber each other).
    """
    if "/" in name or not name:
        raise ValueError(f"name must be a bare file name: {name!r}")
    try:
        target = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        target = None
    if target is not None and stat.S_ISLNK(target.st_mode):
        raise OSError(errno.ELOOP, "refusing to write through a symlink", name)
    if target is not None and require_absent:
        raise FileExistsError(errno.EEXIST, "target already exists", name)
    if target is not None and stat.S_ISREG(target.st_mode):
        mode = stat.S_IMODE(target.st_mode)
        gid = target.st_gid
    else:
        mode = new_file_mode
        gid = os.fstat(dir_fd).st_gid

    temp_name = f".{name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    fd = os.open(
        temp_name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    published = False
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        copy_owner_group(fd, gid=gid)
        # After chown, which clears setuid/setgid bits.
        os.fchmod(fd, mode)
        os.fsync(fd)
        if require_absent:
            try:
                os.link(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno not in _NO_HARDLINK_ERRNOS:
                    raise
                # No hard links on this filesystem: publish non-atomically.
                # The name is still unique (O_EXCL), only readers may see a
                # partly written file.
                _write_new_in_place(dir_fd, name, data, mode, gid)
        else:
            os.replace(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            published = True
        _fsync_dir(dir_fd)
    finally:
        os.close(fd)
        if not published:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except OSError:
                pass


def atomic_write_file_bytes(
    path: Path,
    data: bytes,
    *,
    new_file_mode: int = 0o660,
    require_absent: bool = False,
) -> None:
    """Atomically write bytes to ``path``, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        atomic_write_file_at(
            dir_fd,
            path.name,
            data,
            new_file_mode=new_file_mode,
            require_absent=require_absent,
        )
    finally:
        os.close(dir_fd)


def atomic_write_file_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    new_file_mode: int = 0o660,
) -> None:
    """Text twin of ``atomic_write_file_bytes``; newlines are written as-is."""
    atomic_write_file_bytes(path, text.encode(encoding), new_file_mode=new_file_mode)


def replace_durable(source: Path, destination: Path) -> None:
    """``os.replace`` plus an fsync of the directory, for streamed archives."""
    os.replace(source, destination)
    dir_fd = os.open(Path(destination).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _fsync_dir(dir_fd)
    finally:
        os.close(dir_fd)
