"""Tests for CliProcessor.from_settings factory."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from d_brain.services.processor import CliProcessor


def test_cli_processor_from_settings_reads_settings_and_applies_overrides(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        vault_path=tmp_path / "vault",
        todoist_api_key="td-secret",
        ai_cli="codex",
        owner_full_name="Example Owner",
        content_language="en",
        openai_api_key="oa-secret",
        openai_base_url="https://api.example.test/v1",
        openai_model="gpt-test",
        tavily_api_key="tv-secret",
        owner_telegram_id=42,
        recall_planner_disable_thinking=True,
        classification_model="gpt-mini",
    )

    processor = CliProcessor.from_settings(settings)

    assert processor.vault_path == tmp_path / "vault"
    assert processor.todoist_api_key == "td-secret"
    assert processor.ai_cli == "codex"
    assert processor.owner_full_name == "Example Owner"
    assert processor.content_language == "en"
    assert processor.tavily_api_key == "tv-secret"
    assert processor.owner_telegram_id == 42

    overridden = CliProcessor.from_settings(
        settings,
        todoist_api_key="",
        owner_telegram_id=0,
        content_language="ru",
    )

    assert overridden.todoist_api_key == ""
    assert overridden.owner_telegram_id == 0
    assert overridden.content_language == "ru"
    assert overridden.ai_cli == "codex"
