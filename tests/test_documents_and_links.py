import json
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest
from conftest import _write_vault_manifest

from d_brain.manifest import load_manifest_for_vault
from d_brain.services import document_extractors
from d_brain.services.document_extractors import (
    DOCUMENT_DOWNLOAD_MAX_BYTES,
    detect_document_format,
    extract_document_payload,
)
from d_brain.services.documents import (
    DOCUMENT_SUMMARY_INPUT_CHARS,
    DocumentArchiveService,
    DocumentTooLargeError,
    UnsupportedDocumentError,
)
from d_brain.services.frontmatter import read_frontmatter, validate_document
from d_brain.services.link_summary import (
    LinkSummary,
    LinkSummaryService,
    format_link_summary_message,
    format_youtube_summary_message,
)
from d_brain.services.qmd import QmdService
from d_brain.services.source_links import (
    SourceInfo,
)
from d_brain.services.web_content import (
    WebContentResult,
    _is_usable_content,
    _parse_jina_reader_response,
)
from d_brain.services.youtube_transcript import (
    YouTubeArchiveService,
    YouTubeTranscript,
    YouTubeTranscriptService,
)


@pytest.fixture(autouse=True)
def _archive_manifest(tmp_path: Path) -> None:
    _write_vault_manifest(tmp_path / "vault")


def _assert_valid_import_note(vault_path: Path, note_path: str, body: str) -> None:
    document = read_frontmatter(vault_path / note_path)
    route, missing, invalid = validate_document(
        note_path,
        document,
        load_manifest_for_vault(vault_path),
    )

    assert route.name == "import"
    assert missing == ()
    assert invalid == ()
    assert document.fields["last_accessed"] == date.today().isoformat()
    assert document.fields["relevance"] == 1.0
    assert document.fields["tier"] == "active"
    assert body in document.body.decode("utf-8")


