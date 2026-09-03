"""Regression tests for the dependency audit fix.

Audit finding: `pyproject.toml` pulled in `todoist-api-python` (never
imported — the project talks to Todoist over MCP, not this SDK), left
`openai` unbounded (a `2 -> 3` major jump would land without review), and
kept `jpype1` (a JVM bridge only used by the optional `ms-project` skill
scripts) in the core dependency set that every install pays for.
"""

import tomllib
from pathlib import Path

_PYPROJECT = tomllib.loads(
    (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
)


def test_todoist_api_python_is_not_a_dependency() -> None:
    deps = _PYPROJECT["project"]["dependencies"]

    assert not any(dep.lower().startswith("todoist-api-python") for dep in deps)


def test_openai_dependency_has_a_major_version_ceiling() -> None:
    deps = _PYPROJECT["project"]["dependencies"]
    openai_specs = [dep for dep in deps if dep.lower().startswith("openai")]

    assert openai_specs == ["openai>=1.99.1,<3"]


def test_jpype1_is_an_optional_ms_project_extra_not_a_core_dependency() -> None:
    deps = _PYPROJECT["project"]["dependencies"]

    assert not any(dep.lower().startswith("jpype1") for dep in deps)

    extras = _PYPROJECT["project"]["optional-dependencies"]
    assert extras["ms-project"] == ["jpype1>=1.7.0"]
