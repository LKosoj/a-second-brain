import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import _write_vault_manifest

from d_brain import run_qmd
from d_brain.services.image_analysis import ImageAnalysisService
from d_brain.services.memory_entries import DailyEntryMemoryStore
from d_brain.services.plaud import (
    PlaudSyncService,
)
from d_brain.services.processor import (
    CliProcessor,
)
from d_brain.services.qmd import QmdService


def _qmd_service(vault_path: Path, *, qmd_index: str = "dbrain") -> QmdService:
    _write_vault_manifest(vault_path, qmd_index=qmd_index)
    return QmdService(vault_path)


def test_qmd_build_env_reads_project_remote_embedding_settings(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "EMB_MODEL=llmgateway/embedding-demo",
                "RERANK_MODEL=llmgateway/rerank-demo",
                "OPENAI_API_KEY=test-openai",
                "BASE_URL=https://gateway.example.com/v1",
            ]
        ),
        encoding="utf-8",
    )
    vault_path = tmp_path / "vault"
    (vault_path / "daily").mkdir(parents=True)

    env = _qmd_service(vault_path).build_env()

    assert env["QMD_EMBED_MODEL"] == "llmgateway/embedding-demo"
    assert env["QMD_RERANK_MODEL"] == "llmgateway/rerank-demo"
    assert env["RERANK_MODEL"] == "llmgateway/rerank-demo"
    assert env["OPENAI_API_KEY"] == "test-openai"
    assert env["BASE_URL"] == "https://gateway.example.com/v1"


def test_qmd_build_env_uses_local_model_when_emb_model_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "EMB_MODEL=",
                "RERANK_MODEL=",
                "OPENAI_API_KEY=test-openai",
                "BASE_URL=https://gateway.example.com/v1",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMB_MODEL", "outside-shell-model")
    monkeypatch.setenv("RERANK_MODEL", "outside-shell-rerank")
    vault_path = tmp_path / "vault"
    (vault_path / "daily").mkdir(parents=True)

    service = _qmd_service(vault_path)
    env = service.build_env()

    assert env["EMB_MODEL"] == ""
    assert env["RERANK_MODEL"] == ""
    assert "Qwen3-Embedding-0.6B" in env["QMD_EMBED_MODEL"]
    assert env["QMD_RERANK_MODEL"] == ""
    assert service._uses_remote_embeddings() is False


