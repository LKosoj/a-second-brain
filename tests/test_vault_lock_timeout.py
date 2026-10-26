"""Deadline support of ``vault_write_lock`` and ``run_with_vault_lock``."""

import fcntl
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from d_brain.bot import vault_busy
from d_brain.services.vault_lock import (
    VaultLockError,
    VaultLockTimeout,
    vault_write_lock,
)


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _hold_lock_file(vault: Path) -> int:
    """Take the vault-write flock from outside, like the nightly pass does."""
    (vault / ".locks").mkdir(exist_ok=True)
    fd = os.open(vault / ".locks" / "vault-write.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_timeout_on_held_lock_raises_and_releases_everything(tmp_path: Path) -> None:
    holder = _hold_lock_file(tmp_path)
    before = _open_fds()
    try:
        started = time.monotonic()
        with pytest.raises(VaultLockTimeout), vault_write_lock(tmp_path, timeout=0.2):
            pytest.fail("lock must not be granted")
        assert time.monotonic() - started < 3
        assert _open_fds() == before
    finally:
        os.close(holder)
    # The root flock taken first was released: the lock is free again.
    with vault_write_lock(tmp_path, timeout=1):
        pass


def test_timeout_is_a_vault_lock_error() -> None:
    assert issubclass(VaultLockTimeout, VaultLockError)


def test_timeout_none_and_free_lock_behave_as_before(tmp_path: Path) -> None:
    with vault_write_lock(tmp_path) as lock:
        assert lock.root == tmp_path
    with vault_write_lock(tmp_path, timeout=5) as lock:
        assert lock.root == tmp_path


def test_timeout_none_still_waits_for_release(tmp_path: Path) -> None:
    holder = _hold_lock_file(tmp_path)
    threading.Timer(0.3, os.close, args=(holder,)).start()
    with vault_write_lock(tmp_path):
        pass


async def test_run_with_vault_lock_timeout_frees_worker_thread(
    tmp_path: Path, monkeypatch
) -> None:
    holder = _hold_lock_file(tmp_path)
    monkeypatch.setattr(vault_busy, "VAULT_LOCK_WAIT_SECONDS", 0.2)
    finished = threading.Event()
    ran: list[str] = []

    @contextmanager
    def tracked_lock(vault_path, timeout=None):  # noqa: ANN001, ANN202
        try:
            with vault_write_lock(vault_path, timeout=timeout) as lock:
                yield lock
        finally:
            finished.set()

    try:
        with pytest.raises(vault_busy.VaultBusyError):
            await vault_busy.run_with_vault_lock(
                tmp_path, lambda lock: ran.append("x"), lock_factory=tracked_lock
            )
        # The worker ended by itself, while the lock was still held elsewhere.
        assert finished.wait(1)
    finally:
        os.close(holder)
    assert ran == []
