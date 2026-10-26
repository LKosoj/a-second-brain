import importlib.util
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from _paths import SKILLS_TEMPLATE_ROOT

from d_brain.config import Settings
from d_brain.services.cli_runner import CliExecutionError
from d_brain.services.qmd import QMD_ENV_FORWARD_KEYS
from d_brain.services.subprocess_group import run_in_process_group


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


@pytest.fixture(autouse=True)
def _isolate_run_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``ensure_run_identity`` from leaking its run id across tests.

    ``run_daily_process.main``/``run_compiled_pass.main`` seed
    ``D_BRAIN_RUN_ID``/``D_BRAIN_WORKFLOW`` with ``os.environ.setdefault``;
    registering both keys with ``monkeypatch`` first makes that write part of
    the per-test undo, so the first test to call ``main()`` cannot pin a run
    id for the rest of the session.
    """
    monkeypatch.delenv("D_BRAIN_RUN_ID", raising=False)
    monkeypatch.delenv("D_BRAIN_WORKFLOW", raising=False)


@pytest.fixture(autouse=True)
def _block_real_ai_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite from spending real model quota on installed CLIs.

    Services build their own ``CliRunner``, so a test that exercises a full
    service path reaches the CLI installed on the developer machine and the
    result then depends on that machine's login state. ``CliRunner.run`` is the
    single entry point for every backend, so blocking it there closes all of
    them at once. It raises the same error a missing backend produces, which is
    what service fallbacks already handle; ``tests/test_cli_runner.py``
    overrides this fixture because it tests the runner itself.
    """

    def _blocked(*args: Any, **kwargs: Any) -> str:
        raise CliExecutionError("real AI CLI invocation attempted in tests")

    monkeypatch.setattr("d_brain.services.cli_runner.CliRunner.run", _blocked)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_env_file: the test reads the ``.env`` of its working directory, "
        "so the developer-file guard stands down",
    )


@pytest.fixture(autouse=True)
def _ignore_developer_env_file(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep ``Settings()`` from reading the developer's ``.env``.

    ``Settings`` reads ``env_file=".env"`` from the working directory, so a
    run from the repository root would pick up the live tokens and owner id and
    make results depend on that machine. Explicit ``Settings(_env_file=...)``
    still reads the file it is given; a test that needs the cwd file opts out
    with ``@pytest.mark.real_env_file``.
    """
    if request.node.get_closest_marker("real_env_file"):
        return
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _scrub_qmd_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the embedding/model variables ``QmdService`` forwards to qmd.

    The service copies ``os.environ``, so a shell that exports ``EMB_MODEL`` or
    ``OPENAI_BASE_URL`` would point test runs at the developer's real endpoint.
    """
    for key in QMD_ENV_FORWARD_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _block_real_qmd_and_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``QmdService`` from launching the real ``qmd``/``node`` binaries.

    Every launch goes through ``qmd.run_in_process_group``. Blocked calls raise
    ``FileNotFoundError``, exactly what a missing binary produces and what the
    service already handles. Other commands (``touch_notes`` runs
    ``sys.executable``) still run for real; tests that patch the runner
    themselves win over this patch.
    """

    def _guarded(args: Any, *a: Any, **kw: Any) -> Any:
        if str(args[0]) in {"qmd", "node"}:
            raise FileNotFoundError(f"real {args[0]} launch attempted in tests")
        return run_in_process_group(args, *a, **kw)

    monkeypatch.setattr("d_brain.services.qmd.run_in_process_group", _guarded)


@pytest.fixture(autouse=True)
def _detach_bot_routers() -> Any:
    """Free the module-level bot routers before and after every test.

    An aiogram router joins one dispatcher only, so a test that builds a
    dispatcher (``create_dispatcher`` or its own) leaves every router it used
    attached, and the next such test fails with "Router is already attached"
    depending on run order.
    """

    def detach() -> None:
        for name, module in list(sys.modules.items()):
            router = getattr(module, "router", None)
            if name.startswith("d_brain.bot.handlers.") and router is not None:
                router._parent_router = None

    detach()
    yield
    detach()


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


@pytest.fixture(autouse=True)
def _reset_owner_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test on the system zone, as when ``TIMEZONE`` is unset.

    ``clock`` caches the owner's zone in a module variable, so a test that
    sets one would otherwise leak it into the next test.
    """
    from d_brain import clock

    monkeypatch.delenv("TIMEZONE", raising=False)
    clock._reset_for_tests()
    yield
    clock._reset_for_tests()


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin the moment every service reads as "now" and return it.

    Tests used to take ``datetime.now()`` themselves while the code under test
    read its own clock a moment later, so a run across midnight or a second
    boundary compared two different instants. Both clock sources (``clock`` and
    the PLAUD service's own ``_utc_now``) answer with this one moment.
    """
    moment = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("d_brain.clock._utcnow", lambda: moment)
    monkeypatch.setattr("d_brain.services.plaud._utc_now", lambda: moment)
    return moment
