"""Daily digest of memory updates and new questions for the owner.

Reads compiled pages and the decisions queue without model calls or writes.
Technical processing details remain in their journals and the queue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from d_brain.services import compiled_page
from d_brain.services.compiled_briefings import (
    MAX_ENRICHMENTS_PER_PAGE_PER_MONTH,
    MAX_MODEL_CALLS_PER_PASS,
    MAX_PAGES_PER_PASS,
    CompiledBriefingCandidate,
    CompiledBriefingService,
)
from d_brain.services.compiled_page import TOKEN_RE
from d_brain.services.decisions_queue import (
    CONFLICT_POINTER_KINDS,
    DECISIONS_QUEUE_RELATIVE_PATH,
    QueueItem,
    list_queue_items,
)

logger = logging.getLogger(__name__)

# ТЗ 5.6: "Страница считается давно не открывавшейся — нет обращений 90 дней".
STALE_REVISIT_DAYS = 90
# ТЗ 7.1: "Стоит посмотреть. Одна-две давно не открывавшиеся страницы".
MAX_REVISIT_PAGES = 2
# Keep the Telegram digest scannable while preserving honest totals.
MAX_DECISIONS_SHOWN = 5
MAX_CHANGES_SHOWN = 5
MAX_CHANGE_FACTS_SHOWN = 5
# ТЗ 6.1 "Уровень памяти управляет бюджетом обогащения" / 6.5 "Повторное
# всплытие": only these two tiers are eligible to resurface -- "core",
# "active", and "warm" pages are in active use and are not what the owner
# has forgotten about.
COLD_REVISIT_TIERS = frozenset({"cold", "archive"})


@dataclass(frozen=True, slots=True)
class PassStatus:
    """Last enrichment pass journal, shared by the CLI and bot callers."""

    status: str
    error: str = ""
    budget_exhausted: tuple[str, ...] = ()
    monthly_capped_pages: tuple[str, ...] = ()
    deferred_queue_sources: int = 0
    queue_evictions: int = 0
    human_zone_ambiguous_pages: tuple[str, ...] = ()
    worker_crash: str = ""
    dropped_sources: tuple[str, ...] = ()
    auto_decisions: int = 0


# ТЗ 5.2 step 6 pass journal path, vault-relative -- written by
# ``compiled_briefings.py``'s ``_write_pass_journal`` after every
# ``run_nightly_maintenance`` call (G6).
PASS_JOURNAL_RELATIVE_PATH = Path(".session") / "compile-enrich.json"
# Diagnostic journal paths, also used by the maintenance workflows.
FACT_CHECK_JOURNAL_RELATIVE_PATH = Path(".session") / "compile-fact-check.json"
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
      recording the reason for the server-side logs.
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
    monthly_capped_raw = payload.get("monthly_capped_pages")
    monthly_capped_pages = (
        tuple(
            item.strip()
            for item in monthly_capped_raw
            if isinstance(item, str) and item.strip()
        )
        if isinstance(monthly_capped_raw, list)
        else ()
    )
    deferred_raw = payload.get("deferred_queue_sources")
    deferred_queue_sources = (
        deferred_raw
        if isinstance(deferred_raw, int)
        and not isinstance(deferred_raw, bool)
        and deferred_raw > 0
        else 0
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
    auto_decisions_raw = payload.get("queue_auto_decisions")
    auto_decisions = (
        auto_decisions_raw
        if isinstance(auto_decisions_raw, int)
        and not isinstance(auto_decisions_raw, bool)
        and auto_decisions_raw > 0
        else 0
    )
    return PassStatus(
        status=status,
        error=error if isinstance(error, str) else "",
        budget_exhausted=budget_exhausted,
        monthly_capped_pages=monthly_capped_pages,
        deferred_queue_sources=deferred_queue_sources,
        queue_evictions=queue_evictions,
        human_zone_ambiguous_pages=human_zone_ambiguous_pages,
        auto_decisions=auto_decisions,
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
    summary: str = ""


@dataclass(frozen=True, slots=True)
class _DecisionItem:
    """One line for the "Требует решения" block: a page-scoped open question."""

    page: str
    summary: str
    kind: str = "conflict"
    since: str = ""


def _parse_date_field(value: object) -> date | None:
    """Best-effort YYYY-MM-DD parse; ``compiled_page.frontmatter_fields``
    always returns plain strings (and never raises on a broken page, so one
    bad page cannot take down the digest), but tolerate a native ``date``
    too rather than assuming one representation. Never raises: an
    unparsable value returns ``None``."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


# Sentence-final punctuation a page's "что добавилось" cell may already end
# with. A page enriched from several sources contributes one such cell per
# source, and they are rendered as one digest line -- joining them with a
# bare "; " glued a separator onto a finished sentence ("...ограничений.;
# LightLLM..."), which is what the owner actually reads in Telegram.
_FACT_SENTENCE_END = (".", "!", "?", "…", ":")


