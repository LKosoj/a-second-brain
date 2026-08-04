"""Public command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from collections.abc import Sequence
from importlib.resources import as_file, files
from pathlib import Path

_PROJECT_FILES = (
    "AGENTS.md",
    ".env.example",
    ".gitignore",
    "mcp-config.json",
    "vault-manifest.json",
)
_PROJECT_AGENT_ALIASES = (
    "CLAUDE.md",
    "GEMINI.md",
    "QWEN.md",
)
_VAULT_DIRECTORIES = (
    "attachments",
    "business",
    "compiled",
    "daily",
    "imports/documents",
    "imports/plaud",
    "imports/web",
    "imports/youtube",
    "MOC",
    "projects",
    "summaries",
    "thoughts/ideas",
    "thoughts/learnings",
    "thoughts/projects",
    "thoughts/reflections",
)
_SKILL_ALIASES = (
    ".claude/skills",
    ".agents/skills",
    ".codex/skills",
    ".qwen/skills",
)


def initialize_project(target: Path) -> Path:
    """Create a private vault and local configuration in ``target``."""
    project_dir = Path(target).expanduser().resolve()
    vault_path = project_dir / "vault"
    if vault_path.exists():
        raise FileExistsError(f"vault already exists: {vault_path}")

    project_dir.mkdir(parents=True, exist_ok=True)
    resources = files("d_brain.resources")
    with as_file(resources.joinpath("vault_template")) as template_path:
        shutil.copytree(template_path, vault_path)

    for relative in _VAULT_DIRECTORIES:
        (vault_path / relative).mkdir(parents=True, exist_ok=True)

    skills_path = project_dir / "skills"
    if not skills_path.exists():
        with as_file(
            resources.joinpath("project_template", "skills")
        ) as skills_template:
            shutil.copytree(skills_template, skills_path)

    private_skills_path = vault_path / "skills" / "private"
    private_skills_path.mkdir(parents=True, exist_ok=True)
    private_alias = skills_path / "private"
    if not private_alias.exists() and not private_alias.is_symlink():
        private_alias.symlink_to(
            "../vault/skills/private",
            target_is_directory=True,
        )

    for relative in _SKILL_ALIASES:
        alias = project_dir / relative
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to("../skills", target_is_directory=True)

    project_template = resources.joinpath("project_template")
    for name in _PROJECT_FILES:
        destination = project_dir / name
        if destination.exists():
            continue
        with (
            project_template.joinpath(name).open("rb") as source,
            destination.open("xb") as output,
        ):
            shutil.copyfileobj(source, output)

    for name in _PROJECT_AGENT_ALIASES:
        alias = project_dir / name
        if alias.exists() or alias.is_symlink():
            continue
        alias.symlink_to("AGENTS.md")

    env_path = project_dir / ".env"
    if not env_path.exists():
        shutil.copyfile(project_dir / ".env.example", env_path)
    os.chmod(env_path, 0o600)
    return vault_path


def _run_bot() -> int:
    from d_brain.__main__ import main as run

    asyncio.run(run())
    return 0


def _init_command(args: argparse.Namespace) -> int:
    try:
        vault_path = initialize_project(args.target)
    except FileExistsError as exc:
        print(f"a-second-brain: {exc}")
        return 2
    print(f"Private vault created at {vault_path}")
    print(f"Edit {vault_path.parent / '.env'} before starting the bot.")
    return 0


def _doctor_command(args: argparse.Namespace) -> int:
    from d_brain.doctor import run_doctor

    return run_doctor(args.target, smoke=args.smoke)


def _qmd_command(args: argparse.Namespace) -> int:
    from d_brain.run_qmd import main as run_qmd

    return run_qmd(args.qmd_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a-second-brain")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="create a private vault and local configuration",
    )
    init_parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="project directory (default: current directory)",
    )
    init_parser.set_defaults(handler=_init_command)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="check project configuration and runtime prerequisites",
    )
    doctor_parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="project directory (default: current directory)",
    )
    doctor_parser.add_argument(
        "--smoke",
        action="store_true",
        help="run a short request through the selected AI CLI",
    )
    doctor_parser.set_defaults(handler=_doctor_command)

    qmd_parser = subparsers.add_parser(
        "qmd",
        help="run QMD against the private vault index",
    )
    qmd_parser.add_argument(
        "qmd_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to QMD",
    )
    qmd_parser.set_defaults(handler=_qmd_command)

    run_parser = subparsers.add_parser("run", help="start the Telegram bot")
    run_parser.set_defaults(handler=lambda _args: _run_bot())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))
