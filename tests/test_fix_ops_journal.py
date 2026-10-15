"""Tests for the vault ops journal (аудит 2026-09-03, пункт 20).

Covers ``.session/ops.jsonl`` written by ``write_validated_vault_markdown``,
the ``.session/ops-snapshots`` backups it creates before overwriting an
existing file, ``recover_ops_journal`` (the engine behind ``d_brain
recover``), and ``graph-builder/scripts/add_links.py`` after its switch to
the same single write point.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _paths import SKILLS_TEMPLATE_ROOT

from d_brain.cli import main as cli_main
from d_brain.services.frontmatter import (
    ensure_run_identity,
    patch_validated_vault_frontmatter,
    prune_ops_journal,
    recover_ops_journal,
    write_import_vault_markdown,
    write_validated_vault_markdown,
)


def _journal_entries(vault: Path) -> list[dict[str, Any]]:
    path = vault / ".session" / "ops.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_graph_link_builder():  # noqa: ANN201
    script_path = SKILLS_TEMPLATE_ROOT / "graph-builder/scripts/add_links.py"
    spec = importlib.util.spec_from_file_location(
        "graph_link_builder_ops_journal", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_write_appends_ops_journal_entry_with_correct_sha(tmp_path, monkeypatch):
    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-1")
    monkeypatch.setenv("D_BRAIN_WORKFLOW", "daily-processing")
    vault = tmp_path / "vault"
    vault.mkdir()
    content = b"---\ntype: note\n---\n# Hello\n"

    write_validated_vault_markdown(vault, vault / "note.md", content)

    entries = _journal_entries(vault)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["run_id"] == "run-1"
    assert entry["workflow"] == "daily-processing"
    assert entry["path"] == "note.md"
    assert entry["sha_before"] is None
    assert entry["sha_after"] == hashlib.sha256(content).hexdigest()
    assert entry["backup"] is None
    datetime.fromisoformat(entry["ts"])  # ISO-parseable UTC timestamp


def test_rewrite_existing_file_creates_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("D_BRAIN_RUN_ID", raising=False)
    monkeypatch.delenv("D_BRAIN_WORKFLOW", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    old_content = b"---\ntype: note\n---\n# Old\n"
    new_content = b"---\ntype: note\n---\n# New\n"

    write_validated_vault_markdown(vault, vault / "note.md", old_content)
    write_validated_vault_markdown(vault, vault / "note.md", new_content)

    entries = _journal_entries(vault)
    assert len(entries) == 2
    sha_before = hashlib.sha256(old_content).hexdigest()
    second = entries[1]
    assert second["sha_before"] == sha_before
    assert second["sha_after"] == hashlib.sha256(new_content).hexdigest()
    assert second["backup"] == f".session/ops-snapshots/{sha_before[:16]}.md.bak"
    assert (vault / second["backup"]).read_bytes() == old_content


def test_recover_restores_modified_file_and_deletes_created_file(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    old_content = b"---\ntype: note\n---\n# Old\n"
    new_content = b"---\ntype: note\n---\n# New\n"
    created_content = b"---\ntype: note\n---\n# Created\n"

    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-before")
    write_validated_vault_markdown(vault, vault / "existing.md", old_content)

    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-A")
    write_validated_vault_markdown(vault, vault / "existing.md", new_content)
    write_validated_vault_markdown(vault, vault / "created.md", created_content)

    result = recover_ops_journal(vault, "run-A")

    assert result["restored"] == ["existing.md"]
    assert result["removed"] == ["created.md"]
    assert result["skipped"] == []
    assert (vault / "existing.md").read_bytes() == old_content
    assert not (vault / "created.md").exists()

    recover_entries = [
        entry for entry in _journal_entries(vault) if entry["workflow"] == "recover"
    ]
    assert len(recover_entries) == 2
    assert {entry["path"] for entry in recover_entries} == {
        "existing.md",
        "created.md",
    }


def test_recover_skips_file_changed_after_the_recorded_write(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    content_v1 = b"---\ntype: note\n---\n# V1\n"
    content_v2 = b"---\ntype: note\n---\n# V2\n"

    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-B")
    write_validated_vault_markdown(vault, vault / "note.md", content_v1)

    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-C")
    write_validated_vault_markdown(vault, vault / "note.md", content_v2)

    result = recover_ops_journal(vault, "run-B")

    assert result["restored"] == []
    assert result["skipped"] == ["note.md"]
    assert (vault / "note.md").read_bytes() == content_v2


def test_broken_session_directory_does_not_block_markdown_write(tmp_path, caplog):
    vault = tmp_path / "vault"
    vault.mkdir()
    # ``.session`` exists as a plain file instead of a directory, so the
    # journal's own ``mkdir(parents=True, exist_ok=True)`` must fail.
    (vault / ".session").write_text("not a directory", encoding="utf-8")
    content = b"---\ntype: note\n---\n# Still Written\n"

    with caplog.at_level(logging.WARNING):
        write_validated_vault_markdown(vault, vault / "note.md", content)

    assert (vault / "note.md").read_bytes() == content
    assert "ops journal" in caplog.text.lower()


def test_add_links_apply_link_writes_through_ops_journal(tmp_path):
    """``apply_link`` now writes via ``write_validated_vault_markdown``; the
    resulting file content must match the pre-migration behaviour, and the
    write must show up in the ops journal like any other vault write."""
    builder = _load_graph_link_builder()
    vault = tmp_path / "vault"
    vault.mkdir()
    page = vault / "aurora.md"
    page.write_text("# Aurora\n\nSome text.\n", encoding="utf-8")

    added = builder.apply_link(vault, page, "topics/quantum-widgets", dry_run=False)

    assert added is True
    content = page.read_text(encoding="utf-8")
    assert "[[topics/quantum-widgets]]" in content

    entries = _journal_entries(vault)
    assert len(entries) == 1
    assert entries[0]["path"] == "aurora.md"
    assert entries[0]["sha_after"] == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


def test_recover_is_idempotent_on_repeated_call(tmp_path, monkeypatch):
    """A second ``recover`` for the same run_id must not undo its own rollback.

    The rollback itself is journaled with ``run_id=f"recover:{run_id}"``
    (аудит 2026-09-03, п.20, review item 1), so the entry-selection filter
    (``entry["run_id"] == run_id``) naturally excludes it on a later call.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    old_content = b"---\ntype: note\n---\n# Old\n"
    new_content = b"---\ntype: note\n---\n# New\n"

    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-before")
    write_validated_vault_markdown(vault, vault / "existing.md", old_content)

    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-A")
    write_validated_vault_markdown(vault, vault / "existing.md", new_content)

    first = recover_ops_journal(vault, "run-A")
    assert first["restored"] == ["existing.md"]
    assert (vault / "existing.md").read_bytes() == old_content

    recover_entries_after_first = [
        entry for entry in _journal_entries(vault) if entry["workflow"] == "recover"
    ]
    assert len(recover_entries_after_first) == 1

    second = recover_ops_journal(vault, "run-A")

    assert second["restored"] == []
    assert (vault / "existing.md").read_bytes() == old_content

    recover_entries_after_second = [
        entry for entry in _journal_entries(vault) if entry["workflow"] == "recover"
    ]
    assert len(recover_entries_after_second) == len(recover_entries_after_first)


