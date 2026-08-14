from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from _paths import SKILLS_TEMPLATE_ROOT
from conftest import _write_vault_manifest

from d_brain.services.frontmatter import (
    parse_frontmatter_bytes,
    read_vault_file_bytes,
    validate_document,
    write_validated_vault_markdown,
)


def _load_vault_health_script(script_name: str):
    script_path = SKILLS_TEMPLATE_ROOT / "vault-health/scripts" / f"{script_name}.py"
    spec = importlib.util.spec_from_file_location(script_name, script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_graph_builder_script(script_name: str):
    script_path = SKILLS_TEMPLATE_ROOT / "graph-builder/scripts" / f"{script_name}.py"
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


def _thought_card_note(title: str, body: str) -> str:
    return (
        "---\n"
        "type: note\n"
        f"description: {title}\n"
        "tags: [idea, test]\n"
        "status: active\n"
        "created: 2026-07-29\n"
        "updated: 2026-07-29\n"
        "last_accessed: 2026-07-29\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def test_generate_moc_refreshes_current_mocs_and_preserves_manual_sections(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path / "MOC").mkdir(parents=True)
    (vault_path / "thoughts" / "ideas").mkdir(parents=True)

    (vault_path / "thoughts" / "ideas" / "2026-04-04-new-direction.md").write_text(
        "# New Direction\n\nBody.\n",
        encoding="utf-8",
    )
    (vault_path / "MOC" / "MOC-ideas.md").write_text(
        (
            "---\ntype: note\n---\n# Ideas\n\nCustom intro.\n\n## Recent\n\n"
            "- [[old]]\n\n## By Topic\n\nKeep this section.\n"
        ),
        encoding="utf-8",
    )

    module = _load_vault_health_script("generate_moc")
    written = module.refresh_thought_mocs(vault_path)

    content = (vault_path / "MOC" / "MOC-ideas.md").read_text(encoding="utf-8")
    assert vault_path / "MOC" / "MOC-ideas.md" in written
    assert parse_frontmatter_bytes(content.encode("utf-8")).fields["type"] == "index"
    assert 'description: "Map of Content: Ideas"' in content
    assert "Custom intro." in content
    assert "[[thoughts/ideas/2026-04-04-new-direction|New Direction]]" in content
    assert "## By Topic" in content
    assert "Keep this section." in content
    assert not (vault_path / "MOC" / "MOC-business.md").exists()


def test_generate_moc_creates_valid_indexes_and_is_idempotent(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    _write_vault_manifest(vault_path)
    module = _load_vault_health_script("generate_moc")

    managed_paths = module.refresh_thought_mocs(vault_path)
    manifest = module.load_manifest_for_vault(vault_path)

    assert len(managed_paths) == 4
    first_run = {
        path: read_vault_file_bytes(vault_path, path) for path in managed_paths
    }
    for path, content in first_run.items():
        document = parse_frontmatter_bytes(content)
        route, missing, invalid = validate_document(
            path.relative_to(vault_path).as_posix(), document, manifest
        )
        assert route.name == "index"
        assert missing == ()
        assert invalid == ()

    assert module.refresh_thought_mocs(vault_path) == managed_paths
    assert {
        path: read_vault_file_bytes(vault_path, path) for path in managed_paths
    } == first_run


def test_connect_orphans_resolves_note_keys_without_md_suffix(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path / "business").mkdir(parents=True)
    (vault_path / "thoughts" / "ideas").mkdir(parents=True)
    (vault_path / "business" / "crm.md").write_text(
        _flat_context_note("CRM", "Business context."),
        encoding="utf-8",
    )
    (vault_path / "thoughts" / "ideas" / "idea.md").write_text(
        _thought_card_note("Idea", "Thought context."),
        encoding="utf-8",
    )

    graph = {
        "orphans": ["business/crm", "thoughts/ideas/idea"],
        "weakly_connected": [],
    }

    module = _load_vault_health_script("connect_orphans")
    stats = module.connect_targets(vault_path, graph, apply=True)

    business_content = (vault_path / "business" / "crm.md").read_text(encoding="utf-8")
    idea_content = (vault_path / "thoughts" / "ideas" / "idea.md").read_text(
        encoding="utf-8"
    )
    assert stats["connected"] == 2
    assert stats["missing"] == 0
    assert "[[business/_index]]" in business_content
    assert "[[MOC/MOC-ideas]]" in idea_content


def test_connect_orphans_rejects_cas_conflict_without_overwriting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    source_path = vault_path / "business" / "crm.md"
    source_path.parent.mkdir(parents=True)
    original = _flat_context_note("CRM", "Original body.")
    concurrent = _flat_context_note("CRM", "Concurrent body.")
    source_path.write_text(original, encoding="utf-8")
    module = _load_vault_health_script("connect_orphans")
    real_write = module.write_validated_vault_markdown

    def mutate_before_write(*args, **kwargs):
        source_path.write_text(concurrent, encoding="utf-8")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(module, "write_validated_vault_markdown", mutate_before_write)
    stats = module.connect_targets(
        vault_path,
        {"orphans": ["business/crm"], "weakly_connected": []},
        apply=True,
    )

    assert stats["connected"] == 0
    assert stats["conflicts"] == 1
    assert source_path.read_text(encoding="utf-8") == concurrent


def test_connect_orphans_rejects_symlink_swap_without_external_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    source_path = vault_path / "business" / "crm.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        _flat_context_note("CRM", "Original body."), encoding="utf-8"
    )
    external_path = tmp_path / "external.md"
    external_content = _flat_context_note("External", "Do not touch.")
    external_path.write_text(external_content, encoding="utf-8")
    module = _load_vault_health_script("connect_orphans")
    real_write = module.write_validated_vault_markdown

    def swap_before_write(*args, **kwargs):
        source_path.unlink()
        source_path.symlink_to(external_path)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(module, "write_validated_vault_markdown", swap_before_write)
    stats = module.connect_targets(
        vault_path,
        {"orphans": ["business/crm"], "weakly_connected": []},
        apply=True,
    )

    assert stats["connected"] == 0
    assert stats["errors"] == 1
    assert external_path.read_text(encoding="utf-8") == external_content


def test_fix_links_collapses_legacy_paths_and_removes_repo_links(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "business").mkdir(parents=True)
    (vault_path / "goals").mkdir(parents=True)
    (vault_path / "business" / "crm.md").write_text("# CRM\n", encoding="utf-8")
    (vault_path / "goals" / "1-yearly-2026.md").write_text(
        "# Goals\n",
        encoding="utf-8",
    )

    module = _load_vault_health_script("fix_links")
    module.VAULT_PATH = vault_path
    stem_index = module.build_stem_index()

    assert module.suggest_fix(
        "daily/2026-04-04",
        "business/crm/acme-corp",
        stem_index,
    ) == ("business/crm", "replace")
    assert module.suggest_fix(
        "daily/2026-04-04",
        "vault/goals/1-yearly-2026",
        stem_index,
    ) == (None, "remove")


def test_fix_links_never_retargets_a_link_into_a_hidden_runtime_cache(
    tmp_path: Path,
) -> None:
    """A deleted page's snapshot copy must not become its replacement target.

    ``.session/compile-enrich/snapshots/<hash>/blobs/compiled/**`` holds a
    verbatim copy of every compiled page a nightly pass was about to
    rewrite. Once the real page is gone from ``compiled/``, that copy is
    the only file left carrying its stem -- so an index that walks hidden
    folders makes ``_unique_stem_match`` look unique and points the
    owner's link at a scratch cache ``SNAPSHOT_RETENTION_DAYS`` deletes 14
    days later. ``analyze.py`` then files such a target under
    ``hidden-path`` and never reports it, so nothing downstream notices.
    """
    vault_path = tmp_path / "vault"
    snapshot_blobs = (
        vault_path
        / ".session/compile-enrich/snapshots/5b42d6fc/blobs/compiled/decisions"
    )
    snapshot_blobs.mkdir(parents=True)
    (snapshot_blobs / "retired-decision.md").write_text(
        "# Retired decision\n",
        encoding="utf-8",
    )
    (vault_path / "compiled" / "decisions").mkdir(parents=True)
    (vault_path / "compiled" / "decisions" / "live-decision.md").write_text(
        "# Live decision\n",
        encoding="utf-8",
    )

    module = _load_vault_health_script("fix_links")
    module.VAULT_PATH = vault_path
    stem_index = module.build_stem_index()

    assert not [key for key in stem_index if key.startswith(".session")]
    assert module.suggest_fix(
        "daily/2026-08-14",
        "compiled/decisions/retired-decision",
        stem_index,
    ) == (None, "remove")
    # A page that still exists keeps being repaired as before.
    assert module.suggest_fix(
        "daily/2026-08-14",
        "compiled/archive/live-decision",
        stem_index,
    ) == ("compiled/decisions/live-decision", "replace")


def test_add_descriptions_skips_hidden_runtime_directories(tmp_path: Path) -> None:
    """The same hidden-folder blind spot must not let writes reach snapshots.

    ``main()`` rewrites the frontmatter of every file it collects, and the
    snapshot blobs are exactly the bytes the compile layer diffs the next
    pass against.
    """
    vault_path = tmp_path / "vault"
    snapshot_blobs = (
        vault_path / ".session/compile-enrich/snapshots/5b42d6fc/blobs/compiled"
    )
    snapshot_blobs.mkdir(parents=True)
    (snapshot_blobs / "retired-decision.md").write_text("# Retired\n", encoding="utf-8")
    (vault_path / "thoughts").mkdir(parents=True)
    (vault_path / "thoughts" / "idea.md").write_text("# Idea\n", encoding="utf-8")

    module = _load_vault_health_script("add_descriptions")
    module.VAULT_PATH = vault_path

    collected = module.collect_candidate_files()
    assert [path.relative_to(vault_path).as_posix() for path in collected] == [
        "thoughts/idea.md"
    ]


def test_fix_links_leaves_human_zone_untouched(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path / "business").mkdir(parents=True)
    source_path = vault_path / "business" / "crm.md"
    source_path.write_text(
        _flat_context_note(
            "CRM",
            "See [[vault/obsolete]].\n\n"
            "<!-- human:start -->\n"
            "Keep [[legacy/manual-note]] exactly as I wrote it.\n"
            "<!-- human:end -->\n",
        ),
        encoding="utf-8",
    )

    module = _load_vault_health_script("fix_links")
    module.VAULT_PATH = vault_path

    # Sandbox note: the real write_validated_vault_markdown needs a kernel
    # privilege this test environment does not grant (same root cause as
    # the many pre-existing sandboxed-write failures in this suite). Swap
    # in a plain write so this test exercises the actual defect under
    # test -- whether the human zone survives _write_repair's transform --
    # without depending on that unrelated sandbox restriction.
    def direct_write(vault_path, file_path, candidate, *, manifest, **kwargs):
        del manifest, kwargs
        (vault_path / file_path).write_bytes(candidate)

    monkeypatch.setattr(module, "write_validated_vault_markdown", direct_write)

    stats = module.fix_targets(
        vault_path,
        {
            "broken_links": [
                {"source": "business/crm", "target": "vault/obsolete"},
                {"source": "business/crm", "target": "legacy/manual-note"},
            ]
        },
        apply=True,
        stem_index={},
    )

    content = source_path.read_text(encoding="utf-8")
    assert "[[vault/obsolete]]" not in content
    assert "Keep [[legacy/manual-note]] exactly as I wrote it." in content
    assert stats["removed"] == 1


def test_fix_links_leaves_every_human_zone_untouched_with_multiple_pairs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Reproduces the code-review report: a file with two human-zone marker
    pairs used to have everything past the *first* pair processed by the
    normal link-repair regex, silently turning the link inside the
    *second* zone into bare text. With the fix, `_protect_human_zone`
    cannot tell which START pairs with which END, so it fails closed and
    leaves the whole file untouched -- both zones, and even the broken
    link outside any zone.
    """
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path / "business").mkdir(parents=True)
    source_path = vault_path / "business" / "crm.md"
    original = _flat_context_note(
        "CRM",
        "See [[vault/obsolete]].\n\n"
        "<!-- human:start -->\n"
        "Keep [[legacy/manual-note-1]] exactly as I wrote it.\n"
        "<!-- human:end -->\n\n"
        "More context in between the two zones.\n\n"
        "<!-- human:start -->\n"
        "Keep [[legacy/manual-note-2]] exactly as I wrote it too.\n"
        "<!-- human:end -->\n",
    )
    source_path.write_text(original, encoding="utf-8")

    module = _load_vault_health_script("fix_links")
    module.VAULT_PATH = vault_path

    # Sandbox note: see test_fix_links_leaves_human_zone_untouched above.
    def direct_write(vault_path, file_path, candidate, *, manifest, **kwargs):
        del manifest, kwargs
        (vault_path / file_path).write_bytes(candidate)

    monkeypatch.setattr(module, "write_validated_vault_markdown", direct_write)

    stats = module.fix_targets(
        vault_path,
        {
            "broken_links": [
                {"source": "business/crm", "target": "vault/obsolete"},
                {"source": "business/crm", "target": "legacy/manual-note-1"},
                {"source": "business/crm", "target": "legacy/manual-note-2"},
            ]
        },
        apply=True,
        stem_index={},
    )

    content = source_path.read_text(encoding="utf-8")
    assert content == original
    assert "Keep [[legacy/manual-note-2]] exactly as I wrote it too." in content
    assert stats["removed"] == 0


def test_protect_human_zone_multiple_pairs_leaves_content_untouched() -> None:
    module = _load_vault_health_script("fix_links")
    content = (
        "See [[vault/obsolete]].\n\n"
        f"{module.HUMAN_ZONE_START}\n"
        "Keep [[legacy/manual-note-1]] exactly.\n"
        f"{module.HUMAN_ZONE_END}\n\n"
        "More text with [[vault/obsolete]] again.\n\n"
        f"{module.HUMAN_ZONE_START}\n"
        "Keep [[legacy/manual-note-2]] exactly too.\n"
        f"{module.HUMAN_ZONE_END}\n"
    )

    result = module._protect_human_zone(
        content, lambda text: text.replace("[[vault/obsolete]]", "REMOVED")
    )

    assert result == content


def test_protect_human_zone_single_start_marker_fails_closed() -> None:
    """A lone START with no matching END is exactly as ambiguous as two
    pairs -- there's no way to tell where the zone would have ended, so
    the file is left untouched rather than treating the marker as if it
    were not there."""
    module = _load_vault_health_script("fix_links")
    content = f"before {module.HUMAN_ZONE_START} middle [[link]] end"

    result = module._protect_human_zone(
        content, lambda text: text.replace("[[link]]", "REMOVED")
    )

    assert result == content


def test_protect_human_zone_single_end_marker_fails_closed() -> None:
    module = _load_vault_health_script("fix_links")
    content = f"before [[link]] middle {module.HUMAN_ZONE_END} end"

    result = module._protect_human_zone(
        content, lambda text: text.replace("[[link]]", "REMOVED")
    )

    assert result == content


def test_protect_human_zone_reversed_single_pair_fails_closed() -> None:
    module = _load_vault_health_script("fix_links")
    content = (
        f"before {module.HUMAN_ZONE_END} middle [[link]] middle2 "
        f"{module.HUMAN_ZONE_START} end"
    )

    result = module._protect_human_zone(
        content, lambda text: text.replace("[[link]]", "REMOVED")
    )

    assert result == content


def test_protect_human_zone_no_markers_transforms_whole_file() -> None:
    module = _load_vault_health_script("fix_links")
    content = "before [[link]] end"

    result = module._protect_human_zone(
        content, lambda text: text.replace("[[link]]", "REMOVED")
    )

    assert result == content.replace("[[link]]", "REMOVED")


def test_protect_human_zone_zero_markers_but_corrupted_evidence_fails_closed() -> None:
    """Zero exact markers usually means "an ordinary vault file with no zone
    to protect" -- but not on a page that still carries the ``## Owner
    Notes`` heading compiled briefings always render together with the
    markers. There the markers were lost to some external edit, and running
    the transform over the whole file would rewrite the owner's own text.
    """
    module = _load_vault_health_script("fix_links")
    content = "# Aurora\n\n## Owner Notes\n\nМои заметки про [[link]].\n"

    result = module._protect_human_zone(
        content, lambda text: text.replace("[[link]]", "REMOVED")
    )

    assert result == content


def test_protect_human_zone_counts_markers_inside_code_blocks_too() -> None:
    """`_protect_human_zone` does a plain byte-for-byte scan with no
    markdown awareness, so example markers written inside a fenced code
    block count exactly like real ones. Here that produces two START/END
    pairs overall (one real zone plus one written as documentation inside
    a code fence), which must be treated the same as any other
    multiple-pair file: left untouched rather than guessed at.
    """
    module = _load_vault_health_script("fix_links")
    content = (
        "```\n"
        f"{module.HUMAN_ZONE_START}\n"
        f"{module.HUMAN_ZONE_END}\n"
        "```\n\n"
        f"{module.HUMAN_ZONE_START}\n"
        "Keep [[legacy/manual-note]] as-is.\n"
        f"{module.HUMAN_ZONE_END}\n"
    )

    result = module._protect_human_zone(
        content, lambda text: text.replace("[[legacy/manual-note]]", "REMOVED")
    )

    assert result == content


def test_protect_human_zone_pair_without_trailing_newline() -> None:
    module = _load_vault_health_script("fix_links")
    content = (
        "See [[vault/obsolete]].\n\n"
        f"{module.HUMAN_ZONE_START}\n"
        "Keep [[legacy/manual-note]] exactly."
        f"{module.HUMAN_ZONE_END}"
    )
    assert not content.endswith("\n")

    result = module._protect_human_zone(
        content, lambda text: text.replace("[[vault/obsolete]]", "REMOVED")
    )

    assert result.endswith(module.HUMAN_ZONE_END)
    assert "Keep [[legacy/manual-note]] exactly." in result
    assert "[[vault/obsolete]]" not in result
    assert "REMOVED" in result


def test_fix_links_rejects_cas_conflict_without_overwriting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    source_path = vault_path / "business" / "crm.md"
    source_path.parent.mkdir(parents=True)
    original = _flat_context_note("CRM", "See [[vault/obsolete]].")
    concurrent = _flat_context_note("CRM", "Concurrent [[vault/obsolete]].")
    source_path.write_text(original, encoding="utf-8")
    module = _load_vault_health_script("fix_links")
    module.VAULT_PATH = vault_path
    real_write = module.write_validated_vault_markdown

    def mutate_before_write(*args, **kwargs):
        source_path.write_text(concurrent, encoding="utf-8")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(module, "write_validated_vault_markdown", mutate_before_write)
    stats = module.fix_targets(
        vault_path,
        {"broken_links": [{"source": "business/crm", "target": "vault/obsolete"}]},
        apply=True,
        stem_index={},
    )

    assert stats["removed"] == 0
    assert stats["conflicts"] == 1
    assert source_path.read_text(encoding="utf-8") == concurrent


def test_fix_links_rejects_symlink_swap_without_external_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    source_path = vault_path / "business" / "crm.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        _flat_context_note("CRM", "See [[vault/obsolete]]."),
        encoding="utf-8",
    )
    external_path = tmp_path / "external.md"
    external_content = _flat_context_note("External", "Do not touch.")
    external_path.write_text(external_content, encoding="utf-8")
    module = _load_vault_health_script("fix_links")
    module.VAULT_PATH = vault_path
    real_write = module.write_validated_vault_markdown

    def swap_before_write(*args, **kwargs):
        source_path.unlink()
        source_path.symlink_to(external_path)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(module, "write_validated_vault_markdown", swap_before_write)
    stats = module.fix_targets(
        vault_path,
        {"broken_links": [{"source": "business/crm", "target": "vault/obsolete"}]},
        apply=True,
        stem_index={},
    )

    assert stats["removed"] == 0
    assert stats["errors"] == 1
    assert external_path.read_text(encoding="utf-8") == external_content


def test_add_descriptions_uses_current_flat_business_and_projects_layout() -> None:
    module = _load_vault_health_script("add_descriptions")

    business_content = (
        "---\ntype: note\n---\n# CRM\n\nСюда сводятся карточки клиентов и сделки.\n"
    )
    business_desc = module.generate_description(
        "business/crm.md",
        business_content,
        {"type": "note"},
    )
    assert business_desc == "Сюда сводятся карточки клиентов и сделки."

    project_content = (
        "---\n"
        "type: project\n"
        "---\n"
        "# Clients\n\n"
        "Сюда сводятся клиентские проекты и аккаунты.\n"
    )
    project_desc = module.generate_description(
        "projects/clients.md",
        project_content,
        {"type": "project"},
    )
    assert project_desc == "Сюда сводятся клиентские проекты и аккаунты."


def test_add_descriptions_parse_frontmatter_tolerates_leading_bom() -> None:
    """A leading UTF-8 BOM must not hide an existing description (see frontmatter.py).

    Without BOM tolerance, ``parse_frontmatter``/``get_body_after_frontmatter``
    would both report "no frontmatter", so main() would regenerate and
    overwrite an already-present description for no reason.
    """
    module = _load_vault_health_script("add_descriptions")

    without_bom = "---\ntype: note\ndescription: Kept\n---\nBody text.\n"
    with_bom = "\ufeff" + without_bom

    baseline_fm = module.parse_frontmatter(without_bom)
    fm = module.parse_frontmatter(with_bom)
    assert fm == baseline_fm == {"type": "note", "description": "Kept"}

    baseline_body = module.get_body_after_frontmatter(without_bom)
    body = module.get_body_after_frontmatter(with_bom)
    assert body == baseline_body == "Body text.\n"


def test_add_descriptions_skips_nonsemantic_routed_profiles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    template_path = vault_path / "templates" / "note.md"
    summary_path = vault_path / "summaries" / "2026-W14-summary.md"
    template_path.parent.mkdir(parents=True)
    summary_path.parent.mkdir(parents=True)
    template_path.write_text(
        "---\ntype: template\n---\n\n# Template\n\nTemplate body.\n",
        encoding="utf-8",
    )
    summary_path.write_text(
        (
            "---\n"
            "type: weekly-summary\n"
            "last_accessed: 2026-04-04\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Summary\n\nUseful weekly result.\n"
        ),
        encoding="utf-8",
    )
    module = _load_vault_health_script("add_descriptions")
    module.VAULT_PATH = vault_path
    monkeypatch.setattr(sys, "argv", ["add_descriptions.py", "--apply"])

    module.main()

    assert "description:" not in template_path.read_text(encoding="utf-8")
    assert "description:" in summary_path.read_text(encoding="utf-8")


def test_add_descriptions_skips_cooperative_concurrent_update(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    summary_path = vault_path / "summaries" / "2026-W14-summary.md"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        (
            "---\n"
            "type: weekly-summary\n"
            "last_accessed: 2026-04-04\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Summary\n\nOriginal body.\n"
        ),
        encoding="utf-8",
    )
    concurrent_content = (
        "---\n"
        "type: weekly-summary\n"
        "description: Concurrent semantic description\n"
        "last_accessed: 2026-04-04\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        "# Summary\n\nConcurrent body.\n"
    )
    module = _load_vault_health_script("add_descriptions")
    module.VAULT_PATH = vault_path
    manifest = module.load_manifest_for_vault(vault_path)

    def concurrent_generation(
        rel_path: str,
        content: str,
        frontmatter: dict,
    ) -> str:
        del rel_path, content, frontmatter
        with module.vault_write_lock(vault_path) as lock:
            write_validated_vault_markdown(
                vault_path,
                summary_path,
                concurrent_content.encode("utf-8"),
                manifest=manifest,
                existing_lock=lock,
            )
        return "Stale generated description"

    monkeypatch.setattr(module, "generate_description", concurrent_generation)
    monkeypatch.setattr(sys, "argv", ["add_descriptions.py", "--apply"])

    module.main()

    assert summary_path.read_text(encoding="utf-8") == concurrent_content
    output = capsys.readouterr().out
    assert "SKIP CONCURRENT: summaries/2026-W14-summary.md" in output
    assert "Concurrent conflicts skipped: 1" in output


def test_backlinks_shell_searches_the_vault_root(tmp_path: Path) -> None:
    source_script = SKILLS_TEMPLATE_ROOT / "vault-health/scripts/backlinks.sh"
    script_dir = tmp_path / "skills/vault-health/scripts"
    script_dir.mkdir(parents=True)
    script_path = script_dir / "backlinks.sh"
    script_path.write_text(source_script.read_text(encoding="utf-8"), encoding="utf-8")
    script_path.chmod(0o755)

    (tmp_path / "vault" / "business").mkdir(parents=True)
    (tmp_path / "vault" / "note.md").write_text(
        "See [[business/crm]] for context.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(script_path), "business/crm"],
        check=True,
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )

    assert result.stdout.strip().splitlines() == ["note.md"]


def test_vault_health_skill_lists_live_maintenance_scripts() -> None:
    skill_path = SKILLS_TEMPLATE_ROOT / "vault-health/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "add_descriptions.py" in content
    assert "connect_orphans.py" in content


def test_graph_analyzer_penalizes_malformed_daily_structure(tmp_path: Path) -> None:
    analyzer = _load_graph_builder_script("analyze")

    valid_vault = tmp_path / "valid" / "vault"
    invalid_vault = tmp_path / "invalid" / "vault"
    (valid_vault / "daily").mkdir(parents=True)
    (invalid_vault / "daily").mkdir(parents=True)

    valid_daily = (
        "---\n"
        "type: daily\n"
        "date: 2026-04-17\n"
        "last_accessed: 2026-04-17\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        "# 2026-04-17\n\n"
        "## 09:00 [text]\n"
        "Нормальный entry\n"
    )
    invalid_daily = (
        "# 2026-04-17\n\n"
        "---\n"
        "type: daily\n"
        "date: 2026-04-17\n"
        "---\n\n"
        "## 09:00 [text]\n"
        "Сломанный preamble\n"
    )

    (valid_vault / "daily" / "2026-04-17.md").write_text(
        valid_daily,
        encoding="utf-8",
    )
    (invalid_vault / "daily" / "2026-04-17.md").write_text(
        invalid_daily,
        encoding="utf-8",
    )

    valid_stats = analyzer.analyze_vault(valid_vault)
    invalid_stats = analyzer.analyze_vault(invalid_vault)

    assert valid_stats["malformed_daily_count"] == 0
    assert invalid_stats["malformed_daily_count"] == 1
    assert invalid_stats["malformed_daily_notes"] == [
        {
            "path": "daily/2026-04-17",
            "issue": "frontmatter-not-first",
        }
    ]
    assert invalid_stats["daily_structure_penalty"] == 4.0
    assert invalid_stats["health_score"] == round(valid_stats["health_score"] - 4.0, 1)
