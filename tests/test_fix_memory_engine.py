"""Regression tests for the audit-2026-09-03 G6 memory-engine fixes.

Covers:
- ``cmd_decay`` crashing on a relative ``"."`` target (the shape
  ``processor.py`` uses for the nightly decay pass with ``cwd=vault``).
- ``find_cards`` no longer walking into hidden runtime directories.
- ``main()``'s ``touch`` dispatch honoring ``--dry-run`` and loading config
  from the vault root instead of a nested note's own folder.
- The ``access_count`` access-based memory strength field.
- ``fix_links.build_stem_index`` no longer double-indexing root-level notes.
- ``connect_orphans.HUB_MAP`` no longer pointing at hub pages that do not
  exist anywhere in the vault template or the generated MOC set.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from _paths import SKILLS_TEMPLATE_ROOT
from conftest import _load_memory_engine, _write_vault_manifest


def _load_vault_health_script(script_name: str):
    script_path = SKILLS_TEMPLATE_ROOT / "vault-health/scripts" / f"{script_name}.py"
    spec = importlib.util.spec_from_file_location(script_name, script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _flat_context_note(title: str, body: str) -> str:
    return (
        "---\n"
        "type: note\n"
        f"description: {title}\n"
        "last_accessed: 2026-07-29\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def _default_note(*, tier: str = "cold", relevance: str = "0.2") -> str:
    return (
        "---\n"
        "type: note\n"
        "last_accessed: 2020-01-01\n"
        f"relevance: {relevance}\n"
        f"tier: {tier}\n"
        "---\n"
        "# Note\n\nBody.\n"
    )


# ─── A) cmd_decay with a relative "." target ─────────────────────


def test_cmd_decay_accepts_relative_dot_target_from_vault_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces ``cd vault && memory-engine.py decay . --dry-run`` crashing.

    ``processor.py`` runs the nightly decay this exact way (``decay "."``
    with ``cwd=vault``); before the fix, ``cmd_decay`` resolved
    ``vault_root`` to an absolute path but still called
    ``card.relative_to(target_dir)`` with the original relative ``"."``,
    raising ``ValueError`` on the first card.
    """
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    note_path = vault_path / "notes" / "decay.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(_default_note(), encoding="utf-8")
    memory_engine = _load_memory_engine()
    monkeypatch.chdir(vault_path)

    memory_engine.cmd_decay(Path("."), memory_engine.DEFAULT_CONFIG)

    fields, _, _ = memory_engine.parse_frontmatter(
        note_path.read_text(encoding="utf-8")
    )
    # 2020-01-01 is far past the "cold" threshold, so decay correctly
    # recomputes the tier as "archive" -- the point of this test is that
    # the write succeeds at all instead of raising ValueError.
    assert fields.get("tier") == "archive"
    assert "relevance" in fields


# ─── C) find_cards must skip hidden directories ──────────────────


def test_find_cards_skips_hidden_directories(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    note_path = vault_path / "thoughts" / "note.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("# Note\n", encoding="utf-8")
    for hidden_dir in (".session", ".trash", ".graph", ".compiled", ".locks"):
        hidden_note = vault_path / hidden_dir / "cache.md"
        hidden_note.parent.mkdir(parents=True)
        hidden_note.write_text("# Cache\n", encoding="utf-8")

    memory_engine = _load_memory_engine()

    assert memory_engine.find_cards(vault_path, memory_engine.DEFAULT_CONFIG) == [
        note_path
    ]


# ─── B) main()'s touch dispatch: --dry-run and vault-root config ─


def test_main_touch_dry_run_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    memory_engine = _load_memory_engine()
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    note_path = vault_path / "notes" / "touch.md"
    note_path.parent.mkdir(parents=True)
    original = _default_note()
    note_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["memory-engine.py", "touch", str(note_path), "--dry-run"]
    )

    memory_engine.main()

    assert note_path.read_text(encoding="utf-8") == original
    assert "[dry]" in capsys.readouterr().out


