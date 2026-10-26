"""Plain helper functions shared by test modules.

Fixtures live in ``conftest.py``; importable functions live here, next to
``_paths.py``.
"""

from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def write_page(vault: Path, rel_path: str, text: str) -> None:
    """Write ``text`` to ``vault / rel_path``, creating parent folders."""
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_page_writer(
    page_text: Callable[..., str],
) -> Callable[..., None]:
    """Build ``(vault, rel_path, **kwargs)`` writer around a module's text builder.

    Each test module keeps its own ``_page_text`` because the page shape the
    tests depend on differs per module; only the write step is shared.
    """

    def _write_page(vault: Path, rel_path: str, **kwargs: object) -> None:
        write_page(vault, rel_path, page_text(**kwargs))  # type: ignore[arg-type]

    return _write_page


def patch_get_settings(
    monkeypatch: pytest.MonkeyPatch,
    vault: Path,
    *,
    module: ModuleType,
    **fields: Any,
) -> None:
    """Make ``module.get_settings()`` return a namespace with ``vault_path``."""
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(vault_path=vault, **fields),
    )
