import importlib.util
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from _paths import SKILLS_TEMPLATE_ROOT


@pytest.fixture(autouse=True)
def _block_real_telegram_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite from delivering Telegram messages to the real owner.

    ``Settings`` reads ``env_file=".env"`` relative to the working directory,
    so a suite run from the repository root loads the live bot token and owner
    chat id. Cycles that deliver their own message -- currently
    ``processor._run_compiled_digest_cycle`` -- then reach a real send whenever
    their vault write succeeds, and five such messages left this repository
    per full run. ``_send_telegram_text_to_target`` is the single point where
    an ``aiogram.Bot`` is built, so blocking it closes every delivery path at
    once. It raises rather than silently passing, so an unmocked send is
    visible in the logs instead of looking like success; tests that exercise
    delivery on purpose install their own patch, which wins over this one.
    """

    async def _blocked(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("real Telegram delivery attempted in tests")

    monkeypatch.setattr(
        "d_brain.services.telegram_delivery._send_telegram_text_to_target",
        _blocked,
    )


def _write_vault_manifest(
    vault_path: Path,
    *,
    qmd_index: str = "dbrain",
    overrides: dict[str, Any] | None = None,
) -> Path:
    """Create an explicit manifest for a temporary vault."""
    manifest_path = vault_path.parent / "vault-manifest.json"
    # pytest may reuse a parent directory for independent ``tmp_path`` vaults.
    # Replace any sibling's manifest so this helper never leaks its root into
    # the current fixture; tests that need two vaults create separate projects.
    payload: dict[str, Any] = {
        "version": 1,
        "qmd_index": qmd_index,
        "context_budget_bytes": 200000,
        "memory_root": vault_path.name,
        "user_content_roots": [
            f"{vault_path.name}/MEMORY.md",
            f"{vault_path.name}/daily",
            f"{vault_path.name}/goals",
            f"{vault_path.name}/business",
            f"{vault_path.name}/projects",
        ],
        "infrastructure": [
            f"{vault_path.name}/.claude",
            f"{vault_path.name}/.compiled",
            f"{vault_path.name}/.graph",
            f"{vault_path.name}/.qmd",
            f"{vault_path.name}/.session",
            f"{vault_path.name}/skills",
        ],
        "frontmatter_required": {
            "default": ["type", "last_accessed", "relevance", "tier"],
            "daily": ["type", "date", "last_accessed", "relevance", "tier"],
            "import": ["type", "last_accessed", "relevance", "tier"],
            "derived": ["type", "description", "last_accessed", "relevance", "tier"],
            "thought-card": [
                "type",
                "description",
                "tags",
                "status",
                "created",
                "updated",
                "last_accessed",
                "relevance",
                "tier",
            ],
            "reflection": [
                "type",
                "description",
                "tags",
                "date",
                "created",
                "updated",
                "last_accessed",
                "relevance",
                "tier",
            ],
            "goal": ["type", "description", "last_accessed", "relevance", "tier"],
            "index": ["type", "description", "last_accessed", "relevance", "tier"],
            "flat-context": [
                "type",
                "description",
                "last_accessed",
                "relevance",
                "tier",
            ],
            "template": ["type", "description"],
            "technical": ["type", "last_accessed", "relevance", "tier"],
            "epistemic": [
                "type",
                "description",
                "epistemic_confidence",
                "epistemic_scope",
                "epistemic_state",
                "created",
                "updated",
                "last_accessed",
                "relevance",
                "tier",
            ],
            "home": ["type", "description", "last_accessed", "relevance", "tier"],
        },
    }
    if overrides:
        payload.update(overrides)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


@pytest.fixture
def write_vault_manifest():  # noqa: ANN201
    """Expose explicit manifest setup to tests that need a temporary vault."""

    return _write_vault_manifest


def _setup_daily_processing_vault(
    vault_path: Path,
    day: date,
) -> None:
    daily_path = vault_path / "daily"
    goals_path = vault_path / "goals"
    phases_path = vault_path.parent / "skills/dbrain-processor/phases"
    graph_path = vault_path / ".graph"

    daily_path.mkdir(parents=True)
    goals_path.mkdir(parents=True)
    phases_path.mkdir(parents=True)
    graph_path.mkdir(parents=True)
    (vault_path / "business").mkdir(parents=True)
    (vault_path / "projects").mkdir(parents=True)
    _write_vault_manifest(vault_path)

    (daily_path / f"{day.isoformat()}.md").write_text(
        (
            f"# {day.isoformat()}\n\n"
            "## 10:00 [text]\n"
            "Нужно подготовить подробный follow-up по проекту и вынести одну идею.\n"
        ),
        encoding="utf-8",
    )
    (goals_path / "3-weekly.md").write_text("weekly", encoding="utf-8")
    (goals_path / "2-monthly.md").write_text("monthly", encoding="utf-8")
    (goals_path / "1-yearly-2026.md").write_text("yearly", encoding="utf-8")
    (vault_path / "business" / "_index.md").write_text("business", encoding="utf-8")
    (vault_path / "projects" / "_index.md").write_text("projects", encoding="utf-8")
    (vault_path / "MEMORY.md").write_text("memory", encoding="utf-8")
    (graph_path / "health-history.json").write_text("[]", encoding="utf-8")

    for phase_name in ("capture", "execute", "reflect", "preview"):
        (phases_path / f"{phase_name}.md").write_text(
            f"# {phase_name}\n",
            encoding="utf-8",
        )
    retrieval_source = SKILLS_TEMPLATE_ROOT / "vault-retrieval/SKILL.md"
    retrieval_target = vault_path.parent / "skills/vault-retrieval/SKILL.md"
    retrieval_target.parent.mkdir(parents=True, exist_ok=True)
    retrieval_target.write_text(
        retrieval_source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def _markdown_section(content: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    assert match is not None
    return match.group(1).strip()


def _load_memory_engine():
    script_path = SKILLS_TEMPLATE_ROOT / "agent-memory/scripts/memory-engine.py"
    spec = importlib.util.spec_from_file_location("memory_engine", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
