import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from d_brain.services import recall_planner as recall_planner_module
from d_brain.services.qmd import QmdService
from d_brain.services.recall_planner import (
    RecallPlan,
    RecallPlannerConfig,
    build_qmd_recall_block,
    plan_qmd_recall,
)


def test_plan_qmd_recall_uses_openai_json_response() -> None:
    content = (
        '{"use_recall": true, "query": "weekly priorities", '
        '"history_scope": "year", "history_start_hint": "2025-01-01", '
        '"deep": false, "limit": 9, "reason": "needs history"}'
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=content),
                        )
                    ]
                )
            )
        )
    )

    plan = plan_qmd_recall(
        "What were the weekly priorities last week?",
        purpose="question_answer",
        config=RecallPlannerConfig(model="gpt-test", api_key="key", language="en"),
        client=client,
    )

    assert plan.use_recall is True
    assert plan.query == "weekly priorities"
    assert plan.history_scope == "year"
    assert plan.history_start_hint == "2025-01-01"
    assert plan.deep is False
    assert plan.limit == 9
    assert plan.reason == "needs history"


def test_build_qmd_recall_block_formats_qmd_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(vault_path)
    content = (
        '{"use_recall": true, "query": "weekly priorities", '
        '"deep": false, "limit": 3, "reason": "needs history"}'
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=content),
                        )
                    ]
                )
            )
        )
    )

    def fake_recall(
        self, query: str, *, deep: bool = False, limit: int = 5
    ) -> dict[str, object]:
        assert query in {"weekly priorities", "weekly priorities last week"}
        assert limit == 100
        if deep:
            return {
                "query": query,
                "backend": "query",
                "mode": "deep-recall",
                "results": [],
            }
        if query != "weekly priorities":
            return {
                "query": query,
                "backend": "query",
                "mode": "recall",
                "results": [],
            }
        return {
            "query": query,
            "backend": "query",
            "mode": "recall",
            "results": [
                {
                    "title": "MEMORY",
                    "file": "qmd://core/MEMORY.md",
                    "rel_path": "MEMORY.md",
                    "score": 0.84,
                    "effective_score": 0.84,
                    "confidence": 0.84,
                    "snippet": "Weekly focus and carry-over risk.",
                }
            ],
        }

    monkeypatch.setattr(QmdService, "recall", fake_recall)

    block = build_qmd_recall_block(
        vault_path,
        task="What were the weekly priorities last week?",
        purpose="question_answer",
        config=RecallPlannerConfig(model="gpt-test", api_key="key", language="en"),
        client=client,
    )

    assert "AUTO ARCHIVE RECALL" in block
    assert "Reason: needs history" in block
    assert "History scope:" in block
    assert "Analyze history from:" in block
    assert "MEMORY" in block
    assert "confidence=" in block
    assert "FULLTEXT FOLLOW-UP" in block
    assert "read every listed source file fully" in block
    assert "HISTORY CHECKLIST" in block
    assert "prefer newer evidence" in block
    assert "Use this block first" in block


def test_plan_qmd_recall_falls_back_to_language_aware_literal_query() -> None:
    content = (
        '{"use_recall": true, "query": "recent activities", '
        '"deep": false, "limit": 3, "reason": "needs history"}'
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=content),
                        )
                    ]
                )
            )
        )
    )

    plan = plan_qmd_recall(
        "Что я недавно делал с qmd и recall planner?",
        purpose="question_answer",
        config=RecallPlannerConfig(model="gpt-test", api_key="key", language="ru"),
        client=client,
    )

    assert plan.use_recall is True
    assert plan.query == "qmd recall planner"
    assert plan.fallback_query == ""


