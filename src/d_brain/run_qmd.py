"""CLI passthrough for the project-local qmd index."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from d_brain.config import get_settings
from d_brain.services.qmd import QmdService


def _parse_recall_args(qmd_args: list[str]) -> tuple[bool, bool, int, str]:
    """Parse ``a-second-brain qmd recall`` arguments."""
    deep = qmd_args[0] == "deep-recall"
    limit = 5
    json_output = False
    query_parts: list[str] = []
    index = 1
    while index < len(qmd_args):
        arg = qmd_args[index]
        if arg == "--json":
            json_output = True
            index += 1
            continue
        if arg in {"-n", "--limit"} and index + 1 < len(qmd_args):
            try:
                limit = max(1, int(qmd_args[index + 1]))
            except ValueError:
                pass
            index += 2
            continue
        query_parts.append(arg)
        index += 1
    return deep, json_output, limit, " ".join(query_parts).strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run qmd against the local vault index"
    )
    parser.add_argument(
        "qmd_args",
        nargs=argparse.REMAINDER,
        help='Arguments passed to qmd, for example: query "домены МПД"',
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    service = QmdService(settings.vault_path)
    qmd_args = args.qmd_args or ["status"]

    try:
        if qmd_args and qmd_args[0] in {"recall", "deep-recall"}:
            deep, json_output, limit, query = _parse_recall_args(qmd_args)
            if not query:
                sys.stderr.write("recall requires a query\n")
                return 1
            payload = service.recall(query, deep=deep, limit=limit)
            if json_output:
                sys.stdout.write(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                )
            else:
                sys.stdout.write(service.format_recall(payload) + "\n")
            return 0
        if qmd_args and qmd_args[0] == "embed":
            extra_args = qmd_args[1:]
            unknown = [arg for arg in extra_args if arg not in {"-f", "--force"}]
            if unknown:
                sys.stderr.write(
                    "embed only accepts -f/--force; unsupported args: "
                    + " ".join(unknown)
                    + "\n"
                )
                return 2
            result = service.run_embed_cli(
                force=any(flag in {"-f", "--force"} for flag in extra_args)
            )
        elif qmd_args == ["cleanup"]:
            result = service.cleanup()
        else:
            result = service.run(*qmd_args)
            if result.returncode == 0 and qmd_args and qmd_args[0] == "get":
                service.touch_notes(qmd_args[1:])
    except FileNotFoundError:
        sys.stderr.write("qmd CLI is not installed\n")
        return 1

    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if result.returncode != 0 and result.stderr:
        sys.stderr.write(result.stderr)
        if not result.stderr.endswith("\n"):
            sys.stderr.write("\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
