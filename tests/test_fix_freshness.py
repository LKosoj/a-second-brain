from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from _paths import SKILLS_TEMPLATE_ROOT


def _load_freshness_lint():
    script_path = SKILLS_TEMPLATE_ROOT / "vault-health/scripts/freshness_lint.py"
    spec = importlib.util.spec_from_file_location("freshness_lint", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations (from ``from __future__ import
    # annotations``) via ``sys.modules[cls.__module__]`` -- register the
    # module before exec so ``@dataclass`` on ``Finding`` can find it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_compiled_page(vault_path: Path, body: str, *, name: str = "demo.md") -> Path:
    page_path = vault_path / "compiled" / "projects" / name
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(body, encoding="utf-8")
    return page_path


def test_number_without_date_or_source_is_a_finding(tmp_path: Path) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "## Current State\n\n"
        "Баланс клиента: $12000.\n",
    )

    findings = module.lint_vault(vault_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.path == "compiled/projects/demo.md"
    assert "12000" in finding.text


def test_as_of_date_in_same_line_clears_the_finding(tmp_path: Path) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "## Current State\n\n"
        "Баланс клиента: $12000 по состоянию на 2026-08-01.\n",
    )

    findings = module.lint_vault(vault_path)

    assert findings == []


def test_source_link_plus_date_on_previous_line_clears_the_finding(
    tmp_path: Path,
) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "## Current State\n\n"
        "Обновлено 2026-07-01.\n"
        "Баланс клиента: $12000 см. [[источник]].\n",
    )

    findings = module.lint_vault(vault_path)

    assert findings == []


def test_line_inside_code_block_is_not_scanned(tmp_path: Path) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "## Current State\n\n"
        "```\n"
        "Баланс клиента: $12000.\n"
        "```\n",
    )

    findings = module.lint_vault(vault_path)

    assert findings == []


def test_bare_year_is_not_a_finding(tmp_path: Path) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "## Current State\n\n"
        "В 2026 году планируется расширение команды.\n",
    )

    findings = module.lint_vault(vault_path)

    assert findings == []


def test_sources_table_section_is_not_scanned(tmp_path: Path) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "## Sources That Shaped This Page\n"
        "| Date | Source | What Added |\n"
        "| --- | --- | --- |\n"
        "| 2026-06-09 | [[daily/2026-06-10]] | added $12000 update |\n",
    )

    findings = module.lint_vault(vault_path)

    assert findings == []


def test_frontmatter_is_not_scanned(tmp_path: Path) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n"
        'description: "Баланс: $50000"\n'
        "---\n\n"
        "# Demo Project\n\n"
        "## Current State\n\n"
        "Everything is on track.\n",
    )

    findings = module.lint_vault(vault_path)

    assert findings == []


def test_non_compiled_notes_are_not_scanned(tmp_path: Path) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    thought_path = vault_path / "thoughts" / "note.md"
    thought_path.parent.mkdir(parents=True)
    thought_path.write_text(
        "---\ntype: note\n---\n\n# Note\n\nБаланс клиента: $12000.\n",
        encoding="utf-8",
    )

    findings = module.lint_vault(vault_path)

    assert findings == []


def test_cli_json_output_is_valid_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "## Current State\n\n"
        "Баланс клиента: $12000.\n",
    )

    monkeypatch.setattr("sys.argv", ["freshness_lint.py", str(vault_path), "--json"])
    module.main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert isinstance(payload, list)
    assert len(payload) == 1
    assert set(payload[0]) == {"path", "line", "text", "reason"}
    assert payload[0]["path"] == "compiled/projects/demo.md"


def test_resources_heading_does_not_suppress_findings(tmp_path: Path) -> None:
    # Regression: "## Resources Needed"/"## Outsource Costs" contain "source"
    # as a substring, but must not be read as a sources section -- only a
    # whole "source(s)" or "источник..." word should count.
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "## Resources Needed\n\n"
        "Budget: $77000.\n\n"
        "## Outsource Costs\n\n"
        "Contractor fee: $9000.\n",
    )

    findings = module.lint_vault(vault_path)

    assert [finding.text for finding in findings] == [
        "Budget: $77000.",
        "Contractor fee: $9000.",
    ]


def test_istochniki_heading_is_still_skipped(tmp_path: Path) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "### Источники\n\n"
        "- Баланс клиента: $12000 [[daily/2026-06-10]]\n",
    )

    findings = module.lint_vault(vault_path)

    assert findings == []


def test_hidden_directory_inside_compiled_is_not_scanned(tmp_path: Path) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    trashed_path = vault_path / "compiled" / "x" / ".trash" / "old.md"
    trashed_path.parent.mkdir(parents=True)
    trashed_path.write_text(
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Old\n\nБаланс клиента: $12000.\n",
        encoding="utf-8",
    )

    findings = module.lint_vault(vault_path)

    assert findings == []


def _write_three_findings(vault_path: Path) -> None:
    _write_compiled_page(
        vault_path,
        "---\ntype: compiled-briefing\ndomain: projects\n---\n\n"
        "# Demo Project\n\n"
        "## Current State\n\n"
        "Баланс клиента: $12000.\n"
        "Баланс партнёра: $30000.\n"
        "Баланс поставщика: $45000.\n",
    )


def test_cli_limit_non_integer_reports_error_and_exits_cleanly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_three_findings(vault_path)

    monkeypatch.setattr(
        "sys.argv", ["freshness_lint.py", str(vault_path), "--limit", "abc"]
    )
    module.main()  # must not raise

    captured = capsys.readouterr()
    assert "Error" in captured.err
    assert "--limit" in captured.err
    assert captured.out == ""


def test_cli_limit_truncation_reports_hidden_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_three_findings(vault_path)

    monkeypatch.setattr(
        "sys.argv", ["freshness_lint.py", str(vault_path), "--limit", "1"]
    )
    module.main()

    captured = capsys.readouterr()
    assert "3 freshness issue(s):" in captured.out
    assert captured.out.count("compiled/projects/demo.md:") == 1
    assert "ещё 2 находок скрыто (--limit)" in captured.out


def test_cli_limit_zero_does_not_claim_no_issues_found(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_freshness_lint()
    vault_path = tmp_path / "vault"
    _write_three_findings(vault_path)

    monkeypatch.setattr(
        "sys.argv", ["freshness_lint.py", str(vault_path), "--limit", "0"]
    )
    module.main()

    captured = capsys.readouterr()
    assert "No freshness issues found" not in captured.out
    assert "3 freshness issue(s):" in captured.out
    assert "ещё 3 находок скрыто (--limit)" in captured.out
