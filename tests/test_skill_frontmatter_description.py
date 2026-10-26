"""Skill scripts read frontmatter on their own and must agree that a
multi-line ``description: >-`` counts as filled in and is left alone.

They still differ in what they *see* (``add_descriptions`` reads the literal
``">-"``, ``memory-engine`` skips the key), which is a known gap kept out of
this check on purpose: the scripts ship standalone and do not share a parser.
"""

import importlib.util

from _paths import SKILLS_TEMPLATE_ROOT

CARD = (
    "---\n"
    "type: note\n"
    "description: >-\n"
    "  First line\n"
    "  second line\n"
    "tags: [a, b]\n"
    "tier: active\n"
    "---\n"
    "# Title\n\nBody.\n"
)


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SKILLS_TEMPLATE_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_graph_analyzer_counts_block_description_as_present() -> None:
    analyzer = _load("graph-builder/scripts/analyze.py", "skill_analyze")

    assert analyzer._has_description(CARD) is True


def test_add_descriptions_sees_a_non_empty_description() -> None:
    module = _load("vault-health/scripts/add_descriptions.py", "skill_add_desc")

    assert module.parse_frontmatter(CARD).get("description")


def test_memory_engine_keeps_the_description_block_when_rewriting() -> None:
    engine = _load("agent-memory/scripts/memory-engine.py", "skill_memory_engine")

    fields, _body, had_yaml = engine.parse_frontmatter(CARD)
    fields["tier"] = "warm"
    rebuilt = engine.build_frontmatter(fields)

    assert had_yaml is True
    assert "description: >-\n  First line\n  second line\n" in rebuilt
    assert "tier: warm" in rebuilt