def _reject_direct_markdown_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_text = Path.write_text
    original_write_bytes = Path.write_bytes

    def protected(path: Path) -> bool:
        return (
            path.suffix == ".md"
            or "imports" in path.parts
            or ".sync" in path.parts
        )

    def reject_markdown_write(path: Path, data: str, *args, **kwargs) -> int:  # type: ignore[no-untyped-def]
        if protected(path):
            raise AssertionError(f"direct vault write: {path}")
        return original_write_text(path, data, *args, **kwargs)

    def reject_vault_bytes(path: Path, data: bytes, *args, **kwargs) -> int:  # type: ignore[no-untyped-def]
        if protected(path):
            raise AssertionError(f"direct vault write: {path}")
        return original_write_bytes(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", reject_markdown_write)
    monkeypatch.setattr(Path, "write_bytes", reject_vault_bytes)


def test_extract_document_payload_normalizes_html_text_like_input(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "example.html"
    input_path.write_text(
        (
            "<html><head><title>Quarterly Report</title></head>"
            "<body><script>ignored()</script><h1>Revenue</h1>"
            "<p>Up 18%</p></body></html>"
        ),
        encoding="utf-8",
    )

    payload = extract_document_payload(
        input_path,
        file_format="html",
        original_name="example.html",
    )

    assert payload["format"] == "html"
    assert payload["title"] == "Quarterly Report"
    assert "Revenue" in payload["plain_text"]
    assert "ignored()" not in payload["plain_text"]
    assert payload["truncated"] is False


def test_detect_document_format_supports_mpp_extension() -> None:
    assert detect_document_format("project-plan.mpp") == "mpp"


def test_extract_document_payload_supports_mpp_via_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    input_path = tmp_path / "plan.mpp"
    input_path.write_bytes(b"mpp-binary")

    monkeypatch.setattr(
        document_extractors,
        "_run_mpp_reader",
        lambda path, warnings: {  # noqa: ARG005
            "summary": {
                "file_name": "Migration Plan",
                "start_date": "2026-04-01",
                "finish_date": "2026-05-15",
                "total_tasks": 2,
                "milestones": 1,
                "critical_tasks": 1,
                "resources_count": 3,
                "percent_complete": 42.5,
            },
            "tasks": [
                {
                    "id": 1,
                    "name": "Kickoff",
                    "start": "2026-04-01",
                    "finish": "2026-04-01",
                    "milestone": True,
                    "critical": True,
                    "resource_names": "Alexey",
                },
                {
                    "id": 2,
                    "name": "Deploy",
                    "start": "2026-04-10",
                    "finish": "2026-05-15",
                    "predecessors": "1",
                },
            ],
        },
    )

    payload = extract_document_payload(
        input_path,
        file_format="mpp",
        original_name="plan.mpp",
    )

    assert payload["format"] == "mpp"
    assert payload["title"] == "Migration Plan"
    assert "Project summary:" in payload["plain_text"]
    assert "Kickoff" in payload["plain_text"]
    assert "Deploy" in payload["plain_text"]
    assert payload["metadata"]["total_tasks"] == 2
    assert payload["metadata"]["tasks_extracted"] == 2
    assert payload["warnings"] == [
        "MPP extraction keeps project/task text only; timeline, Gantt layout, "
        "and native formatting are not preserved."
    ]


def test_run_mpp_reader_accepts_json_with_java_log_noise(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script_path = tmp_path / "mpp_read.py"
    script_path.write_text("# fake reader\n", encoding="utf-8")
    monkeypatch.setattr(
        document_extractors,
        "_MPP_READER_SCRIPT_CANDIDATES",
        (script_path,),
    )

    def fake_run(*args, **kwargs):  # noqa: ANN001, ARG001
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                "2026-05-08T09:48:09Z main ERROR Log4j2 could not find a "
                "logging implementation.\n"
                '{"summary":{"file_name":"Plan"},"tasks":[{"name":"Kickoff"}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    warnings: list[str] = []

    payload = document_extractors._run_mpp_reader(tmp_path / "plan.mpp", warnings)

    assert payload["summary"]["file_name"] == "Plan"
    assert payload["tasks"][0]["name"] == "Kickoff"
    assert warnings == []


def test_document_archive_service_persists_original_text_and_summary_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "vault"
    _reject_direct_markdown_write(monkeypatch)
    service = DocumentArchiveService(vault_path)
    source = SourceInfo(kind="telegram", ref="telegram:1:2", url="", label="")

    result = service.archive_document(
        data=b"# Header\n\nBody line one.\nBody line two.\n",
        file_name="notes.md",
        mime_type="text/markdown",
        file_size=39,
        timestamp=datetime(2026, 4, 4, 19, 50),
        source=source,
        name_hint="42",
        caption="Важный файл",
        refresh_qmd=False,
    )

    assert result.original_path.startswith("imports/documents/raw/2026-04-04/")
    assert result.text_path.startswith("imports/documents/text/2026-04-04/")
    assert result.note_path.startswith("imports/documents/notes/2026/04/")
    assert result.extraction.format == "md"
    assert result.extraction.source_path == result.original_path
    assert result.daily_summary.startswith("Header")
    assert "[[imports/documents/raw/" in result.daily_content

    raw_path = vault_path / result.original_path
    text_path = vault_path / result.text_path
    note_path = vault_path / result.note_path
    assert raw_path.read_bytes().startswith(b"# Header")
    assert "Body line one." in text_path.read_text(encoding="utf-8")
    note_content = note_path.read_text(encoding="utf-8")
    assert "# Header" in note_content
    assert "source_path:" in note_content
    assert "Важный файл" in note_content
    _assert_valid_import_note(vault_path, result.note_path, "# Header")


def test_document_archive_service_handles_relative_vault_path_for_subprocess_extraction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    service = DocumentArchiveService(Path("vault"))

    result = service.archive_document(
        data=b"# Relative Vault\n\nBody line.\n",
        file_name="relative.md",
        mime_type="text/markdown",
        file_size=29,
        timestamp=datetime(2026, 4, 11, 12, 53),
        refresh_qmd=False,
    )

    assert result.text_path.startswith("imports/documents/text/2026-04-11/")
    assert (tmp_path / "vault" / result.text_path).exists()
    assert result.daily_summary.startswith("Relative Vault")


def test_document_archive_service_uses_detailed_llm_summary_when_available(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = DocumentArchiveService(vault_path, ai_cli="qwen")
    captured: dict[str, str] = {}
    service.runner.run = lambda prompt, timeout: (  # type: ignore[method-assign]
        captured.__setitem__("prompt", prompt)
        or (
            '{"summary":"Коротко: документ о квартальном отчёте.'
            '\\n- Указан рост выручки.'
            '\\n- Есть два следующих шага."}'
        )
    )

    result = service.archive_document(
        data=b"# Quarterly Report\n\nRevenue grew by 18%.\nNext: renew contract.\n",
        file_name="report.md",
        mime_type="text/markdown",
        file_size=62,
        timestamp=datetime(2026, 4, 4, 19, 50),
        refresh_qmd=False,
    )

    assert "Make it detailed enough" in captured["prompt"]
    assert "4-7 lines starting with '- '" in captured["prompt"]
    assert result.daily_summary.startswith("Коротко: документ о квартальном отчёте.")
    note_text = (vault_path / result.note_path).read_text(encoding="utf-8")
    assert "Коротко: документ о квартальном отчёте." in note_text


def test_document_archive_service_bounds_large_summary_prompt(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = DocumentArchiveService(vault_path, ai_cli="qwen")
    captured: dict[str, str] = {}
    large_body = "A" * DOCUMENT_SUMMARY_INPUT_CHARS + "TAIL_SHOULD_NOT_APPEAR"
    service.runner.run = lambda prompt, timeout: (  # type: ignore[method-assign]
        captured.__setitem__("prompt", prompt) or '{"summary":"bounded"}'
    )

    result = service.archive_document(
        data=large_body.encode(),
        file_name="large.txt",
        mime_type="text/plain",
        file_size=len(large_body),
        timestamp=datetime(2026, 4, 4, 19, 50),
        refresh_qmd=False,
    )

    assert (vault_path / result.text_path).read_text(encoding="utf-8").startswith(
        large_body
    )
    assert large_body[:DOCUMENT_SUMMARY_INPUT_CHARS] in captured["prompt"]
    assert "TAIL_SHOULD_NOT_APPEAR" not in captured["prompt"]


def test_document_archive_service_validate_input_rejects_unsupported_and_large(
    tmp_path: Path,
) -> None:
    service = DocumentArchiveService(tmp_path / "vault")

    with pytest.raises(UnsupportedDocumentError):
        service.validate_input(
            file_name="archive.zip",
            mime_type="application/zip",
            file_size=100,
        )

    assert service.validate_input(
        file_name="migration-plan.mpp",
        mime_type="application/octet-stream",
        file_size=100,
    ) == "mpp"

    with pytest.raises(DocumentTooLargeError):
        service.validate_input(
            file_name="report.pdf",
            mime_type="application/pdf",
            file_size=DOCUMENT_DOWNLOAD_MAX_BYTES + 1,
        )


def test_document_archive_service_refreshes_qmd_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[bool] = []

    def fake_refresh(self):  # noqa: ANN001
        calls.append(True)
        return {
            "available": True,
            "updated": True,
            "embedded": True,
            "errors": [],
        }

    monkeypatch.setattr(QmdService, "refresh_after_searchable_write", fake_refresh)
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    service = DocumentArchiveService(vault_path)

    service.archive_document(
        data=b"# Header\n\nBody line one.\n",
        file_name="notes.md",
        mime_type="text/markdown",
        file_size=26,
        timestamp=datetime(2026, 4, 4, 19, 50),
    )

    assert calls == [True]


def test_document_archive_service_exposes_control_plane_workflow(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = DocumentArchiveService(vault_path)

    assert service.workflow_name == "integration.documents.archive"


def test_text_decoder_prefers_cp1251_before_utf16_false_positive() -> None:
    decoded, encoding, warnings = document_extractors._decode_text_bytes(
        "Привет мир".encode("cp1251")
    )

    assert decoded == "Привет мир"
    assert encoding == "cp1251"
    assert warnings == ["Decoded text using fallback encoding cp1251."]


def test_link_summary_service_degrades_gracefully_on_summary_error() -> None:
    service = LinkSummaryService("/tmp/vault", "qwen", "ru")
    service._summarize_url = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("llm failed")
    )

    result = service.enrich_text("https://example.com")

    assert result.summaries == []
    # Failed summaries no longer append noise blocks to content
    assert result.content == "https://example.com"


def test_link_summary_llm_prompt_filters_boilerplate() -> None:
    service = LinkSummaryService("/tmp/vault", "qwen", "en")
    captured: dict[str, str] = {}
    service.runner.run = lambda prompt, timeout: (  # type: ignore[method-assign]
        captured.__setitem__("prompt", prompt) or '{"summary":"ok"}'
    )

    summary = service._llm_summary(
        title="Demo page",
        url="https://example.com",
        content="Article body",
    )

    assert summary == "ok"
    assert "Ignore navigation, cookie banners" in captured["prompt"]
    assert "If the content is thin or mostly boilerplate" in captured["prompt"]


def test_link_summary_youtube_prompt_requests_detail() -> None:
    service = LinkSummaryService("/tmp/vault", "qwen", "ru")
    captured: dict[str, str] = {}
    service.runner.run = lambda prompt, timeout: (  # type: ignore[method-assign]
        captured.__setitem__("prompt", prompt) or '{"summary":"ok"}'
    )

    summary = service._llm_youtube_summary(
        title="Workshop",
        url="https://youtu.be/demo1234567",
        content="Transcript body",
    )

    assert summary == "ok"
    assert "Make it detailed enough" in captured["prompt"]
    assert "5-8 lines starting with '- '" in captured["prompt"]
    assert "what problem is being solved" in captured["prompt"]


def test_link_summary_service_rejects_private_urls() -> None:
    service = LinkSummaryService("/tmp/vault", "qwen", "ru")

    assert service._fetch_page_text("http://127.0.0.1/private") is None
    assert service._fetch_page_text("http://localhost/private") is None


def test_link_summary_service_rejects_redirect_to_private_url(monkeypatch) -> None:
    service = LinkSummaryService("/tmp/vault", "qwen", "ru")

    def fake_extract_web_content(url, *, config, timeout, allowed_url):  # noqa: ANN001
        del config, timeout
        assert url == "https://example.com/redirect"
        assert allowed_url("http://127.0.0.1/private") is False
        return WebContentResult(
            url="http://127.0.0.1/private",
            title="Private",
            content="secret",
            source="direct",
        )

    monkeypatch.setattr(
        "d_brain.services.link_summary.extract_web_content",
        fake_extract_web_content,
    )

    assert service._fetch_page_text("https://example.com/redirect") is None


def test_link_summary_service_appends_summary_for_short_message(monkeypatch) -> None:
    service = LinkSummaryService("/tmp/vault", "qwen", "ru")
    service._fetch_page_text = lambda url: {  # type: ignore[method-assign]
        "title": "Demo page",
        "content": "Long enough body",
    }
    service._llm_summary = lambda **kwargs: "Краткое саммари страницы."  # type: ignore[method-assign]

    result = service.enrich_text("https://example.com")

    assert result.transcripts == []
    assert len(result.summaries) == 1
    assert "Саммари по ссылке 1:" in result.content
    assert "Заголовок: Demo page" in result.content
    assert "Краткое саммари страницы." in result.content


def test_link_summary_service_archives_generic_web_source_when_timestamp_provided(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _reject_direct_markdown_write(monkeypatch)
    service = LinkSummaryService(str(vault_path), "qwen", "ru")
    service._fetch_page_text = lambda url: {  # type: ignore[method-assign]
        "url": "https://example.com/final",
        "title": "Demo page",
        "content": "Полный текст веб-источника.",
        "source": "direct",
    }
    service._llm_summary = lambda **kwargs: "Краткое саммари страницы."  # type: ignore[method-assign]
    monkeypatch.setattr(
        service.web_archive,
        "_refresh_compiled_briefings",
        lambda **kwargs: None,
    )

    result = service.enrich_text(
        "https://example.com/article",
        timestamp=datetime(2026, 4, 4, 12, 30, 45),
        source=SourceInfo(kind="telegram", ref="telegram:1:2"),
        refresh_qmd=False,
    )

    assert "imports/web/notes/2026/04/" in result.content
    assert "imports/web/raw/2026-04-04/" in result.content
    assert "imports/web/content/2026-04-04/" in result.content
    note_files = list(
        (vault_path / "imports" / "web" / "notes" / "2026" / "04").glob("*.md")
    )
    raw_files = list(
        (vault_path / "imports" / "web" / "raw" / "2026-04-04").glob("*.json")
    )
    content_files = list(
        (vault_path / "imports" / "web" / "content" / "2026-04-04").glob(
            "*.txt"
        )
    )
    assert len(note_files) == len(raw_files) == len(content_files) == 1
    assert "Краткое саммари страницы." in note_files[0].read_text(encoding="utf-8")
    assert "Полный текст веб-источника." in content_files[0].read_text(
        encoding="utf-8"
    )
    _assert_valid_import_note(
        vault_path,
        note_files[0].relative_to(vault_path).as_posix(),
        "# Demo page",
    )


def test_web_archive_service_exposes_control_plane_workflow(tmp_path: Path) -> None:
    from d_brain.services.web_archive import WebArchiveService

    service = WebArchiveService(tmp_path / "vault")

    assert service.workflow_name == "integration.web.archive"


def test_web_archive_rejects_symlinked_vault_parent_before_sidecars(
    tmp_path: Path,
) -> None:
    from d_brain.services.frontmatter import UnsafeVaultPathError
    from d_brain.services.web_archive import WebArchiveService

    external = tmp_path / "external"
    (external / "vault").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    service = WebArchiveService(alias / "vault")

    with pytest.raises(UnsafeVaultPathError, match="following symlinks"):
        service.archive_page(
            WebContentResult(
                url="https://example.com/article",
                title="Example",
                content="source content",
                source="direct",
            ),
            original_url="https://example.com/article",
            timestamp=datetime(2026, 7, 29, 12, 0),
            refresh_qmd=False,
        )

    assert not (external / "vault" / "imports").exists()


def test_web_archive_rejects_vault_symlink_swap_after_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from d_brain.services.frontmatter import UnsafeVaultPathError
    from d_brain.services.web_archive import WebArchiveService

    vault = tmp_path / "vault"
    pinned = tmp_path / "vault-pinned"
    external = tmp_path / "external"
    external.mkdir()
    service = WebArchiveService(vault)
    original_manifest = service._manifest_for_writes

    def swap_after_precheck():  # type: ignore[no-untyped-def]
        manifest = original_manifest()
        vault.rename(pinned)
        vault.symlink_to(external, target_is_directory=True)
        return manifest

    monkeypatch.setattr(service, "_manifest_for_writes", swap_after_precheck)

    with pytest.raises(UnsafeVaultPathError, match="following symlinks"):
        service.archive_page(
            WebContentResult(
                url="https://example.com/article",
                title="Example",
                content="source content",
                source="direct",
            ),
            original_url="https://example.com/article",
            timestamp=datetime(2026, 7, 29, 12, 0),
            refresh_qmd=False,
        )

    assert list(external.rglob("*")) == []


def test_document_archive_rejects_vault_symlink_swap_after_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from d_brain.services.frontmatter import UnsafeVaultPathError

    vault = tmp_path / "vault"
    pinned = tmp_path / "vault-pinned"
    external = tmp_path / "external"
    external.mkdir()
    service = DocumentArchiveService(vault)
    original_manifest = service._manifest_for_writes

    def swap_after_precheck():  # type: ignore[no-untyped-def]
        manifest = original_manifest()
        vault.rename(pinned)
        vault.symlink_to(external, target_is_directory=True)
        return manifest

    monkeypatch.setattr(service, "_manifest_for_writes", swap_after_precheck)

    with pytest.raises(UnsafeVaultPathError, match="following symlinks"):
        service.archive_document(
            data=b"document body",
            file_name="source.txt",
            mime_type="text/plain",
            file_size=13,
            timestamp=datetime(2026, 7, 29, 12, 0),
            refresh_qmd=False,
        )

    assert list(external.rglob("*")) == []


def test_link_summary_archives_web_source_when_summary_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = LinkSummaryService(str(tmp_path / "vault"), "qwen", "ru")
    service._fetch_page_text = lambda url: {  # type: ignore[method-assign]
        "url": url,
        "title": "Demo page",
        "content": "Полный текст веб-источника.",
        "source": "direct",
    }
    service._llm_summary = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("llm failed")
    )
    monkeypatch.setattr(
        service.web_archive,
        "_refresh_compiled_briefings",
        lambda **kwargs: None,
    )

    result = service.enrich_text(
        "https://example.com/article",
        timestamp=datetime(2026, 4, 4, 12, 30, 45),
        refresh_qmd=False,
    )

    assert result.summaries == []
    assert "imports/web/notes/2026/04/" in result.content
    raw_files = list(
        (tmp_path / "vault/imports/web/raw/2026-04-04").glob("*.json")
    )
    assert len(raw_files) == 1


def test_link_summary_service_skips_long_context() -> None:
    service = LinkSummaryService("/tmp/vault", "qwen", "ru")

    result = service.enrich_text(" ".join(["контекст"] * 30) + " https://example.com")

    assert result.transcripts == []
    assert "Саммари по ссылке" not in result.content


def test_link_summary_service_returns_youtube_summaries_for_reply() -> None:
    service = LinkSummaryService("/tmp/vault", "qwen", "ru")
    service.youtube.fetch_transcript = lambda url: YouTubeTranscript(  # type: ignore[method-assign]
        url="https://youtu.be/demo1234567",
        title="Demo video",
        transcript="line one",
        source="manual subtitles",
    )
    service.should_summarize = lambda text: True  # type: ignore[method-assign]
    service._summarize_url = lambda url, transcript=None: LinkSummary(  # type: ignore[method-assign]
        url=url,
        title="Demo video",
        summary="Короткое YouTube саммари.",
    )

    result = service.enrich_text("https://youtu.be/demo1234567")

    assert len(result.youtube_summaries) == 1
    assert result.youtube_summaries[0].summary == "Короткое YouTube саммари."


def test_link_summary_service_archives_youtube_transcript_when_timestamp_provided(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = LinkSummaryService(str(vault_path), "qwen", "ru")
    service.youtube.fetch_transcript = lambda url: YouTubeTranscript(  # type: ignore[method-assign]
        url=url,
        title="Demo video",
        transcript="line one\nline two",
        source="manual subtitles",
        video_id="demo1234567",
        metadata={"id": "demo1234567"},
    )
    service._llm_youtube_summary = lambda **kwargs: (  # type: ignore[method-assign]
        "Коротко: видео про семантический слой.\n"
        "- Объясняется, зачем нужна единая метрика.\n"
        "- Показаны ограничения BI-слоя."
    )
    timestamp = datetime(2026, 4, 4, 12, 30, 45)

    result = service.enrich_text(
        "https://youtu.be/demo1234567",
        timestamp=timestamp,
        source=SourceInfo(kind="telegram", ref="telegram:1:2"),
        refresh_qmd=False,
    )

    assert len(result.transcripts) == 1
    assert len(result.youtube_summaries) == 1
    assert "imports/youtube/notes/2026/04/" in result.content
    assert "imports/youtube/transcripts/2026-04-04/" in result.content
    assert "Коротко: видео про семантический слой." in result.content
    assert "line one\nline two" not in result.content
    note_files = list(
        (vault_path / "imports" / "youtube" / "notes" / "2026" / "04").glob("*.md")
    )
    transcript_files = list(
        (vault_path / "imports" / "youtube" / "transcripts" / "2026-04-04").glob(
            "*.txt"
        )
    )
    raw_files = list(
        (vault_path / "imports" / "youtube" / "raw" / "2026-04-04").glob("*.json")
    )
    assert len(note_files) == 1
    assert len(transcript_files) == 1
    assert len(raw_files) == 1
    assert "Коротко: видео про семантический слой." in note_files[0].read_text(
        encoding="utf-8"
    )
    assert "line one\nline two" in transcript_files[0].read_text(encoding="utf-8")


def test_link_summary_html_to_text_strips_script_style_and_noscript() -> None:
    extracted = LinkSummaryService._html_to_text(
        "<html><head><title>Demo</title>"
        "<script>window.secret = 1;</script>"
        "<style>body{display:none;}</style>"
        "<noscript>fallback</noscript>"
        "</head><body><p>Полезный текст</p></body></html>"
    )

    assert extracted["title"] == "Demo"
    assert "Полезный текст" in extracted["content"]
    assert "window.secret" not in extracted["content"]
    assert "display:none" not in extracted["content"]
    assert "fallback" not in extracted["content"]


def test_format_youtube_summary_message_renders_telegram_text() -> None:
    text = format_youtube_summary_message(
        [
            LinkSummary(
                url="https://youtu.be/demo1234567",
                title="Demo video",
                summary="Короткое YouTube саммари.",
            )
        ],
        language="ru",
    )

    assert "Саммари YouTube 1:" in text
    assert "Заголовок: Demo video" in text
    assert "Короткое YouTube саммари." in text


def test_format_link_summary_message_renders_generic_and_youtube_labels() -> None:
    text = format_link_summary_message(
        [
            LinkSummary(
                url="https://example.com",
                title="Demo page",
                summary="Короткое саммари страницы.",
            ),
            LinkSummary(
                url="https://youtu.be/demo1234567",
                title="Demo video",
                summary="Короткое YouTube саммари.",
            ),
        ],
        language="ru",
        youtube_urls={"https://youtu.be/demo1234567"},
    )

    assert "Саммари по ссылке 1:" in text
    assert "Саммари YouTube 2:" in text
    assert "Короткое саммари страницы." in text
    assert "Короткое YouTube саммари." in text


def test_web_content_detects_shell_boilerplate() -> None:
    result = WebContentResult(
        url="https://x.com/demo",
        title="",
        content=(
            "Something went wrong, but don’t fret — let’s give it another shot. "
            "Try again Some privacy related extensions may cause issues on x.com."
        ),
        source="direct",
    )

    assert _is_usable_content(result) is False


def test_web_content_accepts_reader_fallback_without_title() -> None:
    result = WebContentResult(
        url="https://x.com/demo",
        title="",
        content=(
            "Полезный развёрнутый текст поста и треда, уже извлечённый "
            "reader backend."
        ),
        source="jina-reader",
    )

    assert _is_usable_content(result, allow_short_without_title=True) is True


def test_parse_jina_reader_response_extracts_title_and_markdown() -> None:
    title, content = _parse_jina_reader_response(
        "Title: Demo title\n"
        "URL Source: https://example.com\n"
        "Markdown Content:\n"
        "Line one\n"
        "Line two\n"
    )

    assert title == "Demo title"
    assert content == "Line one\nLine two"


def test_youtube_transcript_service_extracts_supported_urls() -> None:
    service = YouTubeTranscriptService()
    text = (
        "Смотри https://www.youtube.com/watch?v=dQw4w9WgXcQ и "
        "https://youtu.be/abcdefghijk?t=42"
    )

    urls = service.extract_urls(text)

    assert urls == [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/abcdefghijk?t=42",
    ]


def test_youtube_transcript_service_enriches_text_with_transcripts() -> None:
    service = YouTubeTranscriptService("ru")
    service.fetch_transcript = lambda url: YouTubeTranscript(  # type: ignore[method-assign]
        url=url,
        title="Demo video",
        transcript="line one\nline two",
        source="manual subtitles",
    )

    content, transcripts = service.enrich_text(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )

    assert len(transcripts) == 1
    assert "Demo video" in content
    assert "manual subtitles" in content
    assert "line one\nline two" in content
    assert "Транскрипт YouTube 1:" in content


def test_youtube_transcript_service_prefers_metadata_caption_track() -> None:
    service = YouTubeTranscriptService("ru")
    service._video_metadata = lambda url: {  # type: ignore[method-assign]
        "id": "demo1234567",
        "title": "Demo video",
        "automatic_captions": {
            "en": [
                {
                    "ext": "json3",
                    "url": "https://example.com/captions.json3",
                }
            ]
        },
    }
    service._download_caption_track = lambda url: "line one\nline two"  # type: ignore[method-assign]
    service._download_subtitles = lambda *args, **kwargs: ""  # type: ignore[method-assign]

    transcript = service.fetch_transcript("https://youtu.be/demo1234567")

    assert transcript is not None
    assert transcript.title == "Demo video"
    assert transcript.video_id == "demo1234567"
    assert transcript.transcript == "line one\nline two"
    assert transcript.source == "автосубтитры"


def test_youtube_vtt_cleanup_deduplicates_lines() -> None:
    content = """WEBVTT

00:00:00.000 --> 00:00:01.000
<c>hello</c>

00:00:01.000 --> 00:00:02.000
<c>hello</c>

00:00:02.000 --> 00:00:03.000
world
"""

    text = YouTubeTranscriptService._vtt_to_text(content)

    assert text == "hello\nworld"


def test_youtube_json3_cleanup_deduplicates_lines() -> None:
    payload = {
        "events": [
            {"segs": [{"utf8": "hello"}]},
            {"segs": [{"utf8": "hello"}]},
            {"segs": [{"utf8": "world"}]},
        ]
    }

    text = YouTubeTranscriptService._json3_to_text(json.dumps(payload))

    assert text == "hello\nworld"


def test_youtube_archive_service_persists_note_and_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "vault"
    _reject_direct_markdown_write(monkeypatch)
    service = YouTubeArchiveService(vault_path, "ru")

    result = service.archive_transcript(
        YouTubeTranscript(
            url="https://youtu.be/demo1234567",
            title="Demo video",
            transcript="line one\nline two",
            source="manual subtitles",
            video_id="demo1234567",
            metadata={"id": "demo1234567"},
        ),
        timestamp=datetime(2026, 4, 4, 12, 30, 45),
        summary="Коротко: полезный воркшоп.\n- Есть детали.",
        source=SourceInfo(kind="telegram", ref="telegram:1:2"),
        refresh_qmd=False,
    )

    assert result.raw_path.startswith("imports/youtube/raw/2026-04-04/")
    assert result.transcript_path.startswith("imports/youtube/transcripts/2026-04-04/")
    assert result.note_path.startswith("imports/youtube/notes/2026/04/")
    assert "▶️ [[" in result.daily_content
    assert "[[imports/youtube/transcripts/" in result.daily_content
    assert "Коротко: полезный воркшоп." in result.daily_content
    note_text = (vault_path / result.note_path).read_text(encoding="utf-8")
    assert "## Саммари" in note_text
    assert "YouTube: https://youtu.be/demo1234567" in note_text
    assert "[[imports/youtube/raw/" in note_text
    _assert_valid_import_note(vault_path, result.note_path, "# Demo video")


def test_youtube_archive_service_refreshes_qmd_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[bool] = []

    def fake_refresh(self):  # noqa: ANN001
        calls.append(True)
        return {
            "available": True,
            "updated": True,
            "embedded": True,
            "errors": [],
        }

    monkeypatch.setattr(QmdService, "refresh_after_searchable_write", fake_refresh)
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    service = YouTubeArchiveService(vault_path, "ru")

    service.archive_transcript(
        YouTubeTranscript(
            url="https://youtu.be/demo1234567",
            title="Demo video",
            transcript="line one",
            source="manual subtitles",
            video_id="demo1234567",
        ),
        timestamp=datetime(2026, 4, 4, 12, 30, 45),
        summary="Коротко: видео.",
    )

    assert calls == [True]


def test_youtube_archive_service_exposes_control_plane_workflow(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    service = YouTubeArchiveService(vault_path, "ru")

    assert service.workflow_name == "integration.youtube.archive"


def test_youtube_archive_rejects_vault_symlink_swap_after_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from d_brain.services.frontmatter import UnsafeVaultPathError

    vault = tmp_path / "vault"
    pinned = tmp_path / "vault-pinned"
    external = tmp_path / "external"
    external.mkdir()
    service = YouTubeArchiveService(vault, "ru")
    original_manifest = service._manifest_for_writes

    def swap_after_precheck():  # type: ignore[no-untyped-def]
        manifest = original_manifest()
        vault.rename(pinned)
        vault.symlink_to(external, target_is_directory=True)
        return manifest

    monkeypatch.setattr(service, "_manifest_for_writes", swap_after_precheck)

    with pytest.raises(UnsafeVaultPathError, match="following symlinks"):
        service.archive_transcript(
            YouTubeTranscript(
                url="https://youtu.be/demo1234567",
                title="Demo video",
                transcript="line one",
                source="manual subtitles",
                video_id="demo1234567",
            ),
            timestamp=datetime(2026, 7, 29, 12, 0),
            summary="summary",
            refresh_qmd=False,
        )

    assert list(external.rglob("*")) == []