def test_services_use_absolute_mcp_config_path(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    vault_path = project_path / "vault"
    vault_path.mkdir(parents=True)
    mcp_config_path = project_path / "mcp-config.json"
    mcp_config_path.write_text("{}", encoding="utf-8")

    processor = CliProcessor(vault_path)
    image_analysis = ImageAnalysisService(vault_path)
    plaud = PlaudSyncService(vault_path, bearer_token="token")

    expected = str(mcp_config_path.resolve())

    assert processor._cli_extra_env()["MCP_CONFIG_PATH"] == expected
    assert image_analysis._cli_extra_env()["MCP_CONFIG_PATH"] == expected
    assert plaud._cli_extra_env()["MCP_CONFIG_PATH"] == expected


def test_qmd_touch_uses_configured_absolute_uv_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    note = vault_path / "daily/example.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Example\n", encoding="utf-8")
    service = _qmd_service(vault_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("UV_BIN", "/opt/uv/bin/uv")
    monkeypatch.setattr("d_brain.services.qmd.subprocess.run", fake_run)

    service.touch_notes(["daily/example.md"])

    assert calls[0][:2] == ["/opt/uv/bin/uv", "run"]


def test_run_qmd_get_touches_opened_notes(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    class FakeQmdService:
        def __init__(self, vault_path: Path) -> None:
            assert vault_path == tmp_path

        def run(self, *args: str):  # noqa: ANN202
            calls.append(("run", args))
            return subprocess.CompletedProcess(
                args=["qmd", *args],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

        def touch_notes(self, targets: list[str]) -> None:
            calls.append(("touch", targets))

    monkeypatch.setattr(run_qmd, "QmdService", FakeQmdService)
    monkeypatch.setattr(
        run_qmd,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path),
    )
    monkeypatch.setattr(sys, "argv", ["run_qmd.py", "get", "thoughts/example"])

    assert run_qmd.main() == 0
    assert calls == [
        ("run", ("get", "thoughts/example")),
        ("touch", ["thoughts/example"]),
    ]


def test_run_qmd_query_uses_remote_query_adapter(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeQmdService:
        def __init__(self, vault_path: Path) -> None:
            assert vault_path == tmp_path

        def uses_remote_embeddings(self) -> bool:
            return True

        def query(self, query: str, *, limit: int = 5) -> dict[str, object]:
            calls.append(("query", (query, limit)))
            return {
                "query": query,
                "backend": "query",
                "mode": "query",
                "results": [{"file": "qmd://daily/example.md", "score": 0.91}],
            }

        def run(self, *args: str) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"unexpected raw qmd run: {args}")

    monkeypatch.setattr(run_qmd, "QmdService", FakeQmdService)
    monkeypatch.setattr(
        run_qmd,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path),
    )

    exit_code = run_qmd.main(
        ["query", "remote semantic query", "--format", "json", "-n", "3"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert calls == [("query", ("remote semantic query", 3))]
    assert payload == [{"file": "qmd://daily/example.md", "score": 0.91}]


def test_run_qmd_query_cli_format_renders_text_and_uses_the_cli_default_limit(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Code review: neither the text-output branch nor the two different
    default limits (json wants 20 rows, a terminal wants 5) were covered --
    swapping them broke nothing. Both are what the owner actually sees when
    running ``qmd query`` without flags."""
    calls: list[tuple[str, object]] = []

    class FakeQmdService:
        def __init__(self, vault_path: Path) -> None:
            assert vault_path == tmp_path

        def uses_remote_embeddings(self) -> bool:
            return True

        def query(self, query: str, *, limit: int = 5) -> dict[str, object]:
            calls.append(("query", (query, limit)))
            return {
                "query": query,
                "backend": "query",
                "mode": "query",
                "results": [{"file": "qmd://daily/example.md", "score": 0.91}],
            }

        def format_query(self, payload: dict[str, object]) -> str:
            calls.append(("format_query", payload["query"]))
            return "отрендеренный текст"

        def run(self, *args: str) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"unexpected raw qmd run: {args}")

    monkeypatch.setattr(run_qmd, "QmdService", FakeQmdService)
    monkeypatch.setattr(
        run_qmd,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path),
    )

    exit_code = run_qmd.main(["query", "запрос без флагов"])

    assert exit_code == 0
    assert calls == [
        ("query", ("запрос без флагов", 5)),
        ("format_query", "запрос без флагов"),
    ]
    assert capsys.readouterr().out == "отрендеренный текст\n"


def test_run_qmd_query_json_format_defaults_to_a_wider_limit(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """The json branch feeds another program, not a terminal, so it defaults
    to more rows than the cli branch -- the pair only means something when
    both halves are pinned."""
    calls: list[tuple[str, object]] = []

    class FakeQmdService:
        def __init__(self, vault_path: Path) -> None:
            assert vault_path == tmp_path

        def uses_remote_embeddings(self) -> bool:
            return True

        def query(self, query: str, *, limit: int = 5) -> dict[str, object]:
            calls.append(("query", (query, limit)))
            return {"query": query, "results": []}

        def run(self, *args: str) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"unexpected raw qmd run: {args}")

    monkeypatch.setattr(run_qmd, "QmdService", FakeQmdService)
    monkeypatch.setattr(
        run_qmd,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path),
    )

    exit_code = run_qmd.main(["query", "запрос", "--json"])

    assert exit_code == 0
    assert calls == [("query", ("запрос", 20))]
    assert json.loads(capsys.readouterr().out) == []


def test_run_qmd_query_keeps_stock_qmd_for_local_models(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    calls: list[tuple[str, ...]] = []

    class FakeQmdService:
        def __init__(self, vault_path: Path) -> None:
            assert vault_path == tmp_path

        def uses_remote_embeddings(self) -> bool:
            return False

        def run(self, *args: str) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(
                args=["qmd", *args],
                returncode=0,
                stdout="stock query\n",
                stderr="",
            )

    monkeypatch.setattr(run_qmd, "QmdService", FakeQmdService)
    monkeypatch.setattr(
        run_qmd,
        "get_settings",
        lambda: SimpleNamespace(vault_path=tmp_path),
    )

    exit_code = run_qmd.main(["query", "local query", "-n", "2"])

    assert exit_code == 0
    assert calls == [("query", "local query", "-n", "2")]
    assert capsys.readouterr().out == "stock query\n"


def test_qmd_config_includes_documents_collection_when_notes_exist(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "imports" / "documents" / "notes").mkdir(parents=True)
    (vault_path / "imports" / "youtube" / "notes").mkdir(parents=True)
    (vault_path / "daily").mkdir(parents=True)
    (vault_path / "goals").mkdir()

    service = _qmd_service(vault_path)
    config_path = service.ensure_local_config()
    config_text = config_path.read_text(encoding="utf-8")

    assert "documents:" in config_text
    assert "imports/documents/notes" in config_text
    assert "youtube:" in config_text
    assert "imports/youtube/notes" in config_text


def test_qmd_config_includes_forwarded_documents_collection_when_dir_exists(
    tmp_path: Path,
) -> None:
    """Forwarded documents land under imports/documents/forwarded/ (kept out
    of imports/documents/notes/ so they stay capped at "forwarded" trust,
    see CompiledBriefingService._source_trust_level) -- they still need
    their own qmd collection so those notes stay full-text searchable."""
    vault_path = tmp_path / "vault"
    (vault_path / "imports" / "documents" / "forwarded").mkdir(parents=True)
    (vault_path / "daily").mkdir(parents=True)
    (vault_path / "goals").mkdir()

    service = _qmd_service(vault_path)
    config_path = service.ensure_local_config()
    config_text = config_path.read_text(encoding="utf-8")

    assert "documents_forwarded:" in config_text
    assert "imports/documents/forwarded" in config_text


def test_qmd_service_writes_project_local_config(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "daily").mkdir(parents=True)
    (vault_path / "thoughts").mkdir()
    (vault_path / "imports" / "plaud" / "notes").mkdir(parents=True)
    (vault_path / "imports" / "youtube" / "notes").mkdir(parents=True)
    (vault_path / "goals").mkdir()
    (vault_path / "summaries").mkdir()
    (vault_path / "MOC").mkdir()

    service = _qmd_service(vault_path)
    config_path = service.ensure_local_config()
    env = service.build_env()

    content = config_path.read_text(encoding="utf-8")
    assert 'path: "' in content
    assert "daily:" in content
    assert "plaud:" in content
    assert "documents:" in content
    assert "youtube:" in content
    assert (vault_path / "imports" / "documents" / "notes").exists()
    assert (vault_path / "imports" / "youtube" / "notes").exists()
    assert env["QMD_CONFIG_DIR"].endswith("vault/.qmd/config")
    assert env["XDG_CACHE_HOME"].endswith("vault/.qmd/cache")
    assert "Qwen3-Embedding-0.6B" in env["QMD_EMBED_MODEL"]
    assert env["NODE_LLAMA_CPP_GPU"] == "off"


def test_qmd_service_uses_index_from_manifest(tmp_path: Path, monkeypatch) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "daily").mkdir(parents=True)
    service = _qmd_service(vault_path, qmd_index="personal-memory")
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    env = service.build_env()
    service.run("status")

    assert service._config_path.name == "personal-memory.yml"
    assert env["QMD_DB_PATH"].endswith("personal-memory.sqlite")
    assert commands == [["qmd", "--index", "personal-memory", "status"]]


def test_qmd_recall_prefers_hotter_memory_and_deep_recall_keeps_archive(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "daily").mkdir(parents=True)
    (vault_path / "thoughts").mkdir()
    (vault_path / "daily" / "2026-04-04.md").write_text(
        "# 2026-04-04\n\n## 16:20 [text]\nСвежая заметка про память.\n",
        encoding="utf-8",
    )
    DailyEntryMemoryStore(vault_path).sync_daily_file(
        vault_path / "daily" / "2026-04-04.md"
    )
    (vault_path / "thoughts" / "old.md").write_text(
        (
            "---\n"
            "tier: archive\n"
            "relevance: 0.12\n"
            "last_accessed: 2025-01-01\n"
            "---\n"
            "# Old\n"
        ),
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {
                "file": "qmd://thoughts/old.md",
                "title": "Old",
                "score": 0.8,
                "snippet": "архив",
            },
            {
                "file": "qmd://daily/2026-04-04.md",
                "title": "Today",
                "score": 0.75,
                "snippet": "свежее",
            },
        ],
    )

    shallow = service.recall("память", deep=False, limit=5)
    deep = service.recall("память", deep=True, limit=5)

    assert [item["rel_path"] for item in shallow["results"]] == ["daily/2026-04-04.md"]
    assert [item["rel_path"] for item in deep["results"]] == [
        "daily/2026-04-04.md",
        "thoughts/old.md",
    ]


def test_qmd_demotes_superseded_epistemic_note_and_formats_metadata(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    thoughts = vault_path / "thoughts"
    thoughts.mkdir(parents=True)
    (thoughts / "old.md").write_text(
        """---
tier: active
relevance: 0.80
epistemic_confidence: verified
epistemic_scope: project:test
epistemic_state: superseded
epistemic_verification: issue-123
supersedes: []
superseded_by: thoughts/new.md
---
# Old fact
""",
        encoding="utf-8",
    )
    (thoughts / "new.md").write_text(
        """---
tier: active
relevance: 0.80
epistemic_confidence: inferred
epistemic_scope: project:test
epistemic_state: active
supersedes: [thoughts/old.md]
---
# Corrected fact
""",
        encoding="utf-8",
    )
    (vault_path / "compiled").mkdir()
    (vault_path / "compiled" / "brief.md").write_text(
        "---\nconfidence: high\nstatus: active\n---\n# Compiled\n",
        encoding="utf-8",
    )
    service = _qmd_service(vault_path)
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {
                "file": "qmd://thoughts/old.md",
                "title": "Old fact",
                "score": 0.80,
                "snippet": "old",
            },
            {
                "file": "qmd://thoughts/new.md",
                "title": "Corrected fact",
                "score": 0.80,
                "snippet": "new",
            },
        ],
    )

    payload = service.recall("fact", limit=5)

    assert [item["rel_path"] for item in payload["results"]] == [
        "thoughts/new.md",
        "thoughts/old.md",
    ]
    old = payload["results"][1]
    assert old["epistemic_state"] == "superseded"
    assert old["supersession_adjustment"] < 0
    assert "superseded" in QmdService.format_recall(payload)
    assert "scope=project:test" in QmdService.format_recall(payload)
    compiled_signal = QmdService._read_frontmatter_signal(
        vault_path / "compiled/brief.md"
    )
    assert compiled_signal is not None
    assert "epistemic_confidence" not in compiled_signal


def test_read_frontmatter_signal_survives_leading_utf8_bom(tmp_path: Path) -> None:
    """Regression: the old ``content.startswith("---\\n")`` check anchors at
    literal position 0, so a note saved with a leading UTF-8 BOM (e.g.
    Notepad's "Save As UTF-8" on Windows) decodes to a ``\\ufeff`` character
    in front of the ``---`` and the check failed, silently dropping
    tier/relevance/last_accessed for the note and under-ranking it in
    search.
    """
    note_path = tmp_path / "note.md"
    text = "---\ntier: warm\nrelevance: 0.65\nlast_accessed: 2026-08-01\n---\n# Demo\n"
    note_path.write_bytes(("\ufeff" + text).encode())

    signal = QmdService._read_frontmatter_signal(note_path)

    assert signal is not None
    assert signal["tier"] == "warm"
    assert signal["relevance"] == 0.65
    assert signal["last_accessed"] == "2026-08-01"


def test_read_frontmatter_signal_survives_crlf_line_endings(tmp_path: Path) -> None:
    """Confirms CRLF (e.g. Windows' `git config core.autocrlf true`) doesn't
    need the same explicit fix as the BOM case above: ``path.read_text()``
    already applies universal-newline translation, normalizing ``\\r\\n`` to
    ``\\n`` before ``_read_frontmatter_signal`` ever sees the string. This
    guards against a future refactor swapping that call for raw
    ``path.read_bytes().decode(...)`` (as ``compiled_briefings.py``'s
    ``_frontmatter_fields`` does, which is why *that* method needs its own
    explicit CRLF normalization) silently reintroducing the bug here too.
    """
    note_path = tmp_path / "note.md"
    text = "---\ntier: warm\nrelevance: 0.65\nlast_accessed: 2026-08-01\n---\n# Demo\n"
    note_path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

    signal = QmdService._read_frontmatter_signal(note_path)

    assert signal is not None
    assert signal["tier"] == "warm"
    assert signal["relevance"] == 0.65
    assert signal["last_accessed"] == "2026-08-01"


def test_qmd_recall_shallow_does_not_fallback_to_archive_only_results(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "old.md").write_text(
        (
            "---\n"
            "tier: archive\n"
            "relevance: 0.12\n"
            "last_accessed: 2025-01-01\n"
            "---\n"
            "# Old\n"
        ),
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {
                "file": "qmd://thoughts/old.md",
                "title": "Old",
                "score": 0.8,
                "snippet": "архив",
            }
        ],
    )

    shallow = service.recall("архив", deep=False, limit=5)
    deep = service.recall("архив", deep=True, limit=5)

    assert shallow["results"] == []
    assert len(deep["results"]) == 1


def test_qmd_recall_prioritizes_recent_results_when_scores_are_similar(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "daily").mkdir(parents=True)
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "strategy.md").write_text(
        ("---\ntier: active\nrelevance: 0.70\ncreated: 2024-01-01\n---\n# Strategy\n"),
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._today = lambda: date(2026, 4, 5)  # type: ignore[method-assign]
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "query",
        [
            {
                "file": "qmd://thoughts/strategy.md",
                "title": "Old strategy",
                "score": 0.74,
                "snippet": "старое решение по стратегии",
            },
            {
                "file": "qmd://daily/2026-04-04.md",
                "title": "Recent daily",
                "score": 0.74,
                "snippet": "свежее решение по стратегии",
            },
        ],
    )

    payload = service.recall("стратегия", deep=False, limit=20)

    assert [item["rel_path"] for item in payload["results"][:2]] == [
        "daily/2026-04-04.md",
        "thoughts/strategy.md",
    ]
    assert payload["results"][0]["age_days"] == 1
    assert (
        payload["results"][0]["effective_score"]
        > payload["results"][1]["effective_score"]
    )
    assert payload["results"][1]["age_days"] > payload["results"][0]["age_days"]


def test_qmd_recall_raw_returns_unadjusted_score(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "fresh.md").write_text(
        (
            "---\n"
            "tier: core\n"
            "relevance: 0.90\n"
            "created: 2026-08-01\n"
            "---\n"
            "# Fresh\n"
        ),
        encoding="utf-8",
    )
    (vault_path / "thoughts" / "cold.md").write_text(
        (
            "---\n"
            "tier: cold\n"
            "relevance: 0.10\n"
            "created: 2020-01-01\n"
            "---\n"
            "# Cold\n"
        ),
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {
                "file": "qmd://thoughts/fresh.md",
                "title": "Fresh",
                "score": 0.55,
                "snippet": "x",
            },
            {
                "file": "qmd://thoughts/cold.md",
                "title": "Cold",
                "score": 0.42,
                "snippet": "y",
            },
        ],
    )

    payload = service.recall("query", raw=True, limit=5)

    assert len(payload["results"]) == 2
    for item in payload["results"]:
        assert item["age_adjustment"] == 0.0
        assert item["supersession_adjustment"] == 0.0
    by_path = {item["rel_path"]: item for item in payload["results"]}
    assert by_path["thoughts/fresh.md"]["effective_score"] == round(0.55, 4)
    assert by_path["thoughts/cold.md"]["effective_score"] == round(0.42, 4)


def test_qmd_recall_raw_handles_out_of_range_scores_above_one(tmp_path: Path) -> None:
    """External qmd backends do not guarantee scores in [0,1]. In raw mode,
    effective_score must stay unclamped (ranking depends on it), while
    confidence stays clamped into [0,1] and does not distort the order."""
    vault_path = tmp_path / "vault"
    (vault_path / "thoughts").mkdir(parents=True)
    for name in ("a", "b", "c"):
        (vault_path / "thoughts" / f"{name}.md").write_text(
            "---\ntier: active\nrelevance: 0.5\ncreated: 2026-08-01\n---\n# X\n",
            encoding="utf-8",
        )

    service = _qmd_service(vault_path)
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {"file": "qmd://thoughts/b.md", "title": "B", "score": 0.9, "snippet": "x"},
            {"file": "qmd://thoughts/a.md", "title": "A", "score": 1.3, "snippet": "y"},
            {"file": "qmd://thoughts/c.md", "title": "C", "score": 1.1, "snippet": "z"},
        ],
    )

    payload = service.recall("query", raw=True, limit=5)

    assert [item["rel_path"] for item in payload["results"]] == [
        "thoughts/a.md",
        "thoughts/c.md",
        "thoughts/b.md",
    ]
    by_path = {item["rel_path"]: item for item in payload["results"]}
    assert by_path["thoughts/a.md"]["effective_score"] == 1.3
    assert by_path["thoughts/c.md"]["effective_score"] == 1.1
    assert by_path["thoughts/a.md"]["confidence"] == 1.0
    assert by_path["thoughts/c.md"]["confidence"] == 1.0
    assert by_path["thoughts/b.md"]["confidence"] == 0.9
    assert payload["confidence"] == 1.0


def test_qmd_recall_raw_keeps_cold_and_archive_at_shallow_depth(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "archived.md").write_text(
        (
            "---\n"
            "tier: archive\n"
            "relevance: 0.10\n"
            "last_accessed: 2020-01-01\n"
            "---\n"
            "# Archived\n"
        ),
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {
                "file": "qmd://thoughts/archived.md",
                "title": "Archived",
                "score": 0.5,
                "snippet": "x",
            },
        ],
    )

    payload = service.recall("query", raw=True, deep=False, limit=5)

    assert [item["rel_path"] for item in payload["results"]] == ["thoughts/archived.md"]


def test_qmd_recall_raw_flips_ranking_versus_normal_recall(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "a_core.md").write_text(
        (
            "---\n"
            "tier: core\n"
            "relevance: 0.0\n"
            "created: 2026-08-01\n"
            "---\n"
            "# A\n"
        ),
        encoding="utf-8",
    )
    (vault_path / "thoughts" / "b_archive.md").write_text(
        (
            "---\n"
            "tier: archive\n"
            "relevance: 0.0\n"
            "created: 2026-08-01\n"
            "---\n"
            "# B\n"
        ),
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._today = lambda: date(2026, 8, 5)  # type: ignore[method-assign]
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {
                "file": "qmd://thoughts/a_core.md",
                "title": "A",
                "score": 0.60,
                "snippet": "x",
            },
            {
                "file": "qmd://thoughts/b_archive.md",
                "title": "B",
                "score": 0.85,
                "snippet": "y",
            },
        ],
    )

    normal = service.recall("query", deep=True, limit=5)
    raw = service.recall("query", raw=True, limit=5)

    assert [item["rel_path"] for item in normal["results"]] == [
        "thoughts/a_core.md",
        "thoughts/b_archive.md",
    ]
    assert [item["rel_path"] for item in raw["results"]] == [
        "thoughts/b_archive.md",
        "thoughts/a_core.md",
    ]


def test_qmd_recall_raw_preserves_diagnostic_fields_for_superseded_cold_note(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "old.md").write_text(
        """---
tier: cold
relevance: 0.20
epistemic_confidence: verified
epistemic_scope: project:test
epistemic_state: superseded
epistemic_verification: issue-1
supersedes: []
superseded_by: thoughts/new.md
---
# Old fact
""",
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {
                "file": "qmd://thoughts/old.md",
                "title": "Old fact",
                "score": 0.66,
                "snippet": "x",
            },
        ],
    )

    payload = service.recall("query", raw=True, limit=5)

    assert len(payload["results"]) == 1
    item = payload["results"][0]
    assert item["memory_tier"] == "cold"
    assert item["epistemic_state"] == "superseded"
    assert item["superseded_by"] == "thoughts/new.md"
    assert item["effective_score"] == round(0.66, 4)
    assert item["supersession_adjustment"] == 0.0
    assert item["age_adjustment"] == 0.0


def test_qmd_recall_raw_mode_label_and_deep_independence(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "a.md").write_text(
        "---\ntier: core\nrelevance: 0.5\ncreated: 2026-08-01\n---\n# A\n",
        encoding="utf-8",
    )
    (vault_path / "thoughts" / "b.md").write_text(
        "---\ntier: archive\nrelevance: 0.1\ncreated: 2020-01-01\n---\n# B\n",
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {"file": "qmd://thoughts/a.md", "title": "A", "score": 0.6, "snippet": "x"},
            {"file": "qmd://thoughts/b.md", "title": "B", "score": 0.4, "snippet": "y"},
        ],
    )

    shallow = service.recall("query", raw=True, deep=False, limit=5)
    deep = service.recall("query", raw=True, deep=True, limit=5)

    assert shallow["mode"] == "raw-recall"
    assert deep["mode"] == "raw-recall"
    assert shallow == deep


def test_qmd_recall_normal_formula_exact_anchor(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "active.md").write_text(
        "---\ntier: active\nrelevance: 0.90\ncreated: 2026-08-03\n---\n# Active\n",
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._today = lambda: date(2026, 8, 5)  # type: ignore[method-assign]
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {
                "file": "qmd://thoughts/active.md",
                "title": "Active",
                "score": 0.70,
                "snippet": "x",
            },
        ],
    )

    payload = service.recall("query", limit=5)

    assert payload["results"][0]["effective_score"] == 0.925


def test_qmd_recall_default_applies_tier_bonus_instead_of_raw_score(
    tmp_path: Path,
) -> None:
    """Guard against an accidental default flip to raw=True: a bare call
    must still apply the tier bonus (and thus differ from raw score), and
    must match an explicit raw=False call exactly."""
    vault_path = tmp_path / "vault"
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "note.md").write_text(
        "---\ntier: active\nrelevance: 0.5\ncreated: 2026-08-01\n---\n# Note\n",
        encoding="utf-8",
    )

    service = _qmd_service(vault_path)
    service._today = lambda: date(2026, 8, 1)  # type: ignore[method-assign]
    service._recall_candidates = lambda query, limit: (  # type: ignore[method-assign]
        "search",
        [
            {
                "file": "qmd://thoughts/note.md",
                "title": "Note",
                "score": 0.6,
                "snippet": "x",
            },
        ],
    )

    default_payload = service.recall("query", limit=5)
    explicit_false_payload = service.recall("query", raw=False, limit=5)
    explicit_raw_payload = service.recall("query", raw=True, limit=5)

    assert default_payload == explicit_false_payload
    default_score = default_payload["results"][0]["effective_score"]
    raw_score = explicit_raw_payload["results"][0]["effective_score"]
    assert default_score != raw_score
    # tier=active (+0.10) + relevance 0.5 * 0.05 (+0.025) + same-day
    # recency bonus (+0.08, age_days=0) over the raw score.
    assert default_score == round(raw_score + 0.10 + 0.025 + 0.08, 4)


def test_qmd_recall_uses_remote_query_helper_for_remote_emb_model(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "EMB_MODEL=llmgateway/embedding-demo",
                "RERANK_MODEL=llmgateway/rerank-demo",
                "OPENAI_API_KEY=test-openai",
                "BASE_URL=https://gateway.example.com/v1",
            ]
        ),
        encoding="utf-8",
    )
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)
    runs: list[tuple[str, ...]] = []

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args[0]) if args else ()
        runs.append(command)
        env = kwargs.get("env", {})
        assert env["QMD_RERANK_MODEL"] == "llmgateway/rerank-demo"
        assert env["RERANK_MODEL"] == "llmgateway/rerank-demo"
        if command[:2] == ("node", str(service._remote_query_script_path)):
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "file": "qmd://thoughts/example.md",
                            "title": "Example",
                            "score": 0.91,
                            "snippet": "remote semantic hit",
                        }
                    ]
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected fallback command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    backend, candidates = service._recall_candidates("remote query", 5)

    assert backend == "query"
    assert candidates[0]["file"] == "qmd://thoughts/example.md"
    assert runs == [
        (
            "node",
            str(service._remote_query_script_path),
            "remote query",
            "--limit",
            "20",
        )
    ]


def test_qmd_refresh_runs_project_local_embed_helper(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)
    runs: list[tuple[str, bool]] = []
    state_writes: list[str] = []
    monkeypatch.setattr(service, "_embed_force_required", lambda desired_model: False)
    monkeypatch.setattr(
        service,
        "_write_embed_model_state",
        lambda model_uri: state_writes.append(model_uri),
    )

    def fake_run(*args: str) -> subprocess.CompletedProcess[str]:
        runs.append((args[0], False))
        return subprocess.CompletedProcess(args=["qmd", *args], returncode=0)

    def fake_embed_process(*, force: bool) -> subprocess.CompletedProcess[str]:
        runs.append(("embed-helper", force))
        return subprocess.CompletedProcess(
            args=["node", "qmd_embed_full.mjs"], returncode=0
        )

    monkeypatch.setattr(service, "run", fake_run)
    monkeypatch.setattr(service, "_run_embed_process", fake_embed_process)

    result = service.refresh(with_embeddings=True)

    assert result["updated"] is True
    assert result["embedded"] is True
    assert runs == [("update", False), ("embed-helper", False)]
    assert state_writes == [service.build_env()["QMD_EMBED_MODEL"]]


def test_qmd_cleanup_holds_exclusive_lock(tmp_path: Path, monkeypatch) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)
    events: list[str] = []

    class FakeLock:
        def __enter__(self):  # noqa: ANN204
            events.append("lock")

        def __exit__(self, *args):  # noqa: ANN002, ANN204
            events.append("unlock")

    def fake_run(*args: str) -> subprocess.CompletedProcess[str]:
        events.append("run:" + " ".join(args))
        return subprocess.CompletedProcess(args=["qmd", *args], returncode=0)

    monkeypatch.setattr(service, "_exclusive_lock", FakeLock)
    monkeypatch.setattr(service, "run", fake_run)

    result = service.cleanup()

    assert result.returncode == 0
    assert events == ["lock", "run:cleanup", "unlock"]


def test_qmd_refresh_after_searchable_write_uses_embed_path(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)
    calls: list[bool] = []

    def fake_refresh(*, with_embeddings: bool):  # noqa: ANN001
        calls.append(with_embeddings)
        return {
            "available": True,
            "updated": True,
            "embedded": True,
            "errors": [],
        }

    monkeypatch.setattr(service, "refresh", fake_refresh)

    result = service.refresh_after_searchable_write()

    assert calls == [True]
    assert result["embedded"] is True


def test_qmd_refresh_returns_embed_error_from_helper(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)

    monkeypatch.setattr(service, "_embed_force_required", lambda desired_model: False)
    monkeypatch.setattr(service, "_write_embed_model_state", lambda model_uri: None)

    def fake_run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["qmd", *args], returncode=0)

    def fake_embed_process(*, force: bool) -> subprocess.CompletedProcess[str]:
        del force
        return subprocess.CompletedProcess(
            args=["node", "qmd_embed_full.mjs"],
            returncode=1,
            stdout="",
            stderr="embed failed",
        )

    monkeypatch.setattr(service, "run", fake_run)
    monkeypatch.setattr(service, "_run_embed_process", fake_embed_process)

    result = service.refresh(with_embeddings=True)

    assert result["embedded"] is False
    assert result["errors"] == ["embed failed"]


@pytest.mark.parametrize(
    (
        "partial_stdout",
        "partial_stderr",
        "expected_stdout",
        "expected_stderr",
    ),
    [
        pytest.param(
            b"partial stdout",
            b"partial stderr",
            "partial stdout",
            "partial stderr\nqmd embed timed out after 3600s",
            id="bytes",
        ),
        pytest.param(
            "partial stdout",
            "partial stderr",
            "partial stdout",
            "partial stderr\nqmd embed timed out after 3600s",
            id="str",
        ),
        pytest.param(
            None,
            None,
            "",
            "\nqmd embed timed out after 3600s",
            id="none",
        ),
    ],
)
def test_qmd_embed_timeout_normalizes_partial_output(
    tmp_path: Path,
    monkeypatch,
    partial_stdout: bytes | str | None,
    partial_stderr: bytes | str | None,
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)

    def raise_timeout(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs["timeout"],
            output=partial_stdout,
            stderr=partial_stderr,
        )

    monkeypatch.setattr(subprocess, "run", raise_timeout)

    result = service._run_embed_process(force=False)

    assert result.returncode == 124
    assert result.stdout == expected_stdout
    assert result.stderr == expected_stderr


def test_qmd_refresh_reports_embed_timeout_without_secondary_exception(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)
    state_writes: list[str] = []

    monkeypatch.setattr(service, "_embed_force_required", lambda desired_model: False)
    monkeypatch.setattr(
        service,
        "_write_embed_model_state",
        lambda model_uri: state_writes.append(model_uri),
    )

    def fake_update(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["qmd", *args], returncode=0)

    def raise_timeout(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs["timeout"],
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr(service, "run", fake_update)
    monkeypatch.setattr(subprocess, "run", raise_timeout)

    result = service.refresh(with_embeddings=True)

    assert result == {
        "available": True,
        "updated": True,
        "embedded": False,
        "errors": ["partial stderr\nqmd embed timed out after 3600s"],
    }
    assert state_writes == []


def test_qmd_refresh_forces_first_embed_when_model_changes(
    tmp_path: Path, monkeypatch
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    service = _qmd_service(vault_path)
    runs: list[tuple[str, ...]] = []

    monkeypatch.setattr(service, "_embed_force_required", lambda desired_model: True)
    monkeypatch.setattr(service, "_write_embed_model_state", lambda model_uri: None)

    def fake_run(*args: str) -> subprocess.CompletedProcess[str]:
        runs.append(args)
        return subprocess.CompletedProcess(args=["qmd", *args], returncode=0)

    def fake_embed_process(*, force: bool) -> subprocess.CompletedProcess[str]:
        runs.append(("embed-helper", "--force" if force else ""))
        return subprocess.CompletedProcess(
            args=["node", "qmd_embed_full.mjs"], returncode=0
        )

    monkeypatch.setattr(service, "run", fake_run)
    monkeypatch.setattr(service, "_run_embed_process", fake_embed_process)

    result = service.refresh(with_embeddings=True)

    assert result["embedded"] is True
    assert runs == [("update",), ("embed-helper", "--force")]


def test_run_qmd_routes_embed_through_retrying_service(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    calls: list[bool] = []

    class FakeQmdService:
        def __init__(self, _vault_path: Path) -> None:
            pass

        def run_embed_cli(
            self, *, force: bool = False
        ) -> subprocess.CompletedProcess[str]:
            calls.append(force)
            return subprocess.CompletedProcess(
                args=["qmd", "embed"],
                returncode=0,
                stdout="embedded\n",
                stderr="",
            )

        def run(self, *args: str) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"unexpected raw qmd run: {args}")

    monkeypatch.setattr(
        run_qmd, "get_settings", lambda: SimpleNamespace(vault_path=vault_path)
    )
    monkeypatch.setattr(run_qmd, "QmdService", FakeQmdService)
    monkeypatch.setattr(sys, "argv", ["run_qmd.py", "embed", "-f"])

    exit_code = run_qmd.main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == [True]
    assert "embedded" in captured.out


def test_run_qmd_routes_cleanup_through_locked_service(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls: list[str] = []

    class FakeQmdService:
        def __init__(self, _vault_path: Path) -> None:
            pass

        def cleanup(self) -> subprocess.CompletedProcess[str]:
            calls.append("cleanup")
            return subprocess.CompletedProcess(
                args=["qmd", "cleanup"],
                returncode=0,
                stdout="cleaned\n",
                stderr="",
            )

        def run(self, *args: str) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"unexpected raw qmd run: {args}")

    monkeypatch.setattr(
        run_qmd, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )
    monkeypatch.setattr(run_qmd, "QmdService", FakeQmdService)
    monkeypatch.setattr(sys, "argv", ["run_qmd.py", "cleanup"])

    exit_code = run_qmd.main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == ["cleanup"]
    assert "cleaned" in captured.out
