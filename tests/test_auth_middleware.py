"""Tests for the bot authentication middleware."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from d_brain.bot.main import create_auth_middleware
from d_brain.config import Settings


def _make_settings(owner_id: int = 111) -> Settings:
    return Settings(
        telegram_bot_token="fake-token",
        deepgram_api_key="fake-key",
        owner_telegram_id=owner_id,
    )


def _make_update(user_id: int | None, *, via_callback: bool = False):
    """Build a minimal Update-like object with a user (or no user)."""
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    if via_callback:
        return SimpleNamespace(
            message=None,
            callback_query=SimpleNamespace(from_user=user),
        )
    return SimpleNamespace(
        message=SimpleNamespace(from_user=user),
        callback_query=None,
    )


@pytest.mark.asyncio
async def test_auth_allows_owner() -> None:
    middleware = create_auth_middleware(_make_settings(owner_id=42))
    handler = AsyncMock(return_value="ok")
    result = await middleware(handler, _make_update(42), {})
    handler.assert_awaited_once()
    assert result == "ok"


@pytest.mark.asyncio
async def test_auth_blocks_stranger() -> None:
    middleware = create_auth_middleware(_make_settings(owner_id=42))
    handler = AsyncMock()
    result = await middleware(handler, _make_update(999), {})
    handler.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_auth_blocks_update_without_user() -> None:
    middleware = create_auth_middleware(_make_settings(owner_id=42))
    handler = AsyncMock()
    update = SimpleNamespace(message=None, callback_query=None)
    result = await middleware(handler, update, {})
    handler.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_auth_allows_owner_via_callback_query() -> None:
    middleware = create_auth_middleware(_make_settings(owner_id=42))
    handler = AsyncMock(return_value="cb")
    result = await middleware(handler, _make_update(42, via_callback=True), {})
    handler.assert_awaited_once()
    assert result == "cb"


@pytest.mark.asyncio
async def test_auth_blocks_stranger_via_callback_query() -> None:
    middleware = create_auth_middleware(_make_settings(owner_id=42))
    handler = AsyncMock()
    result = await middleware(handler, _make_update(999, via_callback=True), {})
    handler.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_auth_blocks_callback_query_without_user() -> None:
    middleware = create_auth_middleware(_make_settings(owner_id=42))
    handler = AsyncMock()
    update = SimpleNamespace(
        message=None,
        callback_query=SimpleNamespace(from_user=None),
    )
    result = await middleware(handler, update, {})
    handler.assert_not_awaited()
    assert result is None
