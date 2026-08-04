from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import _load_memory_engine, _write_vault_manifest

from d_brain.manifest import load_manifest_for_vault
from d_brain.services import epistemic_memory
from d_brain.services.epistemic_memory import (
    EpistemicConflictError,
    EpistemicValidationError,
    parse_epistemic_metadata,
    parse_epistemic_note,
    recover_pending_supersession,
    supersede_notes,
)
from d_brain.services.frontmatter import parse_frontmatter_bytes, validate_document


def _note(
    *,
    confidence: str = "verified",
    scope: str = "project:test",
    state: str = "active",
    verification: str = "test evidence",
    supersedes: str = "[]",
    superseded_by: str = "",
    body: bytes = b"# Durable fact\n\nBody stays byte-for-byte stable.\n",
) -> bytes:
    fields = [
        "---",
        "type: epistemic",
        'description: "Test epistemic note"',
        f"epistemic_confidence: {confidence}",
        f'epistemic_scope: "{scope}"',
        f"epistemic_state: {state}",
        f"supersedes: {supersedes}",
        "created: 2026-07-29",
        "updated: 2026-07-29",
        "last_accessed: 2026-07-29",
        "relevance: 1.0",
        "tier: active",
    ]
    if verification:
        fields.append(f'epistemic_verification: "{verification}"')
    if superseded_by:
        fields.append(f'superseded_by: "{superseded_by}"')
    return "\n".join(fields).encode() + b"\n---\n" + body


def _write_pair(vault_path: Path) -> tuple[Path, Path]:
    thoughts = vault_path / "thoughts"
    thoughts.mkdir(parents=True)
    _write_vault_manifest(vault_path)
    old_path = thoughts / "old.md"
    new_path = thoughts / "new.md"
    old_path.write_bytes(_note(body=b"# Old\n\nOld body.\r\n"))
    new_path.write_bytes(
        _note(confidence="inferred", verification="", body=b"# New\n\nNew body.\n")
    )
    return old_path, new_path


def _body_bytes(content: bytes) -> bytes:
    return content.split(b"\n---\n", 1)[1]


@pytest.mark.parametrize(
    "fields",
    [
        {
            "epistemic_confidence": "high",
            "epistemic_scope": "project:test",
            "epistemic_state": "active",
            "supersedes": [],
        },
        {
            "epistemic_confidence": "verified",
            "epistemic_scope": "",
            "epistemic_state": "active",
            "supersedes": [],
        },
        {
            "epistemic_confidence": "verified",
            "epistemic_scope": "project:test",
            "epistemic_state": "active",
            "supersedes": [],
        },
        {
            "epistemic_confidence": "inferred",
            "epistemic_scope": "project:test",
            "epistemic_state": "active",
            "supersedes": "old.md",
        },
    ],
)
def test_parse_epistemic_metadata_rejects_invalid_contract(
    fields: dict[str, object],
) -> None:
    with pytest.raises(EpistemicValidationError):
        parse_epistemic_metadata(fields)


def test_parse_epistemic_metadata_ignores_compiled_confidence() -> None:
    assert parse_epistemic_metadata({"confidence": "high", "status": "active"}) is None
    assert (
        parse_epistemic_metadata(
            {
                "confidence": "high",
                "supersedes": ["decision-42"],
                "superseded_by": "decision-43",
            }
        )
        is None
    )