def test_main_touch_loads_config_from_vault_root_for_nested_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``touch`` on a nested note must use the vault's config, not DEFAULT_CONFIG.

    Before the fix, ``main()`` loaded config from ``Path(target).parent``
    (the note's own folder), which almost never holds
    ``.memory-config.json`` -- so any nested note silently fell back to
    ``DEFAULT_CONFIG``. The vault here sets a much higher ``decay_rate``
    and much lower tier thresholds than the defaults, so the resulting
    relevance value only matches if the vault's own config was loaded.
    """
    memory_engine = _load_memory_engine()
    vault_path = tmp_path / "vault"
    vault_path.mkdir(parents=True)
    _write_vault_manifest(vault_path)
    (vault_path / ".memory-config.json").write_text(
        json.dumps(
            {
                "tiers": {"active": 2, "warm": 4, "cold": 8},
                "decay_rate": 0.5,
                "relevance_floor": 0.1,
            }
        ),
        encoding="utf-8",
    )
    note_path = vault_path / "notes" / "nested.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(_default_note(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["memory-engine.py", "touch", str(note_path)])

    memory_engine.main()

    fields, _, _ = memory_engine.parse_frontmatter(
        note_path.read_text(encoding="utf-8")
    )
    # cold -> warm promotion, target_days = (active + warm) // 2.
    # DEFAULT_CONFIG (7/21/60) gives target_days=14, decay_rate=0.015 ->
    # relevance ~= 0.83. The vault's own config (2/4/8, decay_rate=0.5)
    # gives target_days=3 -> relevance ~= 0.30. Only the second value is
    # reachable if `touch` loaded the vault's config.
    assert fields.get("tier") == "warm"
    assert float(fields.get("relevance", "0")) == pytest.approx(0.3, abs=0.02)
    assert fields.get("access_count") == "1"


# ─── D) access_count strength factor ─────────────────────────────


def test_touch_increments_access_count_across_repeated_touches(
    tmp_path: Path,
) -> None:
    memory_engine = _load_memory_engine()
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    note_path = vault_path / "notes" / "sticky.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(_default_note(), encoding="utf-8")

    memory_engine.cmd_touch(str(note_path), memory_engine.DEFAULT_CONFIG)
    fields, _, _ = memory_engine.parse_frontmatter(
        note_path.read_text(encoding="utf-8")
    )
    assert fields.get("access_count") == "1"

    memory_engine.cmd_touch(str(note_path), memory_engine.DEFAULT_CONFIG)
    fields, _, _ = memory_engine.parse_frontmatter(
        note_path.read_text(encoding="utf-8")
    )
    assert fields.get("access_count") == "2"


def test_calc_relevance_and_tier_slow_down_with_access_count() -> None:
    memory_engine = _load_memory_engine()

    baseline = memory_engine.calc_relevance(30, 0.015, 0.1)
    strengthened = memory_engine.calc_relevance(30, 0.015, 0.1, access_count=5)
    assert strengthened > baseline

    # Backward compatible default (access_count=0) is unchanged.
    assert memory_engine.calc_relevance(30, 0.015, 0.1, 0) == baseline

    tiers = {"active": 7, "warm": 21, "cold": 60}
    assert memory_engine.calc_tier(40, tiers) == "cold"
    assert memory_engine.calc_tier(40, tiers, access_count=10) == "warm"
    assert memory_engine.calc_tier(40, tiers, "", 0) == "cold"


def test_cmd_decay_reads_access_count_and_slows_relevance_loss(
    tmp_path: Path,
) -> None:
    memory_engine = _load_memory_engine()
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    # 40 days back keeps both relevance values well above the 0.1 floor, so
    # the access-count strength factor actually shows up after rounding.
    stale_date = (memory_engine.TODAY - timedelta(days=40)).isoformat()

    def _make_note(name: str, access_count: int) -> Path:
        note_path = vault_path / "notes" / name
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            (
                "---\n"
                "type: note\n"
                f"last_accessed: {stale_date}\n"
                "relevance: 0.9\n"
                "tier: active\n"
                f"access_count: {access_count}\n"
                "---\n"
                "# Note\n\nBody.\n"
            ),
            encoding="utf-8",
        )
        return note_path

    untouched = _make_note("untouched.md", 0)
    touched = _make_note("touched.md", 20)

    memory_engine.cmd_decay(vault_path, memory_engine.DEFAULT_CONFIG)

    untouched_fields, _, _ = memory_engine.parse_frontmatter(
        untouched.read_text(encoding="utf-8")
    )
    touched_fields, _, _ = memory_engine.parse_frontmatter(
        touched.read_text(encoding="utf-8")
    )
    assert float(touched_fields["relevance"]) > float(untouched_fields["relevance"])


# ─── E) fix_links: root-level notes must not double-index ────────


def test_fix_links_resolves_root_level_stem_without_ambiguity(
    tmp_path: Path,
) -> None:
    """A root-level note's stem equaled its bare path, so it was indexed twice.

    ``build_stem_index`` used to append both the file's ``stem`` key and its
    suffix-stripped relative-path key without deduping; for a root-level
    note (e.g. ``MEMORY.md``) those two keys are identical, so the same file
    landed in the list twice. ``_unique_stem_match`` then saw two entries
    and treated a genuinely unique match as ambiguous, so
    ``[[old/MEMORY]]`` was removed instead of repaired to ``[[MEMORY]]``.
    """
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    (vault_path / "thoughts").mkdir()
    (vault_path / "thoughts" / "hello.md").write_text("# Hello\n", encoding="utf-8")

    module = _load_vault_health_script("fix_links")
    module.VAULT_PATH = vault_path
    stem_index = module.build_stem_index()

    assert stem_index["MEMORY"] == ["MEMORY.md"]
    assert module.suggest_fix(
        "daily/2026-09-03", "old/MEMORY", stem_index
    ) == ("MEMORY", "replace")
    assert module.suggest_fix(
        "daily/2026-09-03", "old/hello", stem_index
    ) == ("thoughts/hello", "replace")


# ─── F) connect_orphans: no more broken hub links ────────────────


def test_connect_orphans_maps_plaud_and_summaries_to_an_existing_hub(
    tmp_path: Path,
) -> None:
    """imports/plaud/notes/ and summaries/ must link to a hub that exists.

    ``HUB_MAP`` used to point these two categories at ``MOC/MOC-plaud`` and
    ``MOC/MOC-weekly``, neither of which ships in the vault template nor is
    generated by ``generate_moc.py`` -- so ``connect_orphans.py`` created a
    broken link that ``fix_links.py`` then removed on the next nightly pass
    (ping-pong). They must now resolve to a hub that is actually on disk.
    """
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path / "MOC").mkdir(parents=True)
    (vault_path / "MOC" / "index.md").write_text("# Index\n", encoding="utf-8")
    (vault_path / "imports" / "plaud" / "notes").mkdir(parents=True)
    (vault_path / "imports" / "plaud" / "notes" / "rec1.md").write_text(
        _flat_context_note("Rec1", "Meeting note."), encoding="utf-8"
    )
    (vault_path / "summaries").mkdir(parents=True)
    (vault_path / "summaries" / "weekly.md").write_text(
        _flat_context_note("Weekly", "Summary."), encoding="utf-8"
    )

    module = _load_vault_health_script("connect_orphans")
    graph = {
        "orphans": ["imports/plaud/notes/rec1", "summaries/weekly"],
        "weakly_connected": [],
    }

    stats = module.connect_targets(vault_path, graph, apply=True)

    assert stats["connected"] == 2
    assert stats["missing"] == 0
    assert stats["errors"] == 0
    rec1 = (vault_path / "imports/plaud/notes/rec1.md").read_text(encoding="utf-8")
    weekly = (vault_path / "summaries/weekly.md").read_text(encoding="utf-8")
    assert "[[MOC/index]]" in rec1
    assert "[[MOC/index]]" in weekly
    assert "MOC-plaud" not in rec1
    assert "MOC-weekly" not in weekly
    for hub_path in ("imports/plaud/notes/", "summaries/"):
        hub_link = module.get_hub_for_path(f"{hub_path}anything")
        assert hub_link is not None
        note_name = hub_link.strip("[]") + ".md"
        assert (vault_path / note_name).exists(), (
            f"{hub_path} hub {hub_link} does not exist on disk"
        )
