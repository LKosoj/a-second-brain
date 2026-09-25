"""Weekly wiki care for the compiled layer (T5 "Еженедельный уход за вики").

Independent satellite module, built the same way ``compiled_fact_check.py``
is: it constructs its own ``CompiledBriefingService`` and calls its private
classmethod/staticmethod helpers directly (``_section_bullets``,
``_replace_section``, ``_insert_section_before``, ``_has_section``,
``_render_bullets``, ``_source_links_from_note``, ``_read_page_text``,
``_write_settled_page``, ...) rather than duplicating that logic.

Unlike the nightly compile-enrich pass, this service instance never gets a
``CompileEnrichPass`` (``_active_pass`` stays ``None`` for its whole life),
so ``CompiledBriefingService._run_model``/``_run_json_dict_prompt`` are not
gated by the nightly per-pass budget at all -- ``_ModelCallBudget`` below is
this module's *own* counter, spent manually before every model-consuming
call site, exactly the same "unbudgeted unless something budgets it" shape
``compiled_fact_check.py``'s docstring already documents for its own,
zero-model-call pass.

Four best-effort actions, run in order, sharing one budget
(``DEFAULT_WIKI_CARE_MODEL_CALL_BUDGET`` model calls a run, unless
``no_limit=True``):

1. Missing links -- deterministic candidate scan (shared sources, or one
   page's title mentioned in another's body without a link) confirmed by
   the model in batches, written straight into each page's own
   "Related Pages" section via ``_write_settled_page``.
2. Missing pages -- dangling ``compiled/...`` wikilinks plus model-suggested
   adjacent topics from the T3 catalog (``MOC/compiled-index.md``), each
   resolved to real vault sources via ``qmd.query`` and queued through the
   normal ``enqueue_refresh`` path -- this module never fabricates a page
   out of nothing.
3. Vault-gap questions -- up to 3 model-suggested questions about
   undocumented gaps, answered through the caller's own ``answer_question``
   (the same path ``/do``'s question handling uses).
4. Web search -- Tavily-backed search for weak/outdated topics, imported
   through the same ``extract_web_content``/``WebArchiveService.archive_page``
   path a normal shared link goes through, filed under ``imports/web/auto/``
   (``notes_subdir="auto"``) so its trust level is capped at ``"forwarded"``,
   never ``"own"`` or unqualified ``"integration"`` (ПОПРАВКА 4).

ПОПРАВКА 1: in a normal run (``no_limit=False``) nothing here drains the
refresh queue -- sources this run enqueues (action 2, filed action-3
answers, action-4 imports) are left for the next nightly pass's own budget.
Only ``no_limit=True`` drains the queue to completion at the very end, the
same stop conditions ``run_compiled_import_sweep.py``'s own drain loop uses.

Approximate, not exact, budget accounting for action 3 (documented rather
than hidden): each ``answer_question`` call spends one budget unit, but the
budget only wraps this module's own model calls -- it has no way to see
inside ``answer_question`` if a future change ever made it call the model
more than once per question.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from d_brain.services.compiled_briefings import (
    COMPILED_BRIEFING_DOMAINS,
    DATE_ONLY_RE,
    DEFAULT_QUEUE_BATCH_SIZE,
    MAX_SOURCE_EXCERPT_CHARS,
    WIKILINK_RE,
    CompiledBriefingCandidate,
    CompiledBriefingService,
    _atomic_write_text,
)
from d_brain.services.link_summary import LinkSummaryService
from d_brain.services.ops_log import append_ops_log
from d_brain.services.web_archive import WebArchiveService
from d_brain.services.web_content import (
    WebContentConfig,
    extract_web_content,
    tavily_search,
)

logger = logging.getLogger(__name__)

# Own journal, separate from compile-enrich's ``.session/compile-enrich.json``
# and from fact-check's ``.session/compile-fact-check.json`` -- each of the
# three processes owns its own record so none of them overwrites another's.
_JOURNAL_RELATIVE_PATH = Path(".session") / "compile-wiki-care.json"

DEFAULT_WIKI_CARE_MODEL_CALL_BUDGET = 10
DEFAULT_WIKI_CARE_MIN_INTERVAL_DAYS = 7
DEFAULT_WIKI_CARE_WEB_IMPORT_LIMIT = 3
NO_LIMIT_WIKI_CARE_WEB_IMPORT_LIMIT = 10

# Passed to ``WebArchiveService(notes_subdir=...)`` (ПОПРАВКА 4): notes land
# under ``imports/web/auto/`` rather than the default ``imports/web/notes/``,
# so ``_source_trust_level`` can tell "the owner shared this" from "the bot
# found this on its own initiative" and cap the latter at "forwarded".
IMPORTS_WEB_AUTO_SUBDIR = "auto"

# ПОПРАВКА 2: at most 3 model-call batches for action 1 when the budget is
# limited, so the remaining actions always have enough left: 3 + 1 + (1+3)
# + 2 = 10. No cap at all when ``no_limit=True``.
MISSING_LINK_MAX_BATCHES_LIMITED = 3
MISSING_LINK_BATCH_SIZE = 8

# ПОПРАВКА 3: short titles ("Итоги", "MVP") false-positive far too often on
# a naive substring search to be a usable "mentioned without a link" signal.
MIN_TITLE_MENTION_LENGTH = 5
# ПОПРАВКА 3: sections a page's own code-owned bookkeeping already lists
# other pages/sources in -- scanning them for "mentioned without a link"
# would just flag the page's own Sources/Related Pages list against itself.
_LINK_SCAN_EXCLUDED_SECTIONS = (
    "Sources",
    "Related Pages",
    "Sources That Shaped This Page",
)
_WIKILINK_STRIP_RE = re.compile(r"\[\[[^\]]*\]\]")


class _ModelCallBudget:
    """This module's own model-call counter -- see the module docstring for
    why ``CompiledBriefingService``'s built-in per-pass budget never applies
    here."""

    def __init__(self, limit: int | None) -> None:
        self._limit = limit
        self.used = 0

    def spend(self) -> bool:
        """Reserve one model call, or refuse when the budget is exhausted.

        Every model-consuming call site must check this *before* calling
        the model -- there is no other enforcement.
        """
        if self._limit is not None and self.used >= self._limit:
            return False
        self.used += 1
        return True


def _read_journal(vault_path: Path) -> dict[str, Any]:
    path = vault_path / _JOURNAL_RELATIVE_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _should_run(journal: dict[str, Any], *, today: date) -> bool:
    """Weekly cadence gate: run unless the previous successful run's
    ``last_run`` is less than ``DEFAULT_WIKI_CARE_MIN_INTERVAL_DAYS`` days
    old. A missing or unparseable ``last_run`` always allows a run -- there
    is nothing to rate-limit against yet.
    """
    last_run = str(journal.get("last_run") or "").strip()
    if not DATE_ONLY_RE.match(last_run):
        return True
    try:
        last_run_date = date.fromisoformat(last_run)
    except ValueError:
        return True
    return (today - last_run_date).days >= DEFAULT_WIKI_CARE_MIN_INTERVAL_DAYS


def _write_wiki_care_journal(
    vault_path: Path,
    *,
    today: date,
    status: str,
    previous_last_run: str,
    links_added: int,
    pages_queued: int,
    questions_answered: int,
    articles_imported: int,
    changed_paths: list[str],
    errors: list[str],
    model_calls_used: int,
) -> None:
    """Persist this run's own record. ПОПРАВКА 5: ``last_run`` only ever
    advances on a successful run (``status`` "ok"/"no-work") -- a "failed"
    run keeps whatever ``last_run`` the previous successful run left, so a
    crash never resets the 7-day cadence gate.
    """
    last_run = today.isoformat() if status in ("ok", "no-work") else previous_last_run
    payload = {
        "last_run": last_run,
        "date": today.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "links_added": links_added,
        "pages_queued": pages_queued,
        "questions_answered": questions_answered,
        "articles_imported": articles_imported,
        "changed_paths": changed_paths,
        "errors": errors,
        "model_calls_used": model_calls_used,
    }
    _atomic_write_text(
        vault_path / _JOURNAL_RELATIVE_PATH,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _related_target(rel_path: str) -> str:
    """``compiled/<domain>/<slug>`` -- the "Related Pages" link target
    (ПОПРАВКА 9), i.e. ``rel_path`` without its ``.md`` suffix."""
    return rel_path[:-3] if rel_path.endswith(".md") else rel_path


@dataclass(frozen=True, slots=True)
class _WikiCarePage:
    rel_path: str
    title: str
    sources: frozenset[str]
    related_targets: frozenset[str]
    scrubbed_body_lower: str


@dataclass(frozen=True, slots=True)
class _LinkCandidate:
    rel_path_a: str
    rel_path_b: str
    title_a: str
    title_b: str
    signal: str
    strength: int


def _collect_wiki_care_pages(service: CompiledBriefingService) -> list[_WikiCarePage]:
    pages: list[_WikiCarePage] = []
    for domain in COMPILED_BRIEFING_DOMAINS:
        domain_dir = service.vault_path / "compiled" / domain
        if not domain_dir.exists():
            continue
        for path in sorted(domain_dir.glob("*.md")):
            rel_path = path.relative_to(service.vault_path).as_posix()
            text = CompiledBriefingService._read_page_text(path)
            title = CompiledBriefingService._title_from_text(text).strip() or path.stem
            sources = frozenset(CompiledBriefingService._source_links_from_note(text))
            # Every page this one already links to, anywhere in its text --
            # a body link counts as a link just as a Related Pages one does.
            related_targets = frozenset(
                _related_target(match.split("#", 1)[0].strip())
                for match in WIKILINK_RE.findall(text)
            )
            scrubbed = text
            for heading in _LINK_SCAN_EXCLUDED_SECTIONS:
                scrubbed = CompiledBriefingService._replace_section(
                    scrubbed, heading, []
                )
            scrubbed = _WIKILINK_STRIP_RE.sub("", scrubbed)
            pages.append(
                _WikiCarePage(
                    rel_path=rel_path,
                    title=title,
                    sources=sources,
                    related_targets=related_targets,
                    scrubbed_body_lower=scrubbed.lower(),
                )
            )
    return pages


def _find_missing_link_candidates(pages: list[_WikiCarePage]) -> list[_LinkCandidate]:
    """Deterministic candidate scan, no model involved (ПОПРАВКА 3 for the
    "mentioned without a link" signal). Shared-sources pairs sort ahead of
    mention-only pairs, since two pages citing the same underlying document
    is a much stronger relation signal than a naive text match.
    """
    candidates: list[_LinkCandidate] = []
    for index, page_a in enumerate(pages):
        target_a = _related_target(page_a.rel_path)
        for page_b in pages[index + 1 :]:
            target_b = _related_target(page_b.rel_path)
            if (
                target_b in page_a.related_targets
                or target_a in page_b.related_targets
            ):
                continue
            shared = page_a.sources & page_b.sources
            if len(shared) >= 2:
                candidates.append(
                    _LinkCandidate(
                        rel_path_a=page_a.rel_path,
                        rel_path_b=page_b.rel_path,
                        title_a=page_a.title,
                        title_b=page_b.title,
                        signal=(
                            f"{len(shared)} shared sources "
                            f"({', '.join(sorted(shared)[:3])})"
                        ),
                        strength=100 + len(shared),
                    )
                )
                continue
            mentioned = (
                len(page_a.title) >= MIN_TITLE_MENTION_LENGTH
                and page_a.title.lower() in page_b.scrubbed_body_lower
            ) or (
                len(page_b.title) >= MIN_TITLE_MENTION_LENGTH
                and page_b.title.lower() in page_a.scrubbed_body_lower
            )
            if mentioned:
                candidates.append(
                    _LinkCandidate(
                        rel_path_a=page_a.rel_path,
                        rel_path_b=page_b.rel_path,
                        title_a=page_a.title,
                        title_b=page_b.title,
                        signal="title mentioned in body without a link",
                        strength=1,
                    )
                )
    candidates.sort(key=lambda item: item.strength, reverse=True)
    return candidates


def _missing_links_prompt(batch: list[_LinkCandidate]) -> str:
    lines = [
        "You maintain an LLM-owned compiled markdown knowledge base for a "
        "personal assistant.",
        "Below are candidate pairs of wiki pages that may be related.",
        "For each pair, decide whether linking them is genuinely useful "
        "(not just a coincidental word match).",
        "",
    ]
    for index, item in enumerate(batch, start=1):
        lines.append(f'{index}. A: {item.rel_path_a} -- "{item.title_a}"')
        lines.append(f'   B: {item.rel_path_b} -- "{item.title_b}"')
        lines.append(f"   Signal: {item.signal}")
    lines.extend(
        [
            "",
            "Return ONLY JSON exactly like:",
            '{"confirmed": [{"index": 1, "relevant": true, "reason": "..."}]}',
        ]
    )
    return "\n".join(lines)


def _link_related_page(
    service: CompiledBriefingService,
    *,
    rel_path: str,
    target_rel_path: str,
    target_title: str,
) -> tuple[bool, str | None]:
    """Add ``target_rel_path`` to ``rel_path``'s "Related Pages" section, in
    the ``[[compiled/<domain>/<slug>|<Title>]]`` format (ПОПРАВКА 9), unless
    it is already linked there.

    A no-op, not an error, when the page changed since Action 1 scanned it
    (fresh-read-compare inside ``_write_settled_page``) -- the next weekly
    run scans and retries it.
    """
    note_path = service.vault_path / rel_path
    try:
        text = CompiledBriefingService._read_page_text(note_path)
    except FileNotFoundError:
        return False, "page missing"
    target = _related_target(target_rel_path)
    existing_bullets = CompiledBriefingService._section_bullets(text, "Related Pages")
    existing_targets = {
        match.strip() for match in WIKILINK_RE.findall("\n".join(existing_bullets))
    }
    if target in existing_targets:
        return False, None
    new_bullets = [*existing_bullets, f"[[{target}|{target_title}]]"]
    new_lines = [f"- {bullet}" for bullet in new_bullets]
    if CompiledBriefingService._has_section(text, "Related Pages"):
        new_text = CompiledBriefingService._replace_section(
            text, "Related Pages", new_lines
        )
    else:
        new_text = CompiledBriefingService._insert_section_before(
            text,
            heading="Related Pages",
            before_heading="Sources",
            new_lines=new_lines,
        )
    if new_text == text:
        return False, "could not place Related Pages section"
    parts = Path(rel_path).parts
    domain = parts[1] if len(parts) > 1 else ""
    candidate = CompiledBriefingCandidate(
        rel_path=rel_path,
        domain=domain,
        slug=Path(rel_path).stem,
        title="",
        description="",
        freshness_state="",
        confidence="medium",
        relevance=0.0,
        tier="active",
        text=text,
    )
    written = service._write_settled_page(candidate, new_text)
    return written, None


def _apply_missing_links(
    service: CompiledBriefingService,
    budget: _ModelCallBudget,
    *,
    no_limit: bool,
) -> dict[str, Any]:
    pages = _collect_wiki_care_pages(service)
    all_candidates = _find_missing_link_candidates(pages)
    max_batches = None if no_limit else MISSING_LINK_MAX_BATCHES_LIMITED

    changed_paths: list[str] = []
    errors: list[str] = []
    links_added = 0
    batches_run = 0

    for start in range(0, len(all_candidates), MISSING_LINK_BATCH_SIZE):
        if max_batches is not None and batches_run >= max_batches:
            break
        batch = all_candidates[start : start + MISSING_LINK_BATCH_SIZE]
        if not budget.spend():
            break
        batches_run += 1
        try:
            payload = service._run_json_dict_prompt(
                prompt=_missing_links_prompt(batch),
                timeout=120,
                error_context="wiki-care-related-links",
                json_example=(
                    '{"confirmed": [{"index": 1, "relevant": true, "reason": "..."}]}'
                ),
            )
        except Exception as exc:
            logger.warning("Wiki-care missing-links prompt failed: %s", exc)
            errors.append(f"missing-links batch {batches_run}: {exc}")
            continue
        for item in payload.get("confirmed") or []:
            if not isinstance(item, dict) or not item.get("relevant"):
                continue
            index_raw = item.get("index")
            if index_raw is None:
                continue
            try:
                index = int(index_raw)
            except (TypeError, ValueError):
                continue
            if not 1 <= index <= len(batch):
                continue
            pair = batch[index - 1]
            for rel_path, target_rel_path, target_title in (
                (pair.rel_path_a, pair.rel_path_b, pair.title_b),
                (pair.rel_path_b, pair.rel_path_a, pair.title_a),
            ):
                written, error = _link_related_page(
                    service,
                    rel_path=rel_path,
                    target_rel_path=target_rel_path,
                    target_title=target_title,
                )
                if written:
                    links_added += 1
                    if rel_path not in changed_paths:
                        changed_paths.append(rel_path)
                elif error:
                    errors.append(f"{rel_path} -> {target_rel_path}: {error}")

    return {
        "links_added": links_added,
        "changed_paths": changed_paths,
        "errors": errors,
    }


def _missing_pages_prompt(catalog_text: str) -> str:
    return (
        "You maintain an LLM-owned compiled markdown knowledge base for a "
        "personal assistant.\n"
        "Below is the catalog of existing wiki pages, grouped by domain.\n"
        "Suggest up to 5 topics/pages that are clearly missing -- ones that "
        "logically follow from what is already documented (mentioned in "
        "passing, but with no page of their own). Do not repeat existing "
        "pages.\n"
        f"Each domain must be one of: {', '.join(COMPILED_BRIEFING_DOMAINS)}.\n\n"
        "[CATALOG]\n"
        f"{catalog_text}\n\n"
        "Return ONLY JSON exactly like:\n"
        '{"topics": [{"domain": "topics", "title": "...", "hint": "..."}]}'
    )


def _find_missing_page_topics(
    service: CompiledBriefingService, budget: _ModelCallBudget
) -> list[dict[str, str]]:
    """Two sources of candidate topics (ТЗ, Действие 2), code first, model
    second: dangling ``compiled/...`` wikilinks that point nowhere, and up
    to 5 model-suggested topics that logically follow from the T3 catalog.
    Neither creates a page directly -- see ``_seed_missing_pages``.
    """
    topics: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for domain in COMPILED_BRIEFING_DOMAINS:
        domain_dir = service.vault_path / "compiled" / domain
        if not domain_dir.exists():
            continue
        for path in sorted(domain_dir.glob("*.md")):
            text = CompiledBriefingService._read_page_text(path)
            for target in WIKILINK_RE.findall(text):
                target = target.split("#", 1)[0].strip()
                if not target.startswith("compiled/"):
                    continue
                # Links are usually written without ``.md`` (Related Pages,
                # the T3 catalog); the page file always has it.
                target_path = service.vault_path / target
                if not target_path.suffix:
                    target_path = target_path.with_suffix(".md")
                if target_path.exists():
                    continue
                parts = Path(target).parts
                if len(parts) < 3 or parts[1] not in COMPILED_BRIEFING_DOMAINS:
                    continue
                title = Path(target).stem.replace("-", " ")
                key = (parts[1], title.lower())
                if key in seen:
                    continue
                seen.add(key)
                topics.append({"domain": parts[1], "title": title})

    catalog_path = service.vault_path / "MOC" / "compiled-index.md"
    if catalog_path.exists():
        catalog_text = CompiledBriefingService._read_page_text(catalog_path)
        if catalog_text.strip() and budget.spend():
            try:
                payload = service._run_json_dict_prompt(
                    prompt=_missing_pages_prompt(catalog_text),
                    timeout=120,
                    error_context="wiki-care-missing-pages",
                    json_example=(
                        '{"topics": [{"domain": "topics", "title": "...", '
                        '"hint": "..."}]}'
                    ),
                )
            except Exception as exc:
                logger.warning("Wiki-care missing-pages prompt failed: %s", exc)
                payload = {}
            for item in payload.get("topics") or []:
                if not isinstance(item, dict):
                    continue
                domain = str(item.get("domain") or "").strip()
                title = str(item.get("title") or "").strip()
                if domain not in COMPILED_BRIEFING_DOMAINS or not title:
                    continue
                key = (domain, title.lower())
                if key in seen:
                    continue
                seen.add(key)
                topics.append({"domain": domain, "title": title})

    return topics


def _seed_missing_pages(
    service: CompiledBriefingService, topics: list[dict[str, str]]
) -> dict[str, Any]:
    """Queue real vault sources that already mention a missing topic through
    the normal ``enqueue_refresh`` path, with a text hint prefixed onto the
    excerpt. This is a soft nudge, not a guarantee: the source still goes
    through the model's own Impact/Resolve/Compile/Verify stages exactly as
    any other enqueued source would. A topic with no vault source
    mentioning it at all is skipped outright -- this module never
    fabricates a page out of nothing.
    """
    queued_topics = 0
    errors: list[str] = []
    for topic in topics:
        domain = topic["domain"]
        title = topic["title"]
        try:
            found = service.qmd.query(title, limit=5)
        except Exception as exc:
            logger.warning("Wiki-care qmd query failed for %r: %s", title, exc)
            errors.append(f"{title}: {exc}")
            continue
        results = found.get("results") or []
        if not results:
            continue
        topic_queued = False
        for result in results:
            source_path = str(result.get("file") or "").strip()
            if not source_path:
                continue
            hint = CompiledBriefingService._clip(
                f"[wiki-care: create or expand the page \"{title}\" in the "
                f"{domain} domain, if it does not exist yet]\n\n"
                f"{result.get('snippet', '')}",
                MAX_SOURCE_EXCERPT_CHARS,
            )
            outcome = service.enqueue_refresh(
                source_path=source_path,
                source_excerpt=hint,
                max_updates=2,
            )
            if outcome.get("queued"):
                topic_queued = True
        if topic_queued:
            queued_topics += 1
    return {"queued": queued_topics, "errors": errors}


def _vault_gap_questions_prompt(catalog_text: str) -> str:
    return (
        "You maintain an LLM-owned compiled markdown knowledge base for a "
        "personal assistant.\n"
        "Below is the catalog of existing wiki pages.\n"
        "Formulate up to 3 specific questions about gaps -- things mentioned "
        "but never explained, or things that should logically be known from "
        "the owner's notes but are not reflected in the wiki.\n\n"
        "[CATALOG]\n"
        f"{catalog_text}\n\n"
        "Return ONLY JSON exactly like:\n"
        '{"questions": ["...", "..."]}'
    )


def _find_vault_gap_questions(
    service: CompiledBriefingService, budget: _ModelCallBudget
) -> list[str]:
    catalog_path = service.vault_path / "MOC" / "compiled-index.md"
    if not catalog_path.exists():
        return []
    catalog_text = CompiledBriefingService._read_page_text(catalog_path)
    if not catalog_text.strip() or not budget.spend():
        return []
    try:
        payload = service._run_json_dict_prompt(
            prompt=_vault_gap_questions_prompt(catalog_text),
            timeout=120,
            error_context="wiki-care-vault-gaps",
            json_example='{"questions": ["...", "..."]}',
        )
    except Exception as exc:
        logger.warning("Wiki-care vault-gap prompt failed: %s", exc)
        return []
    return [
        str(question).strip()
        for question in (payload.get("questions") or [])
        if str(question).strip()
    ][:3]


def _answer_vault_gaps(
    questions: list[str],
    budget: _ModelCallBudget,
    *,
    answer_question: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    changed_paths: list[str] = []
    errors: list[str] = []
    answered = 0
    for question in questions:
        if not budget.spend():
            break
        try:
            result = answer_question(question)
        except Exception as exc:
            logger.warning("Wiki-care answer_question failed for %r: %s", question, exc)
            errors.append(f"{question}: {exc}")
            continue
        artifact_path = str((result or {}).get("filed_artifact_path") or "").strip()
        if artifact_path:
            changed_paths.append(artifact_path)
            answered += 1
    return {
        "questions_answered": answered,
        "changed_paths": changed_paths,
        "errors": errors,
    }


def _web_search_queries_prompt(catalog_text: str) -> str:
    return (
        "You maintain an LLM-owned compiled markdown knowledge base for a "
        "personal assistant.\n"
        "Below is the catalog of existing wiki pages.\n"
        "Come up with up to 3 web search queries that would fill in weakly "
        "described or outdated topics. Do not search the web yourself -- "
        "just return the queries.\n\n"
        "[CATALOG]\n"
        f"{catalog_text}\n\n"
        "Return ONLY JSON exactly like:\n"
        '{"queries": ["...", "..."]}'
    )


def _web_search_selection_prompt(
    results: list[dict[str, str]], import_limit: int
) -> str:
    lines = [
        "You maintain an LLM-owned compiled markdown knowledge base for a "
        "personal assistant.",
        "Below are web search results.",
        f"Select up to {import_limit} of the most useful and relevant links "
        "to add to the wiki. Do not pick duplicate links from the same "
        "domain without a reason.",
        "",
        "[RESULTS]",
    ]
    for index, item in enumerate(results, start=1):
        snippet = (item.get("content") or "").strip().replace("\n", " ")[:200]
        lines.append(
            f'{index}. "{item.get("title", "")}" -- {item.get("url", "")} -- {snippet}'
        )
    lines.extend(
        [
            "",
            "Return ONLY JSON exactly like:",
            '{"selected": [{"index": 1, "reason": "..."}]}',
        ]
    )
    return "\n".join(lines)


def _run_web_search_action(
    service: CompiledBriefingService,
    budget: _ModelCallBudget,
    *,
    tavily_api_key: str,
    no_limit: bool,
    content_language: str,
    ai_cli: str,
) -> dict[str, Any]:
    empty_result: dict[str, Any] = {
        "articles_imported": 0,
        "changed_paths": [],
        "errors": [],
    }
    if not tavily_api_key:
        return empty_result
    catalog_path = service.vault_path / "MOC" / "compiled-index.md"
    if not catalog_path.exists():
        return empty_result
    catalog_text = CompiledBriefingService._read_page_text(catalog_path)
    if not catalog_text.strip() or not budget.spend():
        return empty_result

    try:
        query_payload = service._run_json_dict_prompt(
            prompt=_web_search_queries_prompt(catalog_text),
            timeout=120,
            error_context="wiki-care-web-search-queries",
            json_example='{"queries": ["...", "..."]}',
        )
    except Exception as exc:
        logger.warning("Wiki-care web-search query prompt failed: %s", exc)
        return empty_result
    queries = [
        str(query).strip()
        for query in (query_payload.get("queries") or [])
        if str(query).strip()
    ][:3]
    if not queries:
        return empty_result

    results: list[dict[str, str]] = []
    for query in queries:
        for item in tavily_search(query, api_key=tavily_api_key, max_results=5):
            results.append(item)
    if not results or not budget.spend():
        return empty_result

    import_limit = (
        NO_LIMIT_WIKI_CARE_WEB_IMPORT_LIMIT
        if no_limit
        else DEFAULT_WIKI_CARE_WEB_IMPORT_LIMIT
    )
    try:
        selection_payload = service._run_json_dict_prompt(
            prompt=_web_search_selection_prompt(results, import_limit),
            timeout=120,
            error_context="wiki-care-web-search-selection",
            json_example='{"selected": [{"index": 1, "reason": "..."}]}',
        )
    except Exception as exc:
        logger.warning("Wiki-care web-search selection prompt failed: %s", exc)
        return empty_result

    selected_indexes: list[int] = []
    for item in selection_payload.get("selected") or []:
        if not isinstance(item, dict):
            continue
        index_raw = item.get("index")
        if index_raw is None:
            continue
        try:
            index = int(index_raw)
        except (TypeError, ValueError):
            continue
        if 1 <= index <= len(results):
            selected_indexes.append(index)

    imported_paths: list[str] = []
    errors: list[str] = []
    web_archive = WebArchiveService(
        service.vault_path,
        content_language=content_language,
        ai_cli=ai_cli,
        notes_subdir=IMPORTS_WEB_AUTO_SUBDIR,
    )
    for index in selected_indexes[:import_limit]:
        url = results[index - 1].get("url", "")
        if not LinkSummaryService._is_public_http_url(url):
            continue
        try:
            content_result = extract_web_content(
                url,
                config=WebContentConfig(tavily_api_key=tavily_api_key),
                timeout=30.0,
                allowed_url=LinkSummaryService._is_public_http_url,
            )
        except Exception as exc:
            logger.warning("Wiki-care web import failed for %s: %s", url, exc)
            errors.append(f"{url}: {exc}")
            continue
        if not LinkSummaryService._is_public_http_url(content_result.url):
            continue
        if not content_result.content:
            continue
        try:
            archived = web_archive.archive_page(
                content_result,
                original_url=url,
                timestamp=datetime.now(),
                summary="",
                source=None,
                refresh_qmd=True,
            )
        except Exception as exc:
            logger.warning("Wiki-care web archive failed for %s: %s", url, exc)
            errors.append(f"{url}: {exc}")
            continue
        imported_paths.append(archived.note_path)

    if imported_paths:
        # ``archive_page`` already logs its own "ingest" event; this is a
        # separate "web-search" event so the digest/owner can tell a page
        # wiki-care found on its own initiative from one the owner shared.
        append_ops_log(
            service.vault_path,
            "web-search",
            f"запросов {len(queries)}, импортировано {len(imported_paths)}: "
            + ", ".join(imported_paths),
        )

    return {
        "articles_imported": len(imported_paths),
        "changed_paths": imported_paths,
        "errors": errors,
    }


def _drain_to_completion(service: CompiledBriefingService) -> dict[str, Any]:
    """Same loop as ``run_compiled_import_sweep._drain_to_completion``,
    kept as its own copy here rather than imported (ПОПРАВКА 1 moves
    "drain to completion" into this module, reachable only when
    ``no_limit=True`` -- a normal weekly run never drains, so sources
    queued this run are left for the next nightly pass's own budget)."""
    drained = 0
    updated: list[str] = []
    errors: list[str] = []
    while True:
        result = service.drain_queue(force=True, max_events=DEFAULT_QUEUE_BATCH_SIZE)
        if result.get("errors") == ["worker-busy"]:
            errors.extend(result["errors"])
            break
        drained_this_round = int(result.get("drained") or 0)
        drained += drained_this_round
        updated.extend(result.get("updated") or [])
        errors.extend(result.get("errors") or [])
        if drained_this_round <= 0:
            break
    return {
        "drained": drained,
        "updated": list(dict.fromkeys(updated)),
        "errors": errors,
    }


def run_weekly_wiki_care(
    vault_path: Path,
    *,
    content_language: str = "ru",
    ai_cli: str = "claude",
    answer_question: Callable[[str], dict[str, Any]],
    tavily_api_key: str = "",
    today: date | None = None,
    no_limit: bool = False,
) -> dict[str, Any]:
    """T5 entry point. See the module docstring for the four actions and
    ПОПРАВКА 1 for why a normal run never drains the refresh queue.
    """
    resolved_today = today or date.today()
    resolved_vault_path = Path(vault_path).resolve()
    journal = _read_journal(resolved_vault_path)
    previous_last_run = str(journal.get("last_run") or "").strip()

    if not no_limit and not _should_run(journal, today=resolved_today):
        return {
            "status": "skipped-interval",
            "links_added": 0,
            "pages_created": 0,
            "questions_answered": 0,
            "articles_imported": 0,
            "changed_paths": [],
            "errors": [],
            "model_calls_used": 0,
        }

    service = CompiledBriefingService(
        resolved_vault_path, content_language=content_language, ai_cli=ai_cli
    )
    budget = _ModelCallBudget(None if no_limit else DEFAULT_WIKI_CARE_MODEL_CALL_BUDGET)

    changed_paths: list[str] = []
    errors: list[str] = []
    links_added = 0
    pages_queued = 0
    questions_answered = 0
    articles_imported = 0
    wiki_care_exc_type = ""

    try:
        link_result = _apply_missing_links(service, budget, no_limit=no_limit)
        links_added = link_result["links_added"]
        changed_paths.extend(link_result["changed_paths"])
        errors.extend(link_result["errors"])

        topics = _find_missing_page_topics(service, budget)
        pages_result = _seed_missing_pages(service, topics)
        pages_queued = pages_result["queued"]
        errors.extend(pages_result["errors"])

        questions = _find_vault_gap_questions(service, budget)
        answers_result = _answer_vault_gaps(
            questions, budget, answer_question=answer_question
        )
        questions_answered = answers_result["questions_answered"]
        changed_paths.extend(answers_result["changed_paths"])
        errors.extend(answers_result["errors"])

        web_result = _run_web_search_action(
            service,
            budget,
            tavily_api_key=tavily_api_key,
            no_limit=no_limit,
            content_language=content_language,
            ai_cli=ai_cli,
        )
        articles_imported = web_result["articles_imported"]
        changed_paths.extend(web_result["changed_paths"])
        errors.extend(web_result["errors"])

        if no_limit:
            drain = _drain_to_completion(service)
            changed_paths.extend(drain["updated"])
            errors.extend(drain["errors"])

        compiled_index_written = False
        try:
            from d_brain.services.compiled_index import refresh_compiled_index

            compiled_index_written = refresh_compiled_index(resolved_vault_path)
        except Exception as index_exc:  # noqa: BLE001 - best-effort, T3
            logger.warning("Compiled index catalog refresh failed: %s", index_exc)
    except Exception as exc:
        wiki_care_exc_type = type(exc).__name__
        raise
    finally:
        if wiki_care_exc_type:
            status = "failed"
        elif links_added or pages_queued or questions_answered or articles_imported:
            status = "ok"
        else:
            status = "no-work"
        try:
            _write_wiki_care_journal(
                resolved_vault_path,
                today=resolved_today,
                status=status,
                previous_last_run=previous_last_run,
                links_added=links_added,
                pages_queued=pages_queued,
                questions_answered=questions_answered,
                articles_imported=articles_imported,
                changed_paths=changed_paths,
                errors=errors,
                model_calls_used=budget.used,
            )
        except Exception as exc:  # noqa: BLE001 - must not mask the real cause
            logger.warning("Failed to write weekly wiki-care journal: %s", exc)
        if wiki_care_exc_type:
            wiki_care_summary = f"ошибка: {wiki_care_exc_type}"
        else:
            wiki_care_summary = (
                f"связей {links_added}, страниц поставлено {pages_queued}, "
                f"вопросов {questions_answered}, статей {articles_imported}"
            )
        append_ops_log(resolved_vault_path, "wiki-care", wiki_care_summary)

    return {
        "status": status,
        "links_added": links_added,
        "pages_created": pages_queued,
        "questions_answered": questions_answered,
        "articles_imported": articles_imported,
        "changed_paths": changed_paths,
        "errors": errors,
        "model_calls_used": budget.used,
        "compiled_index_written": compiled_index_written,
    }