def test_supersede_is_bidirectional_idempotent_and_preserves_bodies(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    old_body = _body_bytes(old_path.read_bytes())
    new_body = _body_bytes(new_path.read_bytes())

    first = supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")
    second = supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")

    old = parse_epistemic_note(old_path.read_bytes())
    new = parse_epistemic_note(new_path.read_bytes())
    assert first.changed is True
    assert second.changed is False
    assert old.state == "superseded"
    assert old.superseded_by == "thoughts/new.md"
    assert new.state == "active"
    assert new.supersedes == ("thoughts/old.md",)
    assert _body_bytes(old_path.read_bytes()) == old_body
    assert _body_bytes(new_path.read_bytes()) == new_body
    assert not (vault_path / ".locks/epistemic-supersession.json").exists()


def test_supersede_outputs_pass_the_manifest_contract(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)

    supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")

    manifest = load_manifest_for_vault(vault_path)
    for relative_path, path in (
        ("thoughts/old.md", old_path),
        ("thoughts/new.md", new_path),
    ):
        _route, missing, invalid = validate_document(
            relative_path,
            parse_frontmatter_bytes(path.read_bytes()),
            manifest,
        )
        assert missing == ()
        assert invalid == ()


def test_supersede_loads_manifest_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vault_path = tmp_path / "vault"
    _write_pair(vault_path)
    original_load = epistemic_memory._load_vault_manifest  # noqa: SLF001
    calls = 0

    def count_load(path: Path):
        nonlocal calls
        calls += 1
        return original_load(path)

    monkeypatch.setattr(epistemic_memory, "_load_vault_manifest", count_load)

    supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")

    assert calls == 1


def test_recovery_loads_manifest_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    old_before = old_path.read_bytes()
    new_before = new_path.read_bytes()
    plan = epistemic_memory._prepare_supersession_plan(  # noqa: SLF001
        "thoughts/old.md", "thoughts/new.md", old_before, new_before
    )
    with epistemic_memory.vault_write_lock(vault_path) as lock:
        epistemic_memory._write_journal(lock, plan.notes)  # noqa: SLF001
    original_load = epistemic_memory._load_vault_manifest  # noqa: SLF001
    calls = 0

    def count_load(path: Path):
        nonlocal calls
        calls += 1
        return original_load(path)

    monkeypatch.setattr(epistemic_memory, "_load_vault_manifest", count_load)

    assert recover_pending_supersession(vault_path) is True
    assert calls == 1


def test_supersede_preserves_crlf_header_body_and_custom_yaml(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    old_payload = _note(body=b"# Old\n\nPreserve body.\n").replace(
        b"\n", b"\r\n"
    )
    old_payload = old_payload.replace(
        b"epistemic_state: active\r\n",
        (
            b"custom: {owner: \"Ada\", priority: 1}\r\n"
            b"# state rationale\r\n"
            b"epistemic_state: \"active\" # current fact\r\n"
        ),
    )
    new_payload = _note(confidence="inferred", verification="").replace(
        b"\n", b"\r\n"
    )
    new_payload = new_payload.replace(
        b"supersedes: []\r\n",
        b"supersedes: [] # reciprocal history\r\n",
    )
    old_path.write_bytes(old_payload)
    new_path.write_bytes(new_payload)
    old_body = parse_frontmatter_bytes(old_payload).body
    new_body = parse_frontmatter_bytes(new_payload).body

    supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")

    old_after = old_path.read_bytes()
    new_after = new_path.read_bytes()
    assert parse_frontmatter_bytes(old_after).body == old_body
    assert parse_frontmatter_bytes(new_after).body == new_body
    assert b'custom: {owner: "Ada", priority: 1}\r\n' in old_after
    assert b'# state rationale\r\n' in old_after
    assert b'epistemic_state: "superseded" # current fact\r\n' in old_after
    assert b"supersedes: [thoughts/old.md] # reciprocal history\r\n" in new_after


@pytest.mark.parametrize(
    "empty_superseded_by",
    (b"superseded_by:", b'superseded_by: ""'),
)
def test_supersede_accepts_empty_superseded_by_before_state(
    tmp_path: Path,
    empty_superseded_by: bytes,
) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    old_payload = old_path.read_bytes().replace(
        b"epistemic_state: active\n",
        empty_superseded_by + b"\nepistemic_state: active\n",
    )
    old_path.write_bytes(old_payload)
    old_body = parse_frontmatter_bytes(old_payload).body

    supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")

    old_after = old_path.read_bytes()
    assert parse_frontmatter_bytes(old_after).body == old_body
    assert parse_epistemic_note(old_after).state == "superseded"
    assert parse_epistemic_note(old_after).superseded_by == "thoughts/new.md"


@pytest.mark.parametrize("invalid_path", ("thoughts/old.md", "thoughts/new.md"))
def test_supersede_rejects_manifest_invalid_input_before_journal(
    tmp_path: Path, invalid_path: str
) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    before = {old_path: old_path.read_bytes(), new_path: new_path.read_bytes()}
    target = vault_path / invalid_path
    target.write_bytes(
        target.read_bytes().replace(b'description: "Test epistemic note"\n', b"")
    )
    before[target] = target.read_bytes()

    with pytest.raises(EpistemicValidationError, match="manifest validation failed"):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")

    assert {path: path.read_bytes() for path in before} == before
    assert not (vault_path / ".locks").exists()


@pytest.mark.parametrize("raced_path", ("thoughts/old.md", "thoughts/new.md"))
def test_common_writer_cas_preserves_exact_gap_racer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raced_path: str
) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    target = vault_path / raced_path
    original_write = epistemic_memory.write_validated_vault_markdown
    raced = False

    def race_before_commit(vault, path, payload, **kwargs):  # noqa: ANN001, ANN003
        nonlocal raced
        if not raced and path == target:
            raced = True
            target.write_bytes(
                _note(
                    confidence="inferred",
                    verification="",
                    body=b"# External racer\n\nMust survive.\n",
                )
            )
        return original_write(vault, path, payload, **kwargs)

    monkeypatch.setattr(
        epistemic_memory, "write_validated_vault_markdown", race_before_commit
    )

    with pytest.raises(EpistemicConflictError, match="changed before supersession"):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")

    assert raced is True
    assert b"Must survive." in target.read_bytes()
    assert (vault_path / ".locks/epistemic-supersession.json").exists()


def test_recovery_rejects_manifest_invalid_journal_candidate(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    old_before = old_path.read_bytes()
    new_before = new_path.read_bytes()
    invalid_after = old_before.replace(b'description: "Test epistemic note"\n', b"")
    notes = (
        epistemic_memory._JournalNote(  # noqa: SLF001
            "thoughts/old.md",
            epistemic_memory._sha256(old_before),  # noqa: SLF001
            epistemic_memory._sha256(invalid_after),  # noqa: SLF001
            old_before,
            invalid_after,
        ),
        epistemic_memory._JournalNote(  # noqa: SLF001
            "thoughts/new.md",
            epistemic_memory._sha256(new_before),  # noqa: SLF001
            epistemic_memory._sha256(new_before),  # noqa: SLF001
            new_before,
            new_before,
        ),
    )
    with epistemic_memory.vault_write_lock(vault_path) as lock:
        epistemic_memory._write_journal(lock, notes)  # noqa: SLF001

    with pytest.raises(EpistemicValidationError, match="manifest validation failed"):
        recover_pending_supersession(vault_path)

    assert old_path.read_bytes() == old_before
    assert new_path.read_bytes() == new_before
    assert (vault_path / ".locks/epistemic-supersession.json").exists()


def test_supersede_rejects_duplicate_epistemic_fields(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    old_path, _new_path = _write_pair(vault_path)
    old_path.write_bytes(
        _note().replace(
            b"epistemic_state: active\n",
            b"epistemic_state: active\nepistemic_state: active\n",
        )
    )

    with pytest.raises(EpistemicValidationError, match="duplicate YAML key"):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")


def test_supersede_rejects_quoted_duplicate_epistemic_fields(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    old_path, _new_path = _write_pair(vault_path)
    old_path.write_bytes(
        _note().replace(
            b"epistemic_state: active\n",
            b'"epistemic_state": active\nepistemic_state: active\n',
        )
    )

    with pytest.raises(EpistemicValidationError, match="duplicate YAML key"):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")


def test_supersede_preserves_frontmatter_comments_and_accepts_block_form(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    old_path.write_bytes(
        _note().replace(
            b"epistemic_state: active\n",
            (
                b"# state rationale\n"
                b"epistemic_state: active # current fact\n"
                b"# next field\n"
            ),
        )
    )
    new_path.write_bytes(
        _note(confidence="inferred", verification="").replace(
            b"supersedes: []\n",
            b"supersedes: [] # reciprocal history\n",
        )
    )

    supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")

    assert (
        b'# state rationale\nepistemic_state: "superseded" # current fact'
        in old_path.read_bytes()
    )
    assert b"# next field" in old_path.read_bytes()
    assert (
        b"supersedes: [thoughts/old.md] # reciprocal history"
        in new_path.read_bytes()
    )

    blocked_vault = tmp_path / "blocked-vault"
    _old_path, blocked_new = _write_pair(blocked_vault)
    (blocked_vault / "thoughts/previous.md").write_bytes(
        _note(confidence="inferred", verification="")
    )
    blocked_new.write_bytes(
        _note(confidence="inferred", verification="").replace(
            b"supersedes: []\n",
            b"supersedes:\n  - thoughts/previous.md\n",
        )
    )
    body_before = _body_bytes(blocked_new.read_bytes())

    supersede_notes(blocked_vault, "thoughts/old.md", "thoughts/new.md")

    assert parse_epistemic_note(blocked_new.read_bytes()).supersedes == (
        "thoughts/previous.md",
        "thoughts/old.md",
    )
    assert _body_bytes(blocked_new.read_bytes()) == body_before


def test_supersede_rejects_scope_mismatch_and_graph_cycles(tmp_path: Path) -> None:
    scope_vault = tmp_path / "scope-vault"
    _old_path, scoped_new = _write_pair(scope_vault)
    scoped_new.write_bytes(
        _note(confidence="inferred", scope="project:other", verification="")
    )
    with pytest.raises(EpistemicValidationError, match="same epistemic_scope"):
        supersede_notes(scope_vault, "thoughts/old.md", "thoughts/new.md")

    cycle_vault = tmp_path / "cycle-vault"
    cycle_old, _cycle_new = _write_pair(cycle_vault)
    cycle_old.write_bytes(_note(supersedes="[thoughts/new.md]"))
    with pytest.raises(EpistemicValidationError, match="would create a cycle"):
        supersede_notes(cycle_vault, "thoughts/old.md", "thoughts/new.md")


def test_supersede_rejects_transitive_existing_reachability(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _old_path, new_path = _write_pair(vault_path)
    middle_path = vault_path / "thoughts/middle.md"
    middle_path.write_bytes(
        _note(confidence="inferred", verification="", supersedes="[thoughts/old.md]")
    )
    new_path.write_bytes(
        _note(confidence="inferred", verification="", supersedes="[thoughts/middle.md]")
    )

    with pytest.raises(EpistemicValidationError, match="already reaches"):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")


def test_supersede_rejects_traversal_and_symlink(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    old_path, _new_path = _write_pair(vault_path)

    with pytest.raises(EpistemicValidationError):
        supersede_notes(vault_path, "../outside.md", "thoughts/new.md")

    linked = vault_path / "thoughts" / "linked.md"
    linked.symlink_to(old_path)
    with pytest.raises(EpistemicValidationError):
        supersede_notes(vault_path, "thoughts/linked.md", "thoughts/new.md")


def test_recover_rolls_forward_after_second_atomic_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    original_replace = epistemic_memory._replace_note

    def fail_new(lock, relative_path, payload, expected_hash, *, manifest):  # noqa: ANN001
        if relative_path == "thoughts/new.md":
            raise OSError("simulated crash before successor replacement")
        original_replace(
            lock, relative_path, payload, expected_hash, manifest=manifest
        )

    monkeypatch.setattr(epistemic_memory, "_replace_note", fail_new)
    with pytest.raises(OSError, match="simulated crash"):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")
    journal = vault_path / ".locks/epistemic-supersession.json"
    assert journal.exists()
    assert parse_epistemic_note(old_path.read_bytes()).state == "superseded"

    monkeypatch.setattr(epistemic_memory, "_replace_note", original_replace)
    assert recover_pending_supersession(vault_path) is True
    assert parse_epistemic_note(new_path.read_bytes()).supersedes == (
        "thoughts/old.md",
    )
    assert not journal.exists()


def test_recovery_stops_on_hash_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _old_path, new_path = _write_pair(vault_path)
    original_replace = epistemic_memory._replace_note

    def fail_new(lock, relative_path, payload, expected_hash, *, manifest):  # noqa: ANN001
        if relative_path == "thoughts/new.md":
            raise OSError("simulated crash")
        original_replace(
            lock, relative_path, payload, expected_hash, manifest=manifest
        )

    monkeypatch.setattr(epistemic_memory, "_replace_note", fail_new)
    with pytest.raises(OSError):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")
    monkeypatch.setattr(epistemic_memory, "_replace_note", original_replace)
    new_path.write_bytes(_note(body=b"# New\n\nExternally changed.\n"))

    with pytest.raises(EpistemicConflictError, match="differs from journal hashes"):
        recover_pending_supersession(vault_path)
    assert (vault_path / ".locks/epistemic-supersession.json").exists()


def test_final_descriptor_cas_rejects_non_cooperative_toctou_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    old_path, _new_path = _write_pair(vault_path)
    original_replace = epistemic_memory._replace_note

    def change_before_replace(  # noqa: ANN001
        lock, relative_path, payload, expected_hash, *, manifest
    ):
        if relative_path == "thoughts/old.md":
            old_path.write_bytes(
                _note(body=b"# Old\n\nChanged outside cooperative lock.\n")
            )
        original_replace(
            lock, relative_path, payload, expected_hash, manifest=manifest
        )

    monkeypatch.setattr(epistemic_memory, "_replace_note", change_before_replace)

    with pytest.raises(EpistemicConflictError, match="changed before supersession"):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")
    assert b"Changed outside cooperative lock" in old_path.read_bytes()
    assert (vault_path / ".locks/epistemic-supersession.json").exists()


def test_final_descriptor_replace_rejects_symlink_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    old_path, new_path = _write_pair(vault_path)
    original_replace = epistemic_memory._replace_note

    def swap_to_symlink(lock, relative_path, payload, expected_hash, *, manifest):  # noqa: ANN001
        if relative_path == "thoughts/old.md":
            old_path.unlink()
            old_path.symlink_to(new_path)
        original_replace(
            lock, relative_path, payload, expected_hash, manifest=manifest
        )

    monkeypatch.setattr(epistemic_memory, "_replace_note", swap_to_symlink)

    with pytest.raises(EpistemicValidationError, match="vault writer"):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")
    assert old_path.is_symlink()
    assert (vault_path / ".locks/epistemic-supersession.json").exists()


def test_memory_engine_exposes_supersession_cli(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_pair(vault_path)
    memory_engine = _load_memory_engine()
    monkeypatch.chdir(vault_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory-engine.py", "supersede", "thoughts/old.md", "thoughts/new.md"],
    )

    memory_engine.main()

    assert (
        "supersession updated: thoughts/old.md -> thoughts/new.md"
        in capsys.readouterr().out
    )


def test_memory_engine_resolves_project_root_to_its_vault(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    vault_path = project_path / "vault"
    _write_pair(vault_path)
    memory_engine = _load_memory_engine()
    monkeypatch.chdir(project_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory-engine.py",
            "supersede",
            "thoughts/old.md",
            "thoughts/new.md",
            "--vault",
            str(project_path),
        ],
    )

    memory_engine.main()

    assert "supersession updated" in capsys.readouterr().out
    assert (vault_path / ".locks/vault-write.lock").exists()
    assert not (project_path / ".locks").exists()


def test_memory_engine_rejects_positional_vault_with_vault_option(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    memory_engine = _load_memory_engine()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory-engine.py",
            "recover-supersession",
            ".",
            "--vault",
            str(vault_path),
        ],
    )

    with pytest.raises(SystemExit, match="1"):
        memory_engine.main()
    assert "either positional vault directory or --vault" in capsys.readouterr().out


def test_journal_contains_hashes_and_payloads_after_interruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _old_path, new_path = _write_pair(vault_path)
    original_replace = epistemic_memory._replace_note

    def stop_new(lock, relative_path, payload, expected_hash, *, manifest):  # noqa: ANN001
        if relative_path == "thoughts/new.md":
            raise OSError("stop")
        original_replace(
            lock, relative_path, payload, expected_hash, manifest=manifest
        )

    monkeypatch.setattr(
        epistemic_memory,
        "_replace_note",
        stop_new,
    )

    with pytest.raises(OSError, match="stop"):
        supersede_notes(vault_path, "thoughts/old.md", "thoughts/new.md")
    payload = json.loads(
        (vault_path / ".locks/epistemic-supersession.json").read_text(encoding="utf-8")
    )
    assert payload["version"] == 1
    assert all(
        "before_sha256" in item and "after_payload_b64" in item
        for item in payload["notes"]
    )
