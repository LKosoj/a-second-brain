"""Tests for the secret-scrubbing ingest filter (audit item B2-3).

``scrub_secrets`` redacts known secret shapes (AWS presigned URL parameters,
bearer tokens, API keys, URL passwords, PEM private keys) before ingested
text -- PLAUD recordings, archived web pages, extracted documents, and
forwarded Telegram messages -- reaches the vault.
"""

import asyncio
import io
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import _write_vault_manifest

from d_brain.bot.handlers import forward as forward_handler
from d_brain.bot.handlers import photo as photo_handler
from d_brain.services.documents import DocumentArchiveService
from d_brain.services.plaud import PlaudSyncService
from d_brain.services.secrets import scrub_secrets
from d_brain.services.source_links import SourceInfo
from d_brain.services.web_archive import WebArchiveService
from d_brain.services.web_content import WebContentResult
from d_brain.services.youtube_transcript import YouTubeArchiveService, YouTubeTranscript


@pytest.fixture(autouse=True)
def _secrets_manifest(tmp_path: Path) -> None:
    _write_vault_manifest(tmp_path / "vault")


# --- scrub_secrets: pattern coverage --------------------------------------


def test_scrub_secrets_leaves_plain_text_with_email_and_numbers_untouched() -> None:
    text = "Hello world, contact me at john.doe@example.com, order #12345."
    assert scrub_secrets(text) == (text, 0)


def test_scrub_secrets_empty_string_is_unchanged() -> None:
    assert scrub_secrets("") == ("", 0)


def test_scrub_secrets_redacts_aws_presigned_params() -> None:
    text = (
        "https://s3.amazonaws.com/bucket/key"
        "?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20250101"
        "&X-Amz-Signature=abcdef1234567890"
        "&X-Amz-Security-Token=FQoGZXIvYXdzE"
    )
    scrubbed, count = scrub_secrets(text)
    assert count == 3
    assert "AKIAIOSFODNN7EXAMPLE" not in scrubbed
    assert "abcdef1234567890" not in scrubbed
    assert "FQoGZXIvYXdzE" not in scrubbed
    assert "X-Amz-Credential=[redacted]" in scrubbed
    assert "X-Amz-Signature=[redacted]" in scrubbed
    assert "X-Amz-Security-Token=[redacted]" in scrubbed


def test_scrub_secrets_redacts_generic_signature_and_sig_query_params() -> None:
    scrubbed, count = scrub_secrets("https://example.com/download?sig=abc123&x=1")
    assert count == 1
    assert "sig=[redacted]" in scrubbed
    assert "abc123" not in scrubbed

    scrubbed, count = scrub_secrets("https://example.com/download?Signature=abc123")
    assert count == 1
    assert "Signature=[redacted]" in scrubbed


def test_scrub_secrets_redacts_bearer_token_with_authorization_header() -> None:
    scrubbed, count = scrub_secrets("Authorization: Bearer abcDEF123.token-value")
    assert count == 1
    assert scrubbed == "Authorization: Bearer [redacted]"


def test_scrub_secrets_redacts_bare_bearer_token() -> None:
    scrubbed, count = scrub_secrets("curl -H 'Bearer abc.def-ghi_012345'")
    assert count == 1
    assert "abc.def-ghi_012345" not in scrubbed
    assert "Bearer [redacted]" in scrubbed


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnop1234567890",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "github_pat_11ABCDEFGHIJKLMNOPQRSTUVWX",
        "xoxb" + "-1234567890-1234567890123-abcdefghijklmnopqrstuvwx",
        "xoxp" + "-1234567890-1234567890123-abcdefghijklmnopqrstuvwx",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_scrub_secrets_redacts_known_api_key_shapes(secret: str) -> None:
    scrubbed, count = scrub_secrets(f"key={secret}")
    assert count == 1
    assert secret not in scrubbed
    assert "[redacted]" in scrubbed


def test_scrub_secrets_redacts_url_password_and_query_param() -> None:
    scrubbed, count = scrub_secrets(
        "http://user:sEcr3tPass@host.com/path?password=hunter2&other=1"
    )
    assert count == 2
    assert "sEcr3tPass" not in scrubbed
    assert "hunter2" not in scrubbed
    assert "user:[redacted]@" in scrubbed
    assert "password=[redacted]" in scrubbed


