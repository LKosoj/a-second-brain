import re
from pathlib import Path

from _paths import PROJECT_ROOT

DOCUMENTATION_PAGES = {
    "architecture.md",
    "backup-and-restore.md",
    "cli.md",
    "configuration.md",
    "development.md",
    "getting-started.md",
    "integrations.md",
    "operations.md",
    "troubleshooting.md",
}


def _markdown_names(directory: Path) -> set[str]:
    return {path.name for path in directory.glob("*.md")}


def test_language_directories_have_the_same_documentation_pages() -> None:
    english = PROJECT_ROOT / "docs/en"
    russian = PROJECT_ROOT / "docs/ru"

    assert _markdown_names(english) == DOCUMENTATION_PAGES
    assert _markdown_names(russian) == DOCUMENTATION_PAGES


def test_each_page_links_to_its_translation() -> None:
    for name in DOCUMENTATION_PAGES:
        english = (PROJECT_ROOT / "docs/en" / name).read_text(encoding="utf-8")
        russian = (PROJECT_ROOT / "docs/ru" / name).read_text(encoding="utf-8")

        assert f"../ru/{name}" in english
        assert f"../en/{name}" in russian


def test_readmes_and_index_expose_full_documentation() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    readme_ru = (PROJECT_ROOT / "README.ru.md").read_text(encoding="utf-8")
    index = (PROJECT_ROOT / "docs/index.md").read_text(encoding="utf-8")

    assert "docs/index.md" in readme
    assert "docs/index.md" in readme_ru
    for name in DOCUMENTATION_PAGES:
        assert f"en/{name}" in index
        assert f"ru/{name}" in index


def test_readmes_match_checkout_and_wheel_install_contracts() -> None:
    for name in ("README.md", "README.ru.md"):
        readme = (PROJECT_ROOT / name).read_text(encoding="utf-8")

        assert "./install.sh" in readme
        assert "<PUBLIC_REPOSITORY_URL>" not in readme
        assert 'git clone "$REPOSITORY_URL" a-second-brain' in readme
        assert "uv run --frozen --no-dev a-second-brain doctor" in readme
        assert "uv run --frozen --no-dev a-second-brain run" in readme
        assert "Releases" in readme
        assert "`uv build`" in readme
        assert "python -m pip install ./a_second_brain-*.whl" in readme
        assert (
            'INSTANCE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/a-second-brain"'
            in readme
        )
        assert 'a-second-brain init "$INSTANCE_DIR"' in readme
        assert 'cd "$INSTANCE_DIR"' in readme
        assert "/path/to/private-instance" not in readme
        assert "\na-second-brain doctor\n" in readme
        assert "\na-second-brain run\n" in readme
        assert "./scripts/install-systemd-user.sh --enable" in readme
        assert '${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/' in readme


def test_cli_reference_documents_all_public_commands() -> None:
    for language in ("en", "ru"):
        page = (PROJECT_ROOT / f"docs/{language}/cli.md").read_text(
            encoding="utf-8"
        )

        assert "a-second-brain init" in page
        assert "a-second-brain doctor" in page
        assert "a-second-brain run" in page
        assert "--smoke" in page
        assert "| `0` |" in page
        assert "| `1` |" in page
        assert "| `2` |" in page


def test_qmd_local_model_defaults_are_documented_in_both_languages() -> None:
    for language in ("en", "ru"):
        integrations = (
            PROJECT_ROOT / f"docs/{language}/integrations.md"
        ).read_text(encoding="utf-8")
        configuration = (
            PROJECT_ROOT / f"docs/{language}/configuration.md"
        ).read_text(encoding="utf-8")

        for page in (integrations, configuration):
            assert "Qwen3-Embedding-0.6B" in page
            assert "Qwen3-Reranker-0.6B" in page
            assert "CPU" in page


def test_kimi_backend_is_documented_in_both_languages() -> None:
    for language in ("en", "ru"):
        integrations = (
            PROJECT_ROOT / f"docs/{language}/integrations.md"
        ).read_text(encoding="utf-8")
        configuration = (
            PROJECT_ROOT / f"docs/{language}/configuration.md"
        ).read_text(encoding="utf-8")

        assert "Kimi Code" in integrations
        assert "`kimi login`" in integrations
        assert "`.agents/skills`" in integrations
        assert "`kimi`" in configuration


def test_bilingual_documentation_links_resolve() -> None:
    pages = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "README.ru.md",
        PROJECT_ROOT / "docs/index.md",
        *(PROJECT_ROOT / "docs/en").glob("*.md"),
        *(PROJECT_ROOT / "docs/ru").glob("*.md"),
    ]

    for page in pages:
        content = page.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
            if "://" in target or target.startswith("#"):
                continue
            relative_path = target.split("#", maxsplit=1)[0]
            assert (page.parent / relative_path).resolve().exists(), (
                f"{page.relative_to(PROJECT_ROOT)} links to missing {target}"
            )
