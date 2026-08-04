from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from _paths import SKILLS_TEMPLATE_ROOT
from conftest import _write_vault_manifest

from d_brain.manifest import ManifestValidationError


def _load_graph_analyzer():
    script_path = SKILLS_TEMPLATE_ROOT / "graph-builder/scripts/analyze.py"
    spec = importlib.util.spec_from_file_location("graph_analyzer", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_graph_link_builder():
    script_path = SKILLS_TEMPLATE_ROOT / "graph-builder/scripts/add_links.py"
    spec = importlib.util.spec_from_file_location("graph_link_builder", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_graph_analyzer_resolves_path_links_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    (vault_path / "MOC").mkdir(parents=True)
    (vault_path / "summaries").mkdir()
    (vault_path / "thoughts").mkdir()
    (vault_path / "goals").mkdir()
    (vault_path / "daily").mkdir()
    private_skill = vault_path / "skills/private/local-skill/SKILL.md"
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text("# Private skill\n", encoding="utf-8")
    (vault_path / "imports" / "plaud" / "notes" / "2026" / "04").mkdir(parents=True)

    (vault_path / "summaries" / "2026-W14-summary.md").write_text(
        "# Summary\n",
        encoding="utf-8",
    )
    (vault_path / "goals" / "1-yearly-2026.md").write_text(
        "# Goals\n",
        encoding="utf-8",
    )
    (vault_path / "MOC" / "MOC-weekly.md").write_text(
        "## Previous Weeks\n\n- [[summaries/2026-W14-summary.md|2026-W14-summary]]\n",
        encoding="utf-8",
    )
    (vault_path / "thoughts" / "retro.md").write_text(
        "[[summaries/2026-W14-summary]]\n[[missing-note]]\n",
        encoding="utf-8",
    )
    (vault_path / "daily" / "2026-04-04.md").write_text(
        "[[vault/goals/1-yearly-2026]]\n[[src/d_brain/run_agent.py]]\n",
        encoding="utf-8",
    )
    (
        vault_path
        / "imports"
        / "plaud"
        / "notes"
        / "2026"
        / "04"
        / "2026-04-03-110103-example.md"
    ).write_text(
        "[[imports/plaud/raw/2026/04/example.json]]\n",
        encoding="utf-8",
    )

    analyzer = _load_graph_analyzer()
    stats = analyzer.analyze_vault(vault_path)
    analyzer.write_artifacts(vault_path, stats)

    assert stats["total_notes"] == 6
    assert "skills/private/local-skill/SKILL" not in stats["notes"]
    assert stats["total_links"] == 3
    assert stats["broken_link_count"] == 1
    assert stats["ignored_link_count"] == 2
    assert stats["ignored_link_reasons"] == {
        "plaud-raw-payload": 1,
        "repo-path": 1,
    }
    assert stats["links_to"]["summaries/2026-W14-summary"] == [
        "MOC/MOC-weekly",
        "thoughts/retro",
    ]
    assert stats["links_to"]["goals/1-yearly-2026"] == ["daily/2026-04-04"]
    graph_json = vault_path / ".graph" / "vault-graph.json"
    report_md = vault_path / ".graph" / "report.md"
    assert graph_json.exists()
    assert report_md.exists()
    report = report_md.read_text(encoding="utf-8")
    assert "type: technical" in report
    assert "Broken Links" in report
    assert "Ignored Non-note References" in report


def test_graph_artifact_writer_requires_manifest_before_creating_artifacts(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    analyzer = _load_graph_analyzer()

    with pytest.raises(ManifestValidationError, match="is missing"):
        analyzer.write_artifacts(vault_path, {"health_score": 100})

    assert not (vault_path / ".graph").exists()


def test_graph_link_builder_routes_thought_categories_to_matching_mocs(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "MOC").mkdir(parents=True)
    (vault_path / "thoughts" / "ideas").mkdir(parents=True)
    (vault_path / "thoughts" / "learnings").mkdir(parents=True)
    (vault_path / "thoughts" / "reflections").mkdir(parents=True)

    for name in ["MOC-ideas", "MOC-learnings", "MOC-reflections", "MOC-projects"]:
        (vault_path / "MOC" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")

    builder = _load_graph_link_builder()
    mapping = builder.build_moc_mapping(vault_path)

    assert builder.suggest_moc_links(Path("thoughts/ideas/alpha.md"), mapping) == [
        "MOC-ideas"
    ]
    assert builder.suggest_moc_links(
        Path("thoughts/learnings/beta.md"),
        mapping,
    ) == ["MOC-learnings"]
    assert builder.suggest_moc_links(
        Path("thoughts/reflections/gamma.md"),
        mapping,
    ) == ["MOC-reflections"]


def test_graph_link_builder_uses_note_keys_for_duplicate_stem_sources(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "business").mkdir(parents=True)
    (vault_path / "projects").mkdir(parents=True)

    (vault_path / "business" / "_index.md").write_text(
        "# Business\n\nCRM\n",
        encoding="utf-8",
    )
    (vault_path / "projects" / "_index.md").write_text(
        "# Projects\n\nClients\n",
        encoding="utf-8",
    )
    (vault_path / "business" / "crm.md").write_text("# CRM\n", encoding="utf-8")
    (vault_path / "projects" / "clients.md").write_text("# Clients\n", encoding="utf-8")

    builder = _load_graph_link_builder()
    suggestions = builder.analyze_and_suggest(vault_path)

    assert "business/_index" in suggestions
    assert "projects/_index" in suggestions
    assert "_index" not in suggestions
