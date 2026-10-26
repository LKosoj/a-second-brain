"""Direct tests for the /start, /help, /stats and /status handlers."""

import functools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from _helpers import patch_get_settings

from d_brain.bot.dashboard import build_help_text
from d_brain.bot.handlers import commands
from d_brain.services.session import SessionStore

_patch_settings = functools.partial(patch_get_settings, module=commands)


def _message(user_id: int | None = 42) -> SimpleNamespace:
    from_user = None if user_id is None else SimpleNamespace(id=user_id)
    return SimpleNamespace(from_user=from_user)


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(commands, "answer_text", mock)
    return mock


@pytest.mark.parametrize("handler", [commands.cmd_start, commands.cmd_help])
async def test_start_and_help_answer_with_the_help_text(answer, handler) -> None:
    message = _message()

    await handler(message)

    answer.assert_awaited_once_with(message, build_help_text())


@pytest.mark.parametrize("handler", [commands.cmd_stats, commands.cmd_status])
async def test_stats_and_status_answer_with_the_users_stats(
    tmp_path, monkeypatch, answer, handler
) -> None:
    _patch_settings(monkeypatch, tmp_path)
    build = []
    monkeypatch.setattr(
        commands,
        "build_stats_text",
        lambda vault, user_id: build.append((vault, user_id)) or "STATS",
    )
    message = _message(42)

    await handler(message)

    assert build == [(tmp_path, 42)]
    answer.assert_awaited_once_with(message, "STATS")


async def test_stats_command_is_recorded_in_the_users_session(
    tmp_path, monkeypatch, answer
) -> None:
    _patch_settings(monkeypatch, tmp_path)

    await commands.cmd_stats(_message(42))
    await commands.cmd_status(_message(42))

    entries = SessionStore(tmp_path).get_today(42)
    assert [(e["type"], e["cmd"]) for e in entries] == [
        ("command", "/stats"),
        ("command", "/stats"),
    ]
    assert SessionStore(tmp_path).get_today(7) == []


async def test_stats_without_a_sender_uses_user_zero(
    tmp_path, monkeypatch, answer
) -> None:
    _patch_settings(monkeypatch, tmp_path)

    await commands.cmd_stats(_message(None))

    assert [e["cmd"] for e in SessionStore(tmp_path).get_today(0)] == ["/stats"]
    answer.assert_awaited_once()


async def test_stats_reply_is_the_real_report_for_an_empty_vault(
    tmp_path, monkeypatch, answer
) -> None:
    _patch_settings(monkeypatch, tmp_path)

    await commands.cmd_stats(_message(42))

    text = answer.await_args.args[1]
    assert text.startswith("**Статистика**")


def test_router_serves_exactly_the_four_commands() -> None:
    callbacks = {handler.callback for handler in commands.router.message.handlers}

    assert callbacks == {
        commands.cmd_start,
        commands.cmd_help,
        commands.cmd_stats,
        commands.cmd_status,
    }
