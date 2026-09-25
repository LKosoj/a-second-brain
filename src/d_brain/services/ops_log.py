"""Shared ops journal for the vault (T2): one line per completed
operation, in ``vault/.session/log.md``.

Line format -- ``- YYYY-MM-DD HH:MM [kind] summary``, same as the former
``answers.append_log`` (audit 2026-09-03, item 24), which this module
replaces with one shared entry point for every operation (import, wiki
compile, nightly pass, saved answer, monthly fact-check, wiki-care, web
search), not only saved /do and /why answers.

Best-effort, and lock-free between writers: a write is one short
``open(..., "a").write(...)`` call (mode "a" is O_APPEND, and one short
line is under PIPE_BUF, so concurrent writers -- e.g. an ingest import and
the nightly pass at the same time -- simply append their lines one after
another without corrupting the file or needing a separate lock). An
``OSError`` or ``UnicodeError`` (full disk, read-only vault, a surrogate
character in ``summary``) is logged and swallowed here: the operation this
line describes has already finished and must not be treated as failed just
because noting it in the journal failed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

OpsLogKind = Literal[
    "ingest",
    "compile",
    "nightly",
    "answer",
    "fact-check",
    "wiki-care",
    "web-search",
]

_MAX_SUMMARY_CHARS = 300


def append_ops_log(
    vault_path: Path,
    kind: OpsLogKind,
    summary: str,
    *,
    now: datetime | None = None,
) -> None:
    """Append one line to ``vault/.session/log.md`` (best-effort).

    ``summary`` is whitespace-collapsed to one line and clipped to
    ``_MAX_SUMMARY_CHARS`` characters (with a trailing "…") so one long or
    multi-line summary cannot break the one-line-per-entry format.
    """
    resolved_now = now or datetime.now().astimezone()
    text = " ".join(summary.split())
    if len(text) > _MAX_SUMMARY_CHARS:
        text = text[: _MAX_SUMMARY_CHARS - 1] + "…"
    log_path = Path(vault_path) / ".session" / "log.md"
    line = f"- {resolved_now:%Y-%m-%d %H:%M} [{kind}] {text}\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # ``errors="replace"`` so a surrogate character in ``summary`` (e.g.
        # from a mis-decoded transcript) cannot turn this best-effort write
        # into a ``UnicodeEncodeError``; the ``UnicodeError`` catch below is
        # a safety net for anything that still slips past that.
        with log_path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(line)
    except (OSError, UnicodeError) as exc:
        logger.warning("Failed to append ops log entry: %s", exc)
