"""Owner-facing daily digest for the compiled enrichment layer (ТЗ 7.1).

``build_daily_digest`` is a pure function: it never calls a model and never
writes to the vault. It only reads pages already written under ``compiled/``
(via ``CompiledBriefingService``) and, optionally, the owner's decisions
queue. Callers (the CLI, the bot's "Дайджест" button, and the
``maintenance.compiled-digest`` nightly-maintenance hook -- see
``processor.py``'s ``_run_compiled_digest_cycle``) are responsible for
persisting the returned Markdown to ``summaries/compile/YYYY-MM-DD.md`` and
for delivering it to Telegram.

Block order follows ТЗ 7.1 exactly: "Требует решения" (needs a decision)
first, "Что изменилось" (what changed) second, "Стоит посмотреть" (worth a
look) third. An empty block is omitted entirely -- no empty heading is
rendered, matching the ТЗ's explicit rule for the third block; the same
rule is applied uniformly to all three blocks here to keep the digest short
and to avoid one-off placeholder lines the ТЗ never asks for.

``.session/decisions-queue.json`` contract
-------------------------------------------
This module does not own that file -- the enrichment pipeline
(``compiled_briefings.py``) does, and it does not exist yet. Until it does,
this is the minimal shape this module reads, as required by the task:

    [
      {
        "kind": "conflict" | "duplicate-candidate" | "blocked-action"
                | "drift" | "verify-rejected" | ...,  # free-form label
        "page": "compiled/<domain>/<slug>.md",         # vault-relative path
        "summary": "one-line description of the decision needed",
        "since": "YYYY-MM-DD"                          # when it was queued
      },
      ...
    ]

A missing file, a file that is not valid JSON, or a payload that is not a
list of such objects is treated as an empty queue -- never as an error (ТЗ
"Отсутствие или порча файла — это пустой список, а не ошибка").
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from d_brain.services.compiled_briefings import (
    MAX_ENRICHMENTS_PER_PAGE_PER_MONTH,
    MAX_MODEL_CALLS_PER_PASS,
    MAX_PAGES_PER_PASS,
    TOKEN_RE,
    CompiledBriefingCandidate,
    CompiledBriefingService,
)
from d_brain.services.decisions_queue import (
    DECISIONS_QUEUE_RELATIVE_PATH,
    QUEUE_CAP,
    QueueItem,
    list_queue_items,
)

logger = logging.getLogger(__name__)

# ТЗ 5.6: "Страница считается давно не открывавшейся — нет обращений 90 дней".
STALE_REVISIT_DAYS = 90
# ТЗ 7.1: "Стоит посмотреть. Одна-две давно не открывавшиеся страницы".
MAX_REVISIT_PAGES = 2
# ТЗ 6.1 "Уровень памяти управляет бюджетом обогащения" / 6.5 "Повторное
# всплытие": only these two tiers are eligible to resurface -- "core",
# "active", and "warm" pages are in active use and are not what the owner
# has forgotten about.
COLD_REVISIT_TIERS = frozenset({"cold", "archive"})


@dataclass(frozen=True, slots=True)
class PassStatus:
    """The outcome of one compiled-enrichment pass, as needed for the digest.

    This is this module's own minimal status contract, not the ТЗ 5.2 step 6
    pass journal (``.session/compile-enrich.json``) itself -- that journal
    belongs to the enrichment pipeline (``compiled_briefings.py``'s
    ``_write_pass_journal``) and is read into a ``PassStatus`` by
    ``read_pass_status`` below.

    ``status`` is one of "success", "no-work", "unknown", "error" (this
    codebase's real journal writer spells the first two "ok"/"no-work"; see
    ``_NON_FAILURE_STATUSES`` in this module). "unknown" is
    ``read_pass_status``'s own status for a journal that exists but could
    not be read or parsed -- see that function's docstring. Only "no-work"
    is ever suppressed (``build_daily_digest``); every other status
    (including "unknown") always builds. ``error``, when non-empty, is
    shown as the first "Требует решения" line regardless of ``status``;
    when a non-"no-work"/non-success status comes with an empty, missing,
    or non-string ``error``, ``_render_digest`` still shows an honest
    fallback line instead of silence (ТЗ 7.1: "Сбойный проход, наоборот,
    всегда порождает сообщение с причиной") -- "unknown" gets its own,
    distinct line (``_UNKNOWN_STATUS_LINE``) rather than the generic
    failure one, since it is not evidence the pass itself failed, only
    that its outcome could not be confirmed.

    ``budget_exhausted`` mirrors the journal's own field (ТЗ 5.5 inv 7:
    "факт исчерпания бюджета попадает в дайджест"): the technical
    constraint names ``compiled_briefings.py``'s ``_write_pass_journal``
    recorded for this pass (e.g. ``"pages-per-pass"``), if any. Never empty
    strings, never anything but ``str`` -- ``read_pass_status`` already
    filters those out. A non-empty tuple both adds a line to "Требует
    решения" (``_render_digest``) and, on its own, keeps the digest from
    being suppressed as a quiet day (``build_daily_digest``): exhausting a
    budget is never "nothing happened".

    ``queue_evictions`` mirrors the journal's own field (ТЗ 7.2: the owner
    must learn when the ``QUEUE_CAP``-item decisions-queue cap forced old
    entries out): how many pre-existing queue entries this pass evicted on
    overflow, if any. Like ``budget_exhausted``, a nonzero value both adds a
    line to "Требует решения" (``_render_digest``) and keeps the digest from
    being suppressed as a quiet day (``build_daily_digest``). The monthly
    fact-check pass evicts entries too and journals the count separately;
    ``build_daily_digest`` folds that in via ``_with_fact_check_evictions``,
    so this field is the digest's single count of "entries lost today",
    whichever pass caused them.

    ``human_zone_ambiguous_pages`` mirrors the journal's own field: the
    vault-relative paths of pages ``compiled_briefings.py`` skipped this
    pass because their <!-- human:start/end --> markers were ambiguous
    (see that module's ``_record_human_zone_ambiguous``). Unlike a budget
    limit or a queue overflow, a page in this state stays stuck forever on
    its own -- a `cold`-tier page never calls the model again by itself, so
    nothing else will ever prompt a retry -- which is why a non-empty tuple
    both adds a line to "Требует решения" (``_render_digest``) and keeps the
    digest from being suppressed as a quiet day (``build_daily_digest``),
    same as the two fields above.

    ``worker_crash`` does not come from the pass journal at all: it is the
    message from today's background-queue-worker crash journal, folded in by
    ``_with_queue_worker_crash``. Same treatment as the fields above -- a
    line in "Требует решения" and no quiet-day suppression -- and for the
    same reason as ``human_zone_ambiguous_pages``: a dead drain does not
    restart itself, so silence would let pages stop being enriched
    indefinitely.

    ``dropped_sources`` holds the vault-relative source paths the refresh
    queue gave up on entirely (``compiled_briefings``'s
    ``_record_dropped_queue_source``): the owner wrote something, three
    compile attempts failed, and the queue entry -- the only trigger that
    would ever have compiled it -- was deleted. Same treatment and same
    reason as the two fields above: nothing retries these on its own, so the
    note stays out of ``compiled/`` until the owner touches the source
    again.
    """

    status: str
    error: str = ""
    budget_exhausted: tuple[str, ...] = ()
    queue_evictions: int = 0
    human_zone_ambiguous_pages: tuple[str, ...] = ()
    worker_crash: str = ""
    dropped_sources: tuple[str, ...] = ()


# ТЗ 5.2 step 6 pass journal path, vault-relative -- written by
# ``compiled_briefings.py``'s ``_write_pass_journal`` after every
# ``run_nightly_maintenance`` call (G6).
PASS_JOURNAL_RELATIVE_PATH = Path(".session") / "compile-enrich.json"
# The monthly fact-check pass (``compiled_fact_check.py``) keeps a journal of
# its own on purpose -- the file above is overwritten whole by every
# enrichment pass, so sharing it would erase whichever process ran second.
# Declared here rather than imported for the same reason the path above is:
# this module reads both journals, it owns neither.
FACT_CHECK_JOURNAL_RELATIVE_PATH = Path(".session") / "compile-fact-check.json"
# Written by ``compiled_briefings._write_queue_worker_crash_journal`` when the
# detached background drain dies unexpectedly. That writer calls the file an
# "owner-visible trace", but nothing used to read it: the worker runs with
# stdout/stderr on DEVNULL, so a crashed drain quietly stopped enriching
# pages and the owner only ever saw pages that were not updating.
# Written by ``compiled_briefings._record_dropped_queue_source`` when the
# refresh queue permanently gives up on a source. Unlike the two journals
# above this one is not keyed by date: it lists sources still missing from
# ``compiled/``, and the writer removes an entry only once that source
# finally compiles.
DROPPED_SOURCES_JOURNAL_RELATIVE_PATH = (
    Path(".session") / "compile-dropped-sources.json"
)
QUEUE_WORKER_CRASH_JOURNAL_RELATIVE_PATH = (
    Path(".session") / "compile-queue-worker.json"
)


def read_pass_status(vault_path: Path) -> PassStatus:
    """Read the real pass journal (``PASS_JOURNAL_RELATIVE_PATH``) into a
    ``PassStatus`` for ``build_daily_digest``.

    Shared by every caller that wants the actual last-pass outcome instead
    of a hardcoded status: the bot's "Дайджест" button, the
    ``maintenance.compiled-digest`` nightly-maintenance cycle
    (``processor.py``), and (previously inline here) the CLI.

    Two failure shapes are handled differently, on purpose:

    - A *missing* journal (``FileNotFoundError``) means the pass has simply
      never written one yet -- that is not a problem, so it falls back to
      ``PassStatus(status="success")`` -- i.e. "always build" -- the same
      as before.
    - A journal that *exists* but cannot be read or understood (any other
      ``OSError``, a ``UnicodeDecodeError``, invalid JSON, a payload that is
      not an object, or an object with no usable ``status`` field) is a
      different situation: something went wrong (manual edit, filesystem
      corruption, a writer bug), and silently reporting it as "success"
      would hide that from the owner. It is reported honestly as
      ``PassStatus(status="unknown")`` instead, with a ``logger.warning``
      recording the reason for the server-side logs. "unknown" is excluded
      from ``_NON_FAILURE_STATUSES``, so ``_render_digest`` still shows the
      owner a line instead of silence, and ``build_daily_digest`` never
      suppresses the digest for it (suppression only ever checks for the
      literal "no-work" status).
    """
    path = Path(vault_path) / PASS_JOURNAL_RELATIVE_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PassStatus(status="success")
    except (OSError, UnicodeDecodeError):
        logger.warning("Pass journal %s exists but could not be read", path)
        return PassStatus(status="unknown")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Pass journal %s is not valid JSON", path)
        return PassStatus(status="unknown")
    if not isinstance(payload, dict):
        logger.warning(
            "Pass journal %s does not contain a JSON object (got %s)",
            path,
            type(payload).__name__,
        )
        return PassStatus(status="unknown")
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        logger.warning("Pass journal %s has no usable 'status' field", path)
        return PassStatus(status="unknown")
    error = payload.get("error")
    budget_raw = payload.get("budget_exhausted")
    budget_exhausted = (
        tuple(
            item.strip()
            for item in budget_raw
            if isinstance(item, str) and item.strip()
        )
        if isinstance(budget_raw, list)
        else ()
    )
    queue_evictions_raw = payload.get("queue_evictions")
    queue_evictions = (
        queue_evictions_raw
        if isinstance(queue_evictions_raw, int)
        and not isinstance(queue_evictions_raw, bool)
        and queue_evictions_raw > 0
        else 0
    )
    human_zone_raw = payload.get("human_zone_ambiguous_pages")
    human_zone_ambiguous_pages = (
        tuple(
            item.strip()
            for item in human_zone_raw
            if isinstance(item, str) and item.strip()
        )
        if isinstance(human_zone_raw, list)
        else ()
    )
    return PassStatus(
        status=status,
        error=error if isinstance(error, str) else "",
        budget_exhausted=budget_exhausted,
        queue_evictions=queue_evictions,
        human_zone_ambiguous_pages=human_zone_ambiguous_pages,
    )


@dataclass(frozen=True, slots=True)
class _ChangeItem:
    """One page with at least one "Sources That Shaped This Page" row dated
    ``day`` -- either freshly created that day or enriched further."""

    rel_path: str
    title: str
    domain: str
    created_today: bool
    what_added: str
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _DecisionItem:
    """One line for the "Требует решения" block: a page-scoped open question."""

    page: str
    summary: str
    kind: str = "conflict"
    since: str = ""


def _parse_date_field(value: object) -> date | None:
    """Best-effort YYYY-MM-DD parse; ``_page_fields`` always returns plain
    strings, but tolerate a native ``date`` too rather than assuming one
    representation. Never raises: an unparsable value returns ``None``."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _page_fields(text: str) -> dict[str, str]:
    """Parse frontmatter tolerantly instead of raising on a malformed page.

    One broken page in ``compiled/`` (missing closing ``---``, invalid YAML,
    a duplicate key) must not take down the whole digest (ТЗ 7.1: a failed
    pass must still yield a message with a reason, not no message at all).
    ``CompiledBriefingService._iter_candidates`` already reads every
    ``compiled/**`` page with this same regex-based parser for exactly that
    reason -- reuse it here instead of the strict ``parse_frontmatter_bytes``.
    """
    return CompiledBriefingService._frontmatter_fields(text)


# Sentence-final punctuation a page's "что добавилось" cell may already end
# with. A page enriched from several sources contributes one such cell per
# source, and they are rendered as one digest line -- joining them with a
# bare "; " glued a separator onto a finished sentence ("...ограничений.;
# LightLLM..."), which is what the owner actually reads in Telegram.
_FACT_SENTENCE_END = (".", "!", "?", "…", ":")


def _join_change_facts(facts: Iterable[str]) -> str:
    """Join per-source facts into one digest line without doubling punctuation."""
    joined = ""
    for fact in facts:
        if not joined:
            joined = fact
            continue
        separator = " " if joined.endswith(_FACT_SENTENCE_END) else "; "
        joined = f"{joined}{separator}{fact}"
    return joined


def _collect_changes(
    candidates: list[CompiledBriefingCandidate], start: date, end: date
) -> list[_ChangeItem]:
    """Pages with a "Sources That Shaped This Page" row dated anywhere in
    ``[start, end]`` (inclusive).

    Generalizes the original single-day collector (задача N, weekly review):
    the daily digest calls this with ``start == end``, which reproduces the
    original behavior exactly -- rows are still matched by exact string
    equality against one of the window's ISO date strings, the same
    comparison the single-day version used against ``day.isoformat()``, just
    widened from one string to a small set of them.

    ТЗ digest spec, block 2: "страницы, у которых в таблице источников есть
    строки с датой day. Отличай созданную страницу от обогащённой." Creation
    is decided from the ``created`` frontmatter field (set once, on the
    page's first compile pass -- see ``compiled_briefings._upsert_briefing``)
    rather than re-derived from the sources table, which only records
    per-source additions and has no notion of "this was the first one".
    ``created_today`` keeps its original name (a single-day digest is still
    the only caller that renders the "новая страница" vs "обогащена"
    distinction); for a multi-day window it means "created within the
    window", not literally "today".

    ТЗ 7.1: an enriched page's line is a short diff, not a restatement --
    when a window row is the winning side of a temporal supersession (a
    "Claim History" row whose "Superseded By" source is one of the window's
    sources), show it as a replacement of the prior formulation instead of a
    plain addition (uses ``CompiledBriefingService._claim_history_rows``).
    """
    window_days = frozenset(
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    )
    changes: list[_ChangeItem] = []
    for candidate in candidates:
        rows = CompiledBriefingService._sources_shaped_rows(candidate.text)
        window_rows = [row for row in rows if row[0] in window_days]
        if not window_rows:
            continue

        history_rows = CompiledBriefingService._claim_history_rows(candidate.text)
        superseded_by_source: dict[str, tuple[str, str]] = {}
        for _, old_source, old_claim, superseded_by in history_rows:
            superseded_by_source.setdefault(superseded_by, (old_claim, old_source))

        fields = _page_fields(candidate.text)
        created = _parse_date_field(fields.get("created"))

        what_parts: list[str] = []
        for _, source, what in window_rows:
            what = what.strip()
            if not what:
                continue
            replaced = superseded_by_source.get(source)
            if replaced is not None:
                old_claim, old_source = replaced
                what_parts.append(
                    f"{what} (замена: было «{old_claim}» из [[{old_source}]])"
                )
            else:
                what_parts.append(what)
        what_added = _join_change_facts(dict.fromkeys(what_parts))

        sources = tuple(
            dict.fromkeys(
                source.strip() for _, source, _ in window_rows if source.strip()
            )
        )
        changes.append(
            _ChangeItem(
                rel_path=candidate.rel_path,
                title=candidate.title,
                domain=candidate.domain,
                created_today=(created is not None and start <= created <= end),
                what_added=what_added or "(без описания)",
                sources=sources,
            )
        )
    return changes


def _collect_conflicts(
    candidates: list[CompiledBriefingCandidate],
) -> list[_DecisionItem]:
    """Open conflicts (both sides) from every page's "Open Conflicts" table."""
    items: list[_DecisionItem] = []
    for candidate in candidates:
        rows = CompiledBriefingService._open_conflicts_rows(candidate.text)
        for since, existing_claim, existing_source, new_claim, new_source in rows:
            summary = (
                f"«{existing_claim}» ({existing_source}) "
                f"vs «{new_claim}» ({new_source})"
            )
            items.append(
                _DecisionItem(
                    page=candidate.rel_path,
                    summary=summary,
                    kind="conflict",
                    since=since,
                )
            )
    return items


def _read_decisions_queue(vault_path: Path) -> list[_DecisionItem]:
    """Read ``.session/decisions-queue.json``; see the module docstring for
    the expected shape. Any absence or corruption yields an empty list."""
    path = vault_path / DECISIONS_QUEUE_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    items: list[_DecisionItem] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        page = str(entry.get("page") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        if not page or not summary:
            continue
        kind = str(entry.get("kind") or "").strip() or "queued"
        since = str(entry.get("since") or "").strip()
        items.append(_DecisionItem(page=page, summary=summary, kind=kind, since=since))
    return items


# Trailing punctuation (including whitespace around it) to drop when
# normalizing a decision summary for de-dup comparison.
_TRAILING_PUNCT_RE = re.compile(r"[\s.,;:!?…]+$")


def _normalize_decision_summary(summary: str) -> str:
    """Fold near-duplicate summaries onto one key: collapsed whitespace,
    case-insensitive, trailing punctuation dropped. The decisions queue is
    written by the model (ТЗ 7.2), so a duplicate item commonly differs from
    the page-derived conflict only by a trailing period, case, or an extra
    space -- an exact string match would let both through as if distinct.
    """
    collapsed = re.sub(r"\s+", " ", summary).strip()
    return _TRAILING_PUNCT_RE.sub("", collapsed).casefold()


def _merge_decisions(
    conflicts: list[_DecisionItem], queued: list[_DecisionItem]
) -> list[_DecisionItem]:
    """Merge page conflicts with queued items, deduped by (page, normalized
    summary) -- the key stays "page + суть" (ТЗ), only the summary side of
    it is compared normalized.
    """
    merged = list(conflicts)
    seen = {
        (item.page, _normalize_decision_summary(item.summary)) for item in conflicts
    }
    for item in queued:
        key = (item.page, _normalize_decision_summary(item.summary))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _revisit_sort_key(day: date, rel_path: str) -> str:
    """Deterministic-but-varied ordering for revisit candidates: a stable
    hash of the digest date and the page path, not Python's process-random
    ``hash()`` and not plain alphabetical order -- ТЗ 6.5 wants the same
    forgotten page to stop always winning, while a same-day rerun must still
    reproduce identically."""
    digest_input = f"{day.isoformat()}|{rel_path}".encode()
    return hashlib.sha256(digest_input).hexdigest()


def _collect_revisit(
    candidates: list[CompiledBriefingCandidate],
    changes: list[_ChangeItem],
    day: date,
    *,
    limit: int = MAX_REVISIT_PAGES,
) -> list[tuple[str, str]]:
    """1-2 (or ``limit``) stale, cold-tier pages related to today's changes
    by title-word or domain overlap (ТЗ 7.1 block 3 / ТЗ 6.1, 6.5 "повторное
    всплытие").

    Only ``cold``/``archive`` tier pages are eligible (ТЗ 6.1): a page still
    in active use is not "forgotten". Among the eligible ones, the pick is
    ordered by ``_revisit_sort_key`` rather than alphabetically, so the same
    page does not resurface every day just because its path sorts first.

    ``limit`` defaults to the daily digest's ``MAX_REVISIT_PAGES`` (1-2, ТЗ
    7.1); the weekly-review screen (задача N) calls this with ``limit=1``
    ("увидеть одну забытую страницу") instead of adding a second, near-copy
    collector.
    """
    if not changes:
        return []
    changed_paths = {item.rel_path for item in changes}
    changed_domains = {item.domain for item in changes}
    changed_tokens: set[str] = set()
    for item in changes:
        changed_tokens |= {token.lower() for token in TOKEN_RE.findall(item.title)}

    eligible: list[CompiledBriefingCandidate] = []
    for candidate in candidates:
        if candidate.rel_path in changed_paths:
            continue
        if candidate.tier not in COLD_REVISIT_TIERS:
            continue
        fields = _page_fields(candidate.text)
        last_accessed = _parse_date_field(fields.get("last_accessed"))
        if last_accessed is None:
            continue
        if (day - last_accessed).days < STALE_REVISIT_DAYS:
            continue
        title_tokens = {token.lower() for token in TOKEN_RE.findall(candidate.title)}
        related = bool(title_tokens & changed_tokens) or (
            candidate.domain in changed_domains
        )
        if not related:
            continue
        eligible.append(candidate)

    eligible.sort(key=lambda candidate: _revisit_sort_key(day, candidate.rel_path))
    return [
        (candidate.rel_path, candidate.title) for candidate in eligible[:limit]
    ]


def _render_change_line(item: _ChangeItem) -> str:
    label = "новая страница" if item.created_today else "обогащена"
    source_text = (
        ", ".join(f"[[{source}]]" for source in item.sources) if item.sources else "—"
    )
    return (
        f"- [[{item.rel_path}|{item.title}]] ({label}, {item.domain}) "
        f"— {item.what_added} · источник: {source_text}"
    )


# Statuses that must NOT get a "failure" line below even when ``error`` is
# empty -- "no-work" is the ТЗ's one legitimate quiet-day status, and
# "success"/"ok" are this codebase's two spellings of a normal, non-failing
# pass (``read_pass_status``'s own tolerant-fallback default for a *missing*
# journal uses "success"; ``compiled_briefings.py``'s
# ``run_nightly_maintenance`` writes the literal "ok"). "unknown" --
# ``read_pass_status``'s status for a journal that *exists* but could not be
# read or understood -- is deliberately excluded too, but it does not fall
# through to ``_FAILURE_FALLBACK_LINE`` below: it gets its own
# ``_UNKNOWN_STATUS_LINE`` instead, since a journal we could not read is not
# evidence the pass itself failed. Any other status -- most commonly
# "failed", or an unexpected value a future writer might produce -- still
# gets ``_FAILURE_FALLBACK_LINE``.
_NON_FAILURE_STATUSES = frozenset({"no-work", "success", "ok"})

# ТЗ 7.1: "Сбойный проход, наоборот, всегда порождает сообщение с причиной"
# -- shown when the status is a genuine failure (not "unknown", see above)
# but the journal's ``error`` field came back empty, missing, or not a
# string (``read_pass_status`` already tolerates all three when reading the
# journal).
_FAILURE_FALLBACK_LINE = "Проход завершился с ошибкой, подробности недоступны"

# Shown instead of ``_FAILURE_FALLBACK_LINE`` when ``read_pass_status``
# could not read or understand the pass journal at all (``status ==
# "unknown"``): honest about what happened (we could not check the last
# pass) without claiming the pass itself failed and without inventing a
# reason it did not report.
_UNKNOWN_STATUS_LINE = (
    "Не удалось прочитать состояние прошлого прохода обогащения "
    "— статус неизвестен"
)

# Repeat-noise mitigation (owner acceptance, задача N): a decision item
# (queued or an open page conflict alike) that first appeared before today
# is not restated in full every single day -- it is folded into one summary
# line so a month-old conflict does not bloat the digest, while still never
# going silent (see ``build_daily_digest``'s docstring).
_CARRYOVER_SUMMARY_TEMPLATE = (
    "- Ещё {count} пункт(ов) очереди решений без изменений сегодня "
    "— см. «Очередь» в меню."
)

# ТЗ 5.5 inv 7 / 5.6: human-readable translations of the technical
# constraint names ``compiled_briefings.py``'s ``_write_pass_journal`` may
# record in the ``budget_exhausted`` field -- one entry per
# ``pass_obj.budget_exhausted.add(...)`` call site in that module. The
# owner must never see the bare technical name (ТЗ: "перевести в понятный
# текст, а не показывать как есть"). Kept here, not in
# ``compiled_briefings.py``, because that module owns the pass mechanics,
# not the owner-facing wording.
_BUDGET_EXHAUSTED_LABELS: dict[str, str] = {
    "pages-per-pass": (
        "достигнут дневной лимит страниц, которые можно изменить за один "
        f"проход ({MAX_PAGES_PER_PASS}) — остаток остался в очереди до "
        "следующего прохода"
    ),
    "monthly-enrichments-per-page": (
        "какая-то страница уже обогащалась максимально допустимое число "
        f"раз в этом календарном месяце ({MAX_ENRICHMENTS_PER_PAGE_PER_MONTH}) "
        "— остальные источники для неё остались в очереди"
    ),
    "model-calls-per-pass": (
        "исчерпан лимит обращений к модели за один проход "
        f"({MAX_MODEL_CALLS_PER_PASS}) — остаток остался в очереди до "
        "следующего прохода"
    ),
}


def describe_budget_exhausted(names: tuple[str, ...]) -> list[str]:
    """Translate journal ``budget_exhausted`` names into owner-readable
    Russian sentences, one per name, in the given order.

    A name outside ``_BUDGET_EXHAUSTED_LABELS`` (a constraint added to
    ``compiled_briefings.py`` after this table was last updated, or a
    hand-edited journal) still gets a non-empty, non-crashing line instead
    of being dropped or shown raw -- the technical name is kept, but
    wrapped in an explanatory sentence rather than printed bare.
    """
    described = []
    for name in names:
        label = _BUDGET_EXHAUSTED_LABELS.get(name)
        if label is None:
            label = f"исчерпан один из бюджетов прохода (ограничение «{name}»)"
        described.append(label)
    return described


# Owner-visibility gap fix: unlike a queue overflow, there is no "Очередь"
# screen listing pages skipped for ambiguous <!-- human:start/end -->
# markers, so the path itself -- not just a count -- is the useful part of
# this line (ТЗ: the owner is the only one who can fix the marker, and
# needs to know which page to open). Capped so one badly behaved page does
# not turn this into the whole digest; the rest are still counted, just not
# named individually.
_MAX_HUMAN_ZONE_PAGES_SHOWN = 5
# Same cap, same reason, for sources the refresh queue gave up on: a backend
# outage can drop a whole batch at once, and naming every one of them would
# bury the rest of the digest.
_MAX_DROPPED_SOURCES_SHOWN = 5
# A crash journal with no usable message still means the drain died -- report
# it rather than dropping the line for want of a reason.
_WORKER_CRASH_NO_REASON = "причина не записана"


def _describe_human_zone_ambiguous_pages(pages: tuple[str, ...]) -> str:
    """One "Требует решения" line listing pages skipped this pass because
    their human-owned zone markers could not be told apart (see
    ``compiled_briefings.py``'s ``_record_human_zone_ambiguous``).
    """
    shown = pages[:_MAX_HUMAN_ZONE_PAGES_SHOWN]
    page_list = ", ".join(f"[[{page}]]" for page in shown)
    remaining = len(pages) - len(shown)
    if remaining > 0:
        page_list += f" и ещё {remaining}"
    return (
        f"- На {len(pages)} страниц(ах) повреждены метки личной заметки "
        "владельца («<!-- human:start -->» / «<!-- human:end -->» — "
        "должна остаться ровно одна такая пара, начало раньше конца) — "
        "пока метки не поправлены вручную, страница не обновляется и не "
        f"сжимается: {page_list}."
    )


def _describe_dropped_sources(sources: tuple[str, ...]) -> str:
    """One "Требует решения" line naming sources the refresh queue gave up
    on (see ``compiled_briefings.py``'s ``_record_dropped_queue_source``).

    Named, not just counted, for the same reason as the human-zone line
    above: only the owner can act, and the action is to reopen and re-save
    that specific note so it is queued again. Capped the same way so a bad
    day for the model backend cannot turn this into the whole digest.
    """
    shown = sources[:_MAX_DROPPED_SOURCES_SHOWN]
    source_list = ", ".join(f"[[{source}]]" for source in shown)
    remaining = len(sources) - len(shown)
    if remaining > 0:
        source_list += f" и ещё {remaining}"
    return (
        f"- Не удалось собрать страницы по {len(sources)} источник(ам) — "
        "попытки исчерпаны, сами они больше не повторятся; открой и сохрани "
        f"заново, чтобы поставить в очередь: {source_list}."
    )


def _render_digest(
    day: date,
    pass_status: PassStatus,
    decisions: list[_DecisionItem],
    changes: list[_ChangeItem],
    revisit: list[tuple[str, str]],
) -> str:
    lines = [f"**Дайджест обогащения — {day.isoformat()}**"]

    decision_lines: list[str] = []
    error_text = str(pass_status.error or "").strip()
    if error_text:
        decision_lines.append(f"- Проход завершился с ошибкой: {error_text}")
    elif pass_status.status == "unknown":
        decision_lines.append(f"- {_UNKNOWN_STATUS_LINE}")
    elif pass_status.status not in _NON_FAILURE_STATUSES:
        decision_lines.append(f"- {_FAILURE_FALLBACK_LINE}")
    decision_lines.extend(
        f"- Бюджет прохода исчерпан: {description}."
        for description in describe_budget_exhausted(pass_status.budget_exhausted)
    )
    if pass_status.queue_evictions > 0:
        decision_lines.append(
            "- Очередь решений переполнена: "
            f"{pass_status.queue_evictions} старых пункт(ов) вытеснено новыми "
            f"— лимит очереди {QUEUE_CAP} пунктов, проверьте «Очередь» в меню."
        )
    if pass_status.human_zone_ambiguous_pages:
        decision_lines.append(
            _describe_human_zone_ambiguous_pages(
                pass_status.human_zone_ambiguous_pages
            )
        )
    if pass_status.worker_crash:
        decision_lines.append(
            "- Фоновая обработка очереди страниц аварийно остановилась: "
            f"{pass_status.worker_crash} — до перезапуска новые записи "
            "не попадают в собранные страницы."
        )
    if pass_status.dropped_sources:
        decision_lines.append(
            _describe_dropped_sources(pass_status.dropped_sources)
        )

    today_str = day.isoformat()
    today_decisions = [item for item in decisions if item.since == today_str]
    carryover_decisions = [item for item in decisions if item.since != today_str]
    decision_lines.extend(
        f"- [[{item.page}]] — {item.summary}" for item in today_decisions
    )
    if carryover_decisions:
        decision_lines.append(
            _CARRYOVER_SUMMARY_TEMPLATE.format(count=len(carryover_decisions))
        )
    if decision_lines:
        lines += ["", "**Требует решения**", *decision_lines]

    if changes:
        lines += ["", "**Что изменилось**", *(_render_change_line(i) for i in changes)]

    if revisit:
        revisit_lines = [f"- [[{rel_path}|{title}]]" for rel_path, title in revisit]
        lines += ["", "**Стоит посмотреть**", *revisit_lines]

    if len(lines) == 1:
        lines += ["", "Изменений и открытых вопросов нет."]

    return "\n".join(lines)


def _with_fact_check_evictions(
    pass_status: PassStatus, vault_path: Path, day: date
) -> PassStatus:
    """Fold the monthly fact-check pass's own queue evictions into
    ``pass_status`` when that pass ran on ``day``.

    ТЗ 7.2 requires the owner to learn when the queue cap forced old entries
    out, whoever caused it. ``compiled_fact_check.py`` can cause it -- it
    appends a ``fact-check-rejected`` entry per failed page -- but it runs
    with no active ``CompileEnrichPass``, so it records the count in its own
    journal (``FACT_CHECK_JOURNAL_RELATIVE_PATH``) instead. Nothing read that
    journal, so those evictions never reached a digest at all.

    Only counted when the journal's ``date`` is ``day``: this is a *daily*
    digest, and that journal is monthly and is not rewritten in between, so
    folding it in unconditionally would repeat the same eviction line every
    day until the next fact-check run.

    Fail-safe like ``_read_decisions_queue``: a missing, unreadable, or
    malformed journal contributes nothing rather than raising -- the digest
    must still be delivered.
    """
    path = Path(vault_path) / FACT_CHECK_JOURNAL_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return pass_status
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Fact-check journal %s could not be read", path)
        return pass_status
    if not isinstance(payload, dict) or payload.get("date") != day.isoformat():
        return pass_status
    evictions = payload.get("queue_evictions")
    if not isinstance(evictions, int) or isinstance(evictions, bool) or evictions <= 0:
        return pass_status
    return replace(
        pass_status, queue_evictions=pass_status.queue_evictions + evictions
    )


def _with_dropped_sources(pass_status: PassStatus, vault_path: Path) -> PassStatus:
    """Fold the refresh queue's give-up journal into ``pass_status``.

    ``compiled_briefings._record_dropped_queue_source`` writes it when the
    queue deletes the last trigger that would have compiled a source the
    owner wrote. Nothing read it: the drain that produces almost all of
    these runs detached with stdout/stderr on DEVNULL, so its returned
    ``errors`` list went nowhere and the note just never appeared in
    ``compiled/``.

    No ``day`` filter, unlike the two journals above: this file lists what
    is *still* missing rather than what happened on some date, and the
    writer clears an entry itself once that source finally compiles. A
    source dropped last Tuesday and still uncompiled today is still a thing
    the owner has to act on.

    Fail-safe like its siblings: a missing, unreadable, or malformed
    journal contributes nothing rather than raising.
    """
    path = Path(vault_path) / DROPPED_SOURCES_JOURNAL_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return pass_status
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Dropped-sources journal %s could not be read", path)
        return pass_status
    if not isinstance(payload, dict):
        return pass_status
    entries = payload.get("sources")
    if not isinstance(entries, list):
        return pass_status
    dropped: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        source_path = str(item.get("source_path") or "").strip()
        if source_path and source_path not in dropped:
            dropped.append(source_path)
    if not dropped:
        return pass_status
    return replace(pass_status, dropped_sources=tuple(dropped))


def _with_queue_worker_crash(
    pass_status: PassStatus, vault_path: Path, day: date
) -> PassStatus:
    """Fold today's background-queue-worker crash into ``pass_status``.

    ``compiled_briefings._write_queue_worker_crash_journal`` records why the
    detached drain died and calls that record owner-visible, but no reader
    existed: the worker is spawned with stdout/stderr on DEVNULL, so its
    death was invisible everywhere except that file. Meanwhile the drain is
    what enriches pages *during the day* -- with it dead, edits simply stop
    being picked up, which looks identical to "nothing happened today".

    Only today's crash counts, for the same reason as the fact-check
    journal above: the file is overwritten per crash, not per day, so an
    unconditional read would repeat the same line every day forever.

    Fail-safe like its sibling: a missing, unreadable, or malformed journal
    contributes nothing rather than raising.
    """
    path = Path(vault_path) / QUEUE_WORKER_CRASH_JOURNAL_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return pass_status
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Queue worker crash journal %s could not be read", path)
        return pass_status
    if not isinstance(payload, dict):
        return pass_status
    crashed_at = str(payload.get("crashed_at") or "")
    if not crashed_at.startswith(day.isoformat()):
        return pass_status
    error = payload.get("error")
    message = ""
    if isinstance(error, dict):
        message = " ".join(str(error.get("message") or "").split())
    return replace(pass_status, worker_crash=message or _WORKER_CRASH_NO_REASON)


def build_daily_digest(
    vault_path: Path,
    day: date,
    *,
    pass_status: PassStatus,
) -> str | None:
    """Build the ТЗ 7.1 owner digest for one day, or ``None`` if it should
    not be sent at all.

    Pure and read-only: reads ``compiled/**`` pages already written to disk
    (via ``CompiledBriefingService``) plus the optional decisions queue, and
    renders Markdown. Calls no model, writes nothing.

    Suppression (ТЗ 7.1, corrected per owner acceptance): only when the pass
    took no work (``pass_status.status == "no-work"``), it reported no
    error, AND the *merged* decisions list is empty -- JSON-backed queue
    entries (``.session/decisions-queue.json``) plus every open conflict
    read live off each page's own "Open Conflicts" table. "Очередь решений"
    has always had exactly two item kinds (``conflict`` is one of them, see
    ``decisions_queue.py``'s module docstring), so an open conflict is part
    of the queue for this purpose -- it forces a digest just like a
    JSON-backed item, however old it is; the pass having taken no work does
    not make an unresolved conflict disappear.

    (Earlier code here suppressed the digest whenever only the JSON file was
    empty, ignoring page conflicts entirely, and defended that as
    intentional. That read "очередь решений пуста" too narrowly. See
    ``test_open_conflicts_force_digest_even_on_a_no_work_day``, which
    replaces the old test that asserted the opposite.)

    Repeat noise from a long-lived item is bounded a different way, not by
    suppression: ``_render_digest`` shows a decision item in full only when
    it first appeared today (``since == day``); every older item is folded
    into one summary line with a count and a pointer to the "Очередь"
    screen, so a month-old conflict is never fully restated every single
    day, but it is also never silently dropped.

    An error always forces a digest, with the reason as the first "Требует
    решения" line -- and a failure status with no usable reason (empty,
    missing, or non-string ``error``) still gets an honest fallback line
    there instead of reading like a quiet day (see
    ``_NON_FAILURE_STATUSES``/``_render_digest``).

    A non-empty ``pass_status.budget_exhausted`` also forces a digest, for
    the same reason as an error: ТЗ 5.5 inv 7 explicitly forbids treating
    budget exhaustion as a quiet night ("молчаливое усечение запрещено") --
    even a "no-work"-status, error-free, decision-free pass must still be
    reported if a budget was hit, with one "Требует решения" line per
    exhausted budget (``describe_budget_exhausted``).

    A nonzero ``pass_status.queue_evictions`` forces a digest the same way:
    the 30-item decisions-queue cap having forced old entries out is itself
    a fact the owner must learn (ТЗ 7.2), not something a quiet-day
    suppression should hide.

    A non-empty ``pass_status.human_zone_ambiguous_pages`` forces a digest
    the same way, and for a stronger reason than the two above: a page
    stuck on an ambiguous marker does not resolve itself next pass the way
    an exhausted budget or an evicted queue item eventually does -- only
    the owner opening that exact page can fix it, so staying silent about
    it on a "no-work" day would leave it stuck indefinitely.

    A crashed background queue worker forces a digest for the same reason,
    and is the only one of these signals that comes from a journal the
    enrichment pass never writes -- see ``_with_queue_worker_crash``.

    A non-empty ``changes`` list forces a digest too (code review). The
    status describes the *nightly pass*, while ``_collect_changes`` reads
    the pages' own sources tables -- and pages are also enriched during the
    day, off the pass, by the background queue drain
    (``CompiledBriefingService.refresh_after_write``). For an owner whose
    drain keeps up, the night's pass then finds an empty queue and honestly
    reports "no-work" while the day's real page changes sit on disk, so
    suppressing on the status alone hid exactly the digest's block 2 ("что
    изменилось", ТЗ 7.1). ``revisit`` needs no clause of its own: it is
    derived from ``changes`` (``_collect_revisit`` matches candidates
    against the changed pages' domains and title tokens), so it is
    necessarily empty whenever ``changes`` is.
    """
    pass_status = _with_fact_check_evictions(pass_status, Path(vault_path), day)
    pass_status = _with_queue_worker_crash(pass_status, Path(vault_path), day)
    pass_status = _with_dropped_sources(pass_status, Path(vault_path))

    service = CompiledBriefingService(Path(vault_path))
    candidates = service._iter_candidates()

    changes = _collect_changes(candidates, day, day)
    conflicts = _collect_conflicts(candidates)
    queued = _read_decisions_queue(Path(vault_path))
    decisions = _merge_decisions(conflicts, queued)
    revisit = _collect_revisit(candidates, changes, day)

    is_quiet_day = (
        pass_status.status == "no-work"
        and not pass_status.error.strip()
        and not changes
        and not decisions
        and not pass_status.budget_exhausted
        and not pass_status.queue_evictions
        and not pass_status.human_zone_ambiguous_pages
        and not pass_status.worker_crash
        and not pass_status.dropped_sources
    )
    if is_quiet_day:
        return None

    return _render_digest(day, pass_status, decisions, changes, revisit)


def render_digest_note(day: date, digest_markdown: str) -> bytes:
    """Wrap the digest body in the frontmatter the ``derived`` profile
    requires for anything under ``summaries/`` (``frontmatter.route_profile``
    routes that path to ``derived``, which needs type/description/
    last_accessed/relevance/tier).

    Extracted from ``run_compiled_digest.py``'s former private
    ``_render_note`` (задача L) so both the CLI and the bot's "Дайджест"
    button call the same rendering code; ``run_compiled_digest.py`` now
    imports this under its old private name so its own tests keep working
    unchanged.
    """
    today = day.isoformat()
    header = (
        "---\n"
        "type: compiled-digest\n"
        f'description: "Дайджест обогащения compiled/ за {today}"\n'
        f"last_accessed: {today}\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
    )
    return (header + digest_markdown.strip() + "\n").encode("utf-8")


def digest_path(vault_path: Path, day: date) -> Path:
    """ТЗ 7.1 digest file path -- one per day, deterministic: unlike
    ``compiled_briefs.brief_path`` there is no counter-suffix loop, since a
    same-day rerun is expected to overwrite the same file (the CLI's
    existing behavior).

    Extracted from ``run_compiled_digest.py``'s former inline path
    expression (задача L) so both the CLI and the bot's "Дайджест" button
    compute the exact same path without duplicating the literal.
    """
    return vault_path / "summaries" / "compile" / f"{day.isoformat()}.md"


# --- Weekly review (задача N, missed acceptance criterion) ----------------
#
# ТЗ describes a half-hour weekly ritual: work through the decisions queue,
# look at the week's changes, confirm one page as human-reviewed, see one
# forgotten page. This is the dashboard's on-demand "Сводка недели" screen
# (bot/dashboard.py, bot/handlers/menu.py), not a new scheduled workflow --
# the control-plane registry is out of scope for this change. Everything
# below is pure and read-only, exactly like ``build_daily_digest``.


@dataclass(frozen=True, slots=True)
class WeeklyReview:
    """Compact payload for the weekly-review screen.

    ``queue_items`` comes from ``decisions_queue.list_queue_items`` -- the
    exact same source the interactive "Очередь" screen uses, not a second,
    simplified merge of the JSON queue and page conflicts -- so the two
    screens can never disagree about what is open (explicit requirement for
    this task). ``changes``/``revisit`` reuse this module's own daily-digest
    building blocks, widened to the week's date range. ``review_pick`` is
    the one page suggested for a "подтверди, что посмотрел"
    (``human_reviewed``) confirmation, or ``None`` when the week had no
    changes to pick from.
    """

    start: date
    end: date
    queue_items: tuple[QueueItem, ...]
    changes: tuple[_ChangeItem, ...]
    revisit: tuple[tuple[str, str], ...]
    review_pick: tuple[str, str] | None


def _pick_review_candidate(
    changes: list[_ChangeItem], end: date
) -> tuple[str, str] | None:
    """Deterministically pick one page from this week's changes to suggest
    for a "подтверди, что посмотрел" confirmation.

    Same hash-based technique ``_collect_revisit`` already uses
    (``_revisit_sort_key``) instead of e.g. always the alphabetically-first
    path, so the pick is reproducible for a same-window rerun but is not
    pinned to one page forever.
    """
    if not changes:
        return None
    best = min(changes, key=lambda item: _revisit_sort_key(end, item.rel_path))
    return (best.rel_path, best.title)


def collect_weekly_review(vault_path: Path, end: date) -> WeeklyReview:
    """Assemble the ТЗ weekly-review payload for the 7 days ending on and
    including ``end``.

    Pure and read-only, like ``build_daily_digest``: no model calls, no
    writes. Known edge case, inherited unchanged from ``_collect_revisit``
    (its semantics are not touched for this new caller): if the week had no
    changes at all, ``revisit`` comes back empty too, since
    ``_collect_revisit`` only looks for pages related to *this window's*
    changes -- a genuinely quiet week does not get a forgotten-page
    suggestion either.
    """
    vault_path = Path(vault_path)
    start = end - timedelta(days=6)
    service = CompiledBriefingService(vault_path)
    candidates = service._iter_candidates()

    changes = _collect_changes(candidates, start, end)
    revisit = _collect_revisit(candidates, changes, end, limit=1)
    queue_items = list_queue_items(vault_path)
    review_pick = _pick_review_candidate(changes, end)

    return WeeklyReview(
        start=start,
        end=end,
        queue_items=tuple(queue_items),
        changes=tuple(changes),
        revisit=tuple(revisit),
        review_pick=review_pick,
    )