def _join_change_facts(facts: Iterable[str]) -> str:
    """Deduplicate, cap, and join facts without doubling punctuation."""
    unique_facts: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        cleaned = re.sub(r"\s+", " ", fact).strip()
        key = _TRAILING_PUNCT_RE.sub("", cleaned).casefold()
        if not cleaned or not key or key in seen:
            continue
        seen.add(key)
        unique_facts.append(cleaned)

    shown_facts = unique_facts[:MAX_CHANGE_FACTS_SHOWN]
    joined = ""
    for fact in shown_facts:
        if not joined:
            joined = fact
            continue
        separator = " " if joined.endswith(_FACT_SENTENCE_END) else "; "
        joined = f"{joined}{separator}{fact}"
    hidden_count = len(unique_facts) - len(shown_facts)
    if hidden_count:
        separator = " " if joined.endswith(_FACT_SENTENCE_END) else "; "
        joined = f"{joined}{separator}Ещё пунктов: {hidden_count}."
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
    plain addition (uses ``compiled_page.claim_history_rows``).
    """
    window_days = frozenset(
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    )
    changes: list[_ChangeItem] = []
    for candidate in candidates:
        rows = compiled_page.sources_shaped_rows(candidate.text)
        window_rows = [row for row in rows if row[0] in window_days]
        if not window_rows:
            continue

        history_rows = compiled_page.claim_history_rows(candidate.text)
        superseded_by_source: dict[str, list[tuple[str, str]]] = {}
        for _, old_source, old_claim, superseded_by in history_rows:
            replaced = (old_claim, old_source)
            replacements = superseded_by_source.setdefault(superseded_by, [])
            if replaced not in replacements:
                replacements.append(replaced)

        fields = compiled_page.frontmatter_fields(candidate.text)
        created = _parse_date_field(fields.get("created"))

        what_parts: list[str] = []
        for _, _source, what in window_rows:
            what = what.strip()
            if not what:
                continue
            what_parts.append(what)
        what_added = _join_change_facts(dict.fromkeys(what_parts))

        window_sources = tuple(dict.fromkeys(source for _, source, _ in window_rows))
        replacement_parts = [
            f"«{old_claim}» из [[{old_source}]]"
            for source in window_sources
            for old_claim, old_source in superseded_by_source.get(source, [])
        ]
        if replacement_parts:
            replacement_note = "Замена: было " + "; ".join(replacement_parts) + "."
            what_added = f"{what_added} {replacement_note}".strip()

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
                summary=re.sub(r"\s+", " ", what_parts[0]).strip()
                if what_parts
                else "",
            )
        )
    return changes


def _collect_conflicts(
    candidates: list[CompiledBriefingCandidate],
) -> list[_DecisionItem]:
    """Open conflicts (both sides) from every page's "Open Conflicts" table."""
    items: list[_DecisionItem] = []
    for candidate in candidates:
        rows = compiled_page.open_conflicts_rows(candidate.text)
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
    the expected shape. Any absence or corruption yields an empty list.

    Skips ``CONFLICT_POINTER_KINDS`` for the same reason ``list_queue_items``
    does: they point at a page whose conflict is already listed here from
    the page's own Open Conflicts table, so counting them restates one
    disagreement twice. It also keeps this count honest about the screen it
    sends the owner to -- the "Очередь" screen hides these kinds, so an
    entry counted here but hidden there is a decision line nobody can act
    on."""
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
        if kind in CONFLICT_POINTER_KINDS:
            continue
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
        fields = compiled_page.frontmatter_fields(candidate.text)
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
    return [(candidate.rel_path, candidate.title) for candidate in eligible[:limit]]


def _render_change_line(item: _ChangeItem) -> str:
    summary = item.what_added or item.summary
    return f"- [[{item.rel_path}|{item.title}]] — {summary}"


# The one ``budget_exhausted`` name whose owner-facing line is built by a
# function rather than read from the table below: it is the only budget tied
# to a specific page, so its sentence carries the paths the journal recorded
# (see ``_describe_monthly_cap``).
MONTHLY_CAP_BUDGET_NAME = "monthly-enrichments-per-page"

# ТЗ 5.5 inv 7 / 5.6: human-readable translations of the technical
# constraint names ``compiled_briefings.py``'s ``_write_pass_journal`` may
# record in the ``budget_exhausted`` field -- one entry per
# ``pass_obj.budget_exhausted.add(...)`` call site in that module, except
# ``MONTHLY_CAP_BUDGET_NAME`` above. The owner must never see the bare
# technical name (ТЗ: "перевести в понятный текст, а не показывать как
# есть"). Kept here, not in ``compiled_briefings.py``, because that module
# owns the pass mechanics, not the owner-facing wording.
_BUDGET_EXHAUSTED_LABELS: dict[str, str] = {
    "pages-per-pass": (
        "достигнут дневной лимит страниц, которые можно изменить за один "
        f"проход ({MAX_PAGES_PER_PASS}) — остаток остался в очереди до "
        "следующего прохода"
    ),
    "model-calls-per-pass": (
        "исчерпан лимит обращений к модели за один проход "
        f"({MAX_MODEL_CALLS_PER_PASS}) — остаток остался в очереди до "
        "следующего прохода"
    ),
}

_MAX_MONTHLY_CAPPED_PAGES_SHOWN = 5


def _describe_monthly_cap(pages: tuple[str, ...]) -> str:
    """The ``MONTHLY_CAP_BUDGET_NAME`` line, naming the pages that hit the
    cap.

    The only thing the owner can do about this budget is open the page and
    decide whether it is really still one page, so the line has to say which
    page it is. A journal written before ``monthly_capped_pages`` existed
    (or one hand-edited since) has no paths to name, and then the wording
    falls back to the previous, page-less sentence rather than inventing a
    path.
    """
    if not pages:
        return (
            "какая-то страница уже обогащалась максимально допустимое число "
            f"раз в этом календарном месяце ({MAX_ENRICHMENTS_PER_PAGE_PER_MONTH}) "
            "— остальные источники для неё остались в очереди"
        )
    shown = pages[:_MAX_MONTHLY_CAPPED_PAGES_SHOWN]
    page_list = ", ".join(f"[[{page}]]" for page in shown)
    remaining = len(pages) - len(shown)
    if remaining > 0:
        page_list += f" и ещё {remaining}"
    if len(pages) == 1:
        subject = f"страница {page_list} уже обогащалась"
        tail = "остальные источники для неё остались в очереди"
    else:
        subject = f"страницы {page_list} уже обогащались"
        tail = "остальные источники для них остались в очереди"
    return (
        f"{subject} максимально допустимое число раз в этом календарном "
        f"месяце ({MAX_ENRICHMENTS_PER_PAGE_PER_MONTH}) — {tail}"
    )


def describe_budget_exhausted(
    names: tuple[str, ...],
    *,
    monthly_capped_pages: tuple[str, ...] = (),
) -> list[str]:
    """Translate journal ``budget_exhausted`` names into owner-readable
    Russian sentences, one per name, in the given order.

    ``monthly_capped_pages`` is the journal's field of the same name -- the
    pages behind a ``MONTHLY_CAP_BUDGET_NAME`` entry. Passing it turns the
    page-less "какая-то страница" wording into one that names them; leaving
    it out keeps the old wording, which is what a caller reading an older
    journal gets.

    A name outside ``_BUDGET_EXHAUSTED_LABELS`` (a constraint added to
    ``compiled_briefings.py`` after this table was last updated, or a
    hand-edited journal) still gets a non-empty, non-crashing line instead
    of being dropped or shown raw -- the technical name is kept, but
    wrapped in an explanatory sentence rather than printed bare.
    """
    described = []
    for name in names:
        if name == MONTHLY_CAP_BUDGET_NAME:
            described.append(_describe_monthly_cap(monthly_capped_pages))
            continue
        label = _BUDGET_EXHAUSTED_LABELS.get(name)
        if label is None:
            label = f"исчерпан один из бюджетов прохода (ограничение «{name}»)"
        described.append(label)
    return described


def _render_digest(
    day: date,
    decisions: list[_DecisionItem],
    changes: list[_ChangeItem],
) -> str:
    lines = [f"**Обновления памяти — {day.isoformat()}**"]
    if changes:
        lines += ["", *map(_render_change_line, changes[:MAX_CHANGES_SHOWN])]
        lines += ["", f"Обновлено карточек: {len(changes)} (включая разбор архива)."]
    if decisions:
        lines += ["", "**Требует вашего ответа**"]
        lines.extend(
            f"- [[{item.page}]] — {item.summary}"
            for item in decisions[:MAX_DECISIONS_SHOWN]
        )
        if len(decisions) > MAX_DECISIONS_SHOWN:
            lines.append(
                f"- Ещё вопросов: {len(decisions) - MAX_DECISIONS_SHOWN} "
                "— см. «Очередь»."
            )
    return "\n".join(lines)


def build_daily_digest(
    vault_path: Path,
    day: date,
    *,
    pass_status: PassStatus,
) -> str | None:
    """Return updates and today's identity questions, or None on a quiet day.

    Pass status does not trigger delivery: processing details stay in journals.
    The full decisions queue and weekly review retain their existing content.
    """
    service = CompiledBriefingService(Path(vault_path))
    changes = _collect_changes(service.iter_candidates(), day, day)
    decisions = _merge_decisions(
        [],
        [
            item
            for item in _read_decisions_queue(Path(vault_path))
            if item.kind == "entity-identity" and item.since == day.isoformat()
        ],
    )
    if not changes and not decisions:
        return None
    return _render_digest(day, decisions, changes)


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
    candidates = service.iter_candidates()

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
