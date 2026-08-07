import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from d_brain import run_compiled_pass
from d_brain.services.compiled_briefings import CompiledSourceStateError


def _patch_settings(monkeypatch, vault: Path) -> None:
    monkeypatch.setattr(
        run_compiled_pass,
        "get_settings",
        lambda: SimpleNamespace(
            vault_path=vault, content_language="ru", ai_cli="claude"
        ),
    )


class FakeService:
    """Records every call instead of doing real queue/model/file work, like
    ``test_run_compiled_daily_full_pass.py``'s ``FakeService`` for the
    sibling CLI."""

    created: list["FakeService"] = []

    def __init__(self, vault_path, content_language="ru", ai_cli=None) -> None:  # noqa: ANN001
        self.vault_path = Path(vault_path)
        self.content_language = content_language
        self.ai_cli = ai_cli
        self.calls: list[tuple[str, dict]] = []
        self.enqueue_errors: dict[str, list[str]] = {}
        self.nightly_result: dict = {"queued_drained": 1, "errors": []}
        self.rollback_result: dict = {
            "restored": [],
            "skipped": [],
            "manifest_found": True,
        }
        FakeService.created.append(self)

    def enqueue_refresh(self, *, source_path, source_excerpt=""):  # noqa: ANN001
        rel = str(source_path)
        self.calls.append(
            ("enqueue_refresh", {"source_path": rel, "source_excerpt": source_excerpt})
        )
        errors = self.enqueue_errors.get(rel, [])
        return {"queued": not errors, "errors": errors}

    def run_nightly_maintenance(self):
        self.calls.append(("run_nightly_maintenance", {}))
        journal_path = self.vault_path / ".session" / "compile-enrich.json"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(
            json.dumps({"pass_id": "abc123", "status": "ok"}), encoding="utf-8"
        )
        return dict(self.nightly_result)

    def rollback_compile_enrich_pass(self, pass_id):  # noqa: ANN001
        self.calls.append(("rollback_compile_enrich_pass", {"pass_id": pass_id}))
        return dict(self.rollback_result)


@pytest.fixture(autouse=True)
def _reset_fake_service():
    FakeService.created = []
    yield
    FakeService.created = []


# --- regular pass: enqueue then run_nightly_maintenance ------------------


def test_pass_enqueues_date_and_source_then_runs_nightly_maintenance(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-08-05.md").write_text("дневная запись\n", encoding="utf-8")
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(run_compiled_pass, "CompiledBriefingService", FakeService)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--date",
            "2026-08-05",
            "--source",
            "thoughts/idea.md",
        ],
    )

    exit_code = run_compiled_pass.main()

    assert exit_code == 0
    service = FakeService.created[0]
    assert service.calls == [
        (
            "enqueue_refresh",
            {
                "source_path": "daily/2026-08-05.md",
                "source_excerpt": "дневная запись\n",
            },
        ),
        ("enqueue_refresh", {"source_path": "thoughts/idea.md", "source_excerpt": ""}),
        ("run_nightly_maintenance", {}),
    ]


def test_pass_deduplicates_same_source_from_date_and_explicit_source(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(run_compiled_pass, "CompiledBriefingService", FakeService)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--date", "2026-08-05", "--source", "daily/2026-08-05.md"],
    )

    exit_code = run_compiled_pass.main()

    assert exit_code == 0
    service = FakeService.created[0]
    enqueue_calls = [call for call in service.calls if call[0] == "enqueue_refresh"]
    assert len(enqueue_calls) == 1


