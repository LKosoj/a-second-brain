"""CLI entrypoint for one-shot multi-backend agent execution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from d_brain.services.cli_runner import CliExecutionError, CliRunner


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    if args.prompt is not None:
        return str(args.prompt)
    return sys.stdin.read()


def _resolve_mcp_config(workdir: Path) -> str | None:
    candidates = [workdir / "mcp-config.json", workdir.parent / "mcp-config.json"]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run selected AI CLI with a prompt")
    parser.add_argument(
        "--ai-cli",
        default="claude",
        help="claude|codex|qwen|gemini|kimi",
    )
    parser.add_argument(
        "--workdir",
        default=".",
        help="Working directory for the CLI process",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1200,
        help="Execution timeout in seconds",
    )
    parser.add_argument("--prompt", help="Inline prompt text")
    parser.add_argument("--prompt-file", help="Path to file containing prompt text")
    args = parser.parse_args()

    prompt = _read_prompt(args)
    workdir = Path(args.workdir)
    runner = CliRunner(workdir, args.ai_cli)
    extra_env = {}
    mcp_config_path = _resolve_mcp_config(workdir)
    if mcp_config_path:
        extra_env["MCP_CONFIG_PATH"] = mcp_config_path

    try:
        output = runner.run(prompt, timeout=args.timeout, extra_env=extra_env)
    except CliExecutionError as exc:
        sys.stderr.write(str(exc).rstrip() + "\n")
        return exc.returncode or 1
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        sys.stderr.write(str(exc).rstrip() + "\n")
        return 1

    sys.stdout.write(output)
    if output and not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
