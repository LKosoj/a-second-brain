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