def test_build_qmd_recall_block_merges_queries_and_keeps_all_results_over_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(vault_path)
    monkeypatch.setattr(
        recall_planner_module,
        "plan_qmd_recall",
        lambda *args, **kwargs: RecallPlan(
            use_recall=True,
            query="qmd planner",
            fallback_query="qmd planner weekly",
            deep=False,
            limit=2,
            reason="needs recent project context",
        ),
    )
    calls: list[str] = []

    def fake_recall(
        self, query: str, *, deep: bool = False, limit: int = 5
    ) -> dict[str, object]:
        calls.append(query)
        if query == "qmd planner":
            return {
                "query": query,
                "backend": "vec",
                "mode": "recall",
                "results": [
                    {
                        "title": "PLAUD transcript",
                        "file": "qmd://plaud/2026/04/transcript.md",
                        "rel_path": "plaud/2026/04/transcript.md",
                        "snippet": "Разговор вообще про другой проект.",
                        "effective_score": 1.02,
                    }
                ],
            }
        return {
            "query": query,
            "backend": "fts",
            "mode": "recall",
            "results": [
                {
                    "title": "QMD Planner Weekly",
                    "file": "qmd://goals/3-weekly.md",
                    "rel_path": "goals/3-weekly.md",
                    "snippet": "QMD planner rollout and weekly follow-up.",
                    "effective_score": 0.96,
                },
                {
                    "title": "Daily context",
                    "file": "qmd://daily/2026-04-05.md",
                    "rel_path": "daily/2026-04-05.md",
                    "snippet": "QMD planner fix landed this week.",
                    "effective_score": 0.9,
                },
            ],
        }

    monkeypatch.setattr(QmdService, "recall", fake_recall)

    block = build_qmd_recall_block(
        vault_path,
        task="Что мы решили по qmd planner на этой неделе?",
        purpose="question_answer",
        config=RecallPlannerConfig(model="gpt-test", api_key="key", language="ru"),
    )

    assert calls == ["qmd planner", "qmd planner weekly"]
    assert "QMD Planner Weekly" in block
    assert "Daily context" in block
    assert "PLAUD transcript" in block
    assert block.index("QMD Planner Weekly") < block.index("PLAUD transcript")
    assert block.index("Daily context") < block.index("PLAUD transcript")


