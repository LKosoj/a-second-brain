"""Shared progress waiting for background bot tasks."""

import asyncio
from collections.abc import Awaitable, Callable


async def wait_for_task_with_progress[ResultT](
    task: asyncio.Task[ResultT],
    *,
    interval_seconds: float,
    on_progress: Callable[[float], Awaitable[None]],
) -> ResultT:
    """Wait for a task and report each fully elapsed interval."""
    elapsed_seconds = 0.0
    while True:
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=interval_seconds,
            )
        except TimeoutError:
            if task.done():
                return await task
            elapsed_seconds += interval_seconds
            await on_progress(elapsed_seconds)