def test_scrub_secrets_redacts_private_key_block() -> None:
    key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAKj34GkxFhD91aM8xXNo1PsuFhpQNbo\n"
        "-----END RSA PRIVATE KEY-----"
    )
    scrubbed, count = scrub_secrets(f"before\n{key}\nafter")
    assert count == 1
    assert scrubbed == "before\n[redacted private key]\nafter"


def test_scrub_secrets_no_match_returns_same_string_instance() -> None:
    text = "nothing secret about this line"
    scrubbed, count = scrub_secrets(text)
    assert count == 0
    assert scrubbed is text


def test_scrub_secrets_redacts_pgp_private_key_block() -> None:
    key = (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        "lQPGBGT1234567890abcdefghijklmnop\n"
        "-----END PGP PRIVATE KEY BLOCK-----"
    )
    scrubbed, count = scrub_secrets(f"before\n{key}\nafter")
    assert count == 1
    assert scrubbed == "before\n[redacted private key]\nafter"


def test_scrub_secrets_does_not_redact_the_english_word_bearer() -> None:
    # Regression: "bearer" as an ordinary English word (not an auth header)
    # must not be mistaken for a "Bearer <token>" credential.
    text = "He was the bearer of the ring."
    assert scrub_secrets(text) == (text, 0)


def test_scrub_secrets_redacts_sig_param_inside_markdown_link() -> None:
    scrubbed, count = scrub_secrets("[файл](https://cdn.example.com/img.jpg?sig=abc123)")
    assert count == 1
    assert scrubbed == "[файл](https://cdn.example.com/img.jpg?sig=[redacted])"


def test_scrub_secrets_redacts_sig_param_inside_angle_bracket_url() -> None:
    scrubbed, count = scrub_secrets("<https://cdn.example.com/img.jpg?sig=abc123>")
    assert count == 1
    assert scrubbed == "<https://cdn.example.com/img.jpg?sig=[redacted]>"


def test_scrub_secrets_redacts_sig_param_inside_bracketed_url() -> None:
    scrubbed, count = scrub_secrets("[https://cdn.example.com/img.jpg?sig=abc123]")
    assert count == 1
    assert scrubbed == "[https://cdn.example.com/img.jpg?sig=[redacted]]"


def test_scrub_secrets_handles_many_unmatched_private_key_headers_quickly() -> None:
    # Regression for a quadratic-time bug: a naive "BEGIN...*?...END" regex
    # re-scanned to the end of the string for every unmatched BEGIN marker,
    # so ~1MB of adversarial input (many BEGINs, no matching END) took ~40s.
    # Each header stands alone with no key-looking body after it, so it is
    # redacted on its own (see the prose-vs-truncated split below) -- the
    # padding itself, not being base64, must survive untouched.
    marker = "-----BEGIN RSA PRIVATE KEY-----\n"
    padding = "not a key body, just filler text here.\n"
    text = (marker + padding) * 3000  # ~330KB, 3000 unmatched BEGIN markers

    start = time.monotonic()
    scrubbed, count = scrub_secrets(text)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert count == 3000
    assert "BEGIN RSA PRIVATE KEY" not in scrubbed
    assert "not a key body, just filler text here." in scrubbed


def test_scrub_secrets_redacts_both_sides_of_a_decoy_begin_inside_a_real_key() -> None:
    # Regression: a decoy BEGIN inside a real key's body made the real body's
    # own END unreachable for the first header (the footer search stops at
    # the next BEGIN), so nothing was redacted and both fragments leaked.
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "QUJDMTIzNDU2Nzg5MEFCQ0RFRkFCQ0RFRg==\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "WFlaMTIzNDU2Nzg5MFhZWkFCQ0RFRkFCQ0RFRg==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    scrubbed, count = scrub_secrets(f"before\n{text}\nafter")

    assert count > 0
    assert "QUJDMTIzNDU2Nzg5MEFCQ0RFRkFCQ0RFRg==" not in scrubbed
    assert "WFlaMTIzNDU2Nzg5MFhZWkFCQ0RFRkFCQ0RFRg==" not in scrubbed
    assert scrubbed.startswith("before\n")
    assert scrubbed.endswith("\nafter")


