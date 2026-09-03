"""Tests for the two nightly hooks added after the audit:
``prune_ops_journal`` runs once per scheduled cycle, and the final report
carries a compiled-page freshness summary from ``freshness_lint.py``.
"""

import shutil
from datetime import date
from pathlib import Path

import pytest
from _paths import SKILLS_TEMPLATE_ROOT
from conftest import _setup_daily_processing_vault, _write_vault_manifest

from d_brain.services import processor as processor_module
from d_brain.services.processor import SCHEDULED_MODE, CliProcessor


def _stub_scheduled_cycle(processor: CliProcessor) -> None:
    processor.process_daily = lambda d, mode=SCHEDULED_MODE: {  # type: ignore[method-assign]
        "report": "📊 **Daily**",
        "processed_entries": 0,
    }
    processor._run_control_plane_maintenance_workflow = lambda name: {  # type: ignore[method-assign]
        "report": "",
        "processed_entries": 0,
    }
    processor.audit_cycle_result = lambda **kwargs: {  # type: ignore[method-assign]
        "cycle_name": kwargs["cycle_name"],
        "label": kwargs["cycle_name"],
        "summary": "",
        "issues": [],
        "task_candidates": [],
        "tasks_created": [],
    }
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]


def _install_freshness_lint(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "vault-health" / "scripts" / "freshness_lint.py"
    target.parent.mkdir(parents=True)
    shutil.copy(SKILLS_TEMPLATE_ROOT / "vault-health/scripts/freshness_lint.py", target)


def _write_stale_compiled_page(vault_path: Path) -> None:
    page = vault_path / "compiled" / "projects" / "demo.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n## Current State\n\n"
        "Команда выросла до 12 сотрудников.\n",
        encoding="utf-8",
    )


def test_scheduled_cycle_prunes_ops_journal_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 6)  # Monday: no periodic reviews, no goals rollover
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)
    _stub_scheduled_cycle(processor)
    pruned: list[Path] = []
    monkeypatch.setattr(
        processor_module,
        "prune_ops_journal",
        lambda path: pruned.append(path) or {},
    )

    result = processor.run_scheduled_cycle(day)

    assert "error" not in result
    assert pruned == [vault_path]


def test_scheduled_cycle_survives_failed_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 6)
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)
    _stub_scheduled_cycle(processor)

    def boom(path: Path) -> dict[str, int]:
        raise OSError("disk says no")

    monkeypatch.setattr(processor_module, "prune_ops_journal", boom)
    caplog.set_level("WARNING", logger="d_brain.services.processor")

    result = processor.run_scheduled_cycle(day)

    assert "error" not in result
    assert "📊 **Daily**" in result["report"]
    assert any("Ops journal prune failed" in r.getMessage() for r in caplog.records)


def test_freshness_report_is_empty_without_script_or_findings(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)

    assert processor._freshness_lint_report() == ""

    _install_freshness_lint(tmp_path)
    assert processor._freshness_lint_report() == ""  # no compiled/ yet

    page = vault_path / "compiled" / "projects" / "fresh.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "# Fresh\n\nКоманда выросла до 12 сотрудников (2026-04-01).\n", encoding="utf-8"
    )
    assert processor._freshness_lint_report() == ""


def test_freshness_report_lists_findings(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    _install_freshness_lint(tmp_path)
    _write_stale_compiled_page(vault_path)
    processor = CliProcessor(vault_path)

    report = processor._freshness_lint_report()

    assert report.startswith("## 🕰 Свежесть compiled-страниц")
    assert "- Фактов без даты или источника: 1" in report
    assert "`compiled/projects/demo.md:10`" in report

    english = CliProcessor(vault_path, content_language="en")
    assert english._freshness_lint_report().startswith("## 🕰 Compiled Page Freshness")


def test_freshness_report_survives_broken_script(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    script = tmp_path / "skills" / "vault-health" / "scripts" / "freshness_lint.py"
    script.parent.mkdir(parents=True)
    script.write_text("raise RuntimeError('broken lint')\n", encoding="utf-8")
    processor = CliProcessor(vault_path)
    caplog.set_level("WARNING", logger="d_brain.services.processor")

    assert processor._freshness_lint_report() == ""
    assert any("Freshness lint failed" in r.getMessage() for r in caplog.records)


def test_scheduled_cycle_checks_freshness_after_compiled_updates(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 6)
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)
    _stub_scheduled_cycle(processor)
    events: list[str] = []
    processor._run_control_plane_maintenance_workflow = (  # type: ignore[method-assign]
        lambda name: events.append(name)
        or {"report": "", "processed_entries": 0}
    )
    processor._freshness_lint_report = (  # type: ignore[method-assign]
        lambda: events.append("freshness") or "freshness-report"
    )

    result = processor.run_scheduled_cycle(day)

    assert events[-1] == "freshness"
    assert "maintenance.compiled-nightly" in events[:-1]
    assert "freshness-report" in result["report"]
