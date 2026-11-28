"""Voice input routed to active /find, /why, and brief wait states."""

from __future__ import annotations

import io
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User, Voice

from d_brain.bot import voice_input
from d_brain.bot.handlers import brief, find, voice, why
from d_brain.bot.states import BriefCommandState, FindCommandState, WhyCommandState
from d_brain.cli import initialize_project


class _VoiceHarness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        transcript: str = "бюджет проекта",
    ) -> None:
        initialize_project(tmp_path)
        self.vault_path = tmp_path / "vault"
        self.find_calls: list[str] = []
        self.why_calls: list[str] = []
        self.brief_calls: list[tuple[str, str]] = []
        self.answers: list[str] = []
        self.restored_menus: list[object] = []

        async def fake_find(message: Message, query: str, state: object) -> None:
            self.find_calls.append(query)

        async def fake_why(message: Message, query: str, state: object) -> None:
            self.why_calls.append(query)

        async def fake_brief(message: Message, brief_type: str, query: str) -> None:
            self.brief_calls.append((brief_type, query))

        async def fake_restore(message: Message, menu_message_id: object) -> None:
            self.restored_menus.append(menu_message_id)

        async def fake_answer(
            _message: Message, text: str, **kwargs: object
        ) -> SimpleNamespace:
            self.answers.append(text)
            return SimpleNamespace(text=text)

        class FakeTranscriber:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def transcribe(self, audio: bytes) -> str:
                return transcript

        monkeypatch.setattr(
            voice,
            "get_settings",
            lambda: SimpleNamespace(
                vault_path=self.vault_path,
                content_language="ru",
                deepgram_api_key="key",
            ),
        )
        monkeypatch.setattr(voice_input, "DeepgramTranscriber", FakeTranscriber)
        monkeypatch.setattr(voice, "answer_text", fake_answer)
        monkeypatch.setattr(voice, "process_find_request", fake_find)
        monkeypatch.setattr(voice, "process_why_request", fake_why)
        monkeypatch.setattr(voice, "process_brief_request", fake_brief)
        monkeypatch.setattr(voice, "restore_menu", fake_restore)
        monkeypatch.setattr(voice, "_refresh_qmd_in_background", AsyncMock())
        monkeypatch.setattr(brief, "restore_menu", fake_restore)

        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(find.router)
        self.dp.include_router(why.router)
        self.dp.include_router(brief.router)
        self.dp.include_router(voice.router)

        self.bot = Bot("123456:TEST")
        self.bot.session.make_request = AsyncMock()  # type: ignore[method-assign]
        self.bot.get_file = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(file_path="voice/note.ogg")
        )
        self.bot.download_file = AsyncMock(  # type: ignore[method-assign]
            return_value=io.BytesIO(b"audio-bytes")
        )
        self.key = StorageKey(bot_id=self.bot.id, chat_id=5, user_id=5)

    async def wait(self, state: object, **data: object) -> None:
        await self.dp.storage.set_state(self.key, state)  # type: ignore[arg-type]
        await self.dp.storage.update_data(self.key, data)

    async def send_voice(self, *, forwarded: bool = False) -> None:
        chat = Chat(id=5, type="private")
        user = User(id=5, is_bot=False, first_name="o")
        extra: dict[str, object] = {}
        if forwarded:
            extra["forward_origin"] = {
                "type": "user",
                "date": datetime(2026, 6, 15, 12, 0, 0),
                "sender_user": user.model_dump(),
            }
        message = Message(
            message_id=10,
            date=datetime(2026, 6, 15, 12, 0, 0),
            chat=chat,
            from_user=user,
            voice=Voice(file_id="f1", file_unique_id="u1", duration=3),
            **extra,  # type: ignore[arg-type]
        )
        await self.dp.feed_update(self.bot, Update(update_id=1, message=message))


async def test_voice_routes_to_find_wait_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _VoiceHarness(monkeypatch, tmp_path, transcript="бюджет проекта")
    await harness.wait(
        FindCommandState.waiting_for_input, find_entered_at=time.time()
    )

    await harness.send_voice()

    assert harness.find_calls == ["бюджет проекта"]
    assert "🎤 бюджет проекта" in harness.answers
    assert await harness.dp.storage.get_state(harness.key) is None
    assert not (harness.vault_path / "daily" / "2026-06-15.md").exists()


async def test_voice_routes_to_why_wait_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _VoiceHarness(monkeypatch, tmp_path, transcript="закупка сервера")
    await harness.wait(
        WhyCommandState.waiting_for_input, why_entered_at=time.time()
    )

    await harness.send_voice()

    assert harness.why_calls == ["закупка сервера"]
    assert "🎤 закупка сервера" in harness.answers
    assert await harness.dp.storage.get_state(harness.key) is None
    assert not (harness.vault_path / "daily" / "2026-06-15.md").exists()


async def test_voice_routes_to_brief_wait_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _VoiceHarness(monkeypatch, tmp_path, transcript="дорожная карта")
    await harness.wait(
        BriefCommandState.waiting_for_query,
        brief_type="project",
        brief_entered_at=time.time(),
        menu_message_id=77,
    )

    await harness.send_voice()

    assert harness.brief_calls == [("project", "дорожная карта")]
    assert harness.restored_menus == [77]
    assert "🎤 дорожная карта" in harness.answers
    assert await harness.dp.storage.get_state(harness.key) is None
    assert not (harness.vault_path / "daily" / "2026-06-15.md").exists()


async def test_unrecognized_voice_during_wait_state_is_saved_to_daily(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _VoiceHarness(monkeypatch, tmp_path, transcript="")
    await harness.wait(
        FindCommandState.waiting_for_input, find_entered_at=time.time()
    )

    await harness.send_voice()

    assert harness.find_calls == []
    daily = (harness.vault_path / "daily" / "2026-06-15.md").read_text(encoding="utf-8")
    assert "voice_unrecognized" in daily


async def test_expired_wait_state_saves_voice_to_daily(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _VoiceHarness(monkeypatch, tmp_path, transcript="обычная заметка")
    await harness.wait(
        FindCommandState.waiting_for_input, find_entered_at=time.time() - 601
    )

    await harness.send_voice()

    assert harness.find_calls == []
    assert await harness.dp.storage.get_state(harness.key) is None
    daily = (harness.vault_path / "daily" / "2026-06-15.md").read_text(encoding="utf-8")
    assert "обычная заметка" in daily


async def test_forwarded_voice_during_wait_state_saves_to_daily(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _VoiceHarness(monkeypatch, tmp_path, transcript="чужая запись")
    await harness.wait(
        WhyCommandState.waiting_for_input, why_entered_at=time.time()
    )

    await harness.send_voice(forwarded=True)

    assert harness.why_calls == []
    assert await harness.dp.storage.get_state(harness.key) is None
    daily = (harness.vault_path / "daily" / "2026-06-15.md").read_text(encoding="utf-8")
    assert "чужая запись" in daily