def test_scrub_secrets_redacts_truncated_private_key_with_no_end() -> None:
    # Regression: a block with no reachable END (a truncated paste) was left
    # completely untouched, leaking the base64 body.
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAKj34GkxFhD91aM8xXNo1PsuFhpQNbo\n"
        "no end here"
    )
    scrubbed, count = scrub_secrets(f"before\n{text}\nafter")

    assert count == 1
    assert "MIIBOgIBAAJBAKj" not in scrubbed
    assert "before" in scrubbed
    assert "no end here" in scrubbed
    assert "after" in scrubbed


def test_scrub_secrets_redacts_only_the_header_when_mentioned_in_prose() -> None:
    # A header mentioned mid-sentence (no key actually follows on the same
    # line) must not have the rest of the sentence swallowed into the
    # redaction -- only the marker itself is a private-key shape.
    text = (
        "In this article, -----BEGIN RSA PRIVATE KEY-----  is the header "
        "you will see in most PEM files, right before the encoded key "
        "data begins."
    )
    scrubbed, count = scrub_secrets(text)

    assert count == 1
    assert "BEGIN RSA PRIVATE KEY" not in scrubbed
    assert scrubbed == (
        "In this article, [redacted private key]  is the header "
        "you will see in most PEM files, right before the encoded key "
        "data begins."
    )


# --- ingest point: PLAUD raw JSON + note -----------------------------------


class _FakePlaudClient:
    def __init__(
        self,
        items: list[dict[str, object]],
        details: dict[str, dict[str, object]],
    ) -> None:
        self.items = items
        self.details = details

    def iter_recordings(self, *, limit: int = 100, max_pages: int | None = None):  # type: ignore[no-untyped-def]
        del limit, max_pages
        yield from self.items

    def get_recording(self, file_id: str) -> dict[str, object]:
        return self.details[file_id]


