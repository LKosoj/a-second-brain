"""Shared atomic writer: modes, groups, symlinks, durability order, guard."""

from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path

import pytest

from d_brain.services import atomic_io
from d_brain.services.atomic_io import (
    atomic_write_file_at,
    atomic_write_file_bytes,
    atomic_write_file_text,
    copy_owner_group,
    replace_durable,
)


@pytest.fixture
def umask_077():
    old = os.umask(0o077)
    yield
    os.umask(old)


def _leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]


def test_writes_bytes_and_text_creating_parents(tmp_path: Path) -> None:
    atomic_write_file_bytes(tmp_path / "a" / "b" / "x.bin", b"\x00\x01")
    atomic_write_file_text(tmp_path / "t.txt", "a\r\nb\n")
    assert (tmp_path / "a" / "b" / "x.bin").read_bytes() == b"\x00\x01"
    assert (tmp_path / "t.txt").read_bytes() == b"a\r\nb\n"
    assert _leftovers(tmp_path) == []


def test_new_file_mode_ignores_umask(tmp_path: Path, umask_077) -> None:
    atomic_write_file_text(tmp_path / "a", "x")
    atomic_write_file_text(tmp_path / "b", "x", new_file_mode=0o600)
    assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o660
    assert stat.S_IMODE((tmp_path / "b").stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o640, 0o664])
def test_existing_mode_is_kept(tmp_path: Path, mode: int, umask_077) -> None:
    target = tmp_path / "f"
    target.write_text("old")
    target.chmod(mode)
    atomic_write_file_text(target, "new")
    assert target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == mode


@pytest.mark.parametrize("failing", ["replace", "fsync"])
def test_failure_leaves_old_content_and_no_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    target = tmp_path / "f"
    target.write_text("old")

    def boom(*args, **kwargs):
        raise OSError(errno.EIO, "boom")

    monkeypatch.setattr(atomic_io.os, failing, boom)
    with pytest.raises(OSError):
        atomic_write_file_text(target, "new")
    monkeypatch.undo()
    assert target.read_text() == "old"
    assert _leftovers(tmp_path) == []


def test_symlink_target_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (tmp_path / "d").mkdir()
    link = tmp_path / "d" / "f"
    link.symlink_to(outside)
    with pytest.raises(OSError) as info:
        atomic_write_file_text(link, "new")
    assert info.value.errno == errno.ELOOP
    assert outside.read_text() == "keep"
    assert link.is_symlink()


def test_symlinked_directory_in_path_is_allowed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    atomic_write_file_text(tmp_path / "link" / "f", "ok")
    assert (real / "f").read_text() == "ok"


def test_fsync_file_before_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kinds: list[str] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(atomic_io.os, "fsync", spy)
    atomic_write_file_text(tmp_path / "f", "x")
    assert kinds == ["file", "dir"]


def test_group_is_kept_and_inherited(tmp_path: Path) -> None:
    others = [g for g in os.getgroups() if g != os.getgid()]
    if not others:
        pytest.skip("user has no second group")
    gid = others[0]
    target = tmp_path / "f"
    target.write_text("old")
    os.chown(target, -1, gid)
    atomic_write_file_text(target, "new")
    assert target.stat().st_gid == gid
    os.chown(tmp_path, -1, gid)
    atomic_write_file_text(tmp_path / "fresh", "x")
    assert (tmp_path / "fresh").stat().st_gid == gid


