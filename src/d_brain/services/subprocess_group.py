"""Run helper processes in their own process group so a timeout stops the tree."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

PROCESS_STOP_GRACE_SECONDS = 5.0


def stop_process_group(
    proc: subprocess.Popen[str], *, grace: float = PROCESS_STOP_GRACE_SECONDS
) -> None:
    """Stop a timed-out process together with the children it spawned."""

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        proc.kill()
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # Even when the leader exited on SIGTERM, a grandchild may have ignored it
    # and stayed in the group. Once wait() has reaped the leader, its pgid could
    # in theory be reused; the window is milliseconds and is accepted.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def run_in_process_group(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Like `subprocess.run(capture_output=True, text=True)`, but kills the whole
    process group on timeout or interruption, not just the direct child."""

    proc = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    with proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            stop_process_group(proc)
            out: str | None = None
            err: str | None = None
            try:
                # Bounded drain: a grandchild in another process group can
                # keep a pipe open after the leader is gone.
                out, err = proc.communicate(timeout=PROCESS_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    out, err = proc.communicate(timeout=PROCESS_STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    if proc.stdout:
                        proc.stdout.close()
                    if proc.stderr:
                        proc.stderr.close()
            raise subprocess.TimeoutExpired(
                list(args),
                timeout if timeout is not None else 0,
                output=out,
                stderr=err,
            ) from exc
        except BaseException:
            stop_process_group(proc)
            raise
    return subprocess.CompletedProcess(list(args), proc.returncode, stdout, stderr)