def test_plaud_sync_scrubs_secret_from_raw_json_and_note(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    secret = "sk-testsecrettoken1234567890"
    items = [{"file_id": "file-1"}]
    details = {
        "file-1": {
            "file_id": "file-1",
            "title": "Sync call",
            "record_time": 1712217600000,
            "trans_result": f"API key leaked in the call: {secret}",
            "ai_content": {"summary": "Обсудили доступ к API."},
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    service._run_prompt = lambda prompt: json.dumps(  # type: ignore[method-assign]
        {
            "context_type": "personal_memo",
            "archive": True,
            "todoist_create": False,
            "owner_confidence": "medium",
            "reason": "archive only",
            "tasks": [],
        },
        ensure_ascii=False,
    )

    result = service.sync(backfill=True, refresh_qmd=False)

    assert result["imported"] == 1
    raw_files = list((vault_path / "imports" / "plaud" / "raw").rglob("*.json"))
    note_files = list((vault_path / "imports" / "plaud" / "notes").rglob("*.md"))
    assert len(raw_files) == 1
    assert len(note_files) == 1
    raw_text = raw_files[0].read_text(encoding="utf-8")
    note_text = note_files[0].read_text(encoding="utf-8")
    daily_text = (vault_path / "daily" / "2024-04-04.md").read_text(encoding="utf-8")

    assert secret not in raw_text
    assert secret not in note_text
    assert secret not in daily_text
    assert "[redacted]" in raw_text
    assert "[redacted]" in note_text


def test_plaud_classification_prompt_gets_scrubbed_text(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    secret = "sk-testsecrettoken1234567890"
    items = [{"file_id": "file-1"}]
    details = {
        "file-1": {
            "file_id": "file-1",
            "title": "Sync call",
            # Recent recording: classification only runs inside the task
            # window, and that prompt is what must not carry the secret.
            "record_time": int(time.time() * 1000),
            "trans_result": f"API key leaked in the call: {secret}",
            "ai_content": {"summary": f"Ключ {secret} прозвучал вслух."},
        }
    }
    service = PlaudSyncService(
        vault_path,
        bearer_token="token",
        client=_FakePlaudClient(items, details),  # type: ignore[arg-type]
    )
    prompts: list[str] = []

    def fake_run_prompt(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {
                "context_type": "personal_memo",
                "archive": True,
                "todoist_create": False,
                "owner_confidence": "medium",
                "reason": "archive only",
                "tasks": [],
            },
            ensure_ascii=False,
        )

    service._run_prompt = fake_run_prompt  # type: ignore[method-assign]

    result = service.sync(backfill=True, refresh_qmd=False)

    assert result["imported"] == 1
    assert len(prompts) == 1
    assert secret not in prompts[0]
    assert "[redacted]" in prompts[0]


# --- ingest point: web archive page text -----------------------------------


def test_web_archive_scrubs_secret_from_raw_content_and_note(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    secret = "AKIAIOSFODNN7EXAMPLE"
    service = WebArchiveService(vault_path)

    result = service.archive_page(
        WebContentResult(
            url="https://example.com/article",
            title="Example",
            content=f"Some page text with a leaked key: {secret}",
            source="direct",
        ),
        original_url="https://example.com/article",
        timestamp=datetime(2026, 7, 29, 12, 0),
        summary="",
        refresh_qmd=False,
    )

    raw_text = (vault_path / result.raw_path).read_text(encoding="utf-8")
    content_text = (vault_path / result.content_path).read_text(encoding="utf-8")
    note_text = (vault_path / result.note_path).read_text(encoding="utf-8")

    assert secret not in raw_text
    assert secret not in content_text
    assert secret not in note_text
    assert "[redacted]" in content_text


def test_web_archive_scrubs_secret_from_the_source_url_itself(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Regression: only page.content/summary were scrubbed, so a presigned
    # URL passed in as original_url/page.url leaked into the log message,
    # raw JSON (original_url/final_url), frontmatter, note body, and daily
    # block untouched.
    vault_path = tmp_path / "vault"
    secret = "abcdef1234567890"
    secret_url = f"https://bucket.s3.amazonaws.com/key?X-Amz-Signature={secret}"
    service = WebArchiveService(vault_path)

    with caplog.at_level(logging.INFO):
        result = service.archive_page(
            WebContentResult(
                url=secret_url,
                title="Example",
                content="Plain page text, nothing secret here.",
                source="direct",
            ),
            original_url=secret_url,
            timestamp=datetime(2026, 7, 29, 12, 0),
            summary="",
            refresh_qmd=False,
        )

    raw_text = (vault_path / result.raw_path).read_text(encoding="utf-8")
    note_text = (vault_path / result.note_path).read_text(encoding="utf-8")

    assert secret not in raw_text
    assert secret not in note_text
    assert secret not in result.daily_content
    assert "X-Amz-Signature=[redacted]" in raw_text
    assert not any(secret in record.getMessage() for record in caplog.records)


def test_analyze_photo_scrubs_secret_from_ocr_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "abc.def-ghi_012345"

    class FakeAnalyzer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def analyze(self, relative_path: str) -> dict[str, str]:
            return {
                "description": "Скриншот терминала",
                "ocr_text": f"curl -H 'Authorization: Bearer {secret}'",
            }

    monkeypatch.setattr(photo_handler, "ImageAnalysisService", FakeAnalyzer)
    settings = SimpleNamespace(
        vault_path=tmp_path, content_language="ru", ai_cli="codex"
    )

    analysis = asyncio.run(
        photo_handler._analyze_photo(settings, "attachments/2026-04-04/photo.jpg")  # type: ignore[arg-type]
    )

    assert analysis is not None
    assert secret not in analysis["ocr_text"]
    assert "Bearer [redacted]" in analysis["ocr_text"]
    assert analysis["description"] == "Скриншот терминала"
    daily_content = photo_handler._build_photo_daily_content(
        "attachments/2026-04-04/photo.jpg",
        "",
        analysis,
        SourceInfo(kind="telegram", ref="telegram:42:404", url="", label=""),
        "ru",
    )
    assert secret not in daily_content


# --- ingest point: extracted document text ---------------------------------


def test_document_archive_scrubs_secret_from_extracted_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "vault"
    secret = "AKIAIOSFODNN7EXAMPLE"
    service = DocumentArchiveService(vault_path)
    source = SourceInfo(kind="telegram", ref="telegram:1:2", url="", label="")

    result = service.archive_document(
        data=f"Notes\n\nToken leaked here: {secret}\n".encode(),
        file_name="notes.txt",
        mime_type="text/plain",
        file_size=64,
        timestamp=datetime(2026, 4, 4, 19, 50),
        source=source,
    )

    text_path = vault_path / result.text_path
    note_path = vault_path / result.note_path
    assert secret not in text_path.read_text(encoding="utf-8")
    assert secret not in note_path.read_text(encoding="utf-8")
    assert "[redacted]" in text_path.read_text(encoding="utf-8")


def test_document_archive_scrubs_secret_from_forwarded_caption(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    secret = "ghp_abcdefghijklmnopqrstuvwxyz012345"
    service = DocumentArchiveService(vault_path)
    source = SourceInfo(kind="telegram", ref="telegram:1:2", url="", label="")

    result = service.archive_document(
        data=b"Body text, nothing secret in the file itself.\n",
        file_name="notes.txt",
        mime_type="text/plain",
        file_size=48,
        timestamp=datetime(2026, 4, 4, 19, 50),
        source=source,
        caption=f"Access token for the shared repo: {secret}",
        refresh_qmd=False,
    )

    note_text = (vault_path / result.note_path).read_text(encoding="utf-8")
    assert secret not in note_text
    assert secret not in result.daily_content
    assert "[redacted]" in note_text


# --- ingest point: YouTube transcript ---------------------------------------


def test_youtube_archive_scrubs_secret_from_transcript_text(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    secret = "AKIAIOSFODNN7EXAMPLE"
    service = YouTubeArchiveService(vault_path, "ru")

    result = service.archive_transcript(
        YouTubeTranscript(
            url="https://youtu.be/demo1234567",
            title="Demo video",
            transcript=f"Speaker reads out a leaked key: {secret}",
            source="manual subtitles",
            video_id="demo1234567",
        ),
        timestamp=datetime(2026, 4, 4, 12, 30, 45),
        summary="",
        refresh_qmd=False,
    )

    transcript_text = (vault_path / result.transcript_path).read_text(
        encoding="utf-8"
    )
    note_text = (vault_path / result.note_path).read_text(encoding="utf-8")
    assert secret not in transcript_text
    assert secret not in note_text
    assert secret not in result.daily_content
    assert "[redacted]" in transcript_text


# --- ingest point: forwarded Telegram message ------------------------------


def test_handle_forward_scrubs_secret_before_writing_daily(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage_calls: list[dict] = []
    session_calls: list[dict] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    class FakeStorage:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append_to_daily(
            self, content: str, timestamp, msg_type: str, **kwargs
        ) -> None:  # noqa: ANN001, ANN003
            storage_calls.append({"content": content})

    class FakeLinkSummaryService:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def enrich_text(self, text: str, **kwargs):  # noqa: ANN202, ANN003
            return SimpleNamespace(
                content=text,
                transcripts=[],
                summaries=[],
                youtube_summaries=[],
            )

    class FakeSessionStore:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def append(self, *args, text: str = "", **kwargs) -> None:  # noqa: ANN002, ANN003
            session_calls.append({"text": text})

    class FakeUser:
        id = 42

    secret_text = "Authorization: Bearer abc.def-ghi_012345"

    class FakeMessage:
        text = secret_text
        from_user = FakeUser()
        forward_origin = SimpleNamespace(
            sender_user=SimpleNamespace(full_name="Sender"),
        )
        date = datetime(2026, 4, 4, 12, 0, 0)
        message_id = 303
        link = None

        async def answer(self, text: str, parse_mode=None) -> None:  # noqa: ANN001
            return None

    async def fake_answer_text(_message, text: str, **kwargs):  # noqa: ANN001, ARG001, ANN202
        return SimpleNamespace()

    class FakeTask:
        def add_done_callback(self, callback) -> None:  # noqa: ANN001, D401
            return None

    def fake_create_task(coro):  # type: ignore[no-untyped-def]
        coro.close()
        return FakeTask()

    monkeypatch.setattr(
        forward_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            content_language="ru",
            ai_cli="codex",
        ),
    )
    monkeypatch.setattr(forward_handler.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(forward_handler.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(forward_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(
        forward_handler,
        "build_telegram_source_info",
        lambda *args, **kwargs: SourceInfo(
            kind="telegram",
            ref="telegram:42:303",
            url="https://t.me/c/123/303",
            label="Открыть",
        ),
    )
    monkeypatch.setattr(forward_handler, "LinkSummaryService", FakeLinkSummaryService)
    monkeypatch.setattr(forward_handler, "SessionStore", FakeSessionStore)
    monkeypatch.setattr(forward_handler, "answer_text", fake_answer_text)

    asyncio.run(forward_handler.handle_forward(FakeMessage()))

    assert len(storage_calls) == 1
    assert "abc.def-ghi_012345" not in storage_calls[0]["content"]
    assert storage_calls[0]["content"] == "Authorization: Bearer [redacted]"
    assert "abc.def-ghi_012345" not in session_calls[0]["text"]


def test_scrub_secrets_redacts_truncated_encrypted_key_past_blank_separator() -> None:
    body_line = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5"
    body = "\n".join(body_line for _ in range(5))
    text = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "Proc-Type: 4,ENCRYPTED\n"
        "DEK-Info: AES-256-CBC,0123456789ABCDEF0123456789ABCDEF\n"
        "\n"
        f"{body}\n"
        "\n"
        "after the key\n"
    )

    scrubbed, count = scrub_secrets(text)

    assert count == 1
    assert "QUJDREVG" not in scrubbed
    assert "DEK-Info" not in scrubbed
    assert scrubbed == "before\n[redacted private key]\n\nafter the key\n"


def test_save_photo_scrubs_secret_from_caption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_text = "Authorization: Bearer abc.def-ghi_012345"

    class FakeStorage:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def save_attachment(self, *args, **kwargs) -> str:  # noqa: ANN002, ANN003
            return "attachments/2026-04-04/photo.jpg"

    class FakeLinkSummaryService:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def enrich_text(self, text: str, **kwargs):  # noqa: ANN202, ANN003
            return SimpleNamespace(content=text, transcripts=[], summaries=[])

    class FakeBot:
        async def get_file(self, file_id: str):  # noqa: ANN202
            return SimpleNamespace(file_path="photos/file.jpg")

        async def download_file(self, file_path: str):  # noqa: ANN202
            return io.BytesIO(b"jpeg-bytes")

    class FakeMessage:
        photo = [SimpleNamespace(file_id="file-1")]
        caption = secret_text
        forward_origin = None
        date = datetime(2026, 4, 4, 12, 0, 0)
        message_id = 404

    async def fake_analyze(settings, relative_path):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(
        photo_handler,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=tmp_path,
            content_language="ru",
            ai_cli="codex",
        ),
    )
    monkeypatch.setattr(photo_handler, "VaultStorage", FakeStorage)
    monkeypatch.setattr(photo_handler, "LinkSummaryService", FakeLinkSummaryService)
    monkeypatch.setattr(photo_handler, "_analyze_photo", fake_analyze)
    monkeypatch.setattr(
        photo_handler,
        "build_telegram_source_info",
        lambda *args, **kwargs: SourceInfo(
            kind="telegram",
            ref="telegram:42:404",
            url="https://t.me/c/123/404",
            label="Открыть",
        ),
    )

    entry = asyncio.run(photo_handler._save_photo(FakeMessage(), FakeBot()))

    assert entry.caption == "Authorization: Bearer [redacted]"
    daily_content = photo_handler._build_photo_daily_content(
        entry.relative_path,
        entry.caption,
        entry.analysis,
        entry.source,
        entry.content_language,
    )
    assert "abc.def-ghi_012345" not in daily_content
    assert "Bearer [redacted]" in daily_content
