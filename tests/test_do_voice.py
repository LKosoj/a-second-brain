"""/do voice input goes through the same shared code as plain voice messages."""

import io
from types import SimpleNamespace

import pytest

from d_brain.bot import voice_input
from d_brain.bot.handlers import do as do_handler

LIMIT = 20 * 1024 * 1024


class _State:
    def __init__(self) -> None:
        self.cleared = False

    async def get_data(self) -> dict:
        return {}

    async def clear(self) -> None:
        self.cleared = True


class _Chat:
    id = 1

    async def do(self, action: str) -> None:
        pass


class _Bot:
    def __init__(self) -> None:
        self.downloads = 0

    async def get_file(self, file_id: str) -> SimpleNamespace:
        return SimpleNamespace(file_path="voice/file.ogg")

    async def download_file(self, path: str) -> io.BytesIO:
        self.downloads += 1
        return io.BytesIO(b"audio")


def _message(**media: object) -> SimpleNamespace:
    fields = dict(text=None, voice=None, audio=None, video_note=None)
    fields.update(media)
    return SimpleNamespace(
        from_user=SimpleNamespace(id=42), chat=_Chat(), **fields
    )


class _Transcriber:
    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    async def transcribe(self, audio: bytes) -> str:
        return "сделай дело"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    answers: list[str] = []
    prompts: list[str] = []

    async def fake_answer(_message, text: str, **kwargs):  # noqa: ANN001, ANN003, ANN202
        answers.append(text)

    async def fake_process(_message, prompt: str, user_id: int = 0) -> None:
        prompts.append(prompt)

    settings = SimpleNamespace(
        deepgram_api_key="key", content_language="ru", vault_path=None
    )
    monkeypatch.setattr(do_handler, "answer_text", fake_answer)
    monkeypatch.setattr(do_handler, "process_request", fake_process)
    monkeypatch.setattr(do_handler, "get_settings", lambda: settings)
    monkeypatch.setattr(voice_input, "DeepgramTranscriber", _Transcriber)
    return SimpleNamespace(answers=answers, prompts=prompts)


@pytest.mark.parametrize("kind", ["audio", "video_note"])
async def test_do_transcribes_audio_and_video_note(env, kind: str) -> None:
    media = SimpleNamespace(file_id="f", file_size=1000, duration=3)
    message = _message(**{kind: media})

    await do_handler.handle_do_input(message, _Bot(), _State())

    assert env.prompts == ["сделай дело"]
    assert "🎤 сделай дело" in env.answers


async def test_do_rejects_oversized_file_without_download(env) -> None:
    media = SimpleNamespace(file_id="f", file_size=LIMIT + 1, duration=3)
    bot = _Bot()

    await do_handler.handle_do_input(_message(audio=media), bot, _State())

    assert bot.downloads == 0
    assert env.prompts == []
    assert any("20 МБ" in a for a in env.answers)
