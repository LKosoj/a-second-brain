"""Regression tests for audit group G11 (context-pack).

Covers the fix to ``ContextPackBuilder._hygiene_summary`` in
``d_brain.services.context_pack``: it now reads the vault-graph.json keys
that ``analyze.py`` actually writes at the JSON root (``broken_link_count``,
``orphan_count``, ``weakly_connected_count``, ``malformed_daily_count``,
``health_score``) instead of keys the producer never wrote, and treats a
list value defensively by reporting its length.
"""

import json
from datetime import date
from pathlib import Path

from d_brain.services.context_pack import ContextPackBuilder


def _create_vault(tmp_path: Path, write_vault_manifest) -> Path:
    vault = tmp_path / "vault"
    for directory in ("daily", "goals", "business", "projects", ".session", ".graph"):
        (vault / directory).mkdir(parents=True, exist_ok=True)
    files = {
        "MEMORY.md": "Память.\n",
        "goals/3-weekly.md": "Неделя.\n",
        "goals/2-monthly.md": "Месяц.\n",
        "goals/1-yearly-2026.md": "Год.\n",
        "business/_index.md": "Бизнес.\n",
        "projects/_index.md": "Проекты.\n",
        "daily/2026-07-29.md": "Сегодня.\n",
        "daily/2026-07-28.md": "Вчера.\n",
        ".session/handoff.md": "Передача.\n",
    }
    for relative, content in files.items():
        (vault / relative).write_text(content, encoding="utf-8")
    write_vault_manifest(
        vault,
        overrides={
            "context_budget_bytes": 200_000,
            "user_content_roots": [
                "vault/MEMORY.md",
                "vault/daily",
                "vault/goals",
                "vault/business",
                "vault/projects",
            ],
            "infrastructure": [
                "vault/.session",
                "vault/.graph",
                "vault/.qmd",
            ],
        },
    )
    return vault


def test_hygiene_summary_reads_realistic_analyze_output(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    """analyze.py writes counts as *_count keys and lists under bare names."""
    vault = _create_vault(tmp_path, write_vault_manifest)
    graph_payload = {
        "total_notes": 42,
        "broken_links": [
            {"source": "a.md", "target": "missing"},
            {"source": "b.md", "target": "missing2"},
        ],
        "broken_link_count": 2,
        "orphans": ["c.md"],
        "orphan_count": 1,
        "weakly_connected": ["d.md", "e.md"],
        "weakly_connected_count": 2,
        "malformed_daily_notes": [],
        "malformed_daily_count": 0,
        "health_score": 87.5,
    }
    (vault / ".graph/vault-graph.json").write_text(
        json.dumps(graph_payload), encoding="utf-8"
    )

    pack = ContextPackBuilder(vault).build(date(2026, 7, 29))

    assert "health_score=87.5" in pack.text
    assert "broken_links=2" in pack.text
    assert "orphan_files=1" in pack.text
    assert "weak_links=2" in pack.text
    assert "daily_files=0" in pack.text
    # The full broken-link records must never be dumped into the prompt.
    assert "missing" not in pack.text
    assert "missing2" not in pack.text


def test_hygiene_summary_uses_list_length_when_no_count_key_present(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    """If a count key is absent but the raw list is, fall back to its length."""
    vault = _create_vault(tmp_path, write_vault_manifest)
    (vault / ".graph/vault-graph.json").write_text(
        json.dumps({"health_score": 50, "broken_links": ["x", "y", "z"]}),
        encoding="utf-8",
    )

    pack = ContextPackBuilder(vault).build(date(2026, 7, 29))

    assert "broken_links=3" in pack.text
    assert "'x'" not in pack.text and "x, y, z" not in pack.text


def test_hygiene_summary_old_scalar_fixture_still_passes(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    """The pre-fix test fixture (scalar broken_links) keeps working."""
    vault = _create_vault(tmp_path, write_vault_manifest)
    (vault / ".graph/vault-graph.json").write_text(
        json.dumps({"health_score": 91, "broken_links": 0, "ignored": "no"}),
        encoding="utf-8",
    )

    pack = ContextPackBuilder(vault).build(date(2026, 7, 29))

    assert "health_score=91" in pack.text
    assert "broken_links=0" in pack.text
    assert "ignored=no" not in pack.text
