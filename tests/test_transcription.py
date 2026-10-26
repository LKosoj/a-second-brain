"""Direct tests for ``DeepgramTranscriber`` (the Deepgram client is faked)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from d_brain.services import transcription
from d_brain.services.transcription import DeepgramTranscriber


class _FakeClient:
    """Stands in for ``AsyncDeepgramClient``; records how it was built."""

    instances: list["_FakeClient"] = []

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.transcribe_file = AsyncMock()
        self.listen = SimpleNamespace(
            v1=SimpleNamespace(media=SimpleNamespace(transcribe_file=self.transcribe_file))
        )
        _FakeClient.instances.append(self)


@pytest.fixture(autouse=True)
def _fake_deepgram(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.instances = []
    monkeypatch.setattr(transcription, "AsyncDeepgramClient", _FakeClient)


def _response(*transcripts: str | None) -> SimpleNamespace:
    alternatives = [SimpleNamespace(transcript=text) for text in transcripts]
    return SimpleNamespace(
        results=SimpleNamespace(channels=[SimpleNamespace(alternatives=alternatives)])
    )


def test_client_gets_the_api_key() -> None:
    transcriber = DeepgramTranscriber("secret-key")

    assert _FakeClient.instances[0].api_key == "secret-key"
    assert transcriber.client is _FakeClient.instances[0]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (None, "ru"),
        ("", "ru"),
        ("en", "en"),
        ("  en  ", "en"),
    ],
)
def test_language_defaults_to_russian_and_is_trimmed(language, expected) -> None:
    if language is None:
        transcriber = DeepgramTranscriber("k")
    else:
        transcriber = DeepgramTranscriber("k", language)

    assert transcriber.language == expected


async def test_transcribe_sends_audio_with_the_expected_options() -> None:
    transcriber = DeepgramTranscriber("k", " en ")
    transcriber.client.listen.v1.media.transcribe_file.return_value = _response("hi")

    await transcriber.transcribe(b"audio-bytes")

    transcriber.client.listen.v1.media.transcribe_file.assert_awaited_once_with(
        request=b"audio-bytes",
        model="nova-3",
        language="en",
        punctuate=True,
        smart_format=True,
    )


async def test_transcribe_returns_the_first_alternative_of_the_first_channel() -> None:
    transcriber = DeepgramTranscriber("k")
    transcriber.client.listen.v1.media.transcribe_file.return_value = _response(
        "Привет, мир.", "second guess"
    )

    assert await transcriber.transcribe(b"x") == "Привет, мир."


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(results=None),
        SimpleNamespace(results=SimpleNamespace(channels=[])),
        SimpleNamespace(results=SimpleNamespace(channels=None)),
        SimpleNamespace(
            results=SimpleNamespace(channels=[SimpleNamespace(alternatives=[])])
        ),
        _response(None),
        _response(""),
    ],
    ids=[
        "no-results",
        "no-channels",
        "channels-none",
        "no-alternatives",
        "transcript-none",
        "transcript-empty",
    ],
)
async def test_transcribe_returns_an_empty_string_for_silence(response) -> None:
    transcriber = DeepgramTranscriber("k")
    transcriber.client.listen.v1.media.transcribe_file.return_value = response

    assert await transcriber.transcribe(b"x") == ""


async def test_transcribe_lets_client_errors_through() -> None:
    transcriber = DeepgramTranscriber("k")
    transcriber.client.listen.v1.media.transcribe_file.side_effect = RuntimeError(
        "deepgram down"
    )

    with pytest.raises(RuntimeError, match="deepgram down"):
        await transcriber.transcribe(b"x")