def test_recover_skips_journal_entry_with_path_traversal(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    session_dir = vault / ".session"
    session_dir.mkdir()
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "run_id": "run-D",
        "workflow": "daily-processing",
        "path": "../outside.md",
        "sha_before": None,
        "sha_after": "deadbeef",
        "backup": None,
    }
    (session_dir / "ops.jsonl").write_text(
        json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = recover_ops_journal(vault, "run-D")

    assert result["restored"] == []
    assert result["skipped"] == ["../outside.md"]
    assert not (tmp_path / "outside.md").exists()


def test_prune_ops_journal_drops_old_entries_and_snapshots(tmp_path):
    vault = tmp_path / "vault"
    session_dir = vault / ".session"
    snapshots_dir = session_dir / "ops-snapshots"
    snapshots_dir.mkdir(parents=True)

    now = datetime(2026, 9, 3, tzinfo=UTC)
    old_entry = {
        "ts": (now - timedelta(days=40)).isoformat(),
        "run_id": "run-old",
        "workflow": "daily-processing",
        "path": "old.md",
        "sha_before": None,
        "sha_after": "abc",
        "backup": None,
    }
    recent_entry = {
        "ts": (now - timedelta(days=1)).isoformat(),
        "run_id": "run-recent",
        "workflow": "daily-processing",
        "path": "recent.md",
        "sha_before": None,
        "sha_after": "def",
        "backup": None,
    }
    (session_dir / "ops.jsonl").write_text(
        json.dumps(old_entry, ensure_ascii=False)
        + "\n"
        + json.dumps(recent_entry, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    old_snapshot = snapshots_dir / "old-snapshot.md.bak"
    old_snapshot.write_bytes(b"old")
    recent_snapshot = snapshots_dir / "recent-snapshot.md.bak"
    recent_snapshot.write_bytes(b"recent")
    old_mtime = (now - timedelta(days=40)).timestamp()
    recent_mtime = (now - timedelta(days=1)).timestamp()
    os.utime(old_snapshot, (old_mtime, old_mtime))
    os.utime(recent_snapshot, (recent_mtime, recent_mtime))

    result = prune_ops_journal(vault, max_age_days=30, now=now)

    assert result == {"dropped_entries": 1, "dropped_snapshots": 1}
    remaining = _journal_entries(vault)
    assert len(remaining) == 1
    assert remaining[0]["path"] == "recent.md"
    assert not old_snapshot.exists()
    assert recent_snapshot.exists()


def test_prune_ops_journal_never_drops_a_snapshot_a_kept_entry_still_references(
    tmp_path,
):
    """Snapshots are content-addressed by ``sha_before`` and can be shared.

    An old entry A and a fresh entry B can point at the same snapshot file
    (same ``sha_before``, hence same ``<hash16>.md.bak`` name) if a later
    write happened to reproduce an earlier hash. Pruning A's now-old entry
    must not delete a snapshot that B (kept) still references, regardless of
    the snapshot file's own mtime (аудит 2026-09-03, п.20, round-2 error 1).
    """
    vault = tmp_path / "vault"
    session_dir = vault / ".session"
    snapshots_dir = session_dir / "ops-snapshots"
    snapshots_dir.mkdir(parents=True)

    now = datetime(2026, 9, 3, tzinfo=UTC)
    shared_backup = ".session/ops-snapshots/shared1234567890.md.bak"
    old_entry = {
        "ts": (now - timedelta(days=40)).isoformat(),
        "run_id": "run-old",
        "workflow": "daily-processing",
        "path": "old.md",
        "sha_before": "shared1234567890",
        "sha_after": "aaa",
        "backup": shared_backup,
    }
    recent_entry = {
        "ts": (now - timedelta(days=1)).isoformat(),
        "run_id": "run-recent",
        "workflow": "daily-processing",
        "path": "recent.md",
        "sha_before": "shared1234567890",
        "sha_after": "bbb",
        "backup": shared_backup,
    }
    (session_dir / "ops.jsonl").write_text(
        json.dumps(old_entry, ensure_ascii=False)
        + "\n"
        + json.dumps(recent_entry, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    shared_snapshot = vault / shared_backup
    shared_snapshot.write_bytes(b"shared snapshot content")
    old_mtime = (now - timedelta(days=40)).timestamp()
    os.utime(shared_snapshot, (old_mtime, old_mtime))

    result = prune_ops_journal(vault, max_age_days=30, now=now)

    assert result == {"dropped_entries": 1, "dropped_snapshots": 0}
    remaining = _journal_entries(vault)
    assert len(remaining) == 1
    assert remaining[0]["path"] == "recent.md"
    assert shared_snapshot.exists()
    assert shared_snapshot.read_bytes() == b"shared snapshot content"


def test_prune_ops_journal_tolerates_naive_timestamp_without_crashing(tmp_path):
    """A naive (tz-less) ``ts`` compared against the aware ``cutoff`` used to
    raise an uncaught ``TypeError`` and crash the whole prune (round-2
    warning 5). It must instead be treated like any other unparseable
    timestamp: kept, not counted as dropped."""
    vault = tmp_path / "vault"
    session_dir = vault / ".session"
    session_dir.mkdir(parents=True)
    naive_entry = {
        "ts": "2020-01-01T00:00:00",
        "run_id": "run-naive",
        "workflow": "daily-processing",
        "path": "naive.md",
        "sha_before": None,
        "sha_after": "abc",
        "backup": None,
    }
    (session_dir / "ops.jsonl").write_text(
        json.dumps(naive_entry, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = prune_ops_journal(
        vault, max_age_days=30, now=datetime(2026, 9, 3, tzinfo=UTC)
    )

    assert result == {"dropped_entries": 0, "dropped_snapshots": 0}
    remaining = _journal_entries(vault)
    assert len(remaining) == 1
    assert remaining[0]["path"] == "naive.md"


def test_prune_ops_journal_rejects_negative_max_age_days(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError):
        prune_ops_journal(vault, max_age_days=-1)


def test_ops_prune_cli_rejects_negative_days(tmp_path, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()

    exit_code = cli_main(["ops-prune", "--vault", str(vault), "--days", "-1"])

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "days" in (captured.out + captured.err).lower()


def test_recover_rejects_symlinked_directory_component(tmp_path):
    """A symlinked *intermediate* directory component (not the final path
    segment) used to slip past the old ``target.is_symlink()`` check, which
    only looked at the last component (round-2 error 2). ``vault/linked``
    points outside the vault; the journal entry's path walks through it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret_content = b"---\ntype: note\n---\n# Secret\n"
    secret.write_bytes(secret_content)
    (vault / "linked").symlink_to(outside, target_is_directory=True)

    session_dir = vault / ".session"
    session_dir.mkdir()
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "run_id": "run-E",
        "workflow": "daily-processing",
        "path": "linked/secret.md",
        "sha_before": None,
        "sha_after": hashlib.sha256(secret_content).hexdigest(),
        "backup": None,
    }
    (session_dir / "ops.jsonl").write_text(
        json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = recover_ops_journal(vault, "run-E")

    assert result["restored"] == []
    assert result["removed"] == []
    assert result["skipped"] == ["linked/secret.md"]
    assert secret.read_bytes() == secret_content


def test_recover_result_distinguishes_removed_from_restored(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    old_content = b"---\ntype: note\n---\n# Old\n"
    new_content = b"---\ntype: note\n---\n# New\n"
    created_content = b"---\ntype: note\n---\n# Created\n"

    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-before")
    write_validated_vault_markdown(vault, vault / "existing.md", old_content)

    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-A")
    write_validated_vault_markdown(vault, vault / "existing.md", new_content)
    write_validated_vault_markdown(vault, vault / "created.md", created_content)

    result = recover_ops_journal(vault, "run-A")

    assert result["restored"] == ["existing.md"]
    assert result["removed"] == ["created.md"]


# --- round-4 warning 1: production entrypoints must set run identity ------


def test_ensure_run_identity_sets_env_vars_used_by_ops_journal(tmp_path, monkeypatch):
    """``frontmatter.ensure_run_identity`` (round-4 warning 1): the
    env vars it sets via ``os.environ.setdefault`` are exactly what
    ``write_validated_vault_markdown`` reads into the ops journal, so a
    scheduled run's writes become selectable by ``a-second-brain recover``.
    """
    monkeypatch.delenv("D_BRAIN_RUN_ID", raising=False)
    monkeypatch.delenv("D_BRAIN_WORKFLOW", raising=False)

    run_id = ensure_run_identity(
        "daily-process", "daily-process", now=datetime(2026, 9, 3, 21, 0, 0)
    )

    assert run_id == "daily-process-20260903-210000"
    assert os.environ["D_BRAIN_RUN_ID"] == run_id
    assert os.environ["D_BRAIN_WORKFLOW"] == "daily-process"

    vault = tmp_path / "vault"
    vault.mkdir()
    write_validated_vault_markdown(
        vault, vault / "note.md", b"---\ntype: note\n---\n# Hi\n"
    )

    entries = _journal_entries(vault)
    assert entries[-1]["run_id"] == run_id
    assert entries[-1]["workflow"] == "daily-process"


def test_ensure_run_identity_does_not_override_preset_env(monkeypatch):
    """``setdefault`` semantics: a run_id already set by an orchestrating
    wrapper (or a test) must win over the auto-generated one -- checked here
    on a second call with a different prefix."""
    monkeypatch.setenv("D_BRAIN_RUN_ID", "external-run-id")
    monkeypatch.delenv("D_BRAIN_WORKFLOW", raising=False)

    run_id = ensure_run_identity("compiled-pass", "compiled-pass")

    assert run_id == "external-run-id"
    assert os.environ["D_BRAIN_WORKFLOW"] == "compiled-pass"


# --- round-4 warning 2: CLI must not traceback on a missing --vault -------


def test_recover_cli_missing_vault_prints_friendly_error(tmp_path, capsys):
    missing_vault = tmp_path / "does-not-exist"

    exit_code = cli_main(["recover", "run-X", "--vault", str(missing_vault)])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


def test_ops_prune_cli_missing_vault_prints_friendly_error(tmp_path, capsys):
    missing_vault = tmp_path / "does-not-exist"

    exit_code = cli_main(["ops-prune", "--vault", str(missing_vault)])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


# --- round-4 warning 3: patch/import must also land in the ops journal ----


def test_patch_validated_vault_frontmatter_appends_ops_journal_entry(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("D_BRAIN_RUN_ID", raising=False)
    monkeypatch.setenv("D_BRAIN_WORKFLOW", "decisions-queue")
    vault = tmp_path / "vault"
    vault.mkdir()
    content = b"---\ntype: note\nstatus: draft\n---\n# Body\n"
    write_validated_vault_markdown(vault, vault / "note.md", content)

    patch_validated_vault_frontmatter(vault, vault / "note.md", {"status": "done"})

    entries = _journal_entries(vault)
    assert len(entries) == 2
    patch_entry = entries[-1]
    assert patch_entry["path"] == "note.md"
    assert patch_entry["workflow"] == "decisions-queue"
    assert patch_entry["sha_before"] == hashlib.sha256(content).hexdigest()
    patched_bytes = (vault / "note.md").read_bytes()
    assert patch_entry["sha_after"] == hashlib.sha256(patched_bytes).hexdigest()
    assert patch_entry["backup"] is not None
    assert (vault / patch_entry["backup"]).read_bytes() == content


def test_patch_validated_vault_frontmatter_is_recoverable(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    content = b"---\ntype: note\nstatus: draft\n---\n# Body\n"
    write_validated_vault_markdown(vault, vault / "note.md", content)

    monkeypatch.setenv("D_BRAIN_RUN_ID", "patch-run")
    patch_validated_vault_frontmatter(vault, vault / "note.md", {"status": "done"})
    assert b'status: "done"' in (vault / "note.md").read_bytes()

    result = recover_ops_journal(vault, "patch-run")

    assert result["restored"] == ["note.md"]
    assert (vault / "note.md").read_bytes() == content


def test_write_import_vault_markdown_appends_ops_journal_entry_with_no_backup(
    tmp_path, monkeypatch
):
    """``write_import_vault_markdown`` delegates to
    ``write_validated_vault_markdown`` and was already covered by the ops
    journal before round 4 -- this pins that down with a direct test rather
    than leaving it as an unverified assumption."""
    monkeypatch.setenv("D_BRAIN_RUN_ID", "import-run")
    monkeypatch.setenv("D_BRAIN_WORKFLOW", "telegram-import")
    vault = tmp_path / "vault"
    vault.mkdir()
    content = b"---\ntype: import\n---\n# Imported note\n"

    write_import_vault_markdown(
        vault,
        vault / "imported.md",
        content,
        manifest=None,  # type: ignore[arg-type]
    )

    entries = _journal_entries(vault)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["path"] == "imported.md"
    assert entry["run_id"] == "import-run"
    assert entry["workflow"] == "telegram-import"
    assert entry["sha_before"] is None
    assert entry["backup"] is None


# --- round-4 note 5: a malicious `backup` field must not escape the vault -


def test_recover_skips_malicious_backup_path(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    session_dir = vault / ".session"
    session_dir.mkdir()
    content = b"---\ntype: note\n---\n# Current\n"
    (vault / "existing.md").write_bytes(content)
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "run_id": "run-E",
        "workflow": "daily-processing",
        "path": "existing.md",
        "sha_before": "deadbeef" * 8,
        "sha_after": hashlib.sha256(content).hexdigest(),
        "backup": "../../etc/passwd",
    }
    (session_dir / "ops.jsonl").write_text(
        json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = recover_ops_journal(vault, "run-E")

    assert result["restored"] == []
    assert result["skipped"] == ["existing.md"]
    assert (vault / "existing.md").read_bytes() == content



def test_recover_reports_each_undone_entry_for_a_file_rewritten_in_one_run(
    tmp_path, monkeypatch, capsys
):
    vault = tmp_path / "vault"
    vault.mkdir()
    first = b"---\ntype: note\n---\n# First\n"
    second = b"---\ntype: note\n---\n# Second\n"
    third = b"---\ntype: note\n---\n# Third\n"

    monkeypatch.setenv("D_BRAIN_RUN_ID", "run-M")
    write_validated_vault_markdown(vault, vault / "note.md", first)
    write_validated_vault_markdown(vault, vault / "note.md", second)
    write_validated_vault_markdown(vault, vault / "note.md", third)

    exit_code = cli_main(["recover", "run-M", "--vault", str(vault)])

    assert exit_code == 0
    assert not (vault / "note.md").exists()
    out = capsys.readouterr().out.splitlines()
    # Two rewrites undone, then the creating write removed -- one line each,
    # and the summary counts match the lines above it.
    assert out[:3] == [
        "restored: note.md",
        "restored: note.md",
        "removed (created by run): note.md",
    ]
    assert out[-1] == "recover run-M: 2 restored, 1 removed, 0 skipped"
