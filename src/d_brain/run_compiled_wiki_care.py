"""CLI entrypoint for the T5 "Еженедельный уход за вики" weekly wiki care.

1:1 shape with ``run_compiled_import_sweep.py`` (``ensure_run_identity``,
``get_settings``, an argparse ``--no-limit`` flag, JSON on stdout, exit code
1 iff ``result["errors"]`` is non-empty). Unlike that sweep, this CLI has no
drain loop of its own -- ``run_weekly_wiki_care`` (``compiled_wiki_care.py``)
already drains the refresh queue itself when ``--no-limit`` is passed
(ПОПРАВКА 1), and never does otherwise, so a normal run here leaves whatever
it just queued for the next nightly pass's own budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from d_brain.config import get_settings
from d_brain.services.compiled_wiki_care import run_weekly_wiki_care
from d_brain.services.frontmatter import ensure_run_identity
from d_brain.services.processor import CliProcessor


def main() -> int:
    ensure_run_identity("compiled-wiki-care", "compiled-wiki-care")
    parser = argparse.ArgumentParser(description="Weekly wiki-care pass (T5)")
    parser.add_argument(
        "--no-limit",
        action="store_true",
        help="Ignore the 7-day interval and the 10-model-call budget",
    )
    args = parser.parse_args()
    settings = get_settings()

    def _answer_question(question: str) -> dict[str, Any]:
        return CliProcessor(
            settings.vault_path,
            ai_cli=settings.ai_cli,
            content_language=settings.content_language,
        ).answer_question(question)

    try:
        result = run_weekly_wiki_care(
            settings.vault_path,
            content_language=settings.content_language,
            ai_cli=settings.ai_cli,
            answer_question=_answer_question,
            tavily_api_key=settings.tavily_api_key,
            no_limit=args.no_limit,
        )
    except Exception as exc:
        # ``run_weekly_wiki_care`` re-raises after journaling the failure;
        # this CLI family promises a JSON ``{"errors": [...]}`` contract on
        # stdout rather than a raw traceback.
        result = {"errors": [f"{type(exc).__name__}: {exc}"]}

    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