def test_build_qmd_recall_block_keeps_floor_results_when_confidence_is_low(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(vault_path)
    monkeypatch.setattr(
        recall_planner_module,
        "plan_qmd_recall",
        lambda *args, **kwargs: RecallPlan(
            use_recall=True,
            query="weak signal",
            fallback_query="",
            deep=False,
            limit=10,
            reason="status lookup",
        ),
    )

    def fake_recall(
        self, query: str, *, deep: bool = False, limit: int = 5
    ) -> dict[str, object]:
        del query, deep
        assert limit == 100
        return {
            "query": "weak signal",
            "backend": "query",
            "mode": "recall",
            "results": [
                {
                    "title": f"Weak hit {index}",
                    "file": f"qmd://thoughts/weak-{index}.md",
                    "rel_path": f"thoughts/weak-{index}.md",
                    "snippet": "почти нерелевантно",
                    "effective_score": round(0.29 - index * 0.01, 2),
                    "confidence": round(0.29 - index * 0.01, 2),
                }
                for index in range(12)
            ],
        }

    monkeypatch.setattr(QmdService, "recall", fake_recall)

    block = build_qmd_recall_block(
        vault_path,
        task="Какой сейчас статус по проекту?",
        purpose="question_answer",
        config=RecallPlannerConfig(model="gpt-test", api_key="key", language="ru"),
    )

    assert "Weak hit 0" in block
    assert "Weak hit 9" in block
    assert "Weak hit 10" not in block
    assert "Weak hit 11" not in block


def test_build_qmd_recall_block_caps_high_confidence_results_at_twenty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(vault_path)
    monkeypatch.setattr(
        recall_planner_module,
        "plan_qmd_recall",
        lambda *args, **kwargs: RecallPlan(
            use_recall=True,
            query="strong signal",
            fallback_query="",
            deep=False,
            limit=20,
            reason="history lookup",
        ),
    )

    def fake_recall(
        self, query: str, *, deep: bool = False, limit: int = 5
    ) -> dict[str, object]:
        del query, deep
        assert limit == 100
        return {
            "query": "strong signal",
            "backend": "query",
            "mode": "recall",
            "results": [
                {
                    "title": f"Strong hit {index}",
                    "file": f"qmd://thoughts/strong-{index}.md",
                    "rel_path": f"thoughts/strong-{index}.md",
                    "snippet": "уверенный результат",
                    "effective_score": round(0.95 - index * 0.01, 2),
                    "confidence": round(0.95 - index * 0.01, 2),
                }
                for index in range(25)
            ],
        }

    monkeypatch.setattr(QmdService, "recall", fake_recall)

    block = build_qmd_recall_block(
        vault_path,
        task="Что у нас было раньше по этой теме?",
        purpose="question_answer",
        config=RecallPlannerConfig(model="gpt-test", api_key="key", language="ru"),
    )

    assert "Strong hit 0" in block
    assert "Strong hit 19" in block
    assert "Strong hit 20" not in block
    assert "Strong hit 24" not in block


def test_build_qmd_recall_block_limits_fulltext_followup_to_seven_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(vault_path)
    monkeypatch.setattr(
        recall_planner_module,
        "plan_qmd_recall",
        lambda *args, **kwargs: RecallPlan(
            use_recall=True,
            query="project history",
            fallback_query="",
            deep=False,
            limit=20,
            reason="needs timeline",
        ),
    )

    def fake_recall(
        self, query: str, *, deep: bool = False, limit: int = 5
    ) -> dict[str, object]:
        del self, query, deep
        assert limit == 100
        return {
            "query": "project history",
            "backend": "query",
            "mode": "recall",
            "results": [
                {
                    "title": "Memory",
                    "file": "qmd://core/MEMORY.md",
                    "rel_path": "MEMORY.md",
                    "snippet": "memory",
                    "effective_score": 0.95,
                    "confidence": 0.95,
                    "record_date": "",
                },
                {
                    "title": "Weekly goals",
                    "file": "qmd://goals/3-weekly.md",
                    "rel_path": "goals/3-weekly.md",
                    "snippet": "goals",
                    "effective_score": 0.94,
                    "confidence": 0.94,
                    "record_date": "",
                },
                {
                    "title": "CRM",
                    "file": "qmd://business/crm.md",
                    "rel_path": "business/crm.md",
                    "snippet": "crm",
                    "effective_score": 0.93,
                    "confidence": 0.93,
                    "record_date": "",
                },
                {
                    "title": "Projects",
                    "file": "qmd://projects/projects.md",
                    "rel_path": "projects/projects.md",
                    "snippet": "projects",
                    "effective_score": 0.92,
                    "confidence": 0.92,
                    "record_date": "",
                },
                {
                    "title": "Thought",
                    "file": "qmd://thoughts/a.md",
                    "rel_path": "thoughts/a.md",
                    "snippet": "thought",
                    "effective_score": 0.91,
                    "confidence": 0.91,
                    "record_date": "2026-04-01",
                },
                {
                    "title": "Daily",
                    "file": "qmd://daily/2026-04-03.md",
                    "rel_path": "daily/2026-04-03.md",
                    "snippet": "daily",
                    "effective_score": 0.90,
                    "confidence": 0.90,
                    "record_date": "2026-04-03",
                },
                {
                    "title": "Plaud",
                    "file": "qmd://plaud/2026/03/a.md",
                    "rel_path": "imports/plaud/notes/2026/03/a.md",
                    "snippet": "plaud",
                    "effective_score": 0.89,
                    "confidence": 0.89,
                    "record_date": "2026-03-20",
                },
                {
                    "title": "Summary",
                    "file": "qmd://summaries/2026-W13.md",
                    "rel_path": "summaries/2026-W13.md",
                    "snippet": "summary",
                    "effective_score": 0.88,
                    "confidence": 0.88,
                    "record_date": "2026-03-30",
                },
            ],
        }

    monkeypatch.setattr(QmdService, "recall", fake_recall)

    block = build_qmd_recall_block(
        vault_path,
        task="Расскажи историю проекта и ключевые моменты",
        purpose="question_answer",
        config=RecallPlannerConfig(model="gpt-test", api_key="key", language="ru"),
    )

    section = re.search(
        r"FULLTEXT FOLLOW-UP:\n(.*?)\nHISTORY CHECKLIST:",
        block,
        re.DOTALL,
    )
    assert section is not None
    followup_lines = [
        line
        for line in section.group(1).splitlines()
        if re.match(r"^\d+\.\s", line)
    ]
    assert len(followup_lines) == 7
    assert "MEMORY.md" in section.group(1)
    assert "goals/3-weekly.md" in section.group(1)
    assert "imports/plaud/notes/2026/03/a.md" in section.group(1)
    assert "summaries/2026-W13.md" not in section.group(1)


def test_build_qmd_recall_block_adds_deep_history_backfill_for_shallow_timeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(vault_path)
    monkeypatch.setattr(
        recall_planner_module,
        "plan_qmd_recall",
        lambda *args, **kwargs: RecallPlan(
            use_recall=True,
            query="митигация хранилища",
            fallback_query="",
            history_scope="recent",
            deep=False,
            limit=20,
            reason="status timeline",
        ),
    )
    calls: list[tuple[str, bool, int]] = []

    def fake_recall(
        self, query: str, *, deep: bool = False, limit: int = 5
    ) -> dict[str, object]:
        del self
        calls.append((query, deep, limit))
        assert limit == 100
        if not deep:
            return {
                "query": query,
                "backend": "query",
                "mode": "recall",
                "results": [
                    {
                        "title": "Fresh status 1",
                        "file": "qmd://daily/2026-04-03.md",
                        "rel_path": "daily/2026-04-03.md",
                        "snippet": "recent update",
                        "effective_score": 0.92,
                        "confidence": 0.92,
                        "record_date": "2026-04-03",
                        "age_days": 2,
                    },
                    {
                        "title": "Fresh status 2",
                        "file": "qmd://daily/2026-03-27.md",
                        "rel_path": "daily/2026-03-27.md",
                        "snippet": "recent follow-up",
                        "effective_score": 0.90,
                        "confidence": 0.90,
                        "record_date": "2026-03-27",
                        "age_days": 9,
                    },
                ],
            }
        return {
            "query": query,
            "backend": "query",
            "mode": "deep-recall",
            "results": [
                {
                    "title": "Older milestone",
                    "file": "qmd://imports/plaud/2025-12-05.md",
                    "rel_path": (
                        "imports/plaud/notes/2025/12/"
                        "2025-12-05-110250-ec6c392cd6cb113b10e8abb49da8559d.md"
                    ),
                    "snippet": "older milestone",
                    "effective_score": 0.84,
                    "confidence": 0.84,
                    "record_date": "2025-12-05",
                    "age_days": 121,
                }
            ],
        }

    monkeypatch.setattr(QmdService, "recall", fake_recall)

    block = build_qmd_recall_block(
        vault_path,
        task="Какой сейчас статус у проекта Митигация хранилища?",
        purpose="question_answer",
        config=RecallPlannerConfig(model="gpt-test", api_key="key", language="ru"),
    )

    assert calls == [
        ("митигация хранилища", False, 100),
        ("митигация хранилища", True, 100),
    ]
    assert "Older milestone" in block


def test_build_qmd_recall_block_uses_planner_history_scope_for_deep_recall(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(vault_path)
    monkeypatch.setattr(
        recall_planner_module,
        "plan_qmd_recall",
        lambda *args, **kwargs: RecallPlan(
            use_recall=True,
            query="митигация хранилища",
            fallback_query="",
            history_scope="year",
            history_start_hint="2025-04-01",
            deep=False,
            limit=20,
            reason="year-long project status",
        ),
    )
    calls: list[tuple[str, bool, int]] = []

    def fake_recall(
        self, query: str, *, deep: bool = False, limit: int = 5
    ) -> dict[str, object]:
        del self
        calls.append((query, deep, limit))
        assert limit == 100
        return {
            "query": query,
            "backend": "query",
            "mode": "deep-recall" if deep else "recall",
            "results": [
                {
                    "title": "Status",
                    "file": "qmd://daily/2026-04-03.md",
                    "rel_path": "daily/2026-04-03.md",
                    "snippet": "status",
                    "effective_score": 0.9,
                    "confidence": 0.9,
                    "record_date": "2026-04-03",
                    "age_days": 2,
                }
            ],
        }

    monkeypatch.setattr(QmdService, "recall", fake_recall)

    block = build_qmd_recall_block(
        vault_path,
        task="Какой сейчас статус у проекта Митигация хранилища?",
        purpose="question_answer",
        config=RecallPlannerConfig(model="gpt-test", api_key="key", language="ru"),
    )

    assert calls == [
        ("митигация хранилища", False, 100),
        ("митигация хранилища", True, 100),
    ]
    assert "History scope: year" in block
    assert "Analyze history from: 2025-04-01" in block


@pytest.mark.parametrize(
    "disable_thinking, expect_extra_body",
    [
        (False, "absent"),
        (True, {"thinking": {"type": "disabled"}}),
    ],
)
def test_plan_qmd_recall_extra_body_thinking(
    disable_thinking: bool,
    expect_extra_body: object,
) -> None:
    """Pass extra_body thinking:disabled only when the flag is set."""
    captured: dict[str, object] = {}

    def fake_create(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"use_recall": false, "query": "", "reason": "x"}',
                    ),
                ),
            ],
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
    )
    config = RecallPlannerConfig(
        model="test-model",
        api_key="key",
        base_url="https://api.test/v1",
        language="ru",
        disable_thinking=disable_thinking,
    )

    plan_qmd_recall(
        "тест",
        purpose="question",
        config=config,
        client=client,
    )

    actual = captured.get("extra_body", "absent")
    assert actual == expect_extra_body
    # Common shape is preserved either way.
    assert captured["model"] == "test-model"
    assert captured["temperature"] == 0
    assert captured["response_format"] == {"type": "json_object"}
