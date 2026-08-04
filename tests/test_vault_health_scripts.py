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