def test_pass_reports_pass_id_from_journal_and_enqueue_errors(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A source the operator named explicitly that never reached the queue
    was never part of the pass, so it exits non-zero -- same principle as a
    ``--rollback`` whose pass id has no snapshot (see ``_run_rollback``).
    Exit 0 would read as "your source was processed" to anything driving
    this CLI."""
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(run_compiled_pass, "CompiledBriefingService", FakeService)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.enqueue_errors["compiled/topics/aurora.md"] = ["unsupported-path"]
        return service

    monkeypatch.setattr(run_compiled_pass, "CompiledBriefingService", _make_service)
    monkeypatch.setattr(
        sys, "argv", ["prog", "--source", "compiled/topics/aurora.md"]
    )

    exit_code = run_compiled_pass.main()

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["pass_id"] == "abc123"
    assert out["enqueue_errors"] == ["unsupported-path"]


def test_pass_exit_code_reflects_nightly_maintenance_errors(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.nightly_result = {"queued_drained": 0, "errors": ["boom"]}
        return service

    monkeypatch.setattr(run_compiled_pass, "CompiledBriefingService", _make_service)
    monkeypatch.setattr(sys, "argv", ["prog", "--source", "thoughts/idea.md"])

    exit_code = run_compiled_pass.main()

    assert exit_code == 1


# --- rollback --------------------------------------------------------


def test_rollback_calls_service_directly_and_reports_pass_id(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.rollback_result = {
            "restored": ["compiled/topics/aurora.md"],
            "skipped": ["compiled/topics/changed-since.md"],
        }
        return service

    monkeypatch.setattr(run_compiled_pass, "CompiledBriefingService", _make_service)
    monkeypatch.setattr(sys, "argv", ["prog", "--rollback", "pass-xyz"])

    exit_code = run_compiled_pass.main()

    assert exit_code == 0
    service = FakeService.created[0]
    assert service.calls == [("rollback_compile_enrich_pass", {"pass_id": "pass-xyz"})]
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "pass_id": "pass-xyz",
        "restored": ["compiled/topics/aurora.md"],
        "skipped": ["compiled/topics/changed-since.md"],
    }


def test_rollback_unknown_pass_id_is_a_clear_error_not_a_quiet_success(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Задача N дефект 1: when the service reports ``manifest_found: False``
    (no snapshot on disk for this ``pass_id`` at all -- a made-up id, a
    typo, or an already-rolled-back/cleaned-up pass), the CLI must exit
    non-zero with a clear ``errors`` entry instead of the previous silent
    ``{"restored": [], "skipped": []}`` success."""
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.rollback_result = {
            "restored": [],
            "skipped": [],
            "manifest_found": False,
        }
        return service

    monkeypatch.setattr(run_compiled_pass, "CompiledBriefingService", _make_service)
    monkeypatch.setattr(
        sys, "argv", ["prog", "--rollback", "totally-bogus-id-1234"]
    )

    exit_code = run_compiled_pass.main()

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["restored"] == []
    assert out["skipped"] == []
    assert out["errors"]
    assert "totally-bogus-id-1234" in out["errors"][0]


def test_rollback_found_manifest_with_nothing_to_restore_stays_success(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The other half of the same distinction: a known pass whose manifest
    exists but genuinely restored/skipped nothing stays exit 0 with no
    ``errors`` entry -- only a missing manifest is an error."""
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)
        service.rollback_result = {
            "restored": [],
            "skipped": [],
            "manifest_found": True,
        }
        return service

    monkeypatch.setattr(run_compiled_pass, "CompiledBriefingService", _make_service)
    monkeypatch.setattr(sys, "argv", ["prog", "--rollback", "pass-empty"])

    exit_code = run_compiled_pass.main()

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert "errors" not in out


def test_rollback_rejects_combination_with_date_or_source(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        sys, "argv", ["prog", "--rollback", "pass-xyz", "--date", "2026-08-05"]
    )

    with pytest.raises(SystemExit) as exc_info:
        run_compiled_pass.main()

    assert exc_info.value.code == 2


def test_missing_date_and_source_without_rollback_is_an_error(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc_info:
        run_compiled_pass.main()

    assert exc_info.value.code == 2


# --- dry run: preview only, real (unmocked) CompiledBriefingService ------


def test_dry_run_previews_sources_and_queue_without_enqueueing(
    tmp_path: Path, monkeypatch, capsys, write_vault_manifest
) -> None:
    """Uses the real ``CompiledBriefingService`` (not ``FakeService``):
    ``_normalize_rel_path``/``_load_queue`` are pure/read-only, so this
    exercises the same normalization ``enqueue_refresh`` would actually
    apply instead of a hand-rolled approximation of it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    write_vault_manifest(vault)
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--date",
            "2026-08-05",
            "--source",
            "compiled/topics/aurora.md",
            "--dry-run",
        ],
    )

    exit_code = run_compiled_pass.main()

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["would_enqueue"] == [
        {
            "source": "daily/2026-08-05.md",
            "normalized": "daily/2026-08-05.md",
            "would_enqueue": True,
        },
        {
            "source": "compiled/topics/aurora.md",
            "normalized": "compiled/topics/aurora.md",
            "would_enqueue": False,
        },
    ]
    assert out["current_queue"] == []
    assert not (vault / ".compiled").exists()
    assert not (vault / "daily").exists()


