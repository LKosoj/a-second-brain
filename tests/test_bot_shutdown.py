from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from d_brain.bot import main
from d_brain.services import claude_tmux


def _setup(
    monkeypatch: pytest.MonkeyPatch, fail: bool = False
) -> tuple[list[str], SimpleNamespace]:
    events: list[str] = []

    async def start_polling(*args: object, **kwargs: object) -> None:
        events.append("polling")
        if fail:
            raise RuntimeError("boom")

    async def close() -> None:
        events.append("close")

    bot = SimpleNamespace(session=SimpleNamespace(close=close))
    dp = MagicMock()
    dp.start_polling = start_polling
    dp.resolve_used_update_types.return_value = []

    async def ticker(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "create_bot", lambda settings: bot)
    monkeypatch.setattr(main, "create_dispatcher", lambda: dp)
    monkeypatch.setattr(main, "configure_bot_commands", AsyncMock())
    monkeypatch.setattr(main, "create_auth_middleware", lambda settings: object())
    monkeypatch.setattr("d_brain.bot.handlers.reminders.run_reminder_ticker", ticker)
    monkeypatch.setattr(
        claude_tmux, "cleanup_orphan_sessions", lambda: events.append("cleanup") or 0
    )
    monkeypatch.setattr(
        claude_tmux, "shutdown_tmux_sessions", lambda: events.append("shutdown")
    )
    return events, SimpleNamespace(vault_path=Path("/nonexistent"))


def test_run_bot_cleans_orphans_before_polling_and_shuts_down_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, settings = _setup(monkeypatch)

    asyncio.run(main.run_bot(settings))  # type: ignore[arg-type]

    assert events == ["cleanup", "polling", "shutdown", "close"]


def test_run_bot_shuts_down_sessions_when_polling_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, settings = _setup(monkeypatch, fail=True)

    with pytest.raises(RuntimeError):
        asyncio.run(main.run_bot(settings))  # type: ignore[arg-type]

    assert events == ["cleanup", "polling", "shutdown", "close"]


def test_run_bot_starts_even_if_orphan_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, settings = _setup(monkeypatch)

    def boom() -> int:
        raise OverflowError("pid too large")

    monkeypatch.setattr(claude_tmux, "cleanup_orphan_sessions", boom)

    asyncio.run(main.run_bot(settings))  # type: ignore[arg-type]

    assert events == ["polling", "shutdown", "close"]