def test_copy_owner_group_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        atomic_io.os, "fchown", lambda fd, uid, gid: calls.append((uid, gid))
    )
    fd = os.open(tmp_path / "f", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        own_gid = os.fstat(fd).st_gid
        own_uid = os.fstat(fd).st_uid
        monkeypatch.setattr(atomic_io.os, "geteuid", lambda: 1000)
        copy_owner_group(fd, gid=own_gid + 1, uid=own_uid + 1)
        assert calls == [(-1, own_gid + 1)]
        calls.clear()
        monkeypatch.setattr(atomic_io.os, "geteuid", lambda: 0)
        copy_owner_group(fd, gid=own_gid + 1, uid=own_uid + 1)
        assert calls == [(own_uid + 1, own_gid + 1)]
        calls.clear()
        copy_owner_group(fd, gid=own_gid, uid=own_uid)
        assert calls == []

        def denied(fd, uid, gid):
            raise PermissionError(errno.EPERM, "no")

        monkeypatch.setattr(atomic_io.os, "fchown", denied)
        copy_owner_group(fd, gid=own_gid + 1)
    finally:
        os.close(fd)


def test_write_at_dir_fd_and_bad_names(tmp_path: Path) -> None:
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        atomic_write_file_at(dir_fd, "f", b"data")
        with pytest.raises(ValueError):
            atomic_write_file_at(dir_fd, "a/b", b"data")
    finally:
        os.close(dir_fd)
    assert (tmp_path / "f").read_bytes() == b"data"


def test_require_absent_never_overwrites(tmp_path: Path) -> None:
    atomic_write_file_bytes(tmp_path / "f", b"1", require_absent=True)
    with pytest.raises(FileExistsError):
        atomic_write_file_bytes(tmp_path / "f", b"2", require_absent=True)
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    with pytest.raises(OSError):
        atomic_write_file_bytes(tmp_path / "dangling", b"3", require_absent=True)
    assert (tmp_path / "f").read_bytes() == b"1"
    assert not (tmp_path / "missing").exists()
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP])
def test_require_absent_falls_back_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def no_link(*args, **kwargs):
        raise OSError(code, "links not supported")

    monkeypatch.setattr(atomic_io.os, "link", no_link)
    atomic_write_file_bytes(tmp_path / "f", b"1", require_absent=True)
    assert (tmp_path / "f").read_bytes() == b"1"
    assert stat.S_IMODE((tmp_path / "f").stat().st_mode) == 0o660
    with pytest.raises(FileExistsError):
        atomic_write_file_bytes(tmp_path / "f", b"2", require_absent=True)
    assert (tmp_path / "f").read_bytes() == b"1"
    assert _leftovers(tmp_path) == []


def test_require_absent_fallback_does_not_hide_other_link_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args, **kwargs):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(atomic_io.os, "link", broken)
    with pytest.raises(OSError):
        atomic_write_file_bytes(tmp_path / "f", b"1", require_absent=True)
    assert not (tmp_path / "f").exists()


def test_group_chown_refusal_is_a_warning_with_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def denied(fd, uid, gid):
        raise PermissionError(errno.EPERM, "no")

    monkeypatch.setattr(atomic_io.os, "fchown", denied)
    fd = os.open(tmp_path / "f", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with caplog.at_level("WARNING", logger=atomic_io.logger.name):
            copy_owner_group(fd, gid=os.fstat(fd).st_gid + 1)
    finally:
        os.close(fd)
    assert any(
        r.levelname == "WARNING" and str(tmp_path) in r.getMessage()
        for r in caplog.records
    )


def test_replace_durable_replaces_and_syncs_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").write_text("new")
    (tmp_path / "dst").write_text("old")
    synced: list[bool] = []
    real_fsync = os.fsync
    monkeypatch.setattr(
        atomic_io.os,
        "fsync",
        lambda fd: (synced.append(stat.S_ISDIR(os.fstat(fd).st_mode)), real_fsync(fd)),
    )
    replace_durable(tmp_path / "src", tmp_path / "dst")
    assert (tmp_path / "dst").read_text() == "new"
    assert not (tmp_path / "src").exists()
    assert synced == [True]


# Places that may still hold their own temp-file protocol, and why.
_ALLOWED = {
    "services/atomic_io.py": "the shared writer",
    "services/vault_backup.py": "private 0700 directory, gpg stream",
    "services/frontmatter_migration.py": "CAS replace of vault notes",
    "resources/project_template/skills/agent-memory/scripts/memory-engine.py": (
        "standalone script, runs outside the package"
    ),
    "resources/project_template/skills/pptx/scripts/replace.py": (
        "pptx helper script, not a state file"
    ),
}
_PATTERN = re.compile(r"\bos\.replace\(|\btempfile\.mkstemp\(|NamedTemporaryFile\(")


def test_no_private_tempfile_writers_outside_allowlist() -> None:
    root = Path(atomic_io.__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative in _ALLOWED:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if _PATTERN.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{relative}:{number}")
    assert offenders == []


def test_migration_keeps_only_cas_replace() -> None:
    source = (
        Path(atomic_io.__file__).resolve().parent / "frontmatter_migration.py"
    ).read_text()
    assert source.count("os.replace(") == 1
    assert "mkstemp" not in source