def test_dry_run_shows_current_queue_contents(
    tmp_path: Path, monkeypatch, capsys, write_vault_manifest
) -> None:
    vault = tmp_path / "vault"
    write_vault_manifest(vault)
    queue_path = vault / ".compiled" / "queue.json"
    queue_path.parent.mkdir(parents=True)
    queue_path.write_text(
        json.dumps(
            [
                {
                    "source_path": "daily/2026-08-04.md",
                    "state": "pending",
                    "due_at": 123.0,
                    "attempts": 0,
                }
            ]
        ),
        encoding="utf-8",
    )
    _patch_settings(monkeypatch, vault)
    monkeypatch.setattr(
        sys, "argv", ["prog", "--source", "thoughts/idea.md", "--dry-run"]
    )

    exit_code = run_compiled_pass.main()

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["current_queue"] == [
        {
            "source_path": "daily/2026-08-04.md",
            "state": "pending",
            "due_at": 123.0,
            "attempts": 0,
        }
    ]


def test_source_excerpt_survives_one_bad_byte(tmp_path: Path) -> None:
    """A daily file with one broken byte must not take the whole pass down.

    The guard here only caught ``OSError``, and ``UnicodeDecodeError`` is
    not one -- so the exception escaped and the run died instead of
    compiling with whatever text could be read.
    """
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-08-06.md").write_bytes(
        b"## 09:30 [voice]\n\xffnotes\n"
    )

    excerpt = run_compiled_pass._read_source_excerpt(vault, "daily/2026-08-06.md")

    assert "notes" in excerpt
    assert "�" in excerpt


def test_pass_corrupt_source_state_yields_clean_message_not_a_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """``run_nightly_maintenance`` raises ``CompiledSourceStateError`` on a
    corrupt ``.compiled/source-state.json``. ``run_compiled_maintenance.py``
    already turns that into this CLI family's ``{"errors": [...]}`` contract
    (its own "Code review Finding 2"); without the same handling here the
    manual pass crashed with a raw traceback and wrote nothing valid to
    stdout -- worse than a quiet failure for anything parsing that
    contract."""
    vault = tmp_path / "vault"
    _patch_settings(monkeypatch, vault)

    def _make_service(*args, **kwargs):  # noqa: ANN002, ANN003
        service = FakeService(*args, **kwargs)

        def raising_nightly():
            raise CompiledSourceStateError("unsupported compiled source state version")

        service.run_nightly_maintenance = raising_nightly  # type: ignore[method-assign]
        return service

    monkeypatch.setattr(run_compiled_pass, "CompiledBriefingService", _make_service)
    monkeypatch.setattr(sys, "argv", ["prog", "--source", "thoughts/idea.md"])

    exit_code = run_compiled_pass.main()

    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"]
    assert "источник" in out["errors"][0]
