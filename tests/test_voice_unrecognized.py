"""Voice handler keeps the recording when transcription is impossible."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from d_brain.bot import voice_input
from d_brain.bot.handlers import voice as voice_handler
from d_brain.cli import initialize_project

AUDIO = b"voice-bytes"


class _Bot:
    def __init__(self, file_path: str = "voice/file_1.ogg") -> None:
        self.file_path = file_path

    async def get_file(self, file_id: str):  # noqa: ANN202
        return SimpleNamespace(file_path=self.file_path)

    async def download_file(self, file_path: str):  # noqa: ANN202
        return SimpleNamespace(read=lambda: AUDIO)


class _Chat:
    id = 123
    type = "private"
    username = ""

    async def do(self, action: str) -> None:
        return None


def _message(forwarded: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        voice=SimpleNamespace(file_id="f", duration=7),
        audio=None,
        video_note=None,
        from_user=SimpleNamespace(id=42),
        chat=_Chat(),
        date=datetime(2026, 10, 24, 12, 0, 0),
        message_id=202,
        link=None,
        forward_origin=(
            SimpleNamespace(type="hidden_user", sender_user_name="Анна", date=None)
            if forwarded
            else None
        ),
        reply_to_message=None,
    )


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transcriber: type,
    *,
    key: str = "deepgram-key",
    answer_fails: bool = False,
) -> list[str]:
    answers: list[str] = []

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ANN202
        if answer_fails:
            raise RuntimeError("telegram down")
        answers.append(text)
        return SimpleNamespace()

    monkeypatch.setattr(
        voice_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path / "vault",
            content_language="ru",
            deepgram_api_key=key,
        ),
    )
    monkeypatch.setattr(voice_input, "DeepgramTranscriber", transcriber)
    monkeypatch.setattr(voice_handler, "answer_text", fake_answer_text)
    return answers


class _Failing:
    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    async def transcribe(self, audio: bytes) -> str:
        raise RuntimeError("deepgram down")


class _Empty(_Failing):
    async def transcribe(self, audio: bytes) -> str:
        return ""


class _MustNotBeCreated:
    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise AssertionError("transcriber must not be created without a key")


def _run(message: SimpleNamespace, bot: _Bot | None = None) -> None:
    asyncio.run(voice_handler.handle_voice(message, bot or _Bot()))


def _daily(tmp_path: Path) -> str:
    return (tmp_path / "vault" / "daily" / "2026-10-24.md").read_text(encoding="utf-8")


def _attachments(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "vault" / "attachments" / "2026-10-24").iterdir())


@pytest.fixture(autouse=True)
def _project(tmp_path: Path) -> None:
    initialize_project(tmp_path)


def test_deepgram_error_saves_audio_and_daily_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = _patch(monkeypatch, tmp_path, _Failing)

    _run(_message())

    files = _attachments(tmp_path)
    assert [f.read_bytes() for f in files] == [AUDIO]
    text = _daily(tmp_path)
    assert "[voice]" in text
    assert "d-brain:entry-status: voice_unrecognized" in text
    assert "Не распознано" in text
    assert len(answers) == 1 and "повторю" in answers[0]


def test_empty_transcript_saves_audio_and_daily_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = _patch(monkeypatch, tmp_path, _Empty)

    _run(_message())

    assert len(_attachments(tmp_path)) == 1
    assert "voice_unrecognized" in _daily(tmp_path)
    assert "повторю" in answers[0]


def test_missing_deepgram_key_skips_transcriber(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = _patch(monkeypatch, tmp_path, _MustNotBeCreated, key="")

    _run(_message())

    assert "voice_unrecognized" in _daily(tmp_path)
    assert "не настроено" in answers[0]


def test_forwarded_unrecognized_voice_keeps_forward_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch, tmp_path, _Failing)

    _run(_message(forwarded=True))

    text = _daily(tmp_path)
    assert "[forward from:" in text
    assert "[voice]" not in text


def test_unrecognized_attachment_extension_is_sanitised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch, tmp_path, _Failing)

    _run(_message(), _Bot("voice/x.ogg;rm"))

    assert [f.suffix for f in _attachments(tmp_path)] == [".ogg"]


def test_reply_failure_after_save_is_not_reported_as_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = _patch(monkeypatch, tmp_path, _Failing, answer_fails=True)

    _run(_message())

    assert "voice_unrecognized" in _daily(tmp_path)
    assert answers == []
