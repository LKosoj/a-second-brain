from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import tarfile
import threading
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from _paths import TEMPLATE_ROOT

from d_brain import run_frontmatter_migration
from d_brain.manifest import load_manifest_for_vault
from d_brain.services import frontmatter_migration
from d_brain.services.frontmatter import parse_frontmatter_bytes
from d_brain.services.frontmatter_migration import (
    KNOWN_WRITER_UNITS,
    MigrationStateError,
    WriterQuiescenceError,
    _assert_writers_quiesced,
    _is_project_writer_command,
    _project_writer_processes,
    _writer_states,
    apply_migration_state,
    apply_semantic_result,
    atomic_replace_vault_markdown,
    build_migration_state,
    create_backup_gate,
    inventory_vault,
    projected_migration_summary,
    reconcile_migration_state,
    save_migration_state,
    validate_vault,
    verify_backup_gate,
    write_inventory_report,
)
from d_brain.services.vault_lock import vault_write_lock


def _inactive_writers() -> dict[str, str]:
    return {unit: "inactive" for unit in KNOWN_WRITER_UNITS}


def _no_writer_processes(_: Path) -> tuple[int, ...]:
    return ()


def _hold_vault_write_lock(
    vault_path: str,
    acquired: Any,
    release: Any,
) -> None:
    with vault_write_lock(Path(vault_path)):
        acquired.set()
        release.wait(10)


def test_quiesce_gate_fails_closed_for_bus_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        frontmatter_migration, "_systemctl_user_environment", lambda: None
    )

    states = _writer_states()

    assert set(states.values()) == {"bus-unavailable"}
    with pytest.raises(MigrationStateError, match="unverifiable"):
        _assert_writers_quiesced(states)


@pytest.mark.parametrize("unit", [KNOWN_WRITER_UNITS[0], KNOWN_WRITER_UNITS[-1]])
def test_quiesce_gate_rejects_active_service_or_timer(unit: str) -> None:
    states = _inactive_writers()
    states[unit] = "active"

    with pytest.raises(MigrationStateError, match=unit):
        _assert_writers_quiesced(states)


def test_quiesce_gate_rejects_stray_project_writer_process() -> None:
    with pytest.raises(MigrationStateError, match="pid:4242"):
        _assert_writers_quiesced(_inactive_writers(), (4242,))


def test_quiesce_gate_accepts_every_known_unit_inactive_without_writer_process() -> (
    None
):
    _assert_writers_quiesced(_inactive_writers(), ())


@pytest.mark.parametrize(
    "command",
    [
        b"python memory-engine.py touch vault/MEMORY.md",
        b"python memory-engine.py init vault",
        b"python memory-engine.py daily vault",
        b"python memory-engine.py decay vault",
        b"python memory-engine.py supersede old.md new.md",
        b"python memory-engine.py recover-supersession vault",
        b"python -m d_brain.run_process",
    ],
)
def test_proc_writer_fallback_detects_dbrain_and_mutating_memory_engine(
    command: bytes,
) -> None:
    assert _is_project_writer_command(command)


def test_proc_writer_fallback_ignores_read_only_memory_engine_and_migration() -> None:
    assert not _is_project_writer_command(b"python memory-engine.py status vault")
    assert not _is_project_writer_command(
        b"python -m d_brain.run_frontmatter_migration"
    )
    assert not _is_project_writer_command(
        b"python memory-engine.py decay --dry-run vault"
    )


def test_proc_writer_fallback_detects_representative_project_process(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    vault = project / "vault"
    vault.mkdir(parents=True)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
            "memory-engine.py",
            "touch",
        ],
        cwd=project,
    )
    try:
        assert process.pid in _project_writer_processes(project)
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.fixture
def migration_vault(tmp_path: Path, write_vault_manifest):  # noqa: ANN201
    vault = tmp_path / "vault"
    for directory in ("daily", "thoughts", "imports", "compiled", ".locks"):
        (vault / directory).mkdir(parents=True, exist_ok=True)
    profiles = {
        "default": ["type"],
        "daily": ["type", "date"],
        "import": ["type"],
        "derived": ["type", "description"],
        "thought-card": ["type", "description", "tags", "status"],
        "reflection": ["type", "description", "tags"],
        "goal": ["type", "description"],
        "index": ["type", "description"],
        "flat-context": ["type", "description"],
        "template": ["type", "description"],
        "technical": ["type"],
        "epistemic": ["type", "epistemic_state"],
        "home": ["type", "description"],
    }
    write_vault_manifest(
        vault,
        overrides={
            "user_content_roots": ["vault/daily", "vault/thoughts"],
            "frontmatter_required": profiles,
        },
    )
    return vault


def _state(vault: Path) -> tuple[dict[str, object], object]:
    manifest = load_manifest_for_vault(vault)
    return build_migration_state(inventory_vault(vault, manifest)), manifest


def _prepared_gate(state: dict[str, object], tmp_path: Path) -> tuple[Path, Path]:
    return create_backup_gate(
        state,
        backup_dir=tmp_path / "backup",
        proof_path=tmp_path / "proof/quiesce.json",
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )


def _expire_proof(proof_path: Path) -> None:
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["created_at"] = "2000-01-01T00:00:00+00:00"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")


