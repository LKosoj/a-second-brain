import types
from pathlib import Path

import pytest
from _paths import PROJECT_ROOT, SKILLS_TEMPLATE_ROOT, TEMPLATE_ROOT


def test_prompt_source_map_declares_loaded_runtime_and_secondary_layers() -> None:
    source_map = (TEMPLATE_ROOT / ".claude/docs/prompt-source-map.md").read_text(
        encoding="utf-8"
    )

    assert "Canonical Runtime Prompt Layer" in source_map
    assert "Control-Plane Runtime Layer" in source_map
    assert "src/d_brain/control_plane/registry.py" in source_map
    assert "Phase Files Loaded Directly By Runtime" in source_map
    assert "Reference Files Loaded Directly By `processor.py`" in source_map
    assert "src/d_brain/services/documents.py" in source_map
    assert "references/about.md" in source_map
    assert "references/process-goals.md" in source_map
    assert "Bundled Utility Skills Outside Product Runtime" in source_map
    assert "Runtime-Loaded Retrieval Policy" in source_map
    assert "skills/vault-retrieval/SKILL.md" in source_map
    assert "recall_planner.py" in source_map


def test_dbrain_processor_skill_is_thin_wrapper_for_runtime_canon() -> None:
    skill = (SKILLS_TEMPLATE_ROOT / "dbrain-processor/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Do not change runtime behavior in this file alone." in skill
    assert "Prompt Source Map" in skill
    assert "references/business.md" not in skill
    assert "contacts/_index.md" not in skill
    assert "business-context.md" not in skill
    assert "report-template.md" not in skill
    assert "Removed Legacy Files" not in skill
    assert "references/about.md" in skill
    assert "references/process-goals.md" in skill
    assert "skills/vault-retrieval/SKILL.md" in skill


def test_vault_retrieval_skill_is_the_single_retrieval_and_touch_contract() -> None:
    retrieval_skill = (SKILLS_TEMPLATE_ROOT / "vault-retrieval/SKILL.md").read_text(
        encoding="utf-8"
    )
    processor = (PROJECT_ROOT / "src/d_brain/services/processor.py").read_text(
        encoding="utf-8"
    )
    question_answer = (
        SKILLS_TEMPLATE_ROOT / "dbrain-processor/references/question-answer.md"
    ).read_text(encoding="utf-8")
    execute = (SKILLS_TEMPLATE_ROOT / "dbrain-processor/phases/execute.md").read_text(
        encoding="utf-8"
    )
    links = (SKILLS_TEMPLATE_ROOT / "dbrain-processor/references/links.md").read_text(
        encoding="utf-8"
    )

    assert 'a-second-brain qmd recall "<query>"' in retrieval_skill
    assert 'a-second-brain qmd deep-recall "<query>"' in retrieval_skill
    assert 'a-second-brain qmd query "<query>"' in retrieval_skill
    assert "It does not apply `tier`, `relevance`" in retrieval_skill
    assert "a-second-brain qmd get <vault-relative-path>" in retrieval_skill
    assert "qmd-local" not in retrieval_skill
    assert "memory-engine.py touch vault/<file>" in retrieval_skill
    assert "FULLTEXT FOLLOW-UP" in retrieval_skill
    assert processor.count("=== VAULT RETRIEVAL SKILL ===") == 4
    assert "qmd-local recall/get" not in processor
    assert "qmd-local deep-recall/get" not in processor
    assert "## Retrieval Rules" not in question_answer
    assert "Search for related notes in the vault" not in execute
    assert "semantic recall when the relationship" not in links


def test_agent_memory_skill_explains_query_and_recall_ranking() -> None:
    skill = (SKILLS_TEMPLATE_ROOT / "agent-memory/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert 'a-second-brain qmd query "<query>"' in skill
    assert 'a-second-brain qmd recall "<query>"' in skill
    assert "`tier` controls visibility and adds a ranking bonus" in skill
    assert "Card `status`" in skill
    assert "is not a memory-ranking signal" in skill


def test_repo_agents_do_not_use_direct_todoist_mcp_tool_names() -> None:
    agents_root = TEMPLATE_ROOT / ".claude/agents"

    for path in sorted(agents_root.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        assert "mcp__todoist__" not in content, path.name


def test_rules_are_navigation_docs_not_competing_runtime_specs() -> None:
    daily_rule = (TEMPLATE_ROOT / ".claude/rules/daily-format.md").read_text(
        encoding="utf-8"
    )
    assert "phases/capture.md" in daily_rule
    assert "<!-- ✓ processed -->" in daily_rule
    assert "file-level YAML footer" in daily_rule
    assert "processed: 2024-12-20T21:00:00" not in daily_rule

    thoughts_rule = (TEMPLATE_ROOT / ".claude/rules/thoughts-format.md").read_text(
        encoding="utf-8"
    )
    assert "phases/execute.md" in thoughts_rule
    assert "agent-memory card template" in thoughts_rule
    assert "This file should stay lightweight." in thoughts_rule
    assert "same durable claim already exists" in thoughts_rule

    telegram_rule = (TEMPLATE_ROOT / ".claude/rules/telegram-report.md").read_text(
        encoding="utf-8"
    )
    assert "not the canonical report-content template" in telegram_rule
    assert "src/d_brain/bot/formatters.py" in telegram_rule
    assert "Unsupported tags: `<strong>`, `<em>`" in telegram_rule
    assert "## Report Template" not in telegram_rule


def test_phase_files_are_self_contained_and_use_current_layout() -> None:
    phases_root = SKILLS_TEMPLATE_ROOT / "dbrain-processor/phases"

    capture = (phases_root / "capture.md").read_text(encoding="utf-8")
    execute = (phases_root / "execute.md").read_text(encoding="utf-8")
    reflect = (phases_root / "reflect.md").read_text(encoding="utf-8")

    assert "business/crm/acme-corp" not in capture
    assert '"entities": ["Example Studio"]' in capture
    assert "business/crm/*.md" not in execute
    assert "projects/clients/*.md" not in execute
    assert "minimal set of writes" in execute
    assert "GUARANTEED to work" not in execute
    assert "business/crm.md" in execute
    assert "projects/projects.md" in execute
    assert "Use the template from SKILL.md" not in reflect
    assert "Do not rewrite whole files" in reflect
    assert "hidden template elsewhere" in reflect
    assert "Vault Health score" not in reflect
    assert "Vault Health section" not in reflect


def test_navigation_docs_and_secondary_skills_match_current_flat_layout() -> None:
    claude = (TEMPLATE_ROOT / ".claude/CLAUDE.md").read_text(encoding="utf-8")
    assert "business/crm/{kebab-case}.md" not in claude
    assert "business/*.md" in claude
    assert "projects/*.md" in claude
    assert "fresh agent context" in claude
    assert "process.sh → Claude → skill is correct" not in claude

    graph_skill = (SKILLS_TEMPLATE_ROOT / "graph-builder/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "business/crm.md" in graph_skill
    assert "projects/projects.md" in graph_skill
    assert "MOC/index.md" in graph_skill

    health_skill = (SKILLS_TEMPLATE_ROOT / "vault-health/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Current Vault Topology" in health_skill
    assert "business/crm.md" in health_skill
    assert "smallest useful change" in health_skill

    organizer = (TEMPLATE_ROOT / ".claude/agents/note-organizer.md").read_text(
        encoding="utf-8"
    )
    assert "vault/.graph/vault-graph.json" in organizer
    assert "MOC/index.md" in organizer

    todoist_skill = (SKILLS_TEMPLATE_ROOT / "todoist-ai/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Generated by Skill Seeker" not in todoist_skill
    assert "mcp-cli call todoist user-info '{}'" in todoist_skill
    assert "live Todoist catalog" in todoist_skill
    assert 'priority": "p2"' in todoist_skill
    assert "MCP_CONFIG_PATH" in todoist_skill
    assert "mcp-config.json" in todoist_skill
    assert 'export MCP_CONFIG_PATH="$PWD/mcp-config.json"' not in todoist_skill
    assert "already supplied by the runtime" in todoist_skill

    agent_memory_architecture = (
        SKILLS_TEMPLATE_ROOT / "agent-memory/references/architecture.md"
    ).read_text(encoding="utf-8")
    assert "vault/crm/" not in agent_memory_architecture
    assert "memory/YYYY-MM-DD.md" not in agent_memory_architecture
    assert "daily/YYYY-MM-DD.md" in agent_memory_architecture
    assert "one tier at a time" in agent_memory_architecture

    agent_memory_search = (
        SKILLS_TEMPLATE_ROOT / "agent-memory/references/search-protocols.md"
    ).read_text(encoding="utf-8")
    assert "vault/crm/" not in agent_memory_search
    assert (
        "uv run skills/agent-memory/scripts/memory-engine.py "
        "touch vault/<file>" in agent_memory_search
    )

    agent_memory_schema = (
        SKILLS_TEMPLATE_ROOT / "agent-memory/references/yaml-schema.md"
    ).read_text(encoding="utf-8")
    assert "crm/clients/" not in agent_memory_schema
    assert "crm/leads/" not in agent_memory_schema
    assert "crm/personal/" not in agent_memory_schema


def test_runtime_docs_omit_removed_commands_and_legacy_process_entrypoints() -> None:
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    weekly_reflection = (
        TEMPLATE_ROOT / ".claude/rules/weekly-reflection.md"
    ).read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    readme_ru = (PROJECT_ROOT / "README.ru.md").read_text(encoding="utf-8")
    vps = (PROJECT_ROOT / "docs/vps-setup.md").read_text(encoding="utf-8")
    process_unit = (
        PROJECT_ROOT / "deploy/a-second-brain-process.service.in"
    ).read_text(encoding="utf-8")

    assert "| `/align` |" not in agents
    assert "| `/organize` |" not in agents
    assert "| `/graph` |" not in agents
    assert "via `/weekly`" not in weekly_reflection
    assert '"Process" button' not in readme
    assert "Кнопка «Process»" not in readme_ru
    assert "scripts/process.sh" not in vps
    assert "scripts/process.sh" not in process_unit
    assert "python -m d_brain.run_daily_process --mode scheduled" in process_unit


def test_graph_builder_legacy_reference_pack_is_gone() -> None:
    references_root = SKILLS_TEMPLATE_ROOT / "graph-builder/references"

    assert list(references_root.rglob("*.md")) == []


def test_deleted_legacy_prompt_references_do_not_reappear() -> None:
    deleted_paths = [
        "skills/dbrain-processor/references/business-context.md",
        "skills/dbrain-processor/references/contacts.md",
        "skills/dbrain-processor/references/report-template.md",
        "skills/dbrain-processor/references/rules.md",
    ]
    source_map = (TEMPLATE_ROOT / ".claude/docs/prompt-source-map.md").read_text(
        encoding="utf-8"
    )
    skill = (SKILLS_TEMPLATE_ROOT / "dbrain-processor/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Removed Legacy Prompt Files" in source_map

    for rel_path in deleted_paths:
        assert not (TEMPLATE_ROOT / rel_path).exists(), rel_path
        file_name = Path(rel_path).name
        assert file_name in source_map, file_name
        assert file_name not in skill, file_name


def test_dbrain_processor_skill_points_to_control_plane_runtime_layer() -> None:
    skill = (SKILLS_TEMPLATE_ROOT / "dbrain-processor/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "src/d_brain/control_plane/*.py" in skill
    assert "Control-Plane Runtime Layer" in skill
    assert "src/d_brain/control_plane/registry.py" in skill
    assert "docs/control-plane.md" in skill
    assert (
        "router: `src/d_brain/control_plane/registry.py` + "
        "`src/d_brain/control_plane/router.py`"
    ) in skill


def test_project_claude_md_points_to_control_plane_sources() -> None:
    claude_md = (TEMPLATE_ROOT / ".claude/CLAUDE.md").read_text(encoding="utf-8")

    assert "src/d_brain/control_plane/registry.py" in claude_md
    assert "docs/control-plane.md" in claude_md
    assert "scripts/setup_control_plane.sh" in claude_md


def test_readmes_do_not_claim_vault_claude_is_full_orchestration_source_of_truth() -> (
    None
):
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    readme_ru = (PROJECT_ROOT / "README.ru.md").read_text(encoding="utf-8")

    assert (
        "vault/.claude/` is the source of truth for rules, docs, skills, and agents"
        not in readme
    )
    assert "Workflow contracts and routing live in" in readme
    assert "`src/d_brain/control_plane/registry.py`" in readme
    assert "`src/d_brain/control_plane/router.py`" in readme
    assert "Runtime orchestration is implemented in" in readme
    assert "`src/d_brain/services/`" in readme
    assert "`daily_workflow.py`" in readme
    assert "источник истины для правил, docs, skills и agents" not in readme_ru
    assert "Контракты workflow и маршрутизация находятся в" in readme_ru
    assert "`src/d_brain/control_plane/registry.py`" in readme_ru
    assert "`src/d_brain/control_plane/router.py`" in readme_ru
    assert "Runtime-оркестрация реализована в" in readme_ru
    assert "`src/d_brain/services/`" in readme_ru
    assert "`daily_workflow.py`" in readme_ru


def test_control_plane_doc_current_status_mentions_live_integration_adoption() -> None:
    control_plane = (PROJECT_ROOT / "docs/control-plane.md").read_text(encoding="utf-8")

    assert (
        "integration workflow contracts (`integration.documents.archive`, "
        "`integration.web.archive`, `integration.youtube.archive`, "
        "`integration.plaud.sync`)"
    ) in control_plane
    assert "integration runtime adoption of registry contracts" not in control_plane


def test_prompt_source_map_cleanup_section_is_status_not_stale_roadmap() -> None:
    source_map = (TEMPLATE_ROOT / ".claude/docs/prompt-source-map.md").read_text(
        encoding="utf-8"
    )

    assert "## Current Cleanup Status" in source_map
    assert "## Current Cleanup Direction" not in source_map
    assert (
        "Orchestration truth lives in `src/d_brain/control_plane/registry.py`"
    ) in source_map
    assert "Move orchestration truth into the control-plane registry" not in source_map


def test_control_plane_registry_is_valid_and_entrypoints_resolve() -> None:
    from d_brain.control_plane.registry import validate_control_plane_registry

    assert validate_control_plane_registry() == []


def test_resolve_entrypoint_surfaces_missing_dependency_when_no_module_ever_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every module-path guess fails to import (getattr is never
    reached), the real root cause (e.g. a missing third-party dependency
    deep in the import chain) must survive -- not a generic "X is not a
    package" error produced by guessing the split point wrong.

    The mocked failures are deliberately ordered weak / real-cause / weak
    so this fails under both a naive "always keep the first error" (picks
    the longest prefix's misleading guess) and a naive "always keep the
    last error" (picks the shortest prefix's misleading guess); it only
    passes when the implementation tells an informative ImportError (whose
    failing module differs from the one requested) apart from a
    self-referential "not a package" guess."""
    from d_brain.control_plane import registry

    real_import_module = registry.importlib.import_module
    root_cause = ModuleNotFoundError("No module named 'acp'", name="acp")
    weak_long = ModuleNotFoundError(
        "No module named 'fake_dep_pkg.fake_mid.fake_sub'; "
        "'fake_dep_pkg.fake_mid' is not a package",
        name="fake_dep_pkg.fake_mid.fake_sub",
    )
    weak_short = ModuleNotFoundError(
        "No module named 'fake_dep_pkg.fake_mid'; 'fake_dep_pkg' is not a package",
        name="fake_dep_pkg",
    )

    def fake_import_module(name: str) -> object:
        if name == "fake_dep_pkg.fake_mid.fake_sub":
            raise weak_long
        if name == "fake_dep_pkg.fake_mid":
            raise root_cause
        if name == "fake_dep_pkg":
            raise weak_short
        return real_import_module(name)

    monkeypatch.setattr(registry.importlib, "import_module", fake_import_module)

    with pytest.raises(ModuleNotFoundError) as excinfo:
        registry.resolve_entrypoint("fake_dep_pkg.fake_mid.fake_sub.fake_attr")

    assert excinfo.value is root_cause


def test_resolve_entrypoint_surfaces_attribute_typo_when_module_imports_fine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a middle-length module prefix imports successfully and only the
    getattr chain fails, that AttributeError names the real missing
    attribute (e.g. a typo in a method name) and must survive.

    The mocked failures are deliberately ordered weak / real-cause / weak
    so this fails under both a naive "always keep the first error" (picks
    the longest prefix's misleading "not a package" guess) and a naive
    "always keep the last error" (picks the shortest prefix's unrelated
    misleading guess)."""
    from d_brain.control_plane import registry

    real_import_module = registry.importlib.import_module

    class FakeClass:
        pass

    fake_module = types.ModuleType("fake_typo_pkg.fake_mod")
    fake_module.FakeClass = FakeClass  # type: ignore[attr-defined]

    weak_long = ModuleNotFoundError(
        "No module named 'fake_typo_pkg.fake_mod.FakeClass'; "
        "'fake_typo_pkg.fake_mod' is not a package",
        name="fake_typo_pkg.fake_mod.FakeClass",
    )
    weak_short = ModuleNotFoundError(
        "No module named 'fake_typo_pkg.fake_mod'; 'fake_typo_pkg' is not a package",
        name="fake_typo_pkg",
    )

    def fake_import_module(name: str) -> object:
        if name == "fake_typo_pkg.fake_mod.FakeClass":
            raise weak_long
        if name == "fake_typo_pkg.fake_mod":
            return fake_module
        if name == "fake_typo_pkg":
            raise weak_short
        return real_import_module(name)

    monkeypatch.setattr(registry.importlib, "import_module", fake_import_module)

    with pytest.raises(AttributeError) as excinfo:
        registry.resolve_entrypoint("fake_typo_pkg.fake_mod.FakeClass.typo_attr")

    assert "typo_attr" in str(excinfo.value)


def test_control_plane_registry_exposes_scheduled_post_maintenance_workflows() -> None:
    from d_brain.control_plane.registry import iter_workflows

    workflows = tuple(iter_workflows(kind="maintenance", trigger="scheduled-post"))

    assert [workflow.name for workflow in workflows] == [
        "maintenance.compiled-nightly",
        "maintenance.vault-health",
        "maintenance.compiled-fact-check",
        "maintenance.compiled-digest",
    ]
    assert [workflow.display_name for workflow in workflows] == [
        "Compiled Maintenance",
        "Vault Health",
        "Compiled Fact Check",
        "Compiled Digest",
    ]


def test_scheduled_cycle_declares_every_write_its_children_declare() -> None:
    """``run_scheduled_cycle`` runs each ``scheduled-post`` maintenance
    workflow in-process, so anything a child declares it writes, the parent
    writes too. Walked rather than hardcoded: three separate gaps of exactly
    this kind (``summaries`` twice, then ``graph``/``moc``/``links``) had
    already opened up between the entries by the time this was written, and
    each one made the registry -- the catalog the docs and reviews treat as
    the source of truth -- understate what a nightly run touches."""
    from d_brain.control_plane.registry import get_workflow, iter_workflows

    parent = get_workflow("maintenance.scheduled-cycle")

    for child in iter_workflows(kind="maintenance", trigger="scheduled-post"):
        missing = set(child.allowed_writes) - set(parent.allowed_writes)
        assert not missing, f"{child.name} writes {sorted(missing)}, parent does not"


def test_scheduled_cycle_declares_the_goals_write_it_makes_directly() -> None:
    """The walk above only compares the parent against its children, so a
    write the parent makes *itself* is invisible to it. ``goals`` was exactly
    that: ``_run_scheduled_cycle_locked`` calls ``rollover_weekly_goals``,
    which rewrites ``goals/3-weekly.md``, and no workflow -- parent or child
    -- declared it."""
    import inspect

    from d_brain.control_plane.registry import get_workflow
    from d_brain.services.processor import CliProcessor

    source = inspect.getsource(CliProcessor._run_scheduled_cycle_locked)
    assert "rollover_weekly_goals" in source
    assert "goals" in get_workflow("maintenance.scheduled-cycle").allowed_writes


def test_control_plane_doc_lists_every_maintenance_workflow_name() -> None:
    """Walk the registry instead of hardcoding names: a newly added
    maintenance workflow (e.g. the compile-enrich fact-check/digest pair)
    must be caught here if it drifts out of `docs/control-plane.md`."""
    from d_brain.control_plane.registry import iter_workflows

    control_plane = (PROJECT_ROOT / "docs/control-plane.md").read_text(
        encoding="utf-8"
    )

    for workflow in iter_workflows(kind="maintenance"):
        assert f"`{workflow.name}`" in control_plane


def test_why_command_is_documented_in_repo_facing_docs() -> None:
    for relative_path in ("AGENTS.md", "README.md", "README.ru.md"):
        text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")

        assert "/why" in text


def test_control_plane_registry_exposes_integration_workflows() -> None:
    from d_brain.control_plane.registry import iter_workflows

    workflows = tuple(iter_workflows(kind="integration"))

    assert [workflow.name for workflow in workflows] == [
        "integration.documents.archive",
        "integration.web.archive",
        "integration.youtube.archive",
        "integration.plaud.sync",
    ]


def test_control_plane_router_matches_expected_question_routes() -> None:
    from d_brain.control_plane.router import (
        build_question_route_block,
        classify_question_route,
        iter_question_context_blocks,
        resolve_text_workflow,
    )

    assert classify_question_route("Какие приоритеты на эту неделю?") == "planning"
    assert classify_question_route("Какой ID проекта Example Project?") == "fact_lookup"
    assert classify_question_route("Что мы решили раньше по Example Project?") == (
        "status_history"
    )
    assert classify_question_route("Что я обещал этому клиенту?") == "relationship"
    assert iter_question_context_blocks("Какой ID проекта Example Project?") == (
        "recall",
        "compiled",
    )
    assert "Route: planning" in build_question_route_block(
        "Какие приоритеты на эту неделю?"
    )
    assert (
        resolve_text_workflow(
            "Какие приоритеты на эту неделю?",
            intent="question",
            confidence="high",
        ).name
        == "question.planning"
    )
    assert (
        resolve_text_workflow(
            "Сохрани это как заметку",
            intent="capture",
            confidence="high",
        ).name
        == "capture.daily-entry"
    )


def test_control_plane_docs_and_scripts_exist() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert (repo_root / "docs/control-plane.md").exists()
    assert (repo_root / "docs/control-plane-security-profile-template.md").exists()
    assert (repo_root / "scripts/setup_control_plane.sh").exists()
    assert (repo_root / "scripts/update_control_plane.sh").exists()
