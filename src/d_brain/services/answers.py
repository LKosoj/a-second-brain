"""Persist owner-approved /do and /why answers as vault notes (аудит
2026-09-03, п.24: "Вопрос -> страница").

Nothing here runs on its own -- an answer is only ever filed after the
owner taps the "Сохранить" button ``do.py``/``why.py`` attach to their
final reply. ``register_pending_answer``/``pop_pending_answer`` hold that
answer in memory between the reply and the tap (a bot restart loses the
store, which is fine -- callers must already treat a missing id as
"устарело", not crash). ``save_answer`` then writes one card under
``vault/answers/`` through the shared ``write_validated_vault_markdown``
lock -- the only sanctioned vault write path (CLAUDE.md) -- and
``append_log`` records the same event as one line in
``vault/.session/log.md``, mirroring ``frontmatter.py``'s own best-effort
``.session/ops.jsonl`` append: a broken chronicle must never look like the
answer itself failed to save.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from d_brain.manifest import load_manifest_for_vault
from d_brain.services.compiled_briefings import CompiledBriefingService
from d_brain.services.frontmatter import (
    UnsafeVaultPathError,
    write_validated_vault_markdown,
)

logger = logging.getLogger(__name__)

AnswerKind = Literal["do", "why"]

# A bot restart wipes this store -- callers already have to handle a
# missing id as "устарело", so nothing durable is lost by capping it too.
_MAX_PENDING_ANSWERS = 50
_pending_answers: dict[str, AnswerPayload] = {}


@dataclass(frozen=True, slots=True)
class AnswerPayload:
    """One /do or /why answer held in memory until the owner taps
    "Сохранить". ``kind`` is what tells a /do answer from a /why answer
    apart -- both flows share the same ``answer:save:<id>`` callback_data."""

    question: str
    answer_markdown: str
    sources: tuple[str, ...]
    kind: AnswerKind


def register_pending_answer(payload: AnswerPayload) -> str:
    """Hold ``payload`` for the owner's tap and return its ``answer_id``."""
    answer_id = secrets.token_hex(4)
    _pending_answers[answer_id] = payload
    while len(_pending_answers) > _MAX_PENDING_ANSWERS:
        _pending_answers.pop(next(iter(_pending_answers)))
    return answer_id


def pop_pending_answer(answer_id: str) -> AnswerPayload | None:
    """Consume and return one pending answer, or ``None`` past its lifetime
    (already saved, evicted for space, or the bot restarted since)."""
    return _pending_answers.pop(answer_id, None)


def _slug_from_question(question: str) -> str:
    """Slug from the question's first words -- reuses the project's one
    existing slugify (``CompiledBriefingService._slugify``, already
    precedented for cross-module reuse by ``compiled_briefs.py``/
    ``compiled_why.py``) instead of a second transliteration table."""
    words = " ".join(question.split()).split(" ")
    prefix = " ".join(word for word in words[:6] if word)
    return CompiledBriefingService._slugify(prefix) or "answer"


def _sources_section(sources: list[str]) -> str:
    if not sources:
        return "_Источники не указаны._"
    return "\n".join(f"- [[{item.removesuffix('.md')}]]" for item in sources)


def save_answer(
    vault_path: Path,
    *,
    question: str,
    answer_markdown: str,
    sources: list[str],
    kind: AnswerKind,
    now: datetime,
) -> Path:
    """Write one owner-approved answer as a card under ``vault/answers/``.

    The filename is ``YYYY-MM-DD-<slug>.md``; a same-day collision gets a
    ``-2``, ``-3``, ... suffix instead of overwriting the earlier note.
    """
    vault_path = Path(vault_path)
    date_str = now.date().isoformat()
    slug = _slug_from_question(question)
    answers_dir = vault_path / "answers"

    content = (
        "---\n"
        "type: note\n"
        f"description: {json.dumps(_one_line(question), ensure_ascii=False)}\n"
        f"tags: [answer, {kind}]\n"
        "status: active\n"
        f"created: {date_str}\n"
        f"updated: {date_str}\n"
        f"last_accessed: {date_str}\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
        "## Вопрос\n"
        f"{question.strip()}\n\n"
        "## Ответ\n"
        f"{answer_markdown.strip()}\n\n"
        "## Источники\n"
        f"{_sources_section(sources)}\n"
    ).encode()

    manifest = load_manifest_for_vault(vault_path)
    candidate = answers_dir / f"{date_str}-{slug}.md"
    suffix = 2
    while True:
        if not candidate.exists():
            # ``require_absent`` makes the write itself the existence check:
            # two "Сохранить" taps for the same question run in separate
            # threads, and a plain exists() pre-check let the second one
            # silently overwrite the first.
            try:
                write_validated_vault_markdown(
                    vault_path,
                    candidate,
                    content,
                    manifest=manifest,
                    require_absent=True,
                )
                return candidate
            except UnsafeVaultPathError:
                if not candidate.exists():
                    raise
        candidate = answers_dir / f"{date_str}-{slug}-{suffix}.md"
        suffix += 1


def _one_line(text: str) -> str:
    """Collapse whitespace so a multi-line question stays one log line /
    one search snippet (``description``) instead of breaking the format."""
    return " ".join(text.split())


def append_log(vault_path: Path, kind: AnswerKind, summary: str, now: datetime) -> None:
    """Append one line to ``vault/.session/log.md`` (append-only chronicle;
    created, along with ``.session/``, if either is missing).

    Best-effort, same reasoning as ``frontmatter._record_ops_journal_entry``'s
    own ``.session/ops.jsonl`` append: any failure here is logged and
    swallowed rather than raised, so a broken chronicle never looks like the
    answer itself failed to save.
    """
    log_path = Path(vault_path) / ".session" / "log.md"
    line = f"- {now.strftime('%Y-%m-%d %H:%M')} [{kind}] {_one_line(summary)}\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        logger.warning("Failed to append answer log entry: %s", exc)