def test_inventory_records_regular_symlink_and_blocks_incomplete_coverage(
    migration_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    outside = migration_vault.parent / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (migration_vault / "daily/link.md").symlink_to(outside)
    manifest = load_manifest_for_vault(migration_vault)

    report = inventory_vault(migration_vault, manifest)

    assert report["source_count"] == report["discovered_markdown_count"] == 2
    assert report["coverage_complete"] is False
    assert report["source_state_counts"]["regular"] == 1
    assert report["source_state_counts"]["symlink"] == 1
    assert any(entry["source_state"] == "symlink" for entry in report["entries"])
    assert report["blocking_symlink_directories"] == [
        {
            "path": "daily/link.md",
            "classification": "blocking",
            "reason": "alias_not_in_infrastructure",
        }
    ]


def test_symlink_directory_with_markdown_makes_inventory_incomplete(
    migration_vault: Path, tmp_path: Path
) -> None:
    outside = migration_vault.parent / "outside"
    outside.mkdir()
    (outside / "hidden.md").write_text("# Hidden\n", encoding="utf-8")
    (migration_vault / "daily/linked").symlink_to(outside, target_is_directory=True)
    manifest = load_manifest_for_vault(migration_vault)

    report = inventory_vault(migration_vault, manifest)

    assert report["coverage_complete"] is False
    assert report["symlink_directories"] == ["daily/linked"]
    assert report["symlink_directory_count"] == 1
    assert report["safe_accounted_aliases"] == []
    assert report["blocking_symlink_directories"] == [
        {
            "path": "daily/linked",
            "classification": "blocking",
            "reason": "alias_not_in_infrastructure",
        }
    ]
    report_path = write_inventory_report(report, tmp_path / "report")
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["projected"] == {
        "available": False,
        "reason": "coverage_incomplete",
    }
    with pytest.raises(MigrationStateError, match="incomplete"):
        build_migration_state(report)


def test_inventory_accounts_for_configured_canonical_infrastructure_alias(
    migration_vault: Path, write_vault_manifest
) -> None:  # noqa: ANN001
    canonical = migration_vault / ".claude/skills"
    canonical.mkdir(parents=True)
    (canonical / "canonical.md").write_text("# Canonical\n", encoding="utf-8")
    (migration_vault / ".codex").mkdir()
    (migration_vault / ".codex/skills").symlink_to(canonical, target_is_directory=True)
    write_vault_manifest(
        migration_vault,
        overrides={
            "infrastructure": ["vault/.claude", "vault/.codex"],
        },
    )

    report = inventory_vault(migration_vault, load_manifest_for_vault(migration_vault))

    assert report["coverage_complete"] is True
    assert report["safe_accounted_aliases"] == [
        {
            "path": ".codex/skills",
            "classification": "safe_accounted_alias",
            "target": ".claude/skills",
            "infrastructure_root": "vault/.codex",
            "target_infrastructure_root": "vault/.claude",
        }
    ]
    assert report["blocking_symlink_directories"] == []
    build_migration_state(report)


def test_inventory_blocks_alias_inside_same_infrastructure_root(
    migration_vault: Path, write_vault_manifest
) -> None:  # noqa: ANN001
    private = migration_vault / ".claude/skills/private/private-skill"
    private.mkdir(parents=True)
    (private / "SKILL.md").write_text("# Private\n", encoding="utf-8")
    (migration_vault / ".claude/skills/private-skill").symlink_to(
        "private/private-skill", target_is_directory=True
    )
    write_vault_manifest(
        migration_vault,
        overrides={"infrastructure": ["vault/.claude"]},
    )

    report = inventory_vault(migration_vault, load_manifest_for_vault(migration_vault))

    assert report["coverage_complete"] is False
    assert report["safe_accounted_aliases"] == []
    assert report["blocking_symlink_directories"] == [
        {
            "path": ".claude/skills/private-skill",
            "classification": "blocking",
            "reason": "target_not_other_infrastructure",
        }
    ]


@pytest.mark.parametrize(
    ("kind", "expected_reason"),
    (
        ("external", "external_target"),
        ("dangling", "dangling_or_cyclic"),
        ("cyclic", "dangling_or_cyclic"),
        ("same_root", "target_not_other_infrastructure"),
        ("user_target", "target_not_other_infrastructure"),
    ),
)
def test_infrastructure_aliases_block_when_not_safe_and_accounted(
    migration_vault: Path,
    write_vault_manifest,
    kind: str,
    expected_reason: str,
) -> None:  # noqa: ANN001
    (migration_vault / ".claude/skills").mkdir(parents=True)
    (migration_vault / ".claude/skills/canonical.md").write_text(
        "# Canonical\n", encoding="utf-8"
    )
    (migration_vault / ".codex").mkdir()
    alias = migration_vault / ".codex/alias"
    if kind == "external":
        external = migration_vault.parent / "external"
        external.mkdir()
        alias.symlink_to(external, target_is_directory=True)
    elif kind == "dangling":
        alias.symlink_to(migration_vault / "missing", target_is_directory=True)
    elif kind == "cyclic":
        alias.symlink_to(alias, target_is_directory=True)
    elif kind == "same_root":
        same_root_target = migration_vault / ".codex/canonical"
        same_root_target.mkdir()
        (same_root_target / "canonical.md").write_text(
            "# Canonical\n", encoding="utf-8"
        )
        alias.symlink_to(same_root_target, target_is_directory=True)
    else:
        (migration_vault / "daily").mkdir(exist_ok=True)
        alias.symlink_to(migration_vault / "daily", target_is_directory=True)
    write_vault_manifest(
        migration_vault,
        overrides={
            "infrastructure": ["vault/.claude", "vault/.codex"],
        },
    )

    report = inventory_vault(migration_vault, load_manifest_for_vault(migration_vault))

    assert report["coverage_complete"] is False
    assert report["safe_accounted_aliases"] == []
    assert report["blocking_symlink_directories"][0]["reason"] == expected_reason


def test_infrastructure_alias_blocks_when_canonical_subtree_was_not_inventoried(
    migration_vault: Path, write_vault_manifest
) -> None:  # noqa: ANN001
    canonical = migration_vault / ".claude/skills"
    canonical.mkdir(parents=True)
    (canonical / "canonical.md").write_text("# Canonical\n", encoding="utf-8")
    (migration_vault / ".codex").mkdir()
    alias = migration_vault / ".codex/skills"
    alias.symlink_to(canonical, target_is_directory=True)
    write_vault_manifest(
        migration_vault,
        overrides={
            "infrastructure": ["vault/.claude", "vault/.codex"],
        },
    )

    record = frontmatter_migration._classify_symlink_directory(
        migration_vault,
        load_manifest_for_vault(migration_vault),
        ".codex/skills",
        set(),
    )

    assert record == {
        "path": ".codex/skills",
        "classification": "blocking",
        "reason": "target_not_covered",
    }


def test_mechanical_planner_honors_use_git_dates(
    migration_vault: Path, monkeypatch: pytest.MonkeyPatch, write_vault_manifest
) -> None:  # noqa: ANN001
    write_vault_manifest(migration_vault)
    note = migration_vault / "thoughts/card.md"
    note.write_text("# Day\n", encoding="utf-8")
    (migration_vault / ".memory-config.json").write_text(
        json.dumps({"use_git_dates": False}), encoding="utf-8"
    )
    monkeypatch.setattr(
        frontmatter_migration,
        "_git_dates",
        lambda *_args: pytest.fail("Git must be skipped when use_git_dates=false"),
    )
    state = build_migration_state(
        inventory_vault(migration_vault, load_manifest_for_vault(migration_vault))
    )
    assert state["entries"][0]["mechanical_provenance"]["created"]["source"] == (
        "filesystem-mtime"
    )
    assert (
        state["entries"][0]["mechanical_provenance"]["last_accessed"]["source"]
        == "memory-engine:filesystem-mtime"
    )

    (migration_vault / ".memory-config.json").write_text(
        json.dumps({"use_git_dates": True}), encoding="utf-8"
    )
    monkeypatch.setattr(
        frontmatter_migration,
        "_git_dates",
        lambda *_args: (date(2020, 1, 2), date(2021, 3, 4)),
    )
    state = build_migration_state(
        inventory_vault(migration_vault, load_manifest_for_vault(migration_vault))
    )
    provenance = state["entries"][0]["mechanical_provenance"]
    assert provenance["created"]["source"] == "git-earliest"
    assert provenance["updated"]["source"] == "git-latest"


def test_atomic_replace_rejects_target_swap_before_replace(
    migration_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    outside = migration_vault.parent / "outside.md"
    note.write_text("# Original\n", encoding="utf-8")
    outside.write_text("# Outside\n", encoding="utf-8")
    original_stat = os.stat

    def swap_target(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == note.name and kwargs.get("dir_fd") is not None:
            note.unlink()
            note.symlink_to(outside)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(frontmatter_migration.os, "stat", swap_target)
    with pytest.raises(MigrationStateError, match="target changed"):
        atomic_replace_vault_markdown(
            migration_vault,
            "daily/2026-07-29.md",
            expected_full_sha256=frontmatter_migration._sha256(b"# Original\n"),
            content=b"# Replacement\n",
        )

    assert outside.read_bytes() == b"# Outside\n"


def test_inventory_records_invalid_yaml_as_parse_error_with_hashes(
    migration_vault: Path,
) -> None:
    note = migration_vault / "imports/bad.md"
    note.write_text("---\ntype: note\ntype: duplicate\n---\n# Body\n", encoding="utf-8")

    report = inventory_vault(migration_vault, load_manifest_for_vault(migration_vault))

    entry = report["entries"][0]
    assert entry["parse_error"]
    assert entry["full_sha256"] and entry["body_sha256"]


def test_mechanical_planner_classifies_every_pending_field_with_provenance(
    migration_vault: Path, write_vault_manifest
) -> None:  # noqa: ANN001
    write_vault_manifest(migration_vault)
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    reflection = migration_vault / "thoughts/reflections/2026-07-28.md"
    reflection.parent.mkdir(parents=True, exist_ok=True)
    reflection.write_text("# Reflection\n", encoding="utf-8")
    (migration_vault / "thoughts/card.md").write_text(
        "---\ntype: note\ntags: [single]\nstatus: active\n---\n# Card\n",
        encoding="utf-8",
    )
    manifest = load_manifest_for_vault(migration_vault)
    state = build_migration_state(inventory_vault(migration_vault, manifest))
    entries = {entry["path"]: entry for entry in state["entries"]}

    daily = entries["daily/2026-07-29.md"]
    assert daily["updates"]["type"] == "daily"
    assert daily["updates"]["date"] == "2026-07-29"
    assert set(daily["mechanical_provenance"]) >= {
        "type",
        "date",
        "last_accessed",
        "relevance",
        "tier",
    }

    reflected = entries["thoughts/reflections/2026-07-28.md"]
    assert reflected["updates"]["date"] == "2026-07-28"
    assert reflected["mechanical_provenance"]["date"]["source"] == (
        "reflection-filename"
    )
    assert reflected["mechanical_provenance"]["created"]["source"] == (
        "filesystem-mtime"
    )

    card = entries["thoughts/card.md"]
    assert card["semantic"]["requested_fields"] == ("description", "tags")
    assert card["field_classification"]["description"] == "semantic"
    assert card["field_classification"]["tags"] == "semantic"
    assert projected_migration_summary(state)["unserviceable"] == []


def test_daily_bogus_type_is_invalid_and_planned_as_daily(
    migration_vault: Path,
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    note.write_text(
        "---\ntype: note\ndate: 2026-07-29\n---\n# Day\n",
        encoding="utf-8",
    )

    inventory = inventory_vault(
        migration_vault, load_manifest_for_vault(migration_vault)
    )
    entry = inventory["entries"][0]

    assert entry["invalid_fields"] == ("type",)
    assert entry["planned_updates"]["type"] == "daily"
    assert entry["mechanical_provenance"]["type"] == {
        "source": "profile-router",
        "value": "daily",
    }


def test_apply_refuses_without_external_backup_and_quiesce_proof(
    migration_vault: Path,
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, manifest = _state(migration_vault)

    with pytest.raises(MigrationStateError, match="requires external state"):
        apply_migration_state(state, manifest=manifest, apply=True)


def test_prepare_rejects_active_writer_and_creates_exact_backup_metadata(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    note.write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    active = _inactive_writers()
    active[KNOWN_WRITER_UNITS[0]] = "active"

    with pytest.raises(MigrationStateError, match="writers are active"):
        create_backup_gate(
            state,
            backup_dir=tmp_path / "backup",
            proof_path=tmp_path / "proof.json",
            writer_states=lambda: active,
            writer_processes=_no_writer_processes,
        )
    manifest_path, proof_path = _prepared_gate(state, tmp_path)
    backup = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert proof_path.exists()
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert backup["plan_hash"] == proof["plan_hash"] == state["plan_hash"]
    assert proof["wal_set_hash"] == frontmatter_migration._wal_set_hash(
        state["entries"]
    )
    assert backup["entries"] == [
        {
            "body_sha256": state["entries"][0]["frozen_body_sha256"],
            "frontmatter_sha256": state["entries"][0]["frozen_frontmatter_sha256"],
            "full_sha256": state["entries"][0]["frozen_full_sha256"],
            "gid": note.stat().st_gid,
            "mode": note.stat().st_mode & 0o7777,
            "mtime_ns": note.stat().st_mtime_ns,
            "path": "daily/2026-07-29.md",
            "uid": note.stat().st_uid,
        }
    ]


def test_prepare_refreshes_proof_for_resumed_expected_state_without_replacing_backup(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    note.write_text("# Day\n", encoding="utf-8")
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    manifest_path, proof_path = _prepared_gate(state, tmp_path)
    backup = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = Path(backup["archive"])
    original_archive = archive.read_bytes()
    entry = state["entries"][0]
    frozen = {
        key: entry[key]
        for key in (
            "frozen_full_sha256",
            "frozen_body_sha256",
            "frozen_frontmatter_sha256",
        )
    }

    summary = apply_migration_state(
        state,
        manifest=manifest,
        apply=True,
        state_path=state_path,
        backup_manifest_path=manifest_path,
        proof_path=proof_path,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )
    assert summary["applied"] == summary["complete"] == 1
    assert entry["mechanical_plan_applied"] is True
    _expire_proof(proof_path)

    refreshed_manifest, refreshed_proof = create_backup_gate(
        state,
        backup_dir=tmp_path / "backup",
        proof_path=proof_path,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )

    assert refreshed_manifest == manifest_path
    assert refreshed_proof == proof_path
    assert archive.read_bytes() == original_archive
    assert {key: entry[key] for key in frozen} == frozen
    verify_backup_gate(
        state,
        backup_manifest_path=manifest_path,
        proof_path=proof_path,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )


@pytest.mark.parametrize("tamper", ["updates", "provenance"])
def test_apply_rejects_tampered_immutable_mechanical_plan(
    migration_vault: Path,
    tmp_path: Path,
    tamper: str,
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    original = b"# Day\n"
    note.write_bytes(original)
    state, manifest = _state(migration_vault)
    backup, proof = _prepared_gate(state, tmp_path)
    entry = state["entries"][0]
    if tamper == "updates":
        entry["updates"]["type"] = "note"
    else:
        entry["mechanical_provenance"]["type"]["source"] = "filesystem-mtime"

    with pytest.raises(MigrationStateError, match="mechanical plan"):
        apply_migration_state(
            state,
            manifest=manifest,
            apply=True,
            state_path=tmp_path / "state.json",
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )

    assert note.read_bytes() == original


def test_state_rejects_unproven_mechanical_completion_but_allows_empty_plan(
    migration_vault: Path, tmp_path: Path
) -> None:
    daily = migration_vault / "daily/2026-07-29.md"
    daily.write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    entry = state["entries"][0]
    assert entry["mechanical_plan"]["candidate"] is not None
    entry["mechanical_plan_applied"] = True
    entry["updates"] = {}
    entry["header_repair"] = None

    with pytest.raises(MigrationStateError, match="completion evidence"):
        save_migration_state(tmp_path / "tampered-state.json", state)

    daily.unlink()
    card = migration_vault / "thoughts/card.md"
    card.write_text("---\ntype: note\n---\n# Card\n", encoding="utf-8")
    semantic_only, _ = _state(migration_vault)
    semantic_entry = semantic_only["entries"][0]
    assert semantic_entry["mechanical_plan"]["candidate"] is None
    assert semantic_entry["mechanical_plan_applied"] is True
    assert semantic_entry["mechanical_completion"] is None
    save_migration_state(tmp_path / "empty-plan-state.json", semantic_only)


def test_apply_rejects_tampered_header_repair_plan(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "templates/crm-template.md"
    note.parent.mkdir(parents=True)
    original = (
        b"---\n"
        b"type: crm\n"
        b"description: >-\n"
        b"[One-line summary: industry, key deal, what makes this client notable]\n"
        b"---\n"
        b"# Body\n"
    )
    note.write_bytes(original)
    state, manifest = _state(migration_vault)
    backup, proof = _prepared_gate(state, tmp_path)
    assert (
        state["entries"][0]["header_repair"] == "crm-template-description-placeholder"
    )
    state["entries"][0]["header_repair"] = "different-repair"

    with pytest.raises(MigrationStateError, match="mechanical plan"):
        apply_migration_state(
            state,
            manifest=manifest,
            apply=True,
            state_path=tmp_path / "state.json",
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )

    assert note.read_bytes() == original


def test_prepare_refresh_rejects_changed_frozen_body_invariant(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    note.write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    _prepared_gate(state, tmp_path)
    entry = state["entries"][0]
    changed = frontmatter_migration.patch_frontmatter_bytes(
        b"# Changed body\n", entry["updates"]
    )
    document = parse_frontmatter_bytes(changed)
    note.write_bytes(changed)
    entry["expected_full_sha256"] = document.full_sha256
    entry["expected_body_sha256"] = document.body_sha256
    entry["expected_frontmatter_sha256"] = document.frontmatter_sha256

    with pytest.raises(MigrationStateError, match="immutable frozen body"):
        create_backup_gate(
            state,
            backup_dir=tmp_path / "backup",
            proof_path=tmp_path / "proof/quiesce.json",
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )


def test_backup_archive_validation_rejects_metadata_mismatch(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    note.write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    manifest_path, proof_path = _prepared_gate(state, tmp_path)
    backup = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup["entries"][0]["mode"] ^= 0o100
    manifest_path.write_text(json.dumps(backup), encoding="utf-8")

    with pytest.raises(MigrationStateError, match="metadata"):
        verify_backup_gate(
            state,
            backup_manifest_path=manifest_path,
            proof_path=proof_path,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )


def test_backup_round_trips_and_validates_fractional_mtime_ns(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    note.write_text("# Day\n", encoding="utf-8")
    requested_mtime_ns = 1_750_000_000_123_456_789
    os.utime(note, ns=(requested_mtime_ns, requested_mtime_ns))
    actual_mtime_ns = note.stat().st_mtime_ns
    state, _ = _state(migration_vault)
    manifest_path, proof_path = _prepared_gate(state, tmp_path)
    backup = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert backup["entries"][0]["mtime_ns"] == actual_mtime_ns
    with tarfile.open(backup["archive"], "r") as archive:
        member = archive.getmembers()[0]
        assert frontmatter_migration._tar_member_mtime_ns(member) == actual_mtime_ns
    backup["entries"][0]["mtime_ns"] += 1
    manifest_path.write_text(json.dumps(backup), encoding="utf-8")

    with pytest.raises(MigrationStateError, match="metadata"):
        verify_backup_gate(
            state,
            backup_manifest_path=manifest_path,
            proof_path=proof_path,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )


def test_interrupted_backup_never_leaves_proof_and_a_retry_recovers(
    migration_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    backup_dir = tmp_path / "backup"
    proof = tmp_path / "proof/quiesce.json"
    original_validate = frontmatter_migration._validate_backup_archive

    def interrupted_archive(*_args: object, **_kwargs: object) -> None:
        raise MigrationStateError("simulated archive interruption")

    monkeypatch.setattr(
        frontmatter_migration, "_validate_backup_archive", interrupted_archive
    )
    with pytest.raises(MigrationStateError, match="interruption"):
        create_backup_gate(
            state,
            backup_dir=backup_dir,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )

    assert not proof.exists()
    assert not (backup_dir / "vault-frontmatter-source.tar").exists()
    assert not list(backup_dir.glob("*.tmp"))

    monkeypatch.setattr(
        frontmatter_migration, "_validate_backup_archive", original_validate
    )
    manifest_path, proof_path = create_backup_gate(
        state,
        backup_dir=backup_dir,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )

    assert manifest_path.exists()
    assert proof_path.exists()


def test_prepare_rejects_stray_project_writer_process(
    migration_vault: Path, tmp_path: Path
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)

    with pytest.raises(MigrationStateError, match="pid:31337"):
        create_backup_gate(
            state,
            backup_dir=tmp_path / "backup",
            proof_path=tmp_path / "proof.json",
            writer_states=_inactive_writers,
            writer_processes=lambda _root: (31337,),
        )


def test_apply_uses_backup_gate_cas_and_complete_only_when_profile_valid(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    note.write_text("# Day\n", encoding="utf-8")
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    backup, proof = _prepared_gate(state, tmp_path)

    result = apply_migration_state(
        state,
        manifest=manifest,
        apply=True,
        state_path=state_path,
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )

    assert result["applied"] == result["complete"] == 1
    assert result["pending"] == 0
    assert state["entries"][0]["status"] == "complete"
    assert b'date: "2026-07-29"' in note.read_bytes()


def test_apply_refreshes_progress_proof_between_multiple_commits(
    migration_vault: Path, tmp_path: Path
) -> None:
    for day in ("2026-07-28", "2026-07-29"):
        (migration_vault / f"daily/{day}.md").write_text(f"# {day}\n", encoding="utf-8")
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    backup, proof = _prepared_gate(state, tmp_path)

    summary = apply_migration_state(
        state,
        manifest=manifest,
        apply=True,
        state_path=state_path,
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )

    assert summary["applied"] == summary["complete"] == 2
    assert summary["errors"] == summary["pending"] == 0
    verify_backup_gate(
        state,
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )


def test_mechanical_commit_reconciles_crash_after_replace_before_state_save(
    migration_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    original = b"# Day\n"
    note.write_bytes(original)
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    backup, proof = _prepared_gate(state, tmp_path)
    durable_save = frontmatter_migration.save_migration_state
    save_calls = 0

    def crash_on_finalize(path: Path, value: object) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise RuntimeError("simulated crash after Markdown replace")
        durable_save(path, value)

    monkeypatch.setattr(
        frontmatter_migration, "save_migration_state", crash_on_finalize
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        apply_migration_state(
            state,
            manifest=manifest,
            apply=True,
            state_path=state_path,
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )
    monkeypatch.setattr(frontmatter_migration, "save_migration_state", durable_save)

    crashed_state = frontmatter_migration.load_migration_state(state_path)
    assert crashed_state["entries"][0]["write_ahead"]["kind"] == "mechanical"
    assert note.read_bytes() != original
    assert parse_frontmatter_bytes(note.read_bytes()).body == original
    _expire_proof(proof)

    summary = apply_migration_state(
        crashed_state,
        manifest=manifest,
        apply=True,
        state_path=state_path,
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )

    assert summary["complete"] == 1
    recovered = frontmatter_migration.load_migration_state(state_path)
    assert recovered["entries"][0]["write_ahead"] is None
    assert recovered["entries"][0]["expected_full_sha256"] == (
        frontmatter_migration._sha256(note.read_bytes())
    )
    completion = recovered["entries"][0]["mechanical_completion"]
    assert completion["version"] == (
        frontmatter_migration.MECHANICAL_COMPLETION_VERSION
    )
    assert completion["wal"]["kind"] == "mechanical"
    assert (
        completion["wal"]["candidate_full_sha256"]
        == (recovered["entries"][0]["expected_full_sha256"])
    )


def test_semantic_commit_reconciles_crash_after_replace_before_state_save(
    migration_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = migration_vault / "thoughts/card.md"
    original = b"---\ntype: note\n---\n# Card\n"
    card.write_bytes(original)
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    backup, proof = _prepared_gate(state, tmp_path)
    payload = {
        "description": "Useful card",
        "tags": ["project-alpha", "planning"],
        "status": "active",
    }
    durable_save = frontmatter_migration.save_migration_state
    save_calls = 0

    def crash_on_finalize(path: Path, value: object) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise RuntimeError("simulated semantic crash")
        durable_save(path, value)

    monkeypatch.setattr(
        frontmatter_migration, "save_migration_state", crash_on_finalize
    )
    with pytest.raises(RuntimeError, match="semantic crash"):
        apply_semantic_result(
            state,
            manifest=manifest,
            relative_path="thoughts/card.md",
            payload=payload,
            state_path=state_path,
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )
    monkeypatch.setattr(frontmatter_migration, "save_migration_state", durable_save)

    crashed_state = frontmatter_migration.load_migration_state(state_path)
    assert crashed_state["entries"][0]["write_ahead"]["kind"] == "semantic"
    assert parse_frontmatter_bytes(card.read_bytes()).body == b"# Card\n"
    _expire_proof(proof)

    apply_semantic_result(
        crashed_state,
        manifest=manifest,
        relative_path="thoughts/card.md",
        payload=payload,
        state_path=state_path,
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )

    recovered = frontmatter_migration.load_migration_state(state_path)
    entry = recovered["entries"][0]
    assert entry["write_ahead"] is None
    assert entry["semantic"]["status"] == "complete"
    with pytest.raises(MigrationStateError, match="not pending"):
        apply_semantic_result(
            recovered,
            manifest=manifest,
            relative_path="thoughts/card.md",
            payload=payload,
            state_path=state_path,
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )


def test_write_ahead_old_source_retries_and_other_hash_conflicts(
    migration_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    original = b"# Day\n"
    note.write_bytes(original)
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    backup, proof = _prepared_gate(state, tmp_path)
    original_recheck = frontmatter_migration._recheck_before_commit

    def crash_before_replace(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(
        frontmatter_migration, "_recheck_before_commit", crash_before_replace
    )
    with pytest.raises(RuntimeError, match="before replace"):
        apply_migration_state(
            state,
            manifest=manifest,
            apply=True,
            state_path=state_path,
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )
    monkeypatch.setattr(
        frontmatter_migration, "_recheck_before_commit", original_recheck
    )
    assert note.read_bytes() == original

    retry_state = frontmatter_migration.load_migration_state(state_path)
    summary = apply_migration_state(
        retry_state,
        manifest=manifest,
        apply=True,
        state_path=state_path,
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )
    assert summary["applied"] == summary["complete"] == 1

    conflict_note = migration_vault / "daily/2026-07-30.md"
    conflict_note.write_bytes(b"# Next day\n")
    conflict_state, conflict_manifest = _state(migration_vault)
    conflict_state_path = tmp_path / "conflict-state.json"
    save_migration_state(conflict_state_path, conflict_state)
    conflict_backup, conflict_proof = create_backup_gate(
        conflict_state,
        backup_dir=tmp_path / "conflict-backup",
        proof_path=tmp_path / "conflict-proof.json",
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )
    monkeypatch.setattr(
        frontmatter_migration, "_recheck_before_commit", crash_before_replace
    )
    with pytest.raises(RuntimeError, match="before replace"):
        apply_migration_state(
            conflict_state,
            manifest=conflict_manifest,
            apply=True,
            state_path=conflict_state_path,
            backup_manifest_path=conflict_backup,
            proof_path=conflict_proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )
    monkeypatch.setattr(
        frontmatter_migration, "_recheck_before_commit", original_recheck
    )
    conflict_note.write_bytes(b"# Third-party content\n")
    reloaded = frontmatter_migration.load_migration_state(conflict_state_path)
    with pytest.raises(MigrationStateError, match="neither journal"):
        reconcile_migration_state(
            reloaded,
            state_path=conflict_state_path,
            manifest=conflict_manifest,
            backup_manifest_path=conflict_backup,
            proof_path=conflict_proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )


def test_reconcile_rejects_wal_candidate_tampered_after_prepare(
    migration_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    original = b"# Day\n"
    note.write_bytes(original)
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    backup, proof = _prepared_gate(state, tmp_path)

    def crash_before_replace(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(
        frontmatter_migration, "_recheck_before_commit", crash_before_replace
    )
    with pytest.raises(RuntimeError, match="before replace"):
        apply_migration_state(
            state,
            manifest=manifest,
            apply=True,
            state_path=state_path,
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )

    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_state["entries"][0]["write_ahead"]["candidate_full_sha256"] = "0" * 64
    state_path.write_text(json.dumps(raw_state), encoding="utf-8")

    with pytest.raises(MigrationStateError, match="candidate is not allowed"):
        frontmatter_migration.load_migration_state(state_path)

    assert note.read_bytes() == original


def test_dry_run_keeps_planned_updates_pending_and_nonzero(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    note.write_text("---\ndate: 2026-07-29\n---\n# Day\n", encoding="utf-8")
    state, manifest = _state(migration_vault)

    summary = apply_migration_state(state, manifest=manifest, apply=False)

    assert summary["planned"] == summary["pending"] == 1
    assert state["entries"][0]["status"] == "pending"
    assert run_frontmatter_migration._pending_exit(summary) == 1
    assert note.read_text(encoding="utf-8").startswith("---\ndate: 2026-07-29")


def test_shared_cooperative_lock_blocks_second_writer(migration_vault: Path) -> None:
    acquired = multiprocessing.Event()
    release = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_vault_write_lock,
        args=(str(migration_vault), acquired, release),
    )
    holder.start()
    assert acquired.wait(5)

    entered = threading.Event()

    def wait_for_lock() -> None:
        with vault_write_lock(migration_vault):
            entered.set()

    waiter = threading.Thread(target=wait_for_lock)
    waiter.start()
    assert not entered.wait(0.2)
    release.set()
    holder.join(5)
    waiter.join(5)
    assert holder.exitcode == 0
    assert entered.is_set()


def test_precommit_writer_detection_aborts_without_replacement(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    original = b"# Day\n"
    note.write_bytes(original)
    state, manifest = _state(migration_vault)
    backup, proof = _prepared_gate(state, tmp_path)
    calls = 0

    def writer_appears_before_commit() -> dict[str, str]:
        nonlocal calls
        calls += 1
        states = _inactive_writers()
        if calls > 1:
            states[KNOWN_WRITER_UNITS[0]] = "active"
        return states

    with pytest.raises(WriterQuiescenceError, match="writers are active"):
        apply_migration_state(
            state,
            manifest=manifest,
            apply=True,
            state_path=tmp_path / "state.json",
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=writer_appears_before_commit,
            writer_processes=_no_writer_processes,
        )

    assert note.read_bytes() == original


def test_apply_rejects_source_changed_after_backup_before_any_commit(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "daily/2026-07-29.md"
    note.write_text("# Day\n", encoding="utf-8")
    state, manifest = _state(migration_vault)
    backup, proof = _prepared_gate(state, tmp_path)
    note.write_text("# Concurrent edit\n", encoding="utf-8")

    with pytest.raises(MigrationStateError, match="changed after backup"):
        apply_migration_state(
            state,
            manifest=manifest,
            apply=True,
            state_path=tmp_path / "state.json",
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )


@pytest.mark.parametrize(
    ("relative", "content", "field", "expected"),
    [
        (
            "templates/crm-template.md",
            "---\n"
            "type: crm\n"
            "description: >-\n"
            "[One-line summary: industry, key deal, what makes this client notable]\n"
            "---\n"
            "# Body\n",
            "description",
            "[One-line summary: industry, key deal, what makes this client notable]",
        ),
    ],
)
def test_known_header_repairs_apply_once_preserve_body_and_then_complete(
    migration_vault: Path,
    tmp_path: Path,
    relative: str,
    content: str,
    field: str,
    expected: object,
) -> None:
    note = migration_vault / relative
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(content, encoding="utf-8")
    original_body = b"# Body\n"
    state, manifest = _state(migration_vault)
    backup, proof = _prepared_gate(state, tmp_path)

    result = apply_migration_state(
        state,
        manifest=manifest,
        apply=True,
        state_path=tmp_path / "state.json",
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )

    repaired = parse_frontmatter_bytes(note.read_bytes())
    assert repaired.fields[field] == expected
    assert repaired.body == original_body
    assert state["entries"][0]["header_repair"] is None
    assert state["entries"][0]["semantic"]["frozen_full_sha256"] == repaired.full_sha256
    assert result["complete"] == 1

    second = apply_migration_state(state, manifest=manifest, apply=False)
    assert second["pending"] == second["planned"] == 0
    assert second["complete"] == 1


def test_semantic_apply_requires_full_and_frontmatter_hashes(
    migration_vault: Path, tmp_path: Path
) -> None:
    card = migration_vault / "thoughts/card.md"
    card.write_text("---\ntype: note\n---\n# Card\n", encoding="utf-8")
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    backup, proof = _prepared_gate(state, tmp_path)
    card.write_text("---\ntype: changed\n---\n# Card\n", encoding="utf-8")

    with pytest.raises(MigrationStateError, match="changed after backup"):
        apply_semantic_result(
            state,
            manifest=manifest,
            relative_path="thoughts/card.md",
            payload={
                "description": "Useful card",
                "tags": ["project-alpha", "planning"],
                "status": "active",
            },
            state_path=state_path,
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )


def test_semantic_apply_accepts_valid_epistemic_adapter_payload(
    migration_vault: Path, tmp_path: Path
) -> None:
    note = migration_vault / "thoughts/fact.md"
    note.write_text(
        "---\n"
        "type: epistemic\n"
        "epistemic_confidence: certain\n"
        "epistemic_scope: project:planner\n"
        "epistemic_state: retired\n"
        "---\n"
        "# Fact\n",
        encoding="utf-8",
    )
    state, manifest = _state(migration_vault)
    assert state["entries"][0]["semantic"]["requested_fields"] == (
        "epistemic_confidence",
        "epistemic_scope",
        "epistemic_state",
        "epistemic_verification",
    )
    backup, proof = _prepared_gate(state, tmp_path)

    apply_semantic_result(
        state,
        manifest=manifest,
        relative_path="thoughts/fact.md",
        payload={
            "epistemic_confidence": "inferred",
            "epistemic_scope": "project:planner",
            "epistemic_state": "active",
            "epistemic_verification": "Reviewed source",
        },
        state_path=tmp_path / "state.json",
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )

    document = parse_frontmatter_bytes(note.read_bytes())
    assert document.fields["epistemic_state"] == "active"
    route, missing, invalid = frontmatter_migration.validate_document(
        "thoughts/fact.md", document, manifest
    )
    assert route.name == "epistemic"
    assert missing == invalid == ()


def test_semantic_apply_rejects_duplicate_completed_result(
    migration_vault: Path, tmp_path: Path
) -> None:
    card = migration_vault / "thoughts/card.md"
    card.write_text("---\ntype: note\n---\n# Card\n", encoding="utf-8")
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    backup, proof = _prepared_gate(state, tmp_path)
    payload = {
        "description": "Useful card",
        "tags": ["project-alpha", "planning"],
        "status": "active",
    }

    apply_semantic_result(
        state,
        manifest=manifest,
        relative_path="thoughts/card.md",
        payload=payload,
        state_path=state_path,
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )

    with pytest.raises(MigrationStateError, match="not pending"):
        apply_semantic_result(
            state,
            manifest=manifest,
            relative_path="thoughts/card.md",
            payload=payload,
            state_path=state_path,
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )


def test_semantic_postcommit_writer_detection_leaves_resumable_wal(
    migration_vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = migration_vault / "thoughts/card.md"
    card.write_text("---\ntype: note\n---\n# Card\n", encoding="utf-8")
    state, manifest = _state(migration_vault)
    state_path = tmp_path / "state.json"
    save_migration_state(state_path, state)
    backup, proof = _prepared_gate(state, tmp_path)

    def writer_appears_after_commit(*_args: object, **_kwargs: object) -> None:
        raise WriterQuiescenceError(f"writers are active: {KNOWN_WRITER_UNITS[0]}")

    monkeypatch.setattr(
        frontmatter_migration, "_recheck_after_commit", writer_appears_after_commit
    )

    with pytest.raises(WriterQuiescenceError, match="writers are active"):
        apply_semantic_result(
            state,
            manifest=manifest,
            relative_path="thoughts/card.md",
            payload={
                "description": "Useful card",
                "tags": ["project-alpha", "planning"],
                "status": "active",
            },
            state_path=state_path,
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=_inactive_writers,
            writer_processes=_no_writer_processes,
        )

    durable = frontmatter_migration.load_migration_state(state_path)
    entry = durable["entries"][0]
    assert entry["status"] == "pending"
    assert entry["semantic"]["status"] == "pending"
    assert entry["write_ahead"]["kind"] == "semantic"
    assert b'description: "Useful card"' in card.read_bytes()

    active = _inactive_writers()
    active[KNOWN_WRITER_UNITS[0]] = "active"
    with pytest.raises(WriterQuiescenceError, match="writers are active"):
        reconcile_migration_state(
            durable,
            state_path=state_path,
            manifest=manifest,
            backup_manifest_path=backup,
            proof_path=proof,
            writer_states=lambda: active,
            writer_processes=_no_writer_processes,
        )
    still_pending = frontmatter_migration.load_migration_state(state_path)
    assert still_pending["entries"][0]["write_ahead"] == entry["write_ahead"]
    assert still_pending["entries"][0]["semantic"]["status"] == "pending"

    result = reconcile_migration_state(
        still_pending,
        state_path=state_path,
        manifest=manifest,
        backup_manifest_path=backup,
        proof_path=proof,
        writer_states=_inactive_writers,
        writer_processes=_no_writer_processes,
    )
    assert result["finalized"] == ("thoughts/card.md",)
    recovered = frontmatter_migration.load_migration_state(state_path)
    assert recovered["entries"][0]["write_ahead"] is None
    assert recovered["entries"][0]["semantic"]["status"] == "complete"


def test_state_and_report_artifacts_are_forbidden_inside_vault(
    migration_vault: Path,
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    with pytest.raises(MigrationStateError, match="outside vault"):
        save_migration_state(migration_vault / "state.json", state)
    with pytest.raises(MigrationStateError, match="outside vault"):
        write_inventory_report(
            inventory_vault(migration_vault, load_manifest_for_vault(migration_vault)),
            migration_vault / "report",
        )


def test_service_rejects_non_object_migration_state_entry(
    migration_vault: Path, tmp_path: Path
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    state["entries"][0] = "corrupted"
    state_path = tmp_path / "corrupted-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(
        MigrationStateError, match="migration state entry 0 must be an object"
    ):
        frontmatter_migration.load_migration_state(state_path)


def test_service_rejects_non_string_vault_path_before_path_conversion(
    migration_vault: Path, tmp_path: Path
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    state["vault_path"] = []
    state_path = tmp_path / "bad-vault-path-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(MigrationStateError, match="vault_path"):
        frontmatter_migration.load_migration_state(state_path)


@pytest.mark.parametrize("source_count", [-1, True, 2])
def test_service_rejects_invalid_source_count(
    migration_vault: Path, tmp_path: Path, source_count: object
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    state["source_count"] = source_count
    state_path = tmp_path / f"bad-source-count-{source_count!s}.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(MigrationStateError, match="source_count"):
        frontmatter_migration.load_migration_state(state_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("version", [], "version"),
        ("source_set_hash", [], "source_set_hash"),
        ("plan_hash", {}, "plan_hash"),
    ),
)
def test_service_rejects_non_scalar_top_level_state_fields(
    migration_vault: Path,
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    state[field] = value
    state_path = tmp_path / f"bad-{field}.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(MigrationStateError, match=message):
        frontmatter_migration.load_migration_state(state_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("mechanical_plan", [], "mechanical_plan must be an object"),
        ("semantic", [], "semantic must be an object"),
        ("write_ahead", [], "write_ahead must be an object"),
        (
            "mechanical_completion",
            {"wal": []},
            "mechanical_completion.wal must be an object",
        ),
    ),
)
def test_service_rejects_non_object_nested_state_records(
    migration_vault: Path,
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    state["entries"][0][field] = value
    state_path = tmp_path / f"corrupted-{field}.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(MigrationStateError, match=message):
        frontmatter_migration.load_migration_state(state_path)


def test_cli_reports_corrupted_state_entry_without_traceback(
    migration_vault: Path, tmp_path: Path
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    state["entries"][0] = 42
    state_path = tmp_path / "cli-corrupted-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "d_brain.run_frontmatter_migration",
            "migrate",
            "--state",
            str(state_path),
            "--dry-run",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "migration state entry 0 must be an object" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("vault_path", [], "vault_path"),
        ("source_count", True, "source_count"),
    ),
)
def test_cli_reports_invalid_top_level_state_without_traceback(
    migration_vault: Path,
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    state[field] = value
    state_path = tmp_path / f"cli-bad-{field}.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "d_brain.run_frontmatter_migration",
            "migrate",
            "--state",
            str(state_path),
            "--dry-run",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("nonexistent", "vault-manifest.json: is missing"),
        ("mismatched", "memory_root does not match the requested vault"),
    ),
)
def test_cli_reports_manifest_validation_failure_without_traceback(
    migration_vault: Path,
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    (migration_vault / "daily/2026-07-29.md").write_text("# Day\n", encoding="utf-8")
    state, _ = _state(migration_vault)
    if case == "nonexistent":
        requested_vault = tmp_path / "missing-project/vault"
    else:
        requested_vault = tmp_path / "other-vault"
        requested_vault.mkdir()
    state["vault_path"] = str(requested_vault.resolve())
    state_path = tmp_path / f"cli-{case}-vault-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "d_brain.run_frontmatter_migration",
            "migrate",
            "--state",
            str(state_path),
            "--dry-run",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert result.stderr.startswith("frontmatter-migration: ")
    assert len(result.stderr.strip().splitlines()) == 1
    assert "Traceback" not in result.stderr


def test_validate_reports_invalid_values_and_no_note_becomes_complete_early(
    migration_vault: Path,
) -> None:
    (migration_vault / "thoughts/card.md").write_text(
        "---\ntype: note\ndescription: x\ntags: [bad tag]\n"
        "status: unknown\n---\n# Card\n",
        encoding="utf-8",
    )
    manifest = load_manifest_for_vault(migration_vault)
    report = validate_vault(migration_vault, manifest)
    state = build_migration_state(inventory_vault(migration_vault, manifest))

    assert report["invalid"] == 1
    assert state["entries"][0]["status"] == "pending"
    assert state["entries"][0]["semantic"]["status"] == "pending"
    assert state["entries"][0]["semantic"]["requested_fields"] == (
        "status",
        "tags",
    )


def test_validate_and_cli_fail_when_inventory_coverage_is_blocked(
    migration_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.md").write_text("# Hidden\n", encoding="utf-8")
    (migration_vault / "daily/linked").symlink_to(outside, target_is_directory=True)
    manifest = load_manifest_for_vault(migration_vault)

    report = validate_vault(migration_vault, manifest)

    assert report["coverage_complete"] is False
    assert report["blocking_symlink_directory_count"] == 1
    assert report["blocking_symlink_directories"][0]["path"] == "daily/linked"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "frontmatter-migration",
            "validate",
            "--vault",
            str(migration_vault),
        ],
    )
    assert run_frontmatter_migration.main() == 1


def test_enrich_cli_accepts_resumable_bounded_json_adapter(
    migration_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = migration_vault / "thoughts/card.md"
    card.write_text("---\ntype: note\n---\n# Card\n", encoding="utf-8")
    state, manifest = _state(migration_vault)
    input_path = tmp_path / "results.json"
    input_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "path": "thoughts/card.md",
                        "payload": {
                            "description": "Useful card",
                            "tags": ["project-alpha", "planning"],
                            "status": "active",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    received: list[dict[str, object]] = []
    monkeypatch.setattr(
        run_frontmatter_migration, "load_migration_state", lambda _: state
    )
    monkeypatch.setattr(
        run_frontmatter_migration, "load_manifest_for_vault", lambda _: manifest
    )
    monkeypatch.setattr(
        run_frontmatter_migration,
        "apply_semantic_result",
        lambda *_args, **kwargs: received.append(kwargs),
    )
    monkeypatch.setattr(
        run_frontmatter_migration,
        "apply_migration_state",
        lambda *_args, **_kwargs: {
            "planned": 0,
            "applied": 0,
            "complete": 1,
            "pending": 0,
            "stale": 0,
            "errors": 0,
            "malformed": 0,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "frontmatter-migration",
            "enrich",
            "--state",
            str(tmp_path / "state.json"),
            "--input-json",
            str(input_path),
            "--backup-manifest",
            str(tmp_path / "backup.json"),
            "--proof",
            str(tmp_path / "proof.json"),
            "--batch-size",
            "1",
        ],
    )

    assert run_frontmatter_migration.main() == 0
    assert received[0]["relative_path"] == "thoughts/card.md"


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_enrich_batch_size_must_be_positive(value: str) -> None:
    with pytest.raises(SystemExit) as error:
        run_frontmatter_migration._parser().parse_args(
            [
                "enrich",
                "--state",
                "state.json",
                "--input-json",
                "results.json",
                "--backup-manifest",
                "backup.json",
                "--proof",
                "proof.json",
                "--batch-size",
                value,
            ]
        )
    assert error.value.code == 2


def test_enrich_batch_limit_is_deterministic_and_resumable(
    migration_vault: Path,
) -> None:
    for name in ("alpha", "beta", "gamma"):
        (migration_vault / f"thoughts/{name}.md").write_text(
            "---\ntype: note\n---\n# Card\n", encoding="utf-8"
        )
    state, _ = _state(migration_vault)
    results = [
        {
            "path": f"thoughts/{name}.md",
            "payload": {
                "description": name,
                "tags": ["project-alpha", "planning"],
                "status": "active",
            },
        }
        for name in ("gamma", "alpha", "beta")
    ]

    first = run_frontmatter_migration._bounded_pending_results(state, results, 2)

    assert [result["path"] for result in first] == [
        "thoughts/alpha.md",
        "thoughts/beta.md",
    ]
    entries = {entry["path"]: entry for entry in state["entries"]}
    for result in first:
        entries[result["path"]]["semantic"]["status"] = "complete"
    second = run_frontmatter_migration._bounded_pending_results(state, results, 2)
    assert [result["path"] for result in second] == ["thoughts/gamma.md"]


def test_bases_expose_declared_quarter_age_and_days_untouched_formulas() -> None:
    active = yaml.safe_load((TEMPLATE_ROOT / "bases/active-projects.base").read_text())
    business = yaml.safe_load(
        (TEMPLATE_ROOT / "bases/business-context.base").read_text()
    )
    assert set(active["formulas"]) == {"updated_quarter", "age_days"}
    assert "formula.updated_quarter" in active["views"][0]["order"]
    assert "formula.age_days" in active["views"][0]["order"]
    assert set(business["formulas"]) == {"days_untouched"}
    assert "formula.days_untouched" in business["views"][0]["order"]


def test_all_five_bases_have_complete_filter_property_and_view_contracts() -> None:
    root = TEMPLATE_ROOT / "bases"

    def filter_strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for child in value for item in filter_strings(child)]
        if isinstance(value, dict):
            return [item for child in value.values() for item in filter_strings(child)]
        return []

    expected_filters = {
        "active-projects.base": (
            'file.ext == "md"',
            'file.path.startsWith("compiled/projects/")',
            'status == "active" || status == "draft" || status == "pending"',
        ),
        "business-context.base": (
            'file.ext == "md"',
            'file.path.startsWith("business/")',
            'file.path.startsWith("projects/")',
        ),
        "knowledge-cards.base": (
            'file.ext == "md"',
            'file.path.startsWith("thoughts/")',
        ),
        "recently-touched.base": (
            'file.ext == "md"',
            'file.path == "MEMORY.md"',
            'file.path.startsWith("daily/")',
        ),
        "memory-tiers.base": (
            'file.ext == "md"',
            'file.path == "MEMORY.md"',
            'file.path.startsWith("daily/")',
        ),
    }

    assert {path.name for path in root.glob("*.base")} == set(expected_filters)
    for name, filter_expressions in expected_filters.items():
        raw = (root / name).read_text(encoding="utf-8")
        base = yaml.safe_load(raw)
        assert set(base) >= {"filters", "properties", "views"}
        assert "obsidian-mind" not in raw
        expressions = filter_strings(base["filters"])
        assert all(expression in expressions for expression in filter_expressions)
        assert isinstance(base["properties"], dict) and base["properties"]
        assert all(
            isinstance(config, dict) and config.get("displayName")
            for config in base["properties"].values()
        )
        assert isinstance(base["views"], list) and base["views"]
        view = base["views"][0]
        assert view["type"] == "table"
        assert isinstance(view.get("name"), str) and view["name"]
        assert isinstance(view.get("order"), list) and view["order"]
        assert set(view["order"]) <= set(base["properties"])
        for sort in view.get("sort", []):
            assert sort["property"] in view["order"]
            assert sort["direction"] in {"ASC", "DESC"}
        if "groupBy" in view:
            assert view["groupBy"]["property"] in view["order"]
            assert view["groupBy"]["direction"] in {"ASC", "DESC"}


def _matches_user_content_filter(
    base: dict[str, Any], path: str, extension: str
) -> bool:
    clauses = base["filters"]["and"]
    for clause in clauses:
        if clause == 'file.ext == "md"' and extension != "md":
            return False
        if isinstance(clause, dict) and "or" in clause:
            patterns = set(clause["or"])
            if not (
                'file.path == "MEMORY.md"' in patterns
                and path == "MEMORY.md"
                or any(
                    pattern.removeprefix('file.path.startsWith("').removesuffix('")')
                    and path.startswith(
                        pattern.removeprefix('file.path.startsWith("').removesuffix(
                            '")'
                        )
                    )
                    for pattern in patterns
                    if pattern.startswith('file.path.startsWith("')
                )
            ):
                return False
    return True


@pytest.mark.parametrize("name", ["recently-touched.base", "memory-tiers.base"])
def test_user_content_bases_filter_only_explicit_markdown_roots(name: str) -> None:
    base = yaml.safe_load((TEMPLATE_ROOT / "bases" / name).read_text())

    assert _matches_user_content_filter(base, "daily/2026-07-29.md", "md")
    assert _matches_user_content_filter(base, "compiled/brief.md", "md")
    assert _matches_user_content_filter(base, "MEMORY.md", "md")
    assert not _matches_user_content_filter(base, "daily/raw.txt", "txt")
    assert not _matches_user_content_filter(base, ".obsidian/workspace.md", "md")
    assert not _matches_user_content_filter(base, "bases/memory-tiers.base", "base")
    assert not _matches_user_content_filter(base, ".claude/skills/tool.md", "md")
