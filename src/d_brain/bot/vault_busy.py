"""Run a vault write under the write lock from a button handler."""

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from d_brain.services.vault_lock import VaultLockTimeout

# How long a button handler waits for the vault write lock. The nightly pass
# can hold it for minutes.
VAULT_LOCK_WAIT_SECONDS = 30.0


class VaultBusyError(Exception):
    """The vault write lock was not obtained in time; nothing was written."""


async def run_with_vault_lock[T](
    vault_path: Path,
    work: Callable[[Any], T],
    *,
    lock_factory: Callable[..., AbstractContextManager[Any]],
    timeout: float | None = None,
) -> T:
    """Take the vault write lock in a worker thread and run ``work(lock)`` there.

    The whole ``with`` runs in one thread (flock must be taken and released
    by the same one), so a lock held by the nightly pass does not freeze the
    event loop. The deadline is enforced by the lock itself, so on timeout
    the thread ends on its own instead of staying parked on ``flock`` in the
    shared thread pool. Once ``work`` has started the caller waits for it
    however long it takes, so "busy" is never reported for a write that did
    happen.
    """
    wait = VAULT_LOCK_WAIT_SECONDS if timeout is None else timeout

    def locked_work() -> T:
        with lock_factory(vault_path, timeout=wait) as lock:
            return work(lock)

    try:
        return await asyncio.to_thread(locked_work)
    except VaultLockTimeout:
        raise VaultBusyError from None
