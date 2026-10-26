"""Unpacked-size limit and child-process hardening of document extraction (B3)."""

from __future__ import annotations

import json
import resource
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from d_brain import run_document_extract
from d_brain.services import document_extractors, documents
from d_brain.services.document_extractors import extract_document_payload
from d_brain.services.documents import DocumentArchiveService

_GIB = 1024**3


def _zip(path: Path, parts: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path


def _forbid_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("archive must not be read")

    monkeypatch.setattr(zipfile.ZipFile, "read", fail)
    monkeypatch.setattr(document_extractors, "_markitdown_convert", fail)


def test_oversized_docx_is_rejected_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _zip(tmp_path / "a.docx", {"word/document.xml": b"<x/>" * 256})
    monkeypatch.setattr(document_extractors, "DOCUMENT_MAX_UNPACKED_BYTES", 100)
    _forbid_reads(monkeypatch)

    payload = extract_document_payload(path, file_format="docx", original_name="a.docx")

    assert payload["plain_text"] == ""
    assert any("unpacked size limit exceeded" in w for w in payload["warnings"])


def test_unpacked_size_limit_counts_all_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(document_extractors, "DOCUMENT_MAX_UNPACKED_BYTES", 100)
    monkeypatch.setattr(
        document_extractors, "_markitdown_convert", lambda path, warnings: "ok"
    )
    two = _zip(tmp_path / "two.docx", {"a.xml": b"x" * 60, "b.xml": b"x" * 60})
    one = _zip(tmp_path / "one.docx", {"a.xml": b"x" * 60})

    rejected = extract_document_payload(
        two, file_format="docx", original_name="two.docx"
    )
    accepted = extract_document_payload(
        one, file_format="docx", original_name="one.docx"
    )

    assert rejected["plain_text"] == ""
    assert accepted["plain_text"] == "ok"


@pytest.mark.parametrize("file_format", ["xlsx", "pptx"])
def test_oversized_xlsx_and_pptx_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_format: str
) -> None:
    path = _zip(tmp_path / f"a.{file_format}", {"part.xml": b"x" * 500})
    monkeypatch.setattr(document_extractors, "DOCUMENT_MAX_UNPACKED_BYTES", 100)
    _forbid_reads(monkeypatch)

    payload = extract_document_payload(
        path, file_format=file_format, original_name=path.name
    )

    assert payload["plain_text"] == ""
    assert any("unpacked size limit exceeded" in w for w in payload["warnings"])


def test_normal_docx_passes_size_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xml = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t>hi</w:t></w:r></w:p></w:body></w:document>"
    )
    path = _zip(tmp_path / "ok.docx", {"word/document.xml": xml})
    monkeypatch.setattr(
        document_extractors, "_markitdown_convert", lambda path, warnings: "hi"
    )

    payload = extract_document_payload(
        path, file_format="docx", original_name="ok.docx"
    )

    assert payload["plain_text"] == "hi"
    assert "paragraphs" in payload["metadata"]


def _capture_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_format: str = "pdf"
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        payload = {
            "plain_text": "hi", "title": "t", "format": file_format,
            "warnings": [], "metadata": {}, "truncated": False,
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(documents, "run_in_process_group", fake_run)
    DocumentArchiveService(tmp_path / "vault")._extract_payload(
        original_path=f"imports/r.{file_format}",
        file_format=file_format,
        original_name=f"r.{file_format}",
    )
    return captured


def test_extract_payload_child_env_has_only_allowlisted_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TODOIST_API_KEY"
    ):
        monkeypatch.setenv(key, "secret")

    env = _capture_run(tmp_path, monkeypatch)["env"]

    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "JAVA_HOME", "PYTHONPATH"}
    assert set(env) <= allowed
    src_path = str(Path(documents.__file__).resolve().parents[2])
    assert env["PYTHONPATH"].split(":")[0] == src_path


def test_extract_payload_does_not_use_preexec_fn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # preexec_fn is unsafe when the parent runs from a worker thread.
    assert "preexec_fn" not in _capture_run(tmp_path, monkeypatch)


def _run_main(
    monkeypatch: pytest.MonkeyPatch, file_format: str
) -> list[tuple[int, tuple[int, int]]]:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        run_document_extract.resource, "getrlimit",
        lambda kind: (0, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(
        run_document_extract.resource, "setrlimit",
        lambda kind, value: calls.append((kind, value)),
    )
    monkeypatch.setattr(
        run_document_extract, "extract_document_payload",
        lambda *a, **k: {"plain_text": ""},
    )
    monkeypatch.setattr(
        "sys.argv",
        ["x", "--input", "/tmp/f", "--format", file_format, "--name=f"],
    )
    run_document_extract.main()
    return calls


def test_extractor_sets_address_space_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _run_main(monkeypatch, "pdf") == [
        (resource.RLIMIT_AS, (4 * _GIB, 4 * _GIB))
    ]


def test_memory_limit_failure_is_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(kind, value):  # noqa: ANN001
        raise ValueError("unsupported")

    monkeypatch.setattr(run_document_extract.resource, "setrlimit", boom)
    monkeypatch.setattr(
        run_document_extract, "extract_document_payload",
        lambda *a, **k: {"plain_text": ""},
    )
    monkeypatch.setattr(
        "sys.argv", ["x", "--input", "/tmp/f", "--format", "pdf", "--name=f"]
    )

    assert run_document_extract.main() == 0


def test_mpp_extraction_has_no_memory_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _run_main(monkeypatch, "mpp") == []
