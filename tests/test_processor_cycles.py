from datetime import date
from pathlib import Path
from typing import Any

import pytest
from conftest import _markdown_section, _write_vault_manifest

from d_brain.services.processor import (
    SCHEDULED_MODE,
    CliProcessor,
)
from d_brain.services.vault_lock import vault_write_lock


@pytest.fixture(autouse=True)
def _cycle_manifest(tmp_path: Path) -> None:
    _write_vault_manifest(tmp_path / "vault")


def _setup_weekly_digest_context(
    vault_path: Path,
    *,
    target_day: date | None = None,
) -> None:
    day = target_day or date.today()
    iso_year, iso_week, _ = day.isocalendar()
    goals_path = vault_path / "goals"
    goals_path.mkdir(parents=True, exist_ok=True)
    (vault_path / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (goals_path / "2-monthly.md").write_text("monthly\n", encoding="utf-8")
    (goals_path / "1-yearly.md").write_text("yearly\n", encoding="utf-8")
    (goals_path / "3-weekly.md").write_text(
        (f"---\nweek: {iso_year}-W{iso_week:02d}\n---\n\n# Weekly Focus\n"),
        encoding="utf-8",
    )


def test_generate_weekly_refreshes_qmd_after_summary_write(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "summaries").mkdir(parents=True)
    (vault_path / "MOC").mkdir()
    (vault_path / "MOC" / "MOC-weekly.md").write_text(
        (
            "---\n"
            "type: note\n"
            "description: Weekly summary index\n"
            "last_accessed: 2026-04-04\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Weekly Summary\n\n## Previous Weeks\n"
        ),
        encoding="utf-8",
    )
    _setup_weekly_digest_context(vault_path)

    processor = CliProcessor(vault_path)
    maintenance = {"qmd": False}
    processor._run_prompt = lambda prompt: "📅 **Недельный дайджест**"  # type: ignore[method-assign]
    processor._refresh_qmd_index = lambda: maintenance.__setitem__("qmd", True)  # type: ignore[method-assign]

    result = processor.generate_weekly()

    assert result["report"] == "📅 **Недельный дайджест**"
    assert maintenance == {"qmd": True}
    assert any((vault_path / "summaries").glob("*-summary.md"))
    moc = (vault_path / "MOC" / "MOC-weekly.md").read_text(encoding="utf-8")
    assert 'type: "index"' in moc


def test_periodic_summary_uses_derived_profile(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    summary_path = CliProcessor(vault_path)._save_monthly_summary(
        "# Monthly\n\nBody.", date(2026, 4, 30)
    )

    content = summary_path.read_text(encoding="utf-8")
    assert "type: monthly-summary" in content
    assert "description: monthly summary for 2026-04" in content
    assert "last_accessed: 2026-04-30" in content
    assert "relevance: 1.0" in content
    assert "tier: active" in content


def test_generate_weekly_prompt_reads_core_context_and_forbids_goal_rewrites(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir(parents=True)
    _setup_weekly_digest_context(vault_path)

    processor = CliProcessor(vault_path, content_language="en")
    captured: dict[str, str] = {}
    processor._save_weekly_summary = lambda report_markdown, week_date: (  # type: ignore[method-assign]
        vault_path / "summaries" / f"{week_date.isoformat()}-summary.md"
    )
    processor._update_weekly_moc = lambda summary_path: None  # type: ignore[method-assign]
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    def fake_run(prompt: str) -> str:
        captured["prompt"] = prompt
        return "📅 **Недельный дайджест**"

    processor._run_prompt = fake_run  # type: ignore[method-assign]

    result = processor.generate_weekly()

    prompt = captured["prompt"]
    assert result["report"] == "📅 **Недельный дайджест**"
    assert "in English" in prompt
    assert "MEMORY.md" in prompt
    assert "goals/3-weekly.md" in prompt
    assert "goals/2-monthly.md" in prompt
    assert "goals/1-yearly.md" in prompt
    assert ".session/handoff.md" in prompt
    assert "find-tasks-by-date" in prompt
    assert "Do not rewrite goal files automatically." in prompt
    assert "Return ONLY markdown, not HTML." in prompt
    assert "Start with one short markdown heading for the digest." in prompt


def test_generate_weekly_normalizes_meta_preamble_and_markdown_output(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "summaries").mkdir(parents=True)
    _setup_weekly_digest_context(vault_path)
    processor = CliProcessor(vault_path)
    processor._save_weekly_summary = lambda report_markdown, week_date: (  # type: ignore[method-assign]
        vault_path / "summaries" / f"{week_date.isoformat()}-summary.md"
    )
    processor._update_weekly_moc = lambda summary_path: None  # type: ignore[method-assign]
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    processor._run_prompt = lambda prompt: (  # type: ignore[method-assign]
        "Вот готовый Telegram HTML-дайджест:\n\n"
        "📅 **Недельный дайджест**\n\n"
        "*Короткий итог*"
    )

    result = processor.generate_weekly_digest(refresh_qmd=False)

    assert result["report"].startswith("📅 **Недельный дайджест**")
    assert "Вот готовый" not in result["report"]
    assert "*Короткий итог*" in result["report"]


def test_generate_weekly_prompt_is_localized_for_russian(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _setup_weekly_digest_context(vault_path)
    processor = CliProcessor(vault_path)
    captured: dict[str, str] = {}
    processor._save_weekly_summary = lambda report_markdown, week_date: (  # type: ignore[method-assign]
        vault_path / "summaries" / f"{week_date.isoformat()}-summary.md"
    )
    processor._update_weekly_moc = lambda summary_path: None  # type: ignore[method-assign]
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    def fake_run(prompt: str) -> str:
        captured["prompt"] = prompt
        return "📅 **Недельный дайджест**"

    processor._run_prompt = fake_run  # type: ignore[method-assign]

    processor.generate_weekly_digest(refresh_qmd=False)

    prompt = captured["prompt"]
    assert "ПРАВИЛА НЕДЕЛЬНОГО РАЗБОРА:" in prompt
    assert "ПРАВИЛА РАБОТЫ С TODOIST:" in prompt
    assert "ПОРЯДОК РАБОТЫ:" in prompt
    assert "WEEKLY REVIEW RULES" not in prompt


def test_generate_weekly_blocks_when_weekly_goals_need_rollover(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    goals_path = vault_path / "goals"
    goals_path.mkdir(parents=True)
    (vault_path / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (goals_path / "2-monthly.md").write_text("monthly\n", encoding="utf-8")
    (goals_path / "1-yearly.md").write_text("yearly\n", encoding="utf-8")
    (goals_path / "3-weekly.md").write_text(
        "---\nweek: 2026-W15\n---\n\n# Weekly Focus\n",
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)

    result = processor.generate_weekly_digest(day=date(2026, 4, 17), refresh_qmd=False)

    assert result["processed_entries"] == 0
    assert result["rollover_required"] is True
    assert "нужен rollover goals/3-weekly.md с 2026-W15 на 2026-W16" in result["error"]


def test_rollover_weekly_goals_switches_to_next_iso_week(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    goals_path = vault_path / "goals"
    goals_path.mkdir(parents=True)
    (vault_path / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (goals_path / "3-weekly.md").write_text(
        (
            "---\n"
            "type: weekly\n"
            "updated: 2026-05-08\n"
            "last_accessed: 2026-05-08\n"
            "week: 2026-W19\n"
            "---\n\n"
            "# Weekly Focus\n\n"
            "## ONE Big Thing\n\n"
            "> **If I accomplish nothing else, I will:**\n"
            "> Старый фокус недели.\n\n"
            "<!-- This is read by the bot during daily processing -->\n\n"
            "---\n\n"
            "## Week at a Glance\n\n"
            "**Week:** 19 of 53\n\n"
            "---\n\n"
            "## End of Week Review\n\n"
            "### Next Week Focus\n\n"
            "> Новый фокус следующей недели.\n\n"
            "---\n\n"
            "## Links\n\n"
            "- Previous: 2026-W18\n\n"
            "---\n\n"
            "*Week Started: 2026-05-04*\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)

    result = processor.rollover_weekly_goals(
        day=date(2026, 5, 10),
        refresh_qmd=False,
    )

    content = (goals_path / "3-weekly.md").read_text(encoding="utf-8")
    assert result["processed_entries"] == 1
    assert result["from_week"] == "2026-W19"
    assert result["to_week"] == "2026-W20"
    assert "updated: 2026-05-10" in content
    assert "last_accessed: 2026-05-10" in content
    assert "week: 2026-W20" in content
    assert "> Новый фокус следующей недели." in content
    assert "> Старый фокус недели." not in content
    assert "**Week:** 20 of 53" in content
    assert "- Previous: 2026-W19" in content
    assert "*Week Started: 2026-05-11*" in content
    assert processor._weekly_rollover_guard(date(2026, 5, 11)) is None


def test_rollover_weekly_goals_updates_russian_template(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    goals_path = vault_path / "goals"
    goals_path.mkdir(parents=True)
    (vault_path / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (goals_path / "3-weekly.md").write_text(
        (
            "---\n"
            "type: weekly\n"
            "updated: 2026-07-17\n"
            "last_accessed: 2026-07-18\n"
            "week: 2026-W29\n"
            "---\n\n"
            "# Фокус недели\n\n"
            "## Главное\n\n"
            "> **Если я выполню только одно, то:**\n"
            "> Старый фокус недели.\n\n"
            "<!-- This is read by the bot during daily processing -->\n\n"
            "---\n\n"
            "## Неделя в целом\n\n"
            "**Неделя:** 29 из 53\n\n"
            "---\n\n"
            "## Итоги недели\n\n"
            "### Фокус следующей недели\n\n"
            "> Новый фокус следующей недели.\n\n"
            "---\n\n"
            "## Ссылки\n\n"
            "- Предыдущая неделя: 2026-W28\n\n"
            "---\n\n"
            "*Неделя началась: 2026-07-13*\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)

    result = processor.rollover_weekly_goals(
        day=date(2026, 7, 19),
        refresh_qmd=False,
    )

    content = (goals_path / "3-weekly.md").read_text(encoding="utf-8")
    assert result["from_week"] == "2026-W29"
    assert result["to_week"] == "2026-W30"
    assert "updated: 2026-07-19" in content
    assert "last_accessed: 2026-07-19" in content
    assert "week: 2026-W30" in content
    assert "> Новый фокус следующей недели." in content
    assert "> Старый фокус недели." not in content
    assert "**Неделя:** 30 из 53" in content
    assert "- Предыдущая неделя: 2026-W29" in content
    assert "*Неделя началась: 2026-07-20*" in content


def test_generate_weekly_system_reflection_skip_uses_content_language(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / ".session").mkdir(parents=True)
    (vault_path / ".session" / "handoff.md").write_text(
        (
            "---\n"
            "type: note\n"
            "last_accessed: 2026-04-05\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Передача сессии\n\n"
            "## Last Session\n(none)\n\n"
            "## Key Decisions\n- (none)\n\n"
            "## In Progress\n- (none)\n\n"
            "## Next Steps\n- (none)\n\n"
            "## Observations\n- (none)\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path, content_language="en")

    result = processor.generate_weekly_system_reflection(refresh_qmd=False)

    assert "System Reflection" in result["report"]
    assert "No new system signals." in result["report"]


def test_generate_weekly_system_reflection_skips_without_observations(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / ".session").mkdir(parents=True)
    (vault_path / ".session" / "handoff.md").write_text(
        (
            "---\n"
            "type: note\n"
            "last_accessed: 2026-04-05\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Передача сессии\n\n"
            "## Last Session\n(none)\n\n"
            "## Key Decisions\n- (none)\n\n"
            "## In Progress\n- (none)\n\n"
            "## Next Steps\n- (none)\n\n"
            "## Observations\n- (none)\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)

    result = processor.generate_weekly_system_reflection(refresh_qmd=False)

    assert result["skipped"] is True
    assert result["created_reflection"] is False
    assert result["processed_entries"] == 0
    assert result["carry_forward_observations"] == []
    assert result["report"].startswith("🛠 **Системная рефлексия**")


def test_generate_weekly_system_reflection_writes_note_and_updates_handoff(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    session_path = vault_path / ".session"
    graph_path = vault_path / ".graph"
    daily_path = vault_path / "daily"
    rules_path = vault_path / ".claude" / "rules"
    session_path.mkdir(parents=True)
    graph_path.mkdir(parents=True)
    daily_path.mkdir(parents=True)
    rules_path.mkdir(parents=True)
    (daily_path / f"{date.today().isoformat()}.md").write_text(
        f"# {date.today().isoformat()}\n",
        encoding="utf-8",
    )
    (rules_path / "weekly-reflection.md").write_text(
        "weekly reflection rule",
        encoding="utf-8",
    )
    (graph_path / "health-history.json").write_text(
        '[{"date":"2026-04-05","score":0.9}]',
        encoding="utf-8",
    )
    (session_path / "handoff.md").write_text(
        (
            "---\n"
            "type: note\n"
            "last_accessed: 2026-04-05\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Передача сессии\n\n"
            "## Last Session\nDone.\n\n"
            "## Key Decisions\n- keep\n\n"
            "## In Progress\n- weekly\n\n"
            "## Next Steps\n- reflect\n\n"
            "## Observations\n"
            "- [pattern] existing pattern\n"
            "- [idea] keep this\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)
    refresh_calls = {"count": 0}
    processor._refresh_qmd_index = lambda: refresh_calls.__setitem__(  # type: ignore[method-assign]
        "count",
        refresh_calls["count"] + 1,
    )
    processor._run_json_phase = lambda prompt, phase_name: {  # type: ignore[method-assign]
        "create_reflection": True,
        "title": "System Reflection",
        "report_highlights": ["Найден повторяющийся сигнал."],
        "watch_next_week": ["Проверить broken link"],
        "reflection_markdown": "## Friction Patterns\n- repeated issue\n",
        "carry_forward_observations": ["- [idea] keep this"],
    }

    result = processor.generate_weekly_system_reflection(refresh_qmd=True)

    assert result["created_reflection"] is True
    assert result["processed_observations"] == 1
    assert result["carry_forward_observations"] == ["- [idea] keep this"]
    assert refresh_calls["count"] == 1
    assert "### Ключевые выводы" in result["report"]
    assert "Проверить broken link" in result["report"]

    note_paths = list(
        (vault_path / "thoughts" / "reflections").glob("*-system-reflection.md")
    )
    assert len(note_paths) == 1
    note_content = note_paths[0].read_text(encoding="utf-8")
    assert "# System Reflection" in note_content
    assert "## Friction Patterns" in note_content
    assert "type: reflection" in note_content
    assert "tags: [system, weekly-reflection]" in note_content
    assert "created:" in note_content
    assert "updated:" in note_content

    handoff_content = (session_path / "handoff.md").read_text(encoding="utf-8")
    observations_block = _markdown_section(handoff_content, "Observations")
    assert observations_block == "- [idea] keep this"

    daily_content = (daily_path / f"{date.today().isoformat()}.md").read_text(
        encoding="utf-8"
    )
    assert "Weekly system reflection:" in daily_content
    assert "Processed observations: 1" in daily_content


def test_weekly_reflection_preserves_observation_added_during_llm(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    session_path = vault_path / ".session"
    daily_path = vault_path / "daily"
    session_path.mkdir(parents=True)
    daily_path.mkdir(parents=True)
    today = date.today().isoformat()
    (daily_path / f"{today}.md").write_text(
        f"# {today}\n",
        encoding="utf-8",
    )
    snapshot = [
        "- [pattern] process this",
        "- [idea] carry this",
    ]
    concurrent = "- [friction] added during LLM"
    (session_path / "handoff.md").write_text(
        (
            "---\n"
            "type: note\n"
            f"last_accessed: {today}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Передача сессии\n\n"
            "## Last Session\nDone.\n\n"
            "## Key Decisions\n- keep\n\n"
            "## In Progress\n- weekly\n\n"
            "## Next Steps\n- reflect\n\n"
            "## Observations\n"
            f"{snapshot[0]}\n"
            f"{snapshot[1]}\n"
        ),
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)

    def fake_llm(prompt: str, phase_name: str) -> dict[str, Any]:
        del prompt, phase_name
        processor._write_handoff_observations([*snapshot, concurrent])
        return {
            "create_reflection": False,
            "title": "",
            "report_markdown": "",
            "reflection_markdown": "",
            "carry_forward_observations": [snapshot[1]],
        }

    processor._run_json_phase = fake_llm  # type: ignore[method-assign]

    result = processor.generate_weekly_system_reflection(refresh_qmd=False)

    assert result["processed_observations"] == 0
    assert result["carry_forward_observations"] == [*snapshot, concurrent]
    observations = _markdown_section(
        (session_path / "handoff.md").read_text(encoding="utf-8"),
        "Observations",
    )
    assert snapshot[0] in observations
    assert snapshot[1] in observations
    assert concurrent in observations


def test_weekly_reflection_preserves_same_text_concurrent_observation(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    session_path = vault_path / ".session"
    daily_path = vault_path / "daily"
    session_path.mkdir(parents=True)
    daily_path.mkdir(parents=True)
    today = date.today().isoformat()
    (daily_path / f"{today}.md").write_text(f"# {today}\n", encoding="utf-8")
    snapshot = [
        "- [pattern] same text",
        "- [idea] carry this",
    ]
    (session_path / "handoff.md").write_text(
        (
            "---\n"
            "type: note\n"
            f"last_accessed: {today}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Передача сессии\n\n"
            "## Last Session\nDone.\n\n"
            "## Key Decisions\n- keep\n\n"
            "## In Progress\n- weekly\n\n"
            "## Next Steps\n- reflect\n\n"
            "## Observations\n"
            f"{snapshot[0]}\n"
            f"{snapshot[1]}\n"
        ),
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)

    def fake_llm(prompt: str, phase_name: str) -> dict[str, Any]:
        del prompt, phase_name
        handoff_path = session_path / "handoff.md"
        with vault_write_lock(vault_path) as lock:
            current = handoff_path.read_text(encoding="utf-8")
            concurrent = current.replace(
                f"{snapshot[0]}\n{snapshot[1]}",
                f"{snapshot[0]}\n{snapshot[0]}\n{snapshot[1]}",
                1,
            )
            assert concurrent != current
            processor._write_vault_markdown(
                handoff_path,
                concurrent,
                lock=lock,
            )
        return {
            "create_reflection": False,
            "title": "",
            "report_markdown": "",
            "reflection_markdown": "",
            "carry_forward_observations": [snapshot[1]],
        }

    processor._run_json_phase = fake_llm  # type: ignore[method-assign]

    result = processor.generate_weekly_system_reflection(refresh_qmd=False)

    assert result["processed_observations"] == 0
    assert result["carry_forward_observations"] == snapshot
    observations = _markdown_section(
        (session_path / "handoff.md").read_text(encoding="utf-8"),
        "Observations",
    )
    assert observations.splitlines().count(snapshot[0]) == 2
    assert snapshot[1] in observations


def test_weekly_reflection_removes_processed_snapshot_without_concurrency(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    session_path = vault_path / ".session"
    daily_path = vault_path / "daily"
    session_path.mkdir(parents=True)
    daily_path.mkdir(parents=True)
    today = date.today().isoformat()
    (daily_path / f"{today}.md").write_text(f"# {today}\n", encoding="utf-8")
    snapshot = [
        "- [pattern] process this",
        "- [idea] carry this",
    ]
    (session_path / "handoff.md").write_text(
        (
            "---\n"
            "type: note\n"
            f"last_accessed: {today}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Передача сессии\n\n"
            "## Last Session\nDone.\n\n"
            "## Key Decisions\n- keep\n\n"
            "## In Progress\n- weekly\n\n"
            "## Next Steps\n- reflect\n\n"
            "## Observations\n"
            f"{snapshot[0]}\n"
            f"{snapshot[1]}\n"
        ),
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)
    processor._run_json_phase = lambda prompt, phase_name: {  # type: ignore[method-assign]
        "create_reflection": False,
        "title": "",
        "report_markdown": "",
        "reflection_markdown": "",
        "carry_forward_observations": [snapshot[1]],
    }

    result = processor.generate_weekly_system_reflection(refresh_qmd=False)

    assert result["processed_observations"] == 1
    assert result["carry_forward_observations"] == [snapshot[1]]
    observations = _markdown_section(
        (session_path / "handoff.md").read_text(encoding="utf-8"),
        "Observations",
    )
    assert snapshot[0] not in observations
    assert snapshot[1] in observations


def test_generate_weekly_system_reflection_retains_unresolved_observations(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    session_path = vault_path / ".session"
    graph_path = vault_path / ".graph"
    daily_path = vault_path / "daily"
    rules_path = vault_path / ".claude" / "rules"
    session_path.mkdir(parents=True)
    graph_path.mkdir(parents=True)
    daily_path.mkdir(parents=True)
    rules_path.mkdir(parents=True)
    today = date.today().isoformat()
    (daily_path / f"{today}.md").write_text(f"# {today}\n", encoding="utf-8")
    (rules_path / "weekly-reflection.md").write_text(
        "weekly reflection rule",
        encoding="utf-8",
    )
    (graph_path / "health-history.json").write_text("[]", encoding="utf-8")
    (session_path / "handoff.md").write_text(
        (
            "---\n"
            "type: note\n"
            "last_accessed: 2026-04-05\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Передача сессии\n\n"
            "## Last Session\nDone.\n\n"
            "## Key Decisions\n- keep\n\n"
            "## In Progress\n- weekly\n\n"
            "## Next Steps\n- reflect\n\n"
            "## Observations\n"
            "- [pattern] keep this\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    processor._run_json_phase = lambda prompt, phase_name: {  # type: ignore[method-assign]
        "create_reflection": False,
        "title": "",
        "report_markdown": "",
        "reflection_markdown": "",
        "carry_forward_observations": [],
    }

    result = processor.generate_weekly_system_reflection(refresh_qmd=False)

    assert result["created_reflection"] is False
    assert result["carry_forward_observations"] == ["- [pattern] keep this"]
    assert "оставлены на следующий weekly pass" in result["report"]

    daily_content = (daily_path / f"{today}.md").read_text(encoding="utf-8")
    assert "unresolved observations" in daily_content
    assert "Carry forward: 1" in daily_content


def test_build_weekly_system_report_markdown_is_deterministic(
    tmp_path: Path,
) -> None:
    processor = CliProcessor(tmp_path / "vault")

    report = processor._build_weekly_system_report_markdown(
        title="System Reflection 2026-W14",
        highlights=["Resolved md-native weekly contract."],
        watch_items=["Fix 1 broken link next week."],
        graph_history=[
            {
                "health_score": 65.5,
                "total_links": 251,
                "orphans": 11,
                "weakly_connected": 197,
            },
            {
                "health_score": 94.2,
                "total_links": 468,
                "orphans": 0,
                "weakly_connected": 13,
            },
        ],
        carry_forward_count=0,
    )

    assert report.startswith("## 🛠 System Reflection 2026-W14")
    assert "### Ключевые выводы" in report
    assert "65.5 → 94.2 (+28.7)" in report
    assert "Fix 1 broken link next week." in report
    assert "### Наблюдения на перенос" in report


def test_run_weekly_cycle_combines_digest_and_system_reflection_reports(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path)
    refresh_calls = {"count": 0}

    processor.generate_weekly_digest = lambda refresh_qmd=False: {  # type: ignore[method-assign]
        "report": "📅 **Недельный дайджест**\n\nDigest body",
        "processed_entries": 1,
    }
    processor.generate_weekly_system_reflection = (  # type: ignore[method-assign]
        lambda refresh_qmd=False: {
            "report": "🛠 **Системная рефлексия**\n\nReflection body",
            "processed_entries": 2,
        }
    )
    processor._refresh_qmd_index = lambda: refresh_calls.__setitem__(  # type: ignore[method-assign]
        "count",
        refresh_calls["count"] + 1,
    )

    result = processor.run_weekly_cycle()

    assert "📅 **Недельный дайджест**" in result["report"]
    assert "🛠 **Системная рефлексия**" in result["report"]
    assert result["processed_entries"] == 3
    assert refresh_calls["count"] == 1


def test_generate_weekly_system_reflection_clears_identical_carry_forward(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    session_path = vault_path / ".session"
    graph_path = vault_path / ".graph"
    daily_path = vault_path / "daily"
    rules_path = vault_path / ".claude" / "rules"
    session_path.mkdir(parents=True)
    graph_path.mkdir(parents=True)
    daily_path.mkdir(parents=True)
    rules_path.mkdir(parents=True)
    today = date.today().isoformat()
    (daily_path / f"{today}.md").write_text(f"# {today}\n", encoding="utf-8")
    (rules_path / "weekly-reflection.md").write_text("rule", encoding="utf-8")
    (graph_path / "health-history.json").write_text("[]", encoding="utf-8")
    observations = [
        "- [pattern] first signal",
        "- [pattern] second signal",
    ]
    (session_path / "handoff.md").write_text(
        (
            "---\n"
            "type: note\n"
            f"last_accessed: {today}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            "# Передача сессии\n\n"
            "## Last Session\nDone.\n\n"
            "## Key Decisions\n- keep\n\n"
            "## In Progress\n- weekly\n\n"
            "## Next Steps\n- reflect\n\n"
            "## Observations\n"
            f"{observations[0]}\n"
            f"{observations[1]}\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    processor._run_json_phase = lambda prompt, phase_name: {  # type: ignore[method-assign]
        "create_reflection": True,
        "title": "System Reflection",
        "report_html": "🛠 <b>Системная рефлексия</b>\n\nDone.",
        "reflection_markdown": "## Patterns\n- grouped\n",
        "carry_forward_observations": observations,
    }

    result = processor.generate_weekly_system_reflection(refresh_qmd=False)

    assert result["carry_forward_observations"] == []
    assert result["processed_observations"] == 2
    handoff_content = (session_path / "handoff.md").read_text(encoding="utf-8")
    assert _markdown_section(handoff_content, "Observations") == "- (none)"


def test_run_monthly_cycle_uses_markdown_contract_and_saves_summary(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "summaries").mkdir(parents=True)
    (vault_path / "goals").mkdir(parents=True)
    (vault_path / "goals" / "0-vision-3y.md").write_text("vision", encoding="utf-8")
    (vault_path / "goals" / "1-yearly-2027.md").write_text("yearly", encoding="utf-8")
    (vault_path / "goals" / "2-monthly.md").write_text("monthly", encoding="utf-8")
    (vault_path / "goals" / "3-weekly.md").write_text("weekly", encoding="utf-8")
    (vault_path / "MEMORY.md").write_text("memory", encoding="utf-8")
    processor = CliProcessor(vault_path, content_language="en")
    captured: dict[str, str] = {}
    refresh_calls = {"count": 0}
    processor._refresh_qmd_index = lambda: refresh_calls.__setitem__(  # type: ignore[method-assign]
        "count",
        refresh_calls["count"] + 1,
    )

    def fake_run(prompt: str) -> str:
        captured["prompt"] = prompt
        return (
            "# Monthly Review\n\n"
            "- **Progress**\n"
            "- Saved `summaries/2027-12-monthly-review.md` and updated `MEMORY.md`.\n"
        )

    processor._run_prompt = fake_run  # type: ignore[method-assign]

    result = processor.run_monthly_cycle(day=date(2027, 12, 31), refresh_qmd=True)

    assert "Return ONLY markdown, not HTML." in captured["prompt"]
    assert "Do not create or edit files yourself." in captured["prompt"]
    assert "in English" in captured["prompt"]
    assert "Read weekly summaries for the same month" in captured["prompt"]
    assert result["summary_path"] == "summaries/2027-12-summary.md"
    assert result["searchable_write"] is True
    assert "2027-12-monthly-review.md" not in result["report"]
    assert "MEMORY.md" not in result["report"]
    assert "2027-12-monthly-review.md" not in (
        vault_path / "summaries" / "2027-12-summary.md"
    ).read_text(encoding="utf-8")
    assert refresh_calls["count"] == 1


def test_run_yearly_cycle_marks_vision_rollover_due(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    (vault_path / "summaries").mkdir(parents=True)
    (vault_path / "goals").mkdir(parents=True)
    (vault_path / "goals" / "0-vision-3y.md").write_text(
        "period: 2026-2028\n",
        encoding="utf-8",
    )
    (vault_path / "goals" / "1-yearly-2028.md").write_text("yearly", encoding="utf-8")
    (vault_path / "goals" / "2-monthly.md").write_text("monthly", encoding="utf-8")
    (vault_path / "MEMORY.md").write_text("memory", encoding="utf-8")
    processor = CliProcessor(vault_path)
    captured: dict[str, str] = {}
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    def fake_run(prompt: str) -> str:
        captured["prompt"] = prompt
        return (
            "# Годовой обзор\n\n"
            "- Итог\n"
            "- Сохранено в `summaries/2028-yearly-review.md`, обновлены "
            "`MEMORY.md` и `handoff.md`.\n"
        )

    processor._run_prompt = fake_run  # type: ignore[method-assign]

    result = processor.run_yearly_cycle(day=date(2028, 12, 31), refresh_qmd=False)

    assert "drafted (2029-2031)" in captured["prompt"]
    assert "Do not create or edit files yourself." in captured["prompt"]
    assert result["vision_rollover_due"] is True
    assert result["summary_path"] == "summaries/2028-summary.md"
    assert "2028-yearly-review.md" not in result["report"]
    assert "MEMORY.md" not in result["report"]


def test_audit_cycle_result_deduplicates_recent_followup_tasks(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path, todoist_api_key="todoist-token")
    created_batches: list[list[str]] = []

    processor._run_json_phase = lambda prompt, phase_name: {  # type: ignore[method-assign]
        "summary": "Found one problem",
        "issues": [
            {
                "title": "Reflection report is contradictory",
                "severity": "high",
                "evidence": "Carry forward says 1 while report says no signals.",
                "action": "Починить weekly system reflection logging",
                "project_hint": "Inbox",
            }
        ],
    }

    def fake_create(
        tasks: list[dict[str, Any]],
        *,
        source_context: str = "",
    ) -> list[dict[str, str]]:
        del source_context
        created_batches.append([task["content"] for task in tasks])
        return [{"id": "1", "content": task["content"]} for task in tasks]

    processor._create_todoist_tasks = fake_create  # type: ignore[method-assign]

    first = processor.audit_cycle_result(
        cycle_name="weekly_system_reflection",
        day=date(2026, 4, 5),
        result={"report": "ok"},
    )
    second = processor.audit_cycle_result(
        cycle_name="weekly_system_reflection",
        day=date(2026, 4, 5),
        result={"report": "ok"},
    )

    assert first["task_candidates"] == ["Починить weekly system reflection logging"]
    assert first["tasks_created"] == [
        {"id": "1", "content": "Починить weekly system reflection logging"}
    ]
    assert second["task_candidates"] == []
    assert second["tasks_created"] == []
    assert created_batches == [["Починить weekly system reflection logging"], []]


def test_clear_session_phase_artifacts_removes_stale_audit_raw_outputs(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    session_dir = vault_path / ".session"
    session_dir.mkdir(parents=True)
    stale_paths = [
        session_dir / "capture-raw-output.txt",
        session_dir / "audit-maintenance.compiled-nightly-raw-output.txt",
        session_dir / "audit-maintenance.compiled-nightly-retry-raw-output.txt",
    ]
    keep_path = session_dir / "audit.json"
    for path in stale_paths:
        path.write_text("stale\n", encoding="utf-8")
    keep_path.write_text("{}\n", encoding="utf-8")
    processor = CliProcessor(vault_path)

    processor._clear_session_phase_artifacts()

    assert all(not path.exists() for path in stale_paths)
    assert keep_path.exists()


def test_run_scheduled_cycle_triggers_due_periodic_reviews_and_audits(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path)
    calls: list[str] = []
    refresh_calls = {"count": 0}

    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 2,
        "mode": mode,
    }

    def fake_weekly(day=None, refresh_qmd=False):  # noqa: ANN001, ANN202
        del day, refresh_qmd
        calls.append("weekly_digest")
        return {
            "report": "📅 **Weekly**",
            "processed_entries": 1,
            "summary_path": "summaries/2027-W52-summary.md",
            "searchable_write": True,
        }

    def fake_monthly(day=None, refresh_qmd=False):  # noqa: ANN001, ANN202
        del day, refresh_qmd
        calls.append("monthly")
        return {
            "report": "🗓 **Monthly**",
            "processed_entries": 1,
            "summary_path": "summaries/2027-12-summary.md",
            "searchable_write": True,
        }

    def fake_yearly(day=None, refresh_qmd=False):  # noqa: ANN001, ANN202
        del day, refresh_qmd
        calls.append("yearly")
        return {
            "report": "🧭 **Yearly**",
            "processed_entries": 1,
            "summary_path": "summaries/2027-summary.md",
            "searchable_write": True,
            "vision_rollover_due": False,
        }

    processor.generate_weekly_digest = fake_weekly  # type: ignore[method-assign]
    processor.run_monthly_cycle = fake_monthly  # type: ignore[method-assign]
    processor.run_yearly_cycle = fake_yearly  # type: ignore[method-assign]
    processor.audit_cycle_result = lambda cycle_name, day, result: {  # type: ignore[method-assign]
        "cycle_name": cycle_name,
        "label": cycle_name,
        "summary": "",
        "issues": [],
        "task_candidates": [f"todo:{cycle_name}"] if cycle_name != "daily" else [],
        "tasks_created": [],
    }
    processor._run_compiled_nightly_maintenance = lambda: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._run_vault_health_cycle = lambda: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._refresh_qmd_index = lambda: refresh_calls.__setitem__(  # type: ignore[method-assign]
        "count",
        refresh_calls["count"] + 1,
    )

    result = processor.run_scheduled_cycle(date(2027, 12, 31))

    assert calls == ["weekly_digest", "monthly", "yearly"]
    assert refresh_calls["count"] == 1
    assert [cycle["name"] for cycle in result["periodic_cycles"]] == [
        "weekly_digest",
        "monthly",
        "yearly",
        "maintenance.compiled-nightly",
        "maintenance.vault-health",
    ]
    assert result["processed_entries"] == 5
    assert result["audit_task_candidates"] == [
        "todo:weekly_digest",
        "todo:monthly",
        "todo:yearly",
        "todo:maintenance.compiled-nightly",
        "todo:maintenance.vault-health",
    ]
    assert "📊 **Daily**" in result["report"]
    assert "🧭 **Yearly**" in result["report"]


def test_run_scheduled_cycle_rolls_weekly_goals_after_sunday_reflection(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    goals_path = vault_path / "goals"
    goals_path.mkdir(parents=True)
    (vault_path / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (goals_path / "3-weekly.md").write_text(
        (
            "---\n"
            "type: weekly\n"
            "updated: 2026-05-08\n"
            "last_accessed: 2026-05-08\n"
            "week: 2026-W19\n"
            "---\n\n"
            "# Weekly Focus\n\n"
            "## ONE Big Thing\n\n"
            "> **If I accomplish nothing else, I will:**\n"
            "> Старый фокус недели.\n\n"
            "---\n\n"
            "## End of Week Review\n\n"
            "### Next Week Focus\n\n"
            "> Новый фокус следующей недели.\n\n"
            "---\n\n"
            "## Links\n\n"
            "- Previous: 2026-W18\n\n"
            "---\n\n"
            "*Week Started: 2026-05-04*\n"
        ),
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)
    refresh_calls = {"count": 0}

    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 2,
        "mode": mode,
    }
    processor.generate_weekly_system_reflection = lambda day=None, refresh_qmd=False: {  # type: ignore[method-assign]
        "report": "🛠 **Reflection**",
        "processed_entries": 1,
        "searchable_write": True,
    }
    processor.audit_cycle_result = lambda cycle_name, day, result: {  # type: ignore[method-assign]
        "cycle_name": cycle_name,
        "label": cycle_name,
        "summary": "",
        "issues": [],
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._run_control_plane_maintenance_workflow = lambda name: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._refresh_qmd_index = lambda: refresh_calls.__setitem__(  # type: ignore[method-assign]
        "count",
        refresh_calls["count"] + 1,
    )

    result = processor.run_scheduled_cycle(date(2026, 5, 10))

    assert [cycle["name"] for cycle in result["periodic_cycles"]][:2] == [
        "weekly_system_reflection",
        "weekly_goals_rollover",
    ]
    assert result["periodic_cycles"][1]["result"]["to_week"] == "2026-W20"
    assert refresh_calls["count"] == 1
    assert "Переключение недельного фокуса" in result["report"]
    assert "week: 2026-W20" in (goals_path / "3-weekly.md").read_text(
        encoding="utf-8",
    )
    assert processor._weekly_rollover_guard(date(2026, 5, 11)) is None


def test_sunday_rollover_runs_when_system_reflection_fails(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    goals_path = vault_path / "goals"
    goals_path.mkdir(parents=True)
    (vault_path / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (goals_path / "3-weekly.md").write_text(
        "---\nweek: 2026-W19\n---\n",
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)

    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 1,
        "mode": mode,
    }
    processor.generate_weekly_system_reflection = lambda day=None, refresh_qmd=False: {  # type: ignore[method-assign]
        "error": "LLM unavailable",
        "processed_entries": 0,
    }
    processor.audit_cycle_result = lambda cycle_name, day, result: {  # type: ignore[method-assign]
        "cycle_name": cycle_name,
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._run_control_plane_maintenance_workflow = lambda name: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    result = processor.run_scheduled_cycle(date(2026, 5, 10))

    assert [cycle["name"] for cycle in result["periodic_cycles"]][:2] == [
        "weekly_system_reflection",
        "weekly_goals_rollover",
    ]
    assert result["periodic_cycles"][0]["result"]["error"] == "LLM unavailable"
    assert result["periodic_cycles"][1]["result"]["to_week"] == "2026-W20"
    assert "week: 2026-W20" in (goals_path / "3-weekly.md").read_text(
        encoding="utf-8",
    )


def test_scheduled_cycle_catches_up_stale_week_after_missed_sunday(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    goals_path = vault_path / "goals"
    goals_path.mkdir(parents=True)
    (vault_path / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (goals_path / "3-weekly.md").write_text(
        (
            "---\n"
            "week: 2026-W29\n"
            "---\n\n"
            "- Previous: 2026-W28\n\n"
            "*Week Started: 2026-07-13*\n"
        ),
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)

    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "error": "Daily LLM unavailable",
        "processed_entries": 0,
        "mode": mode,
    }
    processor.audit_cycle_result = lambda cycle_name, day, result: {  # type: ignore[method-assign]
        "cycle_name": cycle_name,
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._run_control_plane_maintenance_workflow = lambda name: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    result = processor.run_scheduled_cycle(date(2026, 7, 20))

    assert result["periodic_cycles"][0]["name"] == "weekly_goals_rollover"
    assert result["periodic_cycles"][0]["result"]["to_week"] == "2026-W30"
    content = (goals_path / "3-weekly.md").read_text(encoding="utf-8")
    assert "week: 2026-W30" in content
    assert "- Previous: 2026-W29" in content
    assert "*Week Started: 2026-07-20*" in content


def test_sunday_rollover_runs_after_other_due_reviews(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    goals_path = vault_path / "goals"
    goals_path.mkdir(parents=True)
    (vault_path / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (goals_path / "3-weekly.md").write_text(
        (
            "---\n"
            "week: 2026-W22\n"
            "---\n\n"
            "# Weekly Focus\n\n"
            "## ONE Big Thing\n\n"
            "> **If I accomplish nothing else, I will:**\n"
            "> Старый фокус недели.\n\n"
            "---\n\n"
            "### Next Week Focus\n\n"
            "> Новый фокус следующей недели.\n"
        ),
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)
    monthly_saw_current_week = {"value": False}

    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 1,
        "mode": mode,
    }
    processor.generate_weekly_system_reflection = lambda day=None, refresh_qmd=False: {  # type: ignore[method-assign]
        "report": "🛠 **Reflection**",
        "processed_entries": 1,
        "searchable_write": True,
    }

    def fake_monthly(day=None, refresh_qmd=False):  # noqa: ANN001, ANN202
        del day, refresh_qmd
        monthly_saw_current_week["value"] = "week: 2026-W22" in (
            goals_path / "3-weekly.md"
        ).read_text(encoding="utf-8")
        return {
            "report": "🗓 **Monthly**",
            "processed_entries": 1,
            "searchable_write": True,
        }

    processor.run_monthly_cycle = fake_monthly  # type: ignore[method-assign]
    processor.audit_cycle_result = lambda cycle_name, day, result: {  # type: ignore[method-assign]
        "cycle_name": cycle_name,
        "label": cycle_name,
        "summary": "",
        "issues": [],
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._run_control_plane_maintenance_workflow = lambda name: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    result = processor.run_scheduled_cycle(date(2026, 5, 31))

    assert [cycle["name"] for cycle in result["periodic_cycles"]][:3] == [
        "weekly_system_reflection",
        "monthly",
        "weekly_goals_rollover",
    ]
    assert monthly_saw_current_week == {"value": True}
    assert "week: 2026-W23" in (goals_path / "3-weekly.md").read_text(
        encoding="utf-8",
    )


def test_run_scheduled_cycle_runs_compiled_nightly_maintenance(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path)
    refresh_calls = {"count": 0}

    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 1,
    }
    processor._scheduled_cycle_names_for_day = lambda day: []  # type: ignore[method-assign]
    processor.audit_cycle_result = lambda cycle_name, day, result: {  # type: ignore[method-assign]
        "cycle_name": cycle_name,
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._run_compiled_nightly_maintenance = lambda: {  # type: ignore[method-assign]
        "report": "## 🧩 Compiled Maintenance\n\n- Queue drained: 2",
        "processed_entries": 1,
        "searchable_write": False,
        "lint_issues": [{"path": "compiled/projects/x.md"}],
        "backfilled": ["compiled/projects/x.md"],
    }
    processor._run_vault_health_cycle = lambda: {  # type: ignore[method-assign]
        "report": "## 🩺 Vault Health\n\n- Score: 79.3/100",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._refresh_qmd_index = lambda: refresh_calls.__setitem__(  # type: ignore[method-assign]
        "count",
        refresh_calls["count"] + 1,
    )

    result = processor.run_scheduled_cycle(date(2026, 4, 4))

    assert [cycle["name"] for cycle in result["periodic_cycles"]] == [
        "maintenance.compiled-nightly",
        "maintenance.vault-health",
    ]
    assert "## 🧩 Compiled Maintenance" in result["report"]
    assert "## 🩺 Vault Health" in result["report"]
    assert result["processed_entries"] == 2
    assert refresh_calls["count"] == 1


def test_run_scheduled_cycle_uses_control_plane_labels_for_maintenance(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path)

    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 1,
    }
    processor._scheduled_cycle_names_for_day = lambda day: []  # type: ignore[method-assign]
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    processor._run_compiled_nightly_maintenance = lambda: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
    }
    processor._run_vault_health_cycle = lambda: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
    }

    result = processor.run_scheduled_cycle(date(2026, 4, 4))

    assert [cycle["label"] for cycle in result["periodic_cycles"]] == [
        "Поддержка compiled-слоя",
        "Здоровье vault",
    ]


def test_scheduled_cycle_keeps_only_post_maintenance_vault_health(
    tmp_path: Path,
) -> None:
    processor = CliProcessor(tmp_path / "vault")
    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": (
            "# Итоги дня\n\n"
            "## 📊 Здоровье хранилища\n\n"
            "Оценка: 99/100\n\n"
            "## ⚠️ Наблюдения\n\n"
            "- Нужна проверка"
        ),
        "processed_entries": 1,
    }
    processor._scheduled_cycle_names_for_day = lambda day: []  # type: ignore[method-assign]
    processor.audit_cycle_result = lambda cycle_name, day, result: {  # type: ignore[method-assign]
        "cycle_name": cycle_name,
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._run_compiled_nightly_maintenance = lambda: {  # type: ignore[method-assign]
        "report": "## 🧩 Поддержка compiled-слоя\n\n- Очередь обработана: 0",
        "processed_entries": 0,
    }
    processor._run_vault_health_cycle = lambda: {  # type: ignore[method-assign]
        "report": "## 🩺 Здоровье vault\n\n- Оценка: 100.0/100",
        "processed_entries": 0,
    }
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]

    result = processor.run_scheduled_cycle(date(2026, 7, 22))

    assert "Здоровье хранилища" not in result["report"]
    assert result["report"].count("Здоровье vault") == 1
    assert "## ⚠️ Наблюдения" in result["report"]


def test_compiled_nightly_report_separates_lint_from_freshness_backlog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)

    def fake_run_nightly(self):  # noqa: ANN001, ANN202
        del self
        return {
            "queued_drained": 0,
            "consolidations": [],
            "backfilled": [],
            "lint_issues": [
                {"path": "compiled/projects/a.md", "issue": "broken-source-link"},
                {"path": "compiled/projects/b.md", "issue": "missing-sources"},
            ],
            "freshness_issues": [
                {
                    "path": f"compiled/projects/{index}.md",
                    "issue": "source-changed",
                }
                for index in range(7)
            ],
            "queue_errors": [],
        }

    monkeypatch.setattr(
        "d_brain.services.processor.CompiledBriefingService.run_nightly_maintenance",
        fake_run_nightly,
    )

    result = processor._run_compiled_nightly_maintenance()

    assert "- Проблемы проверки: 2" in result["report"]
    assert "- Карточки к переоценке: 7" in result["report"]


def test_run_scheduled_cycle_refreshes_qmd_even_when_no_periodic_write(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path)
    refresh_calls = {"count": 0}

    processor.process_daily = lambda day, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 1,
    }
    processor._scheduled_cycle_names_for_day = lambda day: []  # type: ignore[method-assign]
    processor.audit_cycle_result = lambda cycle_name, day, result: {  # type: ignore[method-assign]
        "cycle_name": cycle_name,
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._run_compiled_nightly_maintenance = lambda: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._run_vault_health_cycle = lambda: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
        "searchable_write": False,
    }
    processor._refresh_qmd_index = lambda: refresh_calls.__setitem__(  # type: ignore[method-assign]
        "count",
        refresh_calls["count"] + 1,
    )

    processor.run_scheduled_cycle(date(2026, 4, 4))

    assert refresh_calls["count"] == 1


def test_run_vault_health_cycle_repairs_low_health_with_broken_links(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path)
    calls: list[tuple[str, ...]] = []
    stats_queue = [
        {
            "health_score": 72.4,
            "broken_link_count": 3,
            "orphan_count": 4,
            "weakly_connected_count": 40,
        },
        {
            "health_score": 86.1,
            "broken_link_count": 0,
            "orphan_count": 4,
            "weakly_connected_count": 32,
        },
    ]

    processor._rebuild_graph = lambda: calls.append(("analyze",))  # type: ignore[method-assign]
    processor._load_graph_stats = lambda: stats_queue.pop(0)  # type: ignore[method-assign]
    processor._run_uv_script = lambda *args: calls.append(args)  # type: ignore[method-assign]

    result = processor._run_vault_health_cycle()

    assert result["repair_applied"] is True
    assert result["health_score"] == 86.1
    assert result["broken_link_count"] == 0
    assert result["orphan_count"] == 4
    assert result["weakly_connected_count"] == 32
    assert calls == [
        ("analyze",),
        ("skills/vault-health/scripts/fix_links.py", "--apply"),
        ("analyze",),
    ]


def test_run_scheduled_cycle_skips_maintenance_when_lock_is_busy(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path)
    lock_dir = vault_path / ".locks"
    lock_dir.mkdir(parents=True)
    lock_file = (lock_dir / "full-process.lock").open("a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        processor._run_compiled_nightly_maintenance = (  # type: ignore[method-assign]
            lambda: {"processed_entries": 1}
        )

        result = processor.run_scheduled_cycle(date(2026, 4, 4))
    finally:
        lock_file.close()

    assert result == {
        "error": "Full processing is already running",
        "processed_entries": 0,
    }


def test_run_vault_health_cycle_reports_daily_structure_issues_in_russian(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    processor = CliProcessor(vault_path)
    processor._rebuild_graph = lambda: None  # type: ignore[method-assign]
    processor._load_graph_stats = lambda: {  # type: ignore[method-assign]
        "health_score": 91.0,
        "broken_link_count": 1,
        "orphan_count": 2,
        "weakly_connected_count": 3,
        "malformed_daily_count": 1,
    }

    result = processor._run_vault_health_cycle()

    assert result["malformed_daily_count"] == 1
    assert "## 🩺 Здоровье vault" in result["report"]
    assert "- Нарушения структуры daily: 1" in result["report"]
