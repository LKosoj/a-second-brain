"""LLM-maintained compiled briefings derived from raw/searchable vault notes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from d_brain.manifest import VaultManifest, load_manifest_for_vault
from d_brain.services.cli_runner import (
    CliExecutionError,
    CliRunner,
    detect_terminal_backend_message,
    normalize_ai_cli,
)
from d_brain.services.frontmatter import (
    FrontmatterError,
    parse_frontmatter_bytes,
    patch_frontmatter_bytes,
    write_validated_vault_markdown,
)
from d_brain.services.json_normalizer import extract_first_json_dict
from d_brain.services.localization import normalize_language, prompt_language_name
from d_brain.services.qmd import QmdService
from d_brain.services.vault_lock import VaultWriteLock, vault_write_lock

logger = logging.getLogger(__name__)
QueueLockResult = TypeVar("QueueLockResult")


class CompiledBriefingWriteConflict(RuntimeError):
    """Raised when a briefing changes while its replacement is being built."""


class HumanZoneMarkerError(ValueError):
    """Raised when a page's human-zone markers are missing or malformed.

    Subclasses ``ValueError`` (not ``RuntimeError``) on purpose: callers
    already catch ``ValueError`` around briefing writes (``refresh_after_write``,
    ``_backfill_freshness_notes``), so this fails the page closed for free
    without new except clauses — better to skip a page than lose owner text.
    """


class CompiledPageEncodingError(ValueError):
    """Raised when a compiled page's bytes on disk are not valid UTF-8.

    Every reader here decodes pages with ``errors="replace"`` so that one
    stray byte cannot take a whole report down. Writers cannot: they render
    the page from that decoded text, so writing it back would burn U+FFFD
    over whatever was really in those bytes -- an owner note saved by an
    editor in another encoding looks exactly like this, and the human zone
    is the one thing this layer promises to keep byte-for-byte. Subclasses
    ``ValueError`` for the same reason ``HumanZoneMarkerError`` does: the
    callers around briefing writes already catch it, so the page fails
    closed for free.
    """


class CompiledBriefingVerificationRejectedError(RuntimeError):
    """Raised when Verify rejects a majority of one page's sampled claims.

    ТЗ 5.2 step 4: "если отклонено больше половины новых утверждений
    страницы — страница не применяется целиком и уходит в очередь решений."
    Callers must treat this exactly like ``CompiledBriefingWriteConflict``
    (skip the page, log, keep going) — see ``refresh_after_write`` and
    ``_backfill_freshness_notes``.
    """


class CompiledBriefingPassBudgetExceededError(RuntimeError):
    """Raised when one compile-enrich pass hits a ТЗ 5.6 budget: the
    per-pass model-call cap, the per-pass changed-page cap, or one page's
    per-calendar-month enrichment cap.

    ТЗ 5.5 invariant 7: "При исчерпании бюджета проход завершается штатно,
    остаток остаётся в очереди, факт исчерпания попадает в дайджест."
    Callers must treat this as a normal pass-ending signal, not a failure --
    see ``refresh_after_write``, ``_drain_queue_once``, and
    ``_backfill_freshness_notes``, each of which has a dedicated branch for
    it, kept separate from their existing error handling so an exhausted
    budget can never be mistaken for a real update or a real failure.
    """


class CompiledSourceStateError(ValueError):
    """Raised by ``_load_source_state_unlocked`` when
    ``.compiled/source-state.json`` is corrupt or in an unsupported shape.

    Code review Finding 2: this used to only ever surface on the nightly
    path (``freshness_issues``, ``initialize_source_state``), where it is a
    real signal the owner must see. ``_source_tier_ranks`` now also reads
    source-state, but only to *order* an already-ready queue -- a distinct
    subclass lets that one read-site catch narrowly and degrade instead of
    crashing the ordinary queue-only CLI run, without loosening the nightly
    path's strictness (it still lets this propagate).
    """


COMPILED_BRIEFING_DOMAINS = (
    "projects",
    "people",
    "topics",
    "decisions",
    "meetings",
    "concepts",
)
DOMAIN_HINTS = {
    "projects": "долгоживущие проекты, инициативы, клиенты, pipeline, product threads",
    "people": "люди, контакты, партнёры, клиенты, команды, важные отношения",
    "topics": "устойчивые темы исследования, направления, recurring topics",
    "decisions": "решения с последствиями, договорённости, commitments, constraints",
    "meetings": (
        "повторяющиеся серии встреч, важные переговорные треки, recurring calls"
    ),
    "concepts": (
        "устойчивые понятия, методы, модели, подходы; переносимые между "
        "проектами и клиентами, не привязанные к конкретному проекту, "
        "клиенту или дате"
    ),
}
STATUS_VALUES = {"active", "draft", "pending", "done", "inactive"}
FRESHNESS_VALUES = {"fresh", "watch", "stale"}
CONFIDENCE_VALUES = {"high", "medium", "low"}
RECORD_KIND_VALUES = {"decision", "incident", "briefing"}
DECISION_STATUS_VALUES = {"proposed", "accepted", "rejected", "superseded"}
# The four levels the incident prompt offers (see ``severity`` there). Like
# ``decision_status`` above, this renders as a bare ``severity: {value}``
# line, so an off-menu answer is dropped rather than trusted into YAML.
INCIDENT_SEVERITY_VALUES = {"low", "medium", "high", "critical"}
SOURCES_TRUST_VALUES = {"own", "forwarded", "integration", "inferred"}
DEFAULT_SOURCES_TRUST = "inferred"
# Every frontmatter key ``_render_briefing`` computes itself (whether or not
# a given pass actually emits it -- the decision/incident fields are
# conditional on ``record_kind``, but still core-owned). Used to tell apart
# fields the owner layer writes out-of-band (e.g. ``duplicate_of`` via
# decisions_queue._apply_duplicate_link) from fields core must always
# recompute fresh -- see the passthrough loop at the end of the frontmatter
# ``lines`` build in ``_render_briefing``.
CORE_FRONTMATTER_FIELDS = frozenset(
    {
        "type",
        "domain",
        "description",
        "status",
        "created",
        "updated",
        "last_compiled_at",
        "freshness_state",
        "confidence",
        "source_count",
        "last_accessed",
        "relevance",
        "tier",
        "sources_trust",
        "last_verified",
        "enrichment_count",
        "conflicts_open",
        "human_reviewed",
        "human_zone_populated",
        "quality_status",
        "quality_reason",
        "record_kind",
        "decision_status",
        "decision_owner",
        "decision_date",
        "supersedes",
        "superseded_by",
        "incident_date",
        "severity",
    }
)
# ТЗ 4.4: trust is determined by code, never by the model, and is never used
# to pick a conflict winner (only dates/claim kind do that, see
# _effective_conflict_type). The ТЗ states own/integration are strong enough
# for automated consequential actions while forwarded/inferred are not (4.4
# "Правила"), which only fixes a two-tier grouping, not a total order; the
# order within each tier (own > integration, forwarded > inferred) is this
# implementation's own assumption, not stated verbatim in the ТЗ.
TRUST_RANK = {"own": 4, "integration": 3, "forwarded": 2, "inferred": 1}
# ТЗ 4.4 "Правила": the two trust levels strong enough, alone, to justify an
# automatic action with consequences (task creation, CRM edit, silently
# superseding an existing claim). forwarded/inferred must not, alone,
# trigger one -- see _trust_allows_consequential_action.
CONSEQUENTIAL_ACTION_TRUST_LEVELS = {"own", "integration"}
# ТЗ 4.4's table rates "any path under imports/" as integration, but its
# forwarded row separately names "a non-owner reply in a meeting transcript".
# PLAUD (services/plaud.py) is the only imports/ source that is a recording
# of a live conversation rather than a single-author document/page/video,
# and it has no speaker diarization -- an owner's monologue and a meeting
# where someone else does the talking produce the same undifferentiated
# transcript text. Since a claim's speaker can't be verified, this fails
# closed: PLAUD notes are capped at forwarded, never integration, so a
# stranger's line in a meeting recording can't alone win a consequential
# action. Other imports/ sources (documents, web, YouTube) stay integration.
IMPORTS_PLAUD_PREFIX = "imports/plaud/"
# A document the owner forwarded is someone else's file, so it lands under
# its own prefix instead of imports/documents/notes/ and is capped the same
# way as PLAUD: forwarded, never integration. Without this the generic
# imports/ rule below would let a stranger's forwarded file clear
# CONSEQUENTIAL_ACTION_TRUST_LEVELS and supersede the owner's own facts.
IMPORTS_DOCUMENTS_FORWARDED_PREFIX = "imports/documents/forwarded/"
CLAIM_KIND_VALUES = {"fact", "opinion", "commitment"}
CONFLICT_TYPE_VALUES = {"temporal", "factual", "contextual"}
# What the model may answer when it adjudicates one conflict
# (``_adjudicate_conflict``). Each value maps onto an outcome
# ``_apply_claims_and_conflicts`` already knew how to execute back when dates
# and trust picked it instead:
#   new_supersedes  -> temporal, new wins: existing claim moves to Claim History
#   existing_stands -> temporal, existing wins: the new claim is never added
#   both_valid      -> contextual: both stay, context_note explains the split
#   unclear         -> factual: both stay, an Open Conflicts row is written
# ``unclear`` is deliberately the same landing spot the whole conflict path
# used to fail closed into, so "the model could not decide" needs no separate
# recovery branch -- see _adjudicate_conflict.
CONFLICT_OUTCOME_VALUES = {
    "new_supersedes",
    "existing_stands",
    "both_valid",
    "unclear",
}
CONFLICT_OUTCOME_TO_TYPE = {
    "new_supersedes": "temporal",
    "existing_stands": "temporal",
    "both_valid": "contextual",
    "unclear": "factual",
}
ADJUDICATE_TIMEOUT_SECONDS = 180
# Plain-language gloss of each trust level for the adjudication prompt. Trust
# no longer gates the outcome (it used to, via
# _trust_allows_consequential_action); it is now one input the model weighs,
# so it has to arrive as something the model can reason about rather than as
# a bare enum value it has no definition for.
TRUST_LEVEL_EXPLANATIONS = {
    "own": "написано самим владельцем",
    "integration": "загружено интеграцией из документа, веба или видео",
    "forwarded": (
        "переслано владельцем или взято из записи встречи без разметки "
        "говорящих — неизвестно, чьи это слова"
    ),
    "inferred": "происхождение не установлено",
}
ADJUDICATE_JSON_EXAMPLE = (
    "{\n"
    '  "outcome": "new_supersedes",\n'
    '  "context_note": "",\n'
    '  "reason": "новое утверждение датировано позже и описывает тот же '
    'предмет"\n'
    "}\n"
)
# The other half of the queue the owner was being asked to work through:
# a page that hit MAX_ENRICHMENTS_PER_PAGE_PER_MONTH. Hitting that cap is
# only a *suspicion* of drift -- a genuinely busy project page hits it the
# same way a page slowly losing its shape does, and the counter cannot tell
# them apart. So the same treatment as conflicts: the model reads what was
# actually added this month and answers.
DRIFT_JSON_EXAMPLE = (
    "{\n"
    '  "drift": true,\n'
    '  "reason": "страница смешала три разных проекта и потеряла предмет"\n'
    "}\n"
)
# Cap on claims accepted from one model response (ТЗ 5.6 budgets exist for
# pages/candidates/model-calls per pass; claims-per-pass has no listed
# default, so this stays generous but bounded to keep one Verify batch and
# one rendered page a reasonable size).
MAX_CLAIMS_PER_PASS = 20
# ТЗ 5.6: Verify samples 100% of new claims for core/active pages, 25% (min
# 1) for warm pages. Pages outside {core, active} (warm, cold, archive, or an
# unset/unknown tier) fall back to the warm fraction -- fail-closed sampling
# rather than skipping Verify entirely for a tier the ТЗ table does not name.
VERIFY_WARM_SAMPLE_FRACTION = 0.25
VERIFY_TIMEOUT_SECONDS = 180
PROJECT_ROOT_SOURCE_PREFIXES = (
    "src/",
    "scripts/",
    "docs/",
    "deploy/",
    "skills/",
)
PROJECT_ROOT_SOURCE_FILES = {
    "README.md",
    "README.ru.md",
}
HIDDEN_STATE_SOURCE_PREFIXES = {
    "compiled/": ".compiled/",
    "sync/": ".sync/",
}
TIER_RANK = {
    "core": 5,
    "active": 4,
    "warm": 3,
    "cold": 2,
    "archive": 1,
}
# Code review Finding 1: how many drain cycles a ready queue event may be
# passed over by higher-tier arrivals before ``_claim_ready_queue_events``
# forces it to the front regardless of tier. See that function's aging
# comment for the full rationale.
QUEUE_STARVATION_SKIP_LIMIT = 5
QUESTION_DOMAIN_HINTS = {
    "projects": ("проект", "клиент", "лид", "статус", "pipeline"),
    "people": ("человек", "контакт", "клиент", "партнер", "partner", "founder"),
    "topics": ("тема", "исслед", "идея", "направлен", "why", "how"),
    "decisions": ("реш", "договор", "commit", "выбра", "почему"),
    "meetings": ("встреч", "созвон", "call", "meeting", "переговор"),
    "concepts": ("концепц", "метод", "модель", "подход", "framework", "принцип"),
}
QUESTION_CONTEXT_LIMIT = 3
MAX_SOURCE_EXCERPT_CHARS = 8000
MAX_EXISTING_NOTE_CHARS = 10000
MAX_BODY_SNIPPET_CHARS = 2500
MAX_OUTPUT_ARTIFACT_CHARS = 12000
MAX_JSON_REPAIR_CHARS = 12000
IMPACT_TIMEOUT_SECONDS = 360
COMPILE_TIMEOUT_SECONDS = 180
JSON_REPAIR_TIMEOUT_SECONDS = 45
BATCH_CONSOLIDATION_TIMEOUT_SECONDS = 180
DEFAULT_DEBOUNCE_SECONDS = 45
DEFAULT_QUEUE_BATCH_SIZE = 8
DEFAULT_WORKER_IDLE_SECONDS = DEFAULT_DEBOUNCE_SECONDS + 15
DEFAULT_WORKER_POLL_SECONDS = 5.0
DEFAULT_WORKER_STALE_SECONDS = 90
DEFAULT_QUEUE_CLAIM_STALE_SECONDS = 900
QUEUE_WORKER_HISTORY_LIMIT = 10
IMPACT_CATALOG_MAX_ITEMS = 48
IMPACT_CATALOG_MAX_CHARS = 48000
# Resolve stage thresholds (ТЗ 5.2 "Resolve", 5.6 "Параметры по умолчанию").
# Compared against qmd raw-recall `confidence` (already clamped to [0, 1] by
# QmdService._clamp_confidence) -- never `effective_score`, which is not
# guaranteed to stay inside that range (see
# tests/test_qmd_service.py::test_qmd_recall_raw_handles_out_of_range_scores_above_one).
RESOLVE_SAME_PAGE_CONFIDENCE_THRESHOLD = 0.95
RESOLVE_POSSIBLE_DUPLICATE_CONFIDENCE_THRESHOLD = 0.85
RESOLVE_MAX_CANDIDATES_PER_SOURCE = 5
BATCH_CONSOLIDATION_MIN_EVENTS = 2
BATCH_CONSOLIDATION_MAX_EVENTS = 6
BATCH_CONSOLIDATION_EXCERPT_CHARS = 1600
SOURCE_STATE_VERSION = 1
SOURCE_STATE_IGNORED_FRONTMATTER_FIELDS = {
    "last_accessed",
    "relevance",
    "tier",
}
SOURCE_STATE_IGNORED_PATH_PREFIXES = (
    "compiled/",
    ".compiled/",
    ".session/",
)
# Cap on `applied_chunks[source]` length in source-state.json (Resolve
# code-review defect 2). That list only recognizes a repeat of the SAME
# fragment being applied to the SAME page (see `_duplicate_source_chunk`);
# it is not a history log. source-state.json is read and rewritten whole
# under the same lock used to enqueue new messages, so an unbounded list
# makes every page write pay for the full file's growth (measured ~14ms at
# ~900KB vs ~66ms at ~3.5MB). The bound is per (page, source) pair, and one
# source is one daily file: `_daily_source_chunks` splits it per `## HH:MM`
# entry, so an active day is tens of chunks, not the 3-4 a single oversized
# entry gets cut into. Evicting a hash makes that fragment look new again on
# a manual reprocess run, which re-invokes the model over text the page
# already holds -- exactly what this gate exists to prevent -- so the cap is
# set well above any realistic single-day volume rather than tight.
SOURCE_STATE_MAX_APPLIED_CHUNK_HASHES = 200

# A page whose Verify keeps rejecting the same source content must not be
# retried by the nightly freshness backfill forever: every retry costs a
# compile call plus a verify call and eats the run's small candidate budget,
# starving pages that would actually succeed. Bounded the same way a failing
# queue event is (three tries), and keyed on the page's source snapshot, so
# any real change to a source starts the count over.
MAX_VERIFY_REJECTED_RETRIES = 3

# ТЗ 5.6 "Максимум изменяемых страниц за проход": bounds how many distinct
# compiled pages one compile-enrich pass may write via ``_upsert_briefing``,
# so one noisy night cannot rewrite the whole compiled/ tree in one pass.
MAX_PAGES_PER_PASS = 40
# ТЗ 5.6 "Максимум вызовов модели за проход": every model call a pass makes
# (impact, compile, verify, JSON repair) goes through the same budgeted
# choke point (see ``_run_model``), so pass cost stays bounded regardless of
# vault size.
MAX_MODEL_CALLS_PER_PASS = 200
# ТЗ 5.6 "Максимум обогащений одной страницы за календарный месяц": beyond
# this, further source material for the page waits for the owner's decision
# queue instead of compounding unattended drift onto one page.
MAX_ENRICHMENTS_PER_PAGE_PER_MONTH = 20
# ТЗ 5.5 inv 8 / 5.6 "Хранение снимков для отката": how long a pass's
# pre-write snapshots stay on disk before cleanup, so a delayed manual
# rollback stays possible without snapshots accumulating forever.
SNAPSHOT_RETENTION_DAYS = 14

# ТЗ 6 "Забывание и распределение внимания" -- memory tier controls how much
# enrichment attention a page gets per pass, so cost stops growing linearly
# with vault size (see the tier gate in ``_upsert_briefing``).
# ТЗ 5.6 "Значимый сигнал для уровня warm": the cheap half of the check
# (precomputed from the sources table, see ``_warm_recent_source_signal``).
WARM_SIGNAL_WINDOW_DAYS = 7
# Marks a "Sources That Shaped This Page" row added without enrichment (ТЗ
# 6.1: `cold` tier, and a `warm` page whose speculative compile turned out
# insignificant) -- styled like the existing "(not captured)" placeholder so
# it reads the same way in the table. Excluded from the monthly-enrichment
# count (see ``_upsert_briefing``) and from ``enrichment_count`` (see
# ``_record_non_enrichment_source``).
NOT_ENRICHMENT_SOURCE_MARKER = "(not enrichment)"
# ТЗ 6.3 "Сжатие при остывании": how many most-recent Recent Changes items
# stay in place; the rest move to History.
RECENT_CHANGES_KEEP = 5
# ТЗ 6.3: Open Loops older than this move to History marked abandoned.
OPEN_LOOP_ABANDON_DAYS = 60
# ТЗ 6.3 compression is code-only (no model calls) and runs as its own
# nightly-maintenance step (see ``_compress_cooled_pages``), with its own
# small budget so it never competes with the enrichment page budget
# (``MAX_PAGES_PER_PASS``).
MAX_COMPRESSED_PAGES_PER_PASS = 20
# How many still-open conflicts one nightly pass re-adjudicates
# (``_resolve_open_conflicts``). One model call each, so this is a slice of
# MAX_MODEL_CALLS_PER_PASS -- small enough that the retry never crowds out
# the pass's actual enrichment work, large enough to drain a normal backlog
# in a night or two rather than a month.
MAX_CONFLICT_RETRIES_PER_PASS = 20
# How many queued drift suspicions one nightly pass judges
# (``_adjudicate_drift_entries``). One model call each, same slice logic as
# the conflict retry above; drift entries accumulate far more slowly, so
# this is smaller.
MAX_DRIFT_JUDGEMENTS_PER_PASS = 5
# ТЗ 6.4 "Архивация вместо удаления": a page at tier `archive` idle at least
# this many days, with no incoming links, moves to compiled/archive/.
ARCHIVE_TIER_IDLE_DAYS = 180


def _atomic_write_text(path: Path, payload: str) -> None:
    """Write text atomically via tempfile + os.replace to survive crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Byte-exact sibling of ``_atomic_write_text`` for pass snapshots (G4).

    A rollback must restore the exact original bytes of a compiled page, so
    a lossy UTF-8 decode/encode round-trip is not acceptable here the way it
    is for the rest of this module's text handling.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


TOKEN_RE = re.compile(
    r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_-]{2,}"
)
# A leading BOM (\ufeff) is tolerated: e.g. Notepad's "Save As
# UTF-8" on Windows writes one (the owner edits these files by hand).
# Without it, `^` (no re.MULTILINE) never matches and every caller below
# silently treats the whole frontmatter block, including `tier`, as absent.
FRONTMATTER_RE = re.compile(r"^\ufeff?---\n(.*?)\n---\n*", re.DOTALL)
# Sibling of FRONTMATTER_RE used only by _body_without_frontmatter (code
# review defect 2): that function's return value must preserve the original
# bytes past the frontmatter block verbatim, including any CRLF line endings
# inside the owner's human zone -- so, unlike every other FRONTMATTER_RE
# caller here (which normalizes CRLF to LF first purely to make the match
# succeed, then only reads matched group text, never slices the input),
# it cannot match against a normalized copy and slice from that copy. This
# pattern tolerates "\r\n" *and* a bare "\r" (classic Mac line endings --
# frontmatter.py::_detect_newline already recognizes this style, and
# patch_frontmatter_bytes preserves it on point-edits, so a page saved once
# in this format stays in it) directly, so it can match the real,
# un-normalized text and hand back a slice of that same text. The
# alternation order matters: "\r\n" must be tried before the bare "\r"
# branch, or a CRLF input would match only its "\r" half and leave the "\n"
# dangling in the slice.
_FRONTMATTER_BOUNDARY_RE = re.compile(
    r"^\ufeff?---(?:\r\n|\r|\n).*?(?:\r\n|\r|\n)---(?:\r\n|\r|\n)*", re.DOTALL
)
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
DAILY_ENTRY_SPLIT_RE = re.compile(
    r"(?=^##\s+\d{2}:\d{2}\s+\[[^\]]+\]\s*$)",
    re.MULTILINE,
)

# Human-owned zone markers (see HumanZoneMarkerError below): the block they
# wrap survives every recompilation, archival, and link-repair pass verbatim.
HUMAN_ZONE_START = "<!-- human:start -->"
HUMAN_ZONE_END = "<!-- human:end -->"
# Model-authored text that happens to contain one of the markers above --
# quoted from a forwarded message, echoed back from an injection attempt,
# or simply describing this very mechanism -- must not turn into a second
# marker pair on the page. Unlike daily entries, where a marker only counts
# as one when it owns a whole line (storage.escape_embedded_runtime_markup
# defuses those by indenting), the human zone is found with a plain
# substring count, so indenting would change nothing and the substring
# itself has to be broken. Dropping the "--" after the "<!" does that
# while keeping the text readable and its meaning obvious.
#
# The replacement deliberately adds no character of its own: the callers
# below collapse runs of whitespace *before* defusing, and a claim's text
# is later matched byte for byte against the copy the model was shown
# (see _apply_claims_and_conflicts). Inserting a space here would make
# defusing non-idempotent -- the second cleaning pass would collapse the
# doubled space, the strings would stop matching, and the conflict
# pointing at that claim would be silently dropped as stale.
_HUMAN_ZONE_MARKER_LOOKALIKE_RE = re.compile(r"<!--(\s*human:(?:start|end)\s*)-->")
# Model-authored text that starts a line with "## " is the same class of
# forgery one level up: _section_text/_replace_section/_insert_section_before
# find every section of a compiled page with "^##\s+" (MULTILINE), and take
# the *first* match, so a forged heading landing before the real one makes
# the page's own accumulated rows unreachable -- they stop being rendered
# on the next pass. _paragraph needs this because it keeps the newlines a
# forged heading needs. _clean_line/_normalize_list collapse their value
# onto one line, which is almost always written after a "- " or "| " prefix
# and so cannot start a line -- the one exception is next_check, which is
# rendered as a bare line and therefore defuses its own value (see
# _render_briefing). Any new field rendered that way has to do the same.
_EMBEDDED_HEADING_RE = re.compile(r"^(##\s)", re.MULTILINE)


def _defuse_human_zone_markers(text: str) -> str:
    """Break any human-zone marker embedded in model-authored text."""

    return _HUMAN_ZONE_MARKER_LOOKALIKE_RE.sub(r"<!\1-->", text)


def _defuse_embedded_headings(text: str) -> str:
    """Indent any line of model-authored text that reads as a page heading."""

    return _EMBEDDED_HEADING_RE.sub(r" \1", text)


# JSON allows a "\ud800"-style escape with no matching second half, and
# json.loads accepts it: the result is a valid Python str holding an
# unpaired UTF-16 surrogate, which is not encodable as UTF-8 at all.
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _strip_lone_surrogates(value: Any) -> Any:
    """Replace unpaired surrogates anywhere in one model JSON payload.

    Without this, a single such escape in the model's answer reaches
    ``rendered.encode("utf-8")`` inside ``_upsert_briefing`` and raises
    ``UnicodeEncodeError``. That is a ``ValueError``, so the queue drain
    reads it as an ordinary retriable failure: the same answer fails the
    same way three times and the refresh event is then dropped for good --
    the owner's note silently never reaches its compiled page, with nothing
    in the digest and no entry in the decisions queue. Substituting U+FFFD
    is idempotent (U+FFFD is not itself a surrogate), so the cleaned text
    still round-trips through the claim-matching in
    ``_apply_claims_and_conflicts``.
    """

    if isinstance(value, str):
        return _LONE_SURROGATE_RE.sub("�", value)
    if isinstance(value, dict):
        return {
            _strip_lone_surrogates(key): _strip_lone_surrogates(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_strip_lone_surrogates(item) for item in value]
    return value


def _strip_payload_surrogates(payload: dict[str, Any]) -> dict[str, Any]:
    """``_strip_lone_surrogates`` for one whole model JSON payload."""

    return {
        str(_strip_lone_surrogates(key)): _strip_lone_surrogates(value)
        for key, value in payload.items()
    }
# Sentinel span meaning a page's human-zone markers are ambiguous (see
# CompiledBriefingService._human_zone_span): two or more of either marker,
# or a single pair in reversed order. Mirrors add_links.py's
# AMBIGUOUS_HUMAN_ZONE / fix_links.py's _protect_human_zone -- which START
# pairs with which END can't be guessed without risking silent corruption
# of the owner's text.
_AMBIGUOUS_HUMAN_ZONE = (-1, -1)
# First corruption detector for the "zero exact markers" case (see
# _extract_human_zone/_human_zone_span below, and the mirrored copies in
# add_links.py::human_zone_span / fix_links.py::_protect_human_zone): a
# page that genuinely never had a human zone has no "## Owner Notes"
# heading either, since _render_briefing always writes that heading
# together with the zone markers -- real or scaffold -- on every single
# render (see the end of _render_briefing below). So finding the heading
# with zero exact markers is a structural fact, not a guess: this code
# cannot have produced that combination itself, so the markers must have
# been corrupted since -- a typo, a homoglyph, a stray dash, a missing
# colon, extra text inside the comment, whatever it looks like lexically.
# A regex trying to name every way a marker can be mangled is a losing
# race (looser catches more corruption but also more ordinary prose that
# merely mentions a marker; stricter does the opposite) -- checking for the
# heading instead sidesteps that tradeoff entirely, because it does not
# care what the corruption looks like, only whether it happened.
#
# Not sufficient alone, though (code review defect 1): this heading is
# ordinary text living in the page body, so it can be damaged by the exact
# same kind of external edit that damages the markers themselves -- a
# Pandoc round-trip, markdownlint's `{#owner-notes}` autofix, a plain-text
# sanitizer stripping HTML comments -- leaving *both* signals negative at
# once. See _HUMAN_ZONE_POPULATED_RE below for the second, independent
# signal that catches that case.
_OWNER_NOTES_HEADING_RE = re.compile(r"^##\s+Owner Notes\s*$", re.MULTILINE)
# Second, independent corruption signal (code review, defect 1): the heading
# above lives in the page body, so a single external edit that reformats or
# strips prose -- a Pandoc round-trip, markdownlint's `{#owner-notes}`
# autofix, a plain-text sanitizer dropping HTML comments -- can take the
# heading down together with the exact markers, leaving
# ``human_zone_markers_look_corrupted`` with nothing to see. This flag lives
# in the frontmatter instead, a different part of the file maintained by a
# different mechanism (``_render_briefing``'s frontmatter rebuild, never the
# body-text regex editing that damages the heading), so it survives a
# body-only edit. ``_render_briefing`` sets it the first time this page's
# human zone ever holds real, non-scaffold text (see CORE_FRONTMATTER_FIELDS)
# and never clears it again -- once a page has held owner text, losing every
# marker is corruption regardless of what the zone would render as now.
_HUMAN_ZONE_POPULATED_RE = re.compile(
    r"^human_zone_populated:\s*true\s*$", re.MULTILINE
)


def human_zone_markers_look_corrupted(text: str) -> bool:
    """True if ``text`` shows structural evidence of a human zone that zero
    exact markers should not be possible for.

    Only meaningful when the caller has already confirmed zero *exact*
    ``HUMAN_ZONE_START``/``HUMAN_ZONE_END`` matches -- see
    ``_AMBIGUOUS_HUMAN_ZONE`` above for why that case needs a second look
    before it can be treated as "no zone yet". Structural rather than
    lexical: a page this code has ever rendered always carries the ``##
    Owner Notes`` heading together with the markers, and once a page's zone
    has ever held real text ``_render_briefing`` also marks it in the
    frontmatter (see ``_HUMAN_ZONE_POPULATED_RE`` above) -- either signal
    surviving with zero exact markers present means the markers were
    corrupted since, regardless of what that corruption looks like or
    whether it also took the heading down with it. Prose that merely
    mentions marker-like text on a page with neither signal is not
    corruption and is not flagged.

    Both signals below are matched against a copy of ``text`` with every
    newline convention -- "\\r\\n", a bare "\\r" (classic Mac line endings,
    see ``_FRONTMATTER_BOUNDARY_RE`` above for why that style must be
    tolerated at all), and "\\n" -- folded to "\\n": ``_OWNER_NOTES_HEADING_RE``
    and ``_HUMAN_ZONE_POPULATED_RE`` are both ``re.MULTILINE``, and Python's
    ``^``/``$`` in that mode only ever treat "\\n" as a line boundary, so a
    bare-"\\r" page would otherwise trip neither signal and this function
    would report "not corrupted" with nothing actually checked. Safe to fold
    here, unlike in ``_body_without_frontmatter``: this function only
    returns a bool and never slices or returns the normalized copy itself.
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if _OWNER_NOTES_HEADING_RE.search(normalized) is not None:
        return True
    frontmatter_match = FRONTMATTER_RE.match(normalized)
    if frontmatter_match is None:
        return False
    return _HUMAN_ZONE_POPULATED_RE.search(frontmatter_match.group(1)) is not None


# Duplicate-of-human-zone filter (see _text_duplicates_human_zone): a
# candidate line is dropped once this fraction of its tokens also appears in
# the human zone. Below HUMAN_ZONE_DUPLICATE_MIN_TOKENS tokens the ratio is
# meaningless -- one shared word ("SLA") would already hit 100% overlap on a
# one-token line -- so the filter does not apply at all under that length.
HUMAN_ZONE_DUPLICATE_OVERLAP_THRESHOLD = 0.8
HUMAN_ZONE_DUPLICATE_MIN_TOKENS = 5
# Strict YYYY-MM-DD check for the last_verified/human_reviewed frontmatter
# fields (see _validated_date_field). Those two fields render without
# json.dumps escaping, so a stray value containing a colon would otherwise
# produce invalid YAML that fails validation on every future write.
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# One rendered "Sources That Shaped This Page" row: "| date | [[source]] | what |".
SOURCES_SHAPED_TABLE_ROW_RE = re.compile(
    r"^\|\s*(\S+)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*$"
)
# One rendered "Claim History" row:
# "| date | [[old source]] | claim | [[new source]] |".
CLAIM_HISTORY_ROW_RE = re.compile(
    r"^\|\s*(\S+)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*$"
)
# One rendered "Open Conflicts" row:
# "| date | existing claim | [[existing source]] | new claim | [[new source]] |".
OPEN_CONFLICTS_ROW_RE = re.compile(
    r"^\|\s*(\S+)\s*\|\s*(.*?)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*\[\[([^\]]+)\]\]\s*\|\s*$"
)
# One rendered "Recent Changes" / "Open Loops" / "History" dated bullet:
# "- YYYY-MM-DD: text (source: [[source]])", or without the trailing
# "(source: ...)" clause when no source is known (ТЗ 6.3 accumulation, see
# ``_dated_rows``/``_render_dated_bullets``). The source group is optional so
# a bullet written before this format existed still parses (as a row with no
# known source) instead of being silently dropped.
DATED_BULLET_ROW_RE = re.compile(
    r"^-\s+(\d{4}-\d{2}-\d{2}):\s+(.*?)(?:\s+\(source:\s*\[\[([^\]]+)\]\]\))?\s*$"
)
# Entry-type marker convention (see vault/.claude/rules/daily-format.md and
# bot/handlers/forward.py:119): the SOURCE trust rule (ТЗ 4.4) reads this off
# the excerpt's own entry headers (see _source_trust_level), not off the model
# output. DAILY_ENTRY_MARK_RE matches any entry header so that an unknown
# entry type is recognized as an entry and rated `inferred`, instead of being
# skipped and letting a neighbouring entry speak for it.
DAILY_ENTRY_MARK_RE = re.compile(r"^##\s+\d{1,2}:\d{2}\s+\[")
FORWARD_MARK_RE = re.compile(r"^##\s+\d{1,2}:\d{2}\s+\[forward from:", re.IGNORECASE)
OWN_ENTRY_MARK_RE = re.compile(
    r"^##\s+\d{1,2}:\d{2}\s+\[(?:voice|text|photo|document)\]", re.IGNORECASE
)
IMPACT_JSON_EXAMPLE = (
    "{\n"
    '  "source_shape": "single|mixed|noisy",\n'
    '  "durable_threads": [\n'
    '    {"label": "short thread label", "why": "why this thread is durable"}\n'
    "  ],\n"
    '  "updates": [\n'
    "    {\n"
    '      "domain": "projects|people|topics|decisions|meetings|concepts",\n'
    '      "title": "brief title",\n'
    '      "slug": "brief-slug",\n'
    '      "description": "one-line search snippet",\n'
    '      "reason": "why this briefing should refresh",\n'
    '      "existing_path": "compiled/<domain>/<slug>.md or empty"\n'
    "    }\n"
    "  ]\n"
    "}"
)
COMPILE_JSON_EXAMPLE = (
    "{\n"
    '  "description": "one-line snippet",\n'
    '  "status": "active|draft|pending|done|inactive",\n'
    '  "freshness_state": "fresh|watch|stale",\n'
    '  "confidence": "high|medium|low",\n'
    '  "current_state": "short paragraph",\n'
    '  "recent_changes": ["bullet", "..."],\n'
    '  "open_loops": ["bullet", "..."],\n'
    '  "key_decisions": ["bullet", "..."],\n'
    '  "record_kind": "decision|incident|briefing",\n'
    '  "decision_status": "proposed|accepted|rejected|superseded",\n'
    '  "decision_owner": "owner or empty",\n'
    '  "decision_date": "YYYY-MM-DD or empty",\n'
    '  "rationale": "decision rationale or empty",\n'
    '  "alternatives_considered": ["bullet", "..."],\n'
    '  "supersedes": ["decision identifier", "..."],\n'
    '  "superseded_by": "decision identifier or empty",\n'
    '  "decision_evidence": ["vault/relative/path.md", "..."],\n'
    '  "incident_date": "YYYY-MM-DD or empty",\n'
    '  "severity": "low|medium|high|critical or empty",\n'
    '  "timeline": ["timestamped event", "..."],\n'
    '  "root_cause": "root cause or empty",\n'
    '  "what_worked": ["bullet", "..."],\n'
    '  "what_did_not_work": ["bullet", "..."],\n'
    '  "corrective_actions": ["bullet", "..."],\n'
    '  "generalizable_learning": "reusable learning or empty",\n'
    '  "next_check": "short line",\n'
    '  "source_links": ["vault/relative/path.md", "..."],\n'
    '  "claims": [\n'
    "    {\n"
    '      "text": "utterance to record as a durable claim",\n'
    '      "kind": "fact|opinion|commitment"\n'
    "    }\n"
    "  ],\n"
    '  "conflicts": [\n'
    "    {\n"
    '      "existing_claim": "text already on this page",\n'
    '      "existing_source": "vault/relative/path.md of the existing claim",\n'
    '      "new_claim": "text from claims above, verbatim",\n'
    '      "type": "temporal|factual|contextual",\n'
    '      "context_note": "for type=contextual, how the two claims\' '
    'scopes differ, or empty"\n'
    "    }\n"
    "  ]\n"
    "}"
)
# Verify step (ТЗ 5.2 step 4, 5.3): one batched, read-only, clean-context
# call per page checking whether each sampled claim follows from the source
# excerpt alone. Deliberately omits the ТЗ 5.3 example's "verification"
# field (environment_dependent/judgment split): invariant 13 asks this step
# to run without network access, but that boundary is not technically
# enforced here -- the model runs with permission checks disabled
# (``cli_runner.py``'s ``--dangerously-skip-permissions``) and is only ever
# told, in the prompt text, not to use anything outside the excerpt. Given
# that, every sampled claim is judged the same way (entailment from the
# given excerpt) and the split has nothing to select between here.
VERIFY_JSON_EXAMPLE = (
    "{\n"
    '  "verdicts": [\n'
    "    {\n"
    '      "index": 0,\n'
    '      "text": "claim text, echoed back exactly",\n'
    '      "supported": true,\n'
    '      "reason": "short justification"\n'
    "    }\n"
    "  ],\n"
    '  "page_checks": {\n'
    '    "source_coverage": true,\n'
    '    "target_scope": true,\n'
    '    "timeline_consistency": true\n'
    "  },\n"
    '  "page_issues": ["blocking issue", "..."]\n'
    "}"
)
VERIFY_PAGE_CHECK_KEYS = (
    "source_coverage",
    "target_scope",
    "timeline_consistency",
)
BATCH_CONSOLIDATION_JSON_EXAMPLE = (
    "{\n"
    '  "headline": "short title",\n'
    '  "summary": "2-4 sentence synthesis",\n'
    '  "themes": ["bullet", "..."],\n'
    '  "follow_ups": ["bullet", "..."]\n'
    "}"
)


@dataclass(frozen=True, slots=True)
class CompiledBriefingTarget:
    """Resolved impacted briefing target."""

    domain: str
    title: str
    slug: str
    description: str
    reason: str
    existing_path: str = ""


@dataclass(frozen=True, slots=True)
class CompiledBriefingCandidate:
    """One existing compiled note candidate for routing and ranking."""

    rel_path: str
    domain: str
    slug: str
    title: str
    description: str
    freshness_state: str
    confidence: str
    relevance: float
    tier: str
    text: str


@dataclass(frozen=True, slots=True)
class CompiledBatchConsolidationEvent:
    """One successful queue event eligible for a cross-source consolidation note."""

    source_rel_path: str
    source_excerpt: str
    updated_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticResolveResult:
    """Full outcome of Resolve stage 2 (ТЗ 5.2), including the
    possible-duplicate signal that ``_semantic_resolve_target`` itself only
    logs and discards.

    ``target`` is set only for a confident same-page match (mirrors
    ``_semantic_resolve_target``'s old return value exactly). ``target`` and
    ``duplicate_candidate`` are never both set: a confident match replaces
    the target outright, while a possible-duplicate match leaves the
    original target in place (a new page still gets created) and only flags
    ``duplicate_candidate`` for the caller to queue as an owner decision.
    """

    target: CompiledBriefingTarget | None
    duplicate_candidate: str = ""
    duplicate_confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class BriefingUpsertResult:
    """Where ``_upsert_briefing`` landed and whether it actually wrote bytes.

    ``written=False`` usually means the source fragment had already been
    applied to this exact page (idempotent duplicate skip, see
    ``_duplicate_source_chunk``) -- the model was not called and the file was
    not touched. Callers must not treat a skip as an update: it must not
    enter the ``updated`` list, consolidation events, or backfill/report
    counters (see ``refresh_after_write``, ``_drain_queue_once``,
    ``_backfill_freshness_notes``).

    ``written=False`` can also mean the page's ``<!-- human:start/end -->``
    zone is ambiguous, so ``_record_non_enrichment_source`` failed closed
    without touching the page (see its docstring). ``requeueable=True``
    marks that specific case *only* when it fired before any model call was
    spent this attempt (the ``cold``-tier path in ``_upsert_briefing``,
    never the `warm`-tier "insignificant" path, which reaches the same
    check after compiling) -- ``_drain_queue_once`` uses it to put the
    queue event back instead of permanently acking it, since retrying costs
    nothing here (code review, defect 2).
    """

    path: str
    written: bool
    requeueable: bool = False


@dataclass
class CompileEnrichPass:
    """Mutable per-pass bookkeeping for one compile-enrich run (G0-G6).

    Unlike the frozen dataclasses above, every field here is mutated in
    place while ``run_nightly_maintenance`` runs -- budget counters and the
    touched-page/source collections grow as the pass processes queue events
    and backfill candidates -- and the whole object is read once at the end
    to write the ``.session/compile-enrich.json`` journal (ТЗ 5.2 step 6).
    """

    pass_id: str
    snapshot_enabled: bool
    model_calls_used: int = 0
    touched_pages: set[str] = field(default_factory=set)
    verify_rejected: int = 0
    trust_blocked: int = 0
    # Conflicts the nightly retry (``_resolve_open_conflicts``) settled this
    # pass -- the counterpart of ``trust_blocked`` above, which counts the
    # ones the write path could not settle in the first place. Journalled so
    # a night that only repaired old conflicts still shows work done.
    conflicts_auto_resolved: int = 0
    budget_exhausted: set[str] = field(default_factory=set)
    sources_processed: list[str] = field(default_factory=list)
    snapshot_manifest: dict[str, Any] = field(default_factory=dict)
    # ТЗ 5.5 inv 5, second condition: pages whose only touch this pass was
    # a pure archive->warm tier bump (``_promote_archive_tier``). That write
    # never touches the sources table, so it must not itself trigger the
    # "changed a page with no source link at all" effectiveness-gate check
    # -- see ``run_nightly_maintenance``.
    archive_promoted_pages: set[str] = field(default_factory=set)
    # Sources whose give-up trace this pass earned the right to clear
    # (``_clear_dropped_queue_source``), each paired with the compiled pages
    # that source's drain actually wrote. Held until the pass survives the
    # ТЗ 5.5 inv 5 effectiveness gate: a rollback that puts those pages back
    # the way they were also puts the source back to "never compiled", so a
    # clear applied mid-pass would have told the owner a source was handled
    # by a write that no longer exists.
    #
    # The pages are recorded, rather than just the source name, because the
    # rollback is not all-or-nothing (code review). It restores only pages
    # whose fingerprint still matches what this pass left (see
    # ``rollback_compile_enrich_pass``), and it restores nothing at all when
    # the gate fires on a pass that wrote no page in the first place -- and
    # a source can reach a conclusion without any page write, when the
    # impact stage decides it affects none. Treating "a rollback happened"
    # as "every clear is void" discarded those too, leaving the owner asked
    # forever to re-save a note the pass had already run to a conclusion.
    dropped_sources_cleared: list[tuple[str, tuple[str, ...]]] = field(
        default_factory=list
    )
    # Code review (ТЗ 7.2 "факт вытеснения попадает в дайджест"): total
    # decisions-queue entries this pass evicted, across every
    # ``_queue_*`` producer call (``_record_queue_eviction``). Mirrors
    # ``budget_exhausted`` -- written to the pass journal and translated to
    # one owner-facing digest line by ``compiled_enrich_report.py``.
    queue_evictions: int = 0
    # Code review Finding 3: how many ``_verify_claims_batch`` calls this
    # pass saw a non-empty but entirely unmatched-by-index ``verdicts``
    # response -- Verify format drift, not an honest content rejection.
    # Kept separate from ``verify_rejected`` so the pass journal (and, from
    # it, the owner digest) can tell the two apart instead of a format
    # drift silently reading as a mass content rejection on every page.
    verify_format_drift: int = 0
    # Code review (owner-visibility gap): pages this pass skipped because
    # ``_human_zone_span``/``_extract_human_zone`` found the page's
    # <!-- human:start/end --> markers ambiguous (see
    # ``_record_human_zone_ambiguous``). Mirrors ``queue_evictions`` --
    # written to the pass journal and translated to one owner-facing digest
    # line by ``compiled_enrich_report.py``, since a skipped `cold`-tier
    # page never calls the model again on its own and would otherwise stay
    # silently stuck until the owner happens to notice the broken marker.
    human_zone_ambiguous_pages: set[str] = field(default_factory=set)


class CompiledBriefingService:
    """Maintain a derived compiled layer above raw vault notes."""

    def __init__(
        self,
        vault_path: Path,
        content_language: str = "ru",
        ai_cli: str | None = None,
    ) -> None:
        self.vault_path = Path(vault_path).resolve()
        self.compiled_root = self.vault_path / "compiled"
        self.state_root = self.vault_path / ".compiled"
        self.queue_path = self.state_root / "queue.json"
        # One lock for every state file this service rewrites in place --
        # see ``_state_lock``. Still the file the queue lock has always
        # used, so a worker started before this change keeps excluding one
        # started after it.
        self.state_lock_path = self.state_root / "queue.lock"
        self._state_lock_depth = threading.local()
        self.source_state_path = self.state_root / "source-state.json"
        self.launcher_lock_path = self.state_root / "launcher.lock"
        self.worker_lock_path = self.state_root / "worker.lock"
        self.worker_state_path = self.state_root / "worker-state.json"
        self.queue_worker_history_root = self.state_root / "queue-history"
        self._active_queue_worker_journal: dict[str, Any] | None = None
        self._active_queue_worker_journal_path: Path | None = None
        self.answers_root = self.vault_path / "summaries" / "answers"
        self.content_language = normalize_language(content_language)
        self.ai_cli = self._resolve_ai_cli(ai_cli)
        self.runner = CliRunner(self.vault_path, self.ai_cli)
        self.qmd = QmdService(self.vault_path)
        # Set only by run_nightly_maintenance for the duration of one pass
        # (G0/G5); every budget check and counter below is a no-op with
        # this left at None, so a manual single refresh stays unbudgeted.
        self._active_pass: CompileEnrichPass | None = None

    def enqueue_refresh(
        self,
        *,
        source_path: str | Path,
        source_excerpt: str = "",
        max_updates: int = 3,
        debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
    ) -> dict[str, Any]:
        """Queue one refresh event and debounce by source path."""

        source_rel_path = self._normalize_rel_path(source_path)
        if not source_rel_path or source_rel_path.startswith("compiled/"):
            return {"queued": False, "errors": ["unsupported-path"]}

        self._ensure_state_dirs()
        now = datetime.now().astimezone()
        due_at = now.timestamp() + max(0, debounce_seconds)
        clipped_excerpt = self._clip(source_excerpt, MAX_SOURCE_EXCERPT_CHARS)

        def mutate_queue() -> None:
            queue = self._load_queue()
            updated = False
            for event in queue:
                if str(event.get("source_path") or "") != source_rel_path:
                    continue
                event["source_excerpt"] = clipped_excerpt
                event["due_at"] = due_at
                # A fresh write always re-arms a plain debounce wait, even
                # if the previous attempt at this same source path had
                # been released as a retry backoff (code review defect 1).
                event["backoff"] = False
                event["max_updates"] = max_updates
                event["last_enqueued_at"] = now.isoformat()
                event["state"] = "pending"
                event["claim_token"] = ""
                event["claimed_at"] = ""
                event["claimed_pid"] = 0
                updated = True
                break
            if not updated:
                queue.append(
                    {
                        "source_path": source_rel_path,
                        "source_excerpt": clipped_excerpt,
                        "enqueued_at": now.isoformat(),
                        "last_enqueued_at": now.isoformat(),
                        "due_at": due_at,
                        "attempts": 0,
                        "max_updates": max_updates,
                        "state": "pending",
                        "claim_token": "",
                        "claimed_at": "",
                        "claimed_pid": 0,
                    }
                )
            self._save_queue(queue)

        self._with_queue_lock(mutate_queue)
        return {"queued": True, "errors": []}

    def spawn_background_drain(self) -> bool:
        """Start one detached queue-drain worker unless one is already alive."""

        if not self.is_available():
            return False
        self._ensure_state_dirs()
        with self._launcher_lock(blocking=False) as acquired:
            if not acquired:
                return False

            state = self._load_worker_state()
            if self._worker_state_is_live(state):
                return False
            if state:
                self._clear_worker_state_unlocked()

            command = [
                sys.executable,
                "-m",
                "d_brain.run_compiled_maintenance",
                "--queue-only",
            ]
            try:
                process = subprocess.Popen(  # noqa: S603
                    command,
                    cwd=self.vault_path.parent,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
                )
            except Exception as exc:
                logger.warning("Failed to spawn compiled maintenance worker: %s", exc)
                return False

            now = datetime.now().astimezone().isoformat()
            self._save_worker_state(
                {
                    "pid": process.pid,
                    "status": "starting",
                    "started_at": now,
                    "heartbeat_at": now,
                }
            )
            return True

    def drain_queue(
        self,
        *,
        force: bool = False,
        max_events: int = DEFAULT_QUEUE_BATCH_SIZE,
        refresh_qmd: bool = True,
    ) -> dict[str, Any]:
        """Drain queued refresh events through the compiled compiler."""

        if not self.is_available():
            return {
                "drained": 0,
                "updated": [],
                "consolidations": [],
                "errors": ["ai-cli-unavailable"],
            }

        self._ensure_state_dirs()
        with self._worker_lock(blocking=False) as acquired:
            if not acquired:
                return {
                    "drained": 0,
                    "updated": [],
                    "consolidations": [],
                    "errors": ["worker-busy"],
                }

            result = self._drain_queue_once(force=force, max_events=max_events)
            if result["updated"] and refresh_qmd:
                self._refresh_qmd_index()
            return result

    def run_queue_worker(
        self,
        *,
        force: bool = False,
        max_events: int = DEFAULT_QUEUE_BATCH_SIZE,
        refresh_qmd: bool = True,
        idle_seconds: int = DEFAULT_WORKER_IDLE_SECONDS,
        poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS,
    ) -> dict[str, Any]:
        """Run one short-lived burst worker that absorbs nearby queued writes."""

        if not self.is_available():
            return {
                "drained": 0,
                "updated": [],
                "consolidations": [],
                "errors": ["ai-cli-unavailable"],
            }

        self._ensure_state_dirs()
        with self._worker_lock(blocking=False) as acquired:
            if not acquired:
                return {
                    "drained": 0,
                    "updated": [],
                    "consolidations": [],
                    "errors": ["worker-busy"],
                }

            worker_pid = os.getpid()
            started_at = datetime.now().astimezone().isoformat()
            total_drained = 0
            updated_paths: list[str] = []
            consolidation_paths: list[str] = []
            errors: list[str] = []
            idle_deadline: float | None = None
            # Code review defect 1 (part 2): `force` must still reach the
            # *first* `_drain_queue_once` call unconditionally (a fresh
            # manual `--force` retry has to work right away). But once a
            # poll tick finds nothing left to force through immediately
            # (`force_ready` below goes False), later calls in this same
            # `while` must fall back to honoring `due_at` like an unforced
            # call would -- otherwise a retry-backoff event (released with
            # `due_at` 300s out, see `_release_claimed_queue_event`) gets
            # reclaimed, and its error budget burned, on every poll tick
            # instead of after its backoff window.
            loop_force = force

            try:
                initial_queue_size = len(self._with_queue_lock(self._load_queue))
                self._start_queue_worker_journal(
                    pid=worker_pid,
                    started_at=started_at,
                    force=force,
                    max_events=max_events,
                    refresh_qmd=refresh_qmd,
                    initial_queue_size=initial_queue_size,
                )
                self._write_worker_state(
                    pid=worker_pid,
                    status="running",
                    started_at=started_at,
                )
                while True:
                    self._touch_worker_state(worker_pid)
                    batch = self._drain_queue_once(
                        force=loop_force,
                        max_events=max_events,
                    )
                    total_drained += int(batch.get("drained") or 0)
                    updated_paths.extend(
                        str(path) for path in batch.get("updated", [])
                    )
                    consolidation_paths.extend(
                        str(path) for path in batch.get("consolidations", [])
                    )
                    errors.extend(str(item) for item in batch.get("errors", []))

                    queue = self._with_queue_lock(self._load_queue)
                    # Only a "pending" event can be claimed by the next
                    # ``_drain_queue_once`` call. An event still "in_flight"
                    # (a claim left behind by a killed worker whose pid got
                    # reused, released only by the 15-minute stale-claim
                    # recovery) keeps its old, already-past ``due_at`` -- so
                    # counting it here made the loop below read the queue as
                    # ready on every iteration: ``idle_deadline`` was reset,
                    # no sleep ran, and ``while True`` had no exit condition
                    # at all until the claim finally went stale.
                    claimable = [
                        event
                        for event in queue
                        if str(event.get("state") or "pending") == "pending"
                    ]
                    if claimable:
                        now_ts = datetime.now().astimezone().timestamp()
                        next_due_at = min(
                            float(event.get("due_at") or 0) for event in claimable
                        )
                        # Code review defect 1: a single `_drain_queue_once`
                        # call always honors `force` unconditionally (a
                        # fresh manual `--force` retry must still work right
                        # away, e.g. once an owner fixes an ambiguous
                        # human-zone marker -- see
                        # `_claim_ready_queue_events`). But *this* `while`
                        # loop must not treat `force` as a reason to
                        # immediately re-drain again within the same run
                        # when the only pending work is a retry backoff (an
                        # event released without incrementing `attempts`,
                        # see the `requeueable` caller in
                        # `_drain_queue_once`) -- doing so reclaimed the
                        # same event every iteration with no exit
                        # condition. Fall through to the same wait/poll
                        # path used without `force` instead. Computing
                        # `force_ready` alone was not enough: it only
                        # decided whether to skip *this* iteration's sleep,
                        # while the *next* iteration's `_drain_queue_once`
                        # call still passed the original `force` (see
                        # `loop_force` above) and reclaimed the same
                        # backed-off event again regardless. Once nothing
                        # is left to force through, latch `loop_force` to
                        # `False` for the rest of this run.
                        force_ready = loop_force and any(
                            not event.get("backoff") for event in claimable
                        )
                        if not force_ready:
                            loop_force = False
                        if force_ready or next_due_at <= now_ts:
                            idle_deadline = None
                            continue
                        if idle_deadline is None:
                            idle_deadline = time.monotonic() + max(0, idle_seconds)
                        remaining_idle = idle_deadline - time.monotonic()
                        if remaining_idle <= 0:
                            break
                        sleep_for = min(
                            max(0.1, poll_seconds),
                            remaining_idle,
                            max(0.1, next_due_at - now_ts),
                        )
                        time.sleep(sleep_for)
                        continue

                    if idle_deadline is None:
                        idle_deadline = time.monotonic() + max(0, idle_seconds)
                    remaining_idle = idle_deadline - time.monotonic()
                    if remaining_idle <= 0:
                        break
                    time.sleep(min(max(0.1, poll_seconds), remaining_idle))

                if updated_paths and refresh_qmd:
                    self._refresh_qmd_index()
                self._finish_queue_worker_journal(
                    status="completed",
                    remaining_queue_size=len(
                        self._with_queue_lock(self._load_queue)
                    ),
                    totals={
                        "drained": total_drained,
                        "updated": len(updated_paths),
                        "consolidations": len(consolidation_paths),
                        "errors": len(errors),
                    },
                )
                return {
                    "drained": total_drained,
                    "updated": updated_paths,
                    "consolidations": consolidation_paths,
                    "errors": errors,
                }
            except Exception as exc:
                self._finish_queue_worker_journal(
                    status="crashed",
                    remaining_queue_size=len(self._with_queue_lock(self._load_queue)),
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
                # Resilience review defect 1: this loop had no top-level
                # handler at all. ``spawn_background_drain`` runs this worker
                # as a detached subprocess with stdout/stderr sent to
                # DEVNULL, so an unexpected crash here (e.g. ``OSError`` from
                # a full disk or a read-only filesystem) previously left no
                # trace an owner could find -- only silence where compiled
                # updates used to appear. Any event still claimed mid-batch
                # is already released by ``_drain_queue_once`` before this is
                # reached; this only records the owner-visible trace, in the
                # same ``.session/`` journal convention every other pass in
                # this file already uses, then re-raises so the crash still
                # surfaces loudly to any caller watching stderr/exit code.
                self._write_queue_worker_crash_journal(
                    pid=worker_pid, started_at=started_at, exc=exc
                )
                raise
            finally:
                self._clear_worker_state(pid=worker_pid)

    def _drain_queue_once(
        self,
        *,
        force: bool,
        max_events: int,
    ) -> dict[str, Any]:
        """Drain one ready queue batch without taking the outer worker lock."""

        selected = self._claim_ready_queue_events(
            force=force,
            max_events=max_events,
        )
        if not selected:
            return {
                "drained": 0,
                "updated": [],
                "errors": [],
                "consolidations": [],
            }

        updated_paths: list[str] = []
        errors: list[str] = []
        consolidation_events: list[CompiledBatchConsolidationEvent] = []
        now_ts = datetime.now().astimezone().timestamp()
        processed = 0
        for index, event in enumerate(selected):
            try:
                result = self.refresh_after_write(
                    source_path=str(event.get("source_path") or ""),
                    source_excerpt=str(event.get("source_excerpt") or ""),
                    max_updates=int(event.get("max_updates") or 3),
                )
            except Exception:
                # Resilience review defect 1: an exception ``refresh_after_write``
                # does not already fail closed on (e.g. ``OSError`` from a full
                # disk or a read-only filesystem, unlike the ``CliExecutionError``/
                # ``FileNotFoundError``/``TimeoutError``/``ValueError`` family it
                # already catches internally) used to unwind straight out of
                # this loop, leaving this event -- and every event still
                # waiting behind it in this claimed batch -- stuck "in_flight"
                # forever from the queue's point of view. Release the whole
                # remaining batch back to "pending" with the same no-penalty
                # bookkeeping as the budget_exhausted branch below, then keep
                # propagating so run_queue_worker's own handler can log it.
                for remaining_event in selected[index:]:
                    self._record_queue_worker_event(
                        remaining_event,
                        outcome="released_after_crash",
                        updated=[],
                        errors=[],
                    )
                    self._release_claimed_queue_event(
                        remaining_event,
                        attempts=int(remaining_event.get("attempts") or 0),
                        due_at=now_ts,
                    )
                raise
            updated_paths.extend(str(path) for path in result.get("updated", []))
            if result.get("budget_exhausted"):
                # ТЗ 5.5 inv 7 / G3: budget exhaustion is a normal
                # pass-ending signal, not a failed attempt -- release this
                # event *and every event still waiting behind it in this
                # claimed batch* with their attempts counts unchanged (no
                # penalty), then stop the batch. The whole batch was
                # claimed ("in_flight") up front by ``_claim_ready_queue_events``;
                # releasing only the current event would leave the rest
                # claimed forever in queue.json, invisible to the next
                # pass (which only looks at "pending" events) until the
                # 15-minute stale-claim recovery kicks in. None of the
                # released events count toward ``processed`` below -- they
                # were not actually handled this pass.
                for remaining_event in selected[index:]:
                    self._record_queue_worker_event(
                        remaining_event,
                        outcome="deferred_budget_exhausted",
                        updated=[],
                        errors=[],
                    )
                    # Released with the same backoff as the `requeueable`
                    # branch below, not as immediately due: the monthly
                    # per-page enrichment budget is checked outside a pass
                    # too (``_upsert_briefing``, where ``_active_pass`` is
                    # None), so ``run_queue_worker``'s loop would otherwise
                    # reclaim this event on the very next iteration, spend
                    # another Impact model call resolving its targets, hit
                    # the same month-long budget again -- and never exit.
                    # ``attempts`` still stays unchanged: exhausting a budget
                    # is a normal pass-ending signal, not a failed attempt.
                    self._release_claimed_queue_event(
                        remaining_event,
                        attempts=int(remaining_event.get("attempts") or 0),
                        due_at=now_ts + 300,
                        backoff=True,
                    )
                break
            processed += 1
            event_updated = [
                str(path).strip()
                for path in result.get("updated", [])
                if str(path).strip()
            ]
            event_errors = [
                str(item) for item in result.get("errors", []) if str(item)
            ]
            if not event_errors:
                source_rel_path = str(event.get("source_path") or "").strip()
                source_excerpt = self._source_excerpt(
                    source_rel_path,
                    str(event.get("source_excerpt") or ""),
                )
                if source_rel_path and source_excerpt and event_updated:
                    consolidation_events.append(
                        CompiledBatchConsolidationEvent(
                            source_rel_path=source_rel_path,
                            source_excerpt=self._clip(
                                source_excerpt,
                                BATCH_CONSOLIDATION_EXCERPT_CHARS,
                            ),
                            updated_paths=tuple(event_updated),
                        )
                    )
                if result.get("requeueable"):
                    # Code review defect 2: a cold-tier page's ambiguous
                    # human-zone markers made _upsert_briefing fail closed
                    # without raising (see _record_non_enrichment_source) --
                    # `errors` is empty, the same shape as "already
                    # applied", so acking here like the normal case would
                    # permanently drop the only trigger left to reapply this
                    # source once the owner fixes the marker. Release
                    # instead, with the same backoff as a retriable error
                    # below so the near-real-time worker (run_queue_worker,
                    # whose loop re-drains immediately once an event is due)
                    # cannot hot-loop re-claiming it every iteration, and
                    # with `attempts` left unchanged -- like the budget-
                    # exhaustion release above, this is not a failed attempt
                    # to penalize. Only ever set when a target reached this
                    # for free (no model call spent -- see
                    # BriefingUpsertResult.requeueable), so this retry never
                    # burns the per-pass model-call budget.
                    self._record_queue_worker_event(
                        event,
                        outcome="deferred_requeueable",
                        updated=event_updated,
                        errors=[],
                    )
                    self._release_claimed_queue_event(
                        event,
                        attempts=int(event.get("attempts") or 0),
                        due_at=now_ts + 300,
                        backoff=True,
                    )
                else:
                    self._record_queue_worker_event(
                        event,
                        outcome="updated" if event_updated else "no_changes",
                        updated=event_updated,
                        errors=[],
                    )
                    self._ack_claimed_queue_event(event)
                    # This source made it all the way through, so any earlier
                    # "gave up on it" trace is stale -- see
                    # ``_record_dropped_queue_source``. The pages it wrote
                    # (possibly none, when the impact stage decided it
                    # affects none) go with it so a later rollback can tell
                    # whether it undid this particular conclusion.
                    self._clear_dropped_queue_source(
                        source_rel_path, tuple(event_updated)
                    )
                continue

            retriable = event_errors not in (
                ["ai-cli-unavailable"],
                ["empty-source"],
                ["unsupported-path"],
            )
            if retriable:
                attempts = int(event.get("attempts") or 0) + 1
                if attempts < 3:
                    self._record_queue_worker_event(
                        event,
                        outcome="retry_scheduled",
                        updated=event_updated,
                        errors=event_errors,
                        attempts=attempts,
                    )
                    self._release_claimed_queue_event(
                        event,
                        attempts=attempts,
                        due_at=now_ts + 300,
                        backoff=True,
                    )
                else:
                    self._record_queue_worker_event(
                        event,
                        outcome="dropped",
                        updated=event_updated,
                        errors=event_errors,
                        attempts=attempts,
                    )
                    for page_rel_path in result.get("verify_rejected", []):
                        self._queue_verify_rejected(
                            page_rel_path=str(page_rel_path),
                            source_rel_path=str(event.get("source_path") or ""),
                            source_excerpt=str(event.get("source_excerpt") or ""),
                            max_updates=int(event.get("max_updates") or 3),
                        )
                    self._record_dropped_queue_source(
                        source_rel_path=str(event.get("source_path") or ""),
                        errors=event_errors,
                        attempts=attempts,
                    )
                    self._ack_claimed_queue_event(event)
                errors.extend(event_errors)
                continue

            errors.extend(event_errors)
            # Not retriable: acking here is final too, so it needs the same
            # trace as the exhausted-attempts branch above. ``empty-source``
            # and ``unsupported-path`` are excluded -- those describe a
            # source that was never eligible for a compiled page in the
            # first place, not one that failed to reach it.
            if event_errors not in (["empty-source"], ["unsupported-path"]):
                self._record_dropped_queue_source(
                    source_rel_path=str(event.get("source_path") or ""),
                    errors=event_errors,
                    attempts=int(event.get("attempts") or 0) + 1,
                )
            self._record_queue_worker_event(
                event,
                outcome="rejected",
                updated=event_updated,
                errors=event_errors,
                attempts=int(event.get("attempts") or 0) + 1,
            )
            self._ack_claimed_queue_event(event)

        consolidation_paths: list[str] = []
        consolidation_path = self._write_batch_consolidation(consolidation_events)
        if consolidation_path:
            consolidation_paths.append(consolidation_path)

        return {
            "drained": processed,
            "updated": updated_paths,
            "errors": errors,
            "consolidations": consolidation_paths,
        }

    def run_nightly_maintenance(
        self,
        *,
        backfill_limit: int = 5,
    ) -> dict[str, Any]:
        """Run queued refresh, lint, and bounded source-aware backfill.

        Wraps the whole run in one compile-enrich pass (ТЗ 5.2 step 6 / G5):
        a pass id is minted, ``self._active_pass`` is set for the duration
        so budgets (G1/G2) and snapshots (G4) apply, and the ТЗ 5.5 inv 5
        effectiveness gate runs after the body -- a pass that had queue work
        but changed zero pages is rolled back and reported as failed rather
        than silently looking like a normal empty run, *unless* the reason
        nothing changed is that a budget was exhausted (ТЗ 5.5 inv 7), which
        is a normal pass-ending signal, not a failure. Status is "no-work"
        only when the whole pass -- queue, archival, and backfill -- did
        nothing; an empty queue with a real archival/backfill change is not
        "no-work". The pass journal (G6) is always written in ``finally``,
        even if the body raises.
        """
        pass_id = uuid4().hex
        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self._active_pass = CompileEnrichPass(pass_id=pass_id, snapshot_enabled=True)
        self._cleanup_old_pass_snapshots()
        status = "ok"
        gate_error = ""
        rollback_report: dict[str, Any] | None = None
        try:
            drain_result = self.drain_queue(
                force=True, max_events=50, refresh_qmd=False
            )
            queue_errors = list(drain_result.get("errors", []))
            queue_busy = False
            queue_worker_pid = 0
            if queue_errors == ["worker-busy"]:
                state = self._load_worker_state()
                if self._worker_state_is_live(state):
                    queue_busy = True
                    queue_worker_pid = int(state.get("pid") or 0)
                    queue_errors = []
            archived = self._archive_stale_notes(limit=backfill_limit)
            backfilled = self._backfill_freshness_notes(
                limit=max(0, backfill_limit - len(archived))
            )
            # ТЗ 6.3: compression runs after archival and backfill, on its
            # own small budget (MAX_COMPRESSED_PAGES_PER_PASS) so it never
            # competes with the enrichment page budget above.
            compressed = self._compress_cooled_pages(
                limit=MAX_COMPRESSED_PAGES_PER_PASS
            )
            # The retry for conflicts the write path could not settle. Runs
            # after the queue drain so that pairs left undecided earlier
            # tonight get their second attempt the same night, and last
            # among the write steps so it never spends model calls the
            # enrichment work still needed.
            conflicts_resolved = self._resolve_open_conflicts(
                limit=MAX_CONFLICT_RETRIES_PER_PASS
            )
            # The other automated queue drain: queued drift suspicions get
            # judged rather than waiting for the owner to look at a page.
            drift_marked = self._adjudicate_drift_entries(
                limit=MAX_DRIFT_JUDGEMENTS_PER_PASS
            )
            lint_issues = self.lint_notes()
            freshness_issues = self.freshness_issues()
            if (
                drain_result.get("updated")
                or archived
                or backfilled
                or compressed
                or conflicts_resolved
            ):
                self._refresh_qmd_index()

            took_work = int(drain_result.get("drained") or 0) > 0
            pages_changed = bool(
                drain_result.get("updated")
                or archived
                or backfilled
                or compressed
                or conflicts_resolved
                or drift_marked
                or self._active_pass.touched_pages
            )
            # ТЗ 5.5 inv 7: exhausting a budget ends the pass normally.
            # "Took work but changed zero pages" only fails the
            # effectiveness gate (inv 5) when the emptiness is *not*
            # explained by a budget limit hit somewhere in the pass -- the
            # exhaustion itself is always recorded in the pass journal's
            # ``budget_exhausted`` field regardless of ``status``.
            budget_hit = bool(self._active_pass.budget_exhausted)
            # ТЗ 5.5 inv 5's second condition: a page this pass actually
            # wrote to, left with no source link at all. Scoped to this
            # pass's own touched pages (not every page lint happens to
            # flag) and to "no link at all", not "a broken one" -- a broken
            # link can be an unrelated file move after the write, and a
            # rollback for that would cost more than it prevents. A page
            # whose only touch this pass was a pure archive->warm tier bump
            # (``archive_promoted_pages``) is exempt: that write never
            # touches the sources table, so it cannot be the cause of a
            # missing-sources finding. Unlike the first condition, this one
            # is never excused by a budget hit: a budget explains "we
            # changed nothing", not "we changed it badly".
            missing_source_pages = sorted(
                {
                    issue["path"]
                    for issue in lint_issues
                    if issue["issue"] == "missing-sources"
                    and issue["path"] in self._active_pass.touched_pages
                    and issue["path"] not in self._active_pass.archive_promoted_pages
                }
            )
            errors = queue_errors
            # Both gates record *why* they fired before doing anything about
            # it (code review): rolling back first meant that if the
            # rollback itself blew up -- a full disk, a read-only vault --
            # the handler below overwrote ``gate_error`` with the rollback's
            # own message, and the pass journal ended up saying "disk full"
            # about a pass that had in fact failed the inv-5 gate. The
            # rollback is a consequence of the reason, so the reason is
            # written down first.
            if took_work and not pages_changed and not budget_hit:
                status = "failed"
                gate_error = (
                    "compile-enrich pass took work but changed zero pages; "
                    "rolled back (ТЗ 5.5 inv 5)"
                )
                errors = [*queue_errors, gate_error]
                rollback_report = self.rollback_compile_enrich_pass(pass_id)
            elif missing_source_pages:
                status = "failed"
                gate_error = (
                    "compile-enrich pass changed page(s) with no source link "
                    "at all: " + ", ".join(missing_source_pages) + "; "
                    "rolled back (ТЗ 5.5 inv 5)"
                )
                errors = [*queue_errors, gate_error]
                rollback_report = self.rollback_compile_enrich_pass(pass_id)
            elif not took_work and not pages_changed:
                # ТЗ 7.1: "no-work" must mean the whole pass did nothing --
                # an empty queue with a real archival or backfill change is
                # not a quiet night; it is a change the digest must report,
                # so it must not collapse into the same status as one.
                status = "no-work"

            return {
                "queued_drained": int(drain_result.get("drained") or 0),
                "queue_updated": list(drain_result.get("updated", [])),
                "consolidations": list(drain_result.get("consolidations", [])),
                "queue_errors": queue_errors,
                "queue_busy": queue_busy,
                "queue_worker_pid": queue_worker_pid,
                "lint_issues": lint_issues,
                "freshness_issues": freshness_issues,
                "backfilled": backfilled,
                "archived": archived,
                "compressed": compressed,
                "conflicts_resolved": conflicts_resolved,
                "drift_marked": drift_marked,
                "searchable_write": bool(
                    drain_result.get("updated")
                    or archived
                    or backfilled
                    or compressed
                    or conflicts_resolved
                ),
                "errors": errors,
            }
        except Exception as exc:
            status = "failed"
            # Appended, not written over (code review): a ``gate_error``
            # already set above is the cause, and whatever raised afterwards
            # -- in practice the rollback that cause triggered -- is its
            # consequence. Overwriting left the journal naming only the
            # consequence, so the owner's digest reported a full disk for a
            # pass that failed the inv-5 gate.
            gate_error = f"{gate_error}; then: {exc}" if gate_error else str(exc)
            raise
        finally:
            try:
                self._write_pass_journal(
                    pass_id=pass_id,
                    started_at=started_at,
                    status=status,
                    error=gate_error,
                    rollback=rollback_report,
                )
            except Exception as exc:  # noqa: BLE001 - see below
                # Unguarded, this call decided the fate of everything after
                # it in this ``finally`` (code review): a failing journal
                # write (``_atomic_write_text`` -- a full disk, a read-only
                # ``.session/``) aborted the rest of the block, so the
                # dropped-source clears below never ran and ``_active_pass``
                # was never reset. A pass that had in fact just compiled the
                # owner's note then still left them told to re-save it, and
                # the leftover pass object went on budgeting model calls for
                # the next one. On top of that the ``OSError`` replaced
                # whatever was propagating -- including nothing at all, so a
                # clean pass was reported to the caller as a failure.
                logger.warning("Failed to write compile-enrich pass journal: %s", exc)
            # The drains that ran a previously given-up source to a
            # conclusion get to retire its trace -- see
            # ``_clear_dropped_queue_source`` for why this waits until here.
            # In ``finally`` rather than after the gate (code review): an
            # exception raised past the drain (a lint, archival, backfill, or
            # compression stage blowing up) leaves the page written and the
            # queue event acked exactly as a clean pass would, but never
            # reaches a `return`, and the loop below would then never run --
            # leaving the owner told to re-save a note that had in fact just
            # compiled. ``_forget_dropped_queue_source`` swallows its own
            # errors, so this cannot replace whatever exception is already
            # propagating.
            #
            # Only the sources whose pages the rollback actually put back are
            # held onto (code review): "a rollback happened" is not the same
            # as "this source's conclusion was undone". The rollback restores
            # only pages whose fingerprint still matches what this pass left,
            # and restores nothing at all when the gate fires on a pass that
            # wrote no page -- yet a source can conclude with no page write
            # at all, when the impact stage decides it affects none. Voiding
            # every clear on sight discarded those too.
            restored_pages = set(
                rollback_report.get("restored", ()) if rollback_report else ()
            )
            pending_clears = self._active_pass.dropped_sources_cleared
            for cleared_source, cleared_pages in pending_clears:
                if any(page in restored_pages for page in cleared_pages):
                    continue
                self._forget_dropped_queue_source(cleared_source)
            self._active_pass = None

    def lint_notes(self) -> list[dict[str, str]]:
        """Run deterministic health checks over compiled notes."""

        issues: list[dict[str, str]] = []
        required_sections = {
            "Current State",
            "Recent Changes",
            "Open Loops",
            "Key Decisions",
            "Next Check",
            "Sources",
        }
        for candidate in self._iter_candidates():
            sections = self._sections_from_text(candidate.text)
            if not required_sections.issubset(sections):
                issues.append(
                    {
                        "path": candidate.rel_path,
                        "issue": "missing-sections",
                        "detail": ",".join(sorted(required_sections - sections)),
                    }
                )
            sources = self._source_links_from_note(candidate.text)
            if not sources:
                issues.append(
                    {
                        "path": candidate.rel_path,
                        "issue": "missing-sources",
                        "detail": "compiled note has no source links",
                    }
                )
            for source in sources:
                resolved_source = self._resolve_lint_source_path(source)
                if resolved_source is None or not resolved_source.exists():
                    issues.append(
                        {
                            "path": candidate.rel_path,
                            "issue": "broken-source-link",
                            "detail": source,
                        }
                    )
        return issues

    def freshness_issues(self) -> list[dict[str, str]]:
        """Return compiled notes whose semantic source snapshot needs review."""

        issues: list[dict[str, str]] = []
        source_state = self._load_source_state()
        state_entries = source_state["entries"]
        for candidate in self._iter_candidates():
            issue = self._candidate_freshness_issue(
                candidate,
                state_entries.get(candidate.rel_path),
            )
            if issue is not None:
                issues.append(issue)
        return issues

    def initialize_source_state(self) -> dict[str, Any]:
        """Create the first source-aware baseline without rewriting briefings."""

        candidates = self._iter_candidates()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        initialized = 0
        unchanged = 0
        changed = 0

        with self._state_lock():
            state = self._load_source_state_unlocked()
            entries = state["entries"]
            active_paths = {candidate.rel_path for candidate in candidates}
            removed = sorted(set(entries) - active_paths)
            for rel_path in removed:
                del entries[rel_path]

            for candidate in candidates:
                snapshot = self._source_snapshot(candidate.text)
                existing = entries.get(candidate.rel_path)
                if isinstance(existing, dict):
                    if existing.get("sources") == snapshot:
                        unchanged += 1
                    else:
                        changed += 1
                    continue
                entries[candidate.rel_path] = {
                    "evaluated_at": now,
                    "sources": snapshot,
                }
                initialized += 1

            if initialized or removed or not self.source_state_path.exists():
                self._write_source_state_unlocked(state)

        return {
            "evaluated": len(candidates),
            "initialized": initialized,
            "unchanged": unchanged,
            "changed": changed,
            "removed": removed,
            "errors": [],
        }

    def file_output_artifact(
        self,
        *,
        request: str,
        output_markdown: str,
        artifact_type: str,
    ) -> str | None:
        """Persist useful assistant outputs back into the searchable vault."""

        if not self.should_file_output_artifact(request, output_markdown):
            return None
        now = datetime.now().astimezone()
        dir_path = self.answers_root / now.strftime("%Y") / now.strftime("%m")
        title = self._artifact_title(request)
        slug = self._slugify(title) or "assistant-output"
        note = self._render_output_artifact(
            title=title,
            request=request,
            output_markdown=output_markdown,
            artifact_type=artifact_type,
            created_at=now,
        )
        with vault_write_lock(self.vault_path) as lock:
            path = dir_path / f"{now.strftime('%Y-%m-%d-%H%M%S')}-{slug}.md"
            counter = 1
            while path.exists():
                path = dir_path / (
                    f"{now.strftime('%Y-%m-%d-%H%M%S')}-{slug}-{counter}.md"
                )
                counter += 1
            write_validated_vault_markdown(
                self.vault_path,
                path,
                note.encode("utf-8"),
                manifest=self._manifest(),
                existing_lock=lock,
            )
            rel_path = path.relative_to(self.vault_path).as_posix()
            self.enqueue_refresh(
                source_path=rel_path,
                source_excerpt=self._clip(
                    f"{request.strip()}\n\n{output_markdown.strip()}",
                    MAX_SOURCE_EXCERPT_CHARS,
                ),
                max_updates=2,
            )
        self.spawn_background_drain()
        return rel_path

    @staticmethod
    def should_file_output_artifact(request: str, output_markdown: str) -> bool:
        """Heuristic filter so filing loop does not dump every trivial answer."""

        request_text = " ".join(str(request or "").split()).strip()
        output_text = str(output_markdown or "").strip()
        if len(output_text) < 180:
            return False
        if not request_text or len(request_text) < 8:
            return False
        if "\n- " in output_text or "\n1. " in output_text:
            return True
        if len(output_text) >= 350:
            return True
        return any(
            token in request_text.lower()
            for token in ("что", "status", "статус", "решили", "изменил", "summary")
        )

    def _write_batch_consolidation(
        self,
        events: list[CompiledBatchConsolidationEvent],
    ) -> str | None:
        """Persist one best-effort cross-source consolidation note for a queue batch."""

        eligible = [
            event
            for event in events
            if self._batch_consolidation_source_allowed(event.source_rel_path)
        ]
        if len(eligible) < BATCH_CONSOLIDATION_MIN_EVENTS:
            return None

        unique_sources = {event.source_rel_path for event in eligible}
        if len(unique_sources) < BATCH_CONSOLIDATION_MIN_EVENTS:
            return None

        batch = eligible[:BATCH_CONSOLIDATION_MAX_EVENTS]
        prompt = self._build_batch_consolidation_prompt(batch)
        try:
            payload = self._run_json_dict_prompt(
                prompt=prompt,
                timeout=BATCH_CONSOLIDATION_TIMEOUT_SECONDS,
                error_context="compiled batch consolidation",
                json_example=BATCH_CONSOLIDATION_JSON_EXAMPLE,
            )
        except (CliExecutionError, FileNotFoundError, TimeoutError, ValueError) as exc:
            logger.warning(
                "Compiled batch consolidation skipped for %s source events: %s",
                len(batch),
                exc,
            )
            return None

        return self._persist_batch_consolidation(payload=payload, events=batch)

    @staticmethod
    def _batch_consolidation_source_allowed(source_rel_path: str) -> bool:
        raw = str(source_rel_path or "").strip()
        if not raw:
            return False
        return not raw.startswith(("compiled/", "summaries/", "."))

    def _build_batch_consolidation_prompt(
        self,
        events: list[CompiledBatchConsolidationEvent],
    ) -> str:
        batch_payload = [
            {
                "source_path": event.source_rel_path,
                "updated_briefings": list(event.updated_paths),
                "excerpt": event.source_excerpt,
            }
            for event in events
        ]
        return (
            "You are writing a compact cross-source consolidation note for a "
            "personal assistant memory system.\n"
            f"Write generated text in {prompt_language_name(self.content_language)}.\n"
            "Return ONLY JSON.\n\n"
            "Goal: synthesize the strongest recurring patterns across multiple "
            "recent source events that already refreshed compiled briefings.\n\n"
            "Required JSON schema:\n"
            "{\n"
            '  "headline": "short title",\n'
            '  "summary": "2-4 sentence synthesis",\n'
            '  "themes": ["bullet", "..."],\n'
            '  "follow_ups": ["bullet", "..."]\n'
            "}\n\n"
            "Rules:\n"
            "- Only mention patterns supported by multiple sources or strongly "
            "reinforced by the refreshed briefings.\n"
            "- Stay cumulative and operational; do not restate every raw detail.\n"
            "- If the batch is too mixed for a durable synthesis, return a short "
            "headline, an honest summary, and empty lists.\n"
            "- Keep themes and follow_ups short.\n"
            "- Do not invent people, projects, decisions, or dates.\n\n"
            "[BATCH_EVENTS]\n"
            f"{json.dumps(batch_payload, ensure_ascii=False, indent=2)}\n"
        )

    def _persist_batch_consolidation(
        self,
        *,
        payload: dict[str, Any],
        events: list[CompiledBatchConsolidationEvent],
    ) -> str:
        now = datetime.now().astimezone()
        dir_path = (
            self.vault_path
            / "summaries"
            / "consolidations"
            / now.strftime("%Y")
            / now.strftime("%m")
        )
        headline = (
            self._clean_line(payload.get("headline"))
            or "Compiled Batch Consolidation"
        )
        slug = self._slugify(headline) or "compiled-batch-consolidation"
        note = self._render_batch_consolidation(
            headline=headline,
            payload=payload,
            events=events,
            created_at=now,
        )
        with vault_write_lock(self.vault_path) as lock:
            path = dir_path / f"{now.strftime('%Y-%m-%d-%H%M%S')}-{slug}.md"
            counter = 1
            while path.exists():
                path = dir_path / (
                    f"{now.strftime('%Y-%m-%d-%H%M%S')}-{slug}-{counter}.md"
                )
                counter += 1
            rel_path = path.relative_to(self.vault_path).as_posix()
            rendered = note.encode("utf-8")
            # Code review defect 2: this note is derived entirely from this
            # pass's own queue batch (it cites the `updated_briefings`
            # paths the pass just wrote, see
            # `_build_batch_consolidation_prompt`), so it must roll back
            # with the rest of the pass like every other page write during
            # an active pass -- otherwise a rollback leaves a note behind
            # that describes changes which no longer exist.
            self._snapshot_pass_page(rel_path, before=None, after=rendered)
            write_validated_vault_markdown(
                self.vault_path,
                path,
                rendered,
                manifest=self._manifest(),
                existing_lock=lock,
            )
        return rel_path

    def refresh_after_write(
        self,
        *,
        source_path: str | Path,
        source_excerpt: str = "",
        max_updates: int = 3,
        force_recompile: bool = False,
    ) -> dict[str, Any]:
        """Refresh a small compiled-note budget after one raw/searchable write."""

        if max_updates <= 0:
            return {"available": False, "updated": [], "errors": ["budget-disabled"]}

        source_rel_path = self._normalize_rel_path(source_path)
        if not source_rel_path or source_rel_path.startswith("compiled/"):
            return {"available": False, "updated": [], "errors": ["unsupported-path"]}
        if not self.is_available():
            return {"available": False, "updated": [], "errors": ["ai-cli-unavailable"]}

        excerpt = self._source_excerpt(source_rel_path, source_excerpt)
        if not excerpt:
            return {"available": False, "updated": [], "errors": ["empty-source"]}

        if (
            self._active_pass is not None
            and source_rel_path not in self._active_pass.sources_processed
        ):
            self._active_pass.sources_processed.append(source_rel_path)

        signal = self.qmd._memory_signal_for_rel_path(source_rel_path)
        try:
            targets = self._resolve_targets(
                source_rel_path=source_rel_path,
                source_excerpt=excerpt,
                signal=signal,
                max_updates=max_updates,
            )
        except CompiledBriefingPassBudgetExceededError:
            # ТЗ 5.5 inv 7 / G3: budget exhaustion ends this pass's work on
            # this source normally, it is not an error -- a dedicated
            # branch, never folded into `errors` or `updated`.
            return {
                "available": True,
                "updated": [],
                "errors": [],
                "budget_exhausted": True,
            }
        except (CliExecutionError, FileNotFoundError, TimeoutError, ValueError) as exc:
            logger.warning(
                "Compiled briefing impact resolution skipped for %s: %s",
                source_rel_path,
                exc,
            )
            return {"available": False, "updated": [], "errors": [str(exc)]}

        if not targets:
            return {"available": True, "updated": [], "errors": []}

        updated: list[str] = []
        errors: list[str] = []
        verify_rejected: list[str] = []
        requeueable = False
        for target in targets:
            try:
                upsert_result = self._upsert_briefing(
                    target=target,
                    source_rel_path=source_rel_path,
                    source_excerpt=excerpt,
                    signal=signal,
                    force_recompile=force_recompile,
                )
            except CompiledBriefingPassBudgetExceededError:
                # Same rationale as above: stop trying further targets for
                # this source and report the exhaustion flag, keeping any
                # real writes already collected in `updated`.
                return {
                    "available": True,
                    "updated": updated,
                    "errors": errors,
                    "budget_exhausted": True,
                }
            except (
                CliExecutionError,
                CompiledBriefingVerificationRejectedError,
                CompiledBriefingWriteConflict,
                FileNotFoundError,
                TimeoutError,
                ValueError,
            ) as exc:
                logger.warning(
                    "Compiled briefing refresh failed for %s -> %s: %s",
                    source_rel_path,
                    target.slug,
                    exc,
                )
                errors.append(f"{target.domain}/{target.slug}: {exc}")
                if isinstance(exc, CompiledBriefingVerificationRejectedError):
                    target_rel_path = (
                        self._target_path(target)
                        .relative_to(self.vault_path)
                        .as_posix()
                    )
                    verify_rejected.append(target_rel_path)
                    self._handle_incremental_verify_rejection(
                        target,
                        source_rel_path=source_rel_path,
                        source_excerpt=excerpt,
                        max_updates=max_updates,
                    )
                continue
            if upsert_result.written:
                updated.append(upsert_result.path)
            elif upsert_result.requeueable:
                # Code review defect 2: at least one target this source
                # touched was skipped for an ambiguous human-zone marker
                # via a free-to-retry path (see BriefingUpsertResult) --
                # tell _drain_queue_once so it can put the underlying queue
                # event back instead of acking it away for good.
                requeueable = True

        result: dict[str, Any] = {
            "available": True,
            "updated": updated,
            "errors": errors,
            "requeueable": requeueable,
        }
        if verify_rejected:
            result["verify_rejected"] = verify_rejected
        return result

    def _handle_incremental_verify_rejection(
        self,
        target: CompiledBriefingTarget,
        *,
        source_rel_path: str,
        source_excerpt: str,
        max_updates: int,
    ) -> None:
        """Same ``MAX_VERIFY_REJECTED_RETRIES`` bookkeeping the nightly
        backfill loop (``_backfill_freshness_notes``) already does, applied
        here so an existing page that Verify keeps rejecting on incremental
        writes reaches the owner too -- not only the ones the nightly
        freshness scan happens to revisit (see ``_queue_verify_rejected``).

        A target with no existing page on disk yet is skipped: there is
        nothing stuck to report, and the next source event starts fresh.
        """
        target_path = self._target_path(target)
        if not target_path.is_file():
            return
        try:
            note_text = self._read_page_text(target_path)
        except OSError:
            return
        rel_path = target_path.relative_to(self.vault_path).as_posix()
        rejection_count = self._record_verify_rejection(rel_path, note_text)
        if rejection_count >= MAX_VERIFY_REJECTED_RETRIES:
            self._queue_verify_rejected(
                page_rel_path=rel_path,
                source_rel_path=source_rel_path,
                source_excerpt=source_excerpt,
                max_updates=max_updates,
            )

    def refresh_daily_fully(
        self,
        *,
        source_path: str | Path,
        max_updates_per_chunk: int = 3,
        refresh_qmd: bool = True,
        on_chunk: Callable[[dict[str, Any]], None] | None = None,
        start_chunk: int = 1,
        force_recompile: bool = False,
    ) -> dict[str, Any]:
        (
            "Refresh one daily note chunk-by-chunk without silently dropping "
            "the middle."
        )

        source_rel_path = self._normalize_rel_path(source_path)
        if not source_rel_path.startswith("daily/"):
            return self.refresh_after_write(
                source_path=source_rel_path,
                max_updates=max_updates_per_chunk,
            )

        source_text = self._source_excerpt(source_rel_path, "")
        source_path_obj = self.vault_path / source_rel_path
        if source_path_obj.exists():
            source_text = source_path_obj.read_text(encoding="utf-8", errors="replace")
        if not source_text.strip():
            return {"available": False, "updated": [], "errors": ["empty-source"]}

        chunks = self._daily_source_chunks(source_rel_path, source_text)
        if not chunks:
            return {"available": False, "updated": [], "errors": ["empty-source"]}

        updated: list[str] = []
        errors: list[str] = []
        total_chunks = len(chunks)
        first_chunk = min(max(1, start_chunk), total_chunks + 1)
        processed_chunks = first_chunk - 1
        for index, chunk in enumerate(
            chunks[first_chunk - 1 :], start=first_chunk
        ):
            if on_chunk is not None:
                on_chunk(
                    {
                        "index": index,
                        "total": total_chunks,
                        "status": "started",
                        "source_rel_path": source_rel_path,
                    }
                )
            result = self.refresh_after_write(
                source_path=source_rel_path,
                source_excerpt=chunk,
                max_updates=max_updates_per_chunk,
                force_recompile=force_recompile,
            )
            chunk_updated = [
                str(rel_path) for rel_path in result.get("updated", []) if str(rel_path)
            ]
            chunk_errors = [
                str(error).strip()
                for error in result.get("errors", [])
                if str(error).strip()
            ]
            for rel_path in result.get("updated", []):
                rel_value = str(rel_path)
                if rel_value and rel_value not in updated:
                    updated.append(rel_value)
            for error in result.get("errors", []):
                message = str(error).strip()
                if message:
                    errors.append(f"chunk {index}: {message}")
            processed_chunks = index
            if on_chunk is not None:
                on_chunk(
                    {
                        "index": index,
                        "total": total_chunks,
                        "status": "finished",
                        "source_rel_path": source_rel_path,
                        "updated": chunk_updated,
                        "errors": chunk_errors,
                    }
                )
            if any(detect_terminal_backend_message(error) for error in chunk_errors):
                break

        if updated and refresh_qmd:
            self._refresh_qmd_index()

        return {
            "available": True,
            "updated": updated,
            "errors": errors,
            "chunks": total_chunks,
            "processed_chunks": processed_chunks,
            "source_rel_path": source_rel_path,
        }

    def build_question_context(
        self,
        question: str,
        *,
        limit: int = QUESTION_CONTEXT_LIMIT,
        ranked: Sequence[CompiledBriefingCandidate] | None = None,
    ) -> str:
        """Return a compiled-briefing block for direct-question prompts.

        ``ranked`` lets a caller that has *already* ranked hand the result
        in instead of having it re-derived here (code review). Ranking
        re-reads every ``compiled/**`` page from disk, so ranking twice in a
        row is two separate live scans: a background enrichment landing
        between them would put one set of pages into the model's prompt and
        cite a different set in the owner's provenance footnote. The only
        caller that needs both -- ``processor._build_compiled_briefings_block``
        -- passes its frozen list through here for exactly that reason.
        """

        if ranked is None:
            ranked = self._rank_candidates(question, limit=limit)
        if not ranked:
            return ""

        lines = [
            "=== COMPILED BRIEFINGS ===",
            "Use these compiled briefings first.",
            (
                "They are derived summaries, not ground truth. Verify with curated "
                "core or qmd when needed."
            ),
            "",
        ]
        for index, candidate in enumerate(ranked, start=1):
            lines.extend(
                [
                    f"[{index}] {candidate.rel_path}",
                    f"Title: {candidate.title}",
                    f"Domain: {candidate.domain}",
                    f"Description: {candidate.description or '(none)'}",
                    (
                        f"Freshness: {candidate.freshness_state or 'unknown'} | "
                        f"Confidence: {candidate.confidence or 'unknown'}"
                    ),
                    "",
                    candidate.text.strip(),
                    "",
                ]
            )
        lines.append("=== END COMPILED BRIEFINGS ===")
        return "\n".join(lines).strip()

    def is_available(self) -> bool:
        """Check whether the configured CLI exists before doing extra work."""

        binary = self.runner.spec.argv_prefix[0]
        return shutil.which(binary) is not None

    def _resolve_targets(
        self,
        *,
        source_rel_path: str,
        source_excerpt: str,
        signal: dict[str, Any] | None,
        max_updates: int,
    ) -> list[CompiledBriefingTarget]:
        """Impact stage (ТЗ 5.2 step 1), despite the ``_resolve_*`` name.

        The model proposes which compiled pages a changed source affects
        and, optionally, an ``existing_path`` guess from its own catalog
        view -- this is not deterministic matching. The deterministic
        Resolve stage (ТЗ 5.2 step 2: exact path/slug match, then semantic
        search with fixed thresholds) lives in ``_target_path`` and
        ``_semantic_resolve_target``, both called from ``_upsert_briefing``.
        """
        catalog = self._impact_catalog(
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
        )
        prompt = self._build_impact_prompt(
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
            signal=signal,
            catalog=catalog,
            max_updates=max_updates,
        )
        payload = self._run_json_dict_prompt(
            prompt=prompt,
            timeout=IMPACT_TIMEOUT_SECONDS,
            error_context="compiled briefing impact resolution",
            json_example=IMPACT_JSON_EXAMPLE,
        )
        updates = payload.get("updates")
        if not isinstance(updates, list):
            return []

        targets: list[CompiledBriefingTarget] = []
        seen: set[tuple[str, str]] = set()
        for item in updates:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").strip().lower()
            if domain not in COMPILED_BRIEFING_DOMAINS:
                continue
            title = self._clean_line(item.get("title"))
            if not title:
                continue
            description = self._clean_line(item.get("description"))
            slug = self._slugify(str(item.get("slug") or title))
            if not slug:
                continue
            key = (domain, slug)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                CompiledBriefingTarget(
                    domain=domain,
                    title=title,
                    slug=slug,
                    description=description,
                    reason=self._clean_line(item.get("reason")),
                    existing_path=self._clean_line(item.get("existing_path")),
                )
            )
            if len(targets) >= max_updates:
                break
        return targets

    def _upsert_briefing(
        self,
        *,
        target: CompiledBriefingTarget,
        source_rel_path: str,
        source_excerpt: str,
        signal: dict[str, Any] | None,
        record_source_state: bool = True,
        force_recompile: bool = False,
    ) -> BriefingUpsertResult:
        self._ensure_dirs()
        note_path = self._target_path(target)
        duplicate_candidate = ""
        duplicate_confidence = 0.0
        if not note_path.exists():
            # Resolve stage 1 (exact path/slug) found nothing on disk; try
            # stage 2 (semantic search) before falling back to a new page.
            resolve_result = self._semantic_resolve_target_full(target)
            if resolve_result.target is not None:
                target = resolve_result.target
                note_path = self._target_path(target)
            elif resolve_result.duplicate_candidate:
                # Possible-duplicate zone: the original target is kept (a
                # new page still gets created below), but once that write
                # succeeds the pair is queued as an owner decision -- see
                # the ``_queue_duplicate_candidate`` call further down.
                duplicate_candidate = resolve_result.duplicate_candidate
                duplicate_confidence = resolve_result.duplicate_confidence

        rel_path = note_path.relative_to(self.vault_path).as_posix()
        existing_text = ""
        existing_meta: dict[str, str] = {}
        try:
            existing_bytes = note_path.read_bytes()
        except FileNotFoundError:
            existing_bytes = None
        original_fingerprint = self._full_content_fingerprint(existing_bytes)
        if existing_bytes is not None:
            try:
                # Strict, unlike every reader of this layer: this method
                # renders the whole page from ``existing_text`` (the human
                # zone included, via _render_briefing's _extract_human_zone)
                # and writes that back. Decoding leniently here would
                # replace an undecodable byte with U+FFFD and then commit
                # that replacement to disk, destroying the owner's real
                # bytes with no way back. Fail the page closed instead --
                # same shape as the ambiguous-marker path below.
                existing_text = existing_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                self._queue_undecodable_page(rel_path)
                raise CompiledPageEncodingError(
                    f"compiled page is not valid UTF-8: {rel_path}"
                ) from exc
            existing_meta = self._frontmatter_fields(existing_text)

        if existing_meta.get("tier") == "archive":
            signal = {**(signal or {}), "tier": "warm"}

        if not force_recompile and self._duplicate_source_chunk(
            existing_text=existing_text,
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
            page_rel_path=rel_path,
        ):
            return BriefingUpsertResult(path=rel_path, written=False)

        # ТЗ 6.1 "Уровень памяти управляет бюджетом обогащения": placed
        # after the idempotency-by-chunk skip above, before the monthly
        # enrichment/model-call budget checks below, so a tier-gated page
        # never consumes those two budgets. It still writes a page to disk,
        # though, so it goes through the same pages-per-pass accounting as
        # every other write in this method (ТЗ 5.6 "Максимум изменяемых
        # страниц за проход" bounds pass write volume, not "enrichment").
        pass_obj = self._active_pass
        if pass_obj is not None:
            # ТЗ 5.6 "Максимум изменяемых страниц за проход": only a page
            # this pass has not already committed to (see the
            # ``touched_pages.add`` below) can hit the cap -- re-enriching
            # a page this same pass already wrote is always allowed. Same
            # check the archive/cold branches above already ran.
            self._check_pages_per_pass_budget(pass_obj, rel_path)
        # ТЗ 5.6 "Максимум обогащений одной страницы за календарный месяц":
        # a *calendar month* budget, unlike the per-pass cap above -- so it
        # must hold on the hot write path too (``refresh_after_write``,
        # where ``_active_pass`` is None and which already handles
        # ``CompiledBriefingPassBudgetExceededError``), otherwise a page
        # could be enriched without limit outside the nightly pass and the
        # cap would only ever bind at night. No new frontmatter field is
        # introduced (ТЗ note: an ``epistemic_``-prefixed field would switch
        # the page's validation profile) -- the count is derived from this
        # calendar month's rows already in "Sources That Shaped This Page"
        # (``_sources_shaped_rows``), per the task instructions. ТЗ 6.1:
        # rows marked ``NOT_ENRICHMENT_SOURCE_MARKER`` (cold, or a `warm`
        # page that turned out insignificant) do not consume this budget.
        month_prefix = date.today().isoformat()[:7]
        # Counted by distinct (date, source) pair, not by row: ТЗ 5.6 budgets
        # *enrichments*, and one enrichment appends one row per claim it
        # contributed, so counting rows charged a single pass 3-4 times over.
        # In practice that declared "drift" on pages enriched 1-3 times --
        # 29 of them in one month here, each blocked until the month rolled
        # over. A pass always writes its rows under one source and one date,
        # so the pair is the enrichment.
        monthly_enrichments = len(
            {
                (row_date, source)
                for row_date, source, what in self._sources_shaped_rows(existing_text)
                if row_date.startswith(month_prefix)
                and what != NOT_ENRICHMENT_SOURCE_MARKER
            }
        )
        if monthly_enrichments >= MAX_ENRICHMENTS_PER_PAGE_PER_MONTH:
            if pass_obj is not None:
                # Pass-level bookkeeping for the nightly digest; there is no
                # journal to record into outside a pass.
                pass_obj.budget_exhausted.add("monthly-enrichments-per-page")
            # ТЗ 5.6 table: exceeding this budget also goes "в очередь
            # решений с пометкой о дрейфе", on top of the pass-level
            # budget-exhaustion bookkeeping above (which only reaches the
            # nightly digest, not the owner's decisions queue).
            self._queue_monthly_drift(page_rel_path=rel_path)
            raise CompiledBriefingPassBudgetExceededError(
                f"monthly enrichment budget reached for {rel_path} "
                f"({MAX_ENRICHMENTS_PER_PAGE_PER_MONTH})"
            )

        prompt = self._build_compile_prompt(
            target=target,
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
            signal=signal,
            existing_text=existing_text,
        )
        payload = self._run_json_dict_prompt(
            prompt=prompt,
            timeout=COMPILE_TIMEOUT_SECONDS,
            error_context="compiled briefing render",
            json_example=COMPILE_JSON_EXAMPLE,
        )
        claims, conflicts = self._extract_and_verify_claims(
            payload=payload,
            target=target,
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
            existing_claims=self._existing_claims_catalog(existing_text),
            existing_text=existing_text,
            existing_meta=existing_meta,
            signal=signal,
        )
        try:
            rendered = self._render_briefing(
                target=target,
                payload=payload,
                source_rel_path=source_rel_path,
                existing_text=existing_text,
                existing_meta=existing_meta,
                signal=signal,
                source_excerpt=source_excerpt,
                claims=claims,
                conflicts=conflicts,
            )
        except HumanZoneMarkerError:
            # Same page-skipped-because-of-broken-markers class as the
            # `cold`-tier and compression paths above (ТЗ: fail-closed
            # rather than guess which text is the owner's) -- fold it into
            # the same pass counter so the digest reports every page the
            # owner needs to fix, not just the two that had nowhere else to
            # surface. The caller (``refresh_after_write``) still catches
            # this as a plain ``ValueError`` and logs/retries it exactly as
            # before; this only adds the owner-facing bookkeeping.
            self._record_human_zone_ambiguous(rel_path)
            raise
        with vault_write_lock(self.vault_path) as lock:
            try:
                current_bytes = note_path.read_bytes()
            except FileNotFoundError:
                current_bytes = None
            if self._full_content_fingerprint(current_bytes) != original_fingerprint:
                raise CompiledBriefingWriteConflict(
                    f"compiled briefing changed during build: {rel_path}"
                )
            self._snapshot_pass_page(
                rel_path,
                before=current_bytes,
                after=rendered.encode("utf-8"),
            )
            write_validated_vault_markdown(
                self.vault_path,
                note_path,
                rendered.encode("utf-8"),
                manifest=self._manifest(),
                existing_lock=lock,
            )
            if record_source_state:
                self._record_source_state(
                    rel_path,
                    rendered,
                    source_rel_path=source_rel_path,
                    source_excerpt=source_excerpt,
                )
            if duplicate_candidate:
                # ТЗ 5.2 Resolve, possible-duplicate zone: both pages must
                # already exist on disk once the entry is queued -- the new
                # page's write above just satisfied that, and this call
                # passes through the lock already held here rather than
                # taking its own (``append_decision_queue_entries`` never
                # does).
                self._queue_duplicate_candidate(
                    page_rel_path=rel_path,
                    candidate_rel_path=duplicate_candidate,
                    confidence=duplicate_confidence,
                    existing_lock=lock,
                )
        if pass_obj is not None:
            pass_obj.touched_pages.add(rel_path)
        return BriefingUpsertResult(path=rel_path, written=True)

    @staticmethod
    def _check_pages_per_pass_budget(
        pass_obj: CompileEnrichPass | None, rel_path: str
    ) -> None:
        """Raise once this pass has already written ``MAX_PAGES_PER_PASS``
        other pages and ``rel_path`` is not one of them.

        ТЗ 5.6 "Максимум изменяемых страниц за проход" bounds how much a
        pass writes to disk in total, not how much it "enriches" -- shared
        by every write path in ``_upsert_briefing``: the tier-gated
        archive/cold writes above (which spend neither the monthly
        enrichment budget nor a model call) and the full enrichment path
        further down.
        """
        if pass_obj is None:
            return
        if (
            rel_path not in pass_obj.touched_pages
            and len(pass_obj.touched_pages) >= MAX_PAGES_PER_PASS
        ):
            pass_obj.budget_exhausted.add("pages-per-pass")
            raise CompiledBriefingPassBudgetExceededError(
                f"max pages per pass reached ({MAX_PAGES_PER_PASS}): {rel_path}"
            )

    def _promote_archive_tier(self, *, note_path: Path, rel_path: str) -> bool:
        """ТЗ 6.1/6.4 plan point 4: a source landing on an `archive`-tier
        page bumps it to `warm` and nothing else. No compile, no Verify, no
        "Sources That Shaped This Page" row -- real enrichment happens on a
        later pass once the page is already `warm`, so this cannot cascade
        into an enrichment for the same source in the same pass.

        Uses the same point-edit primitive the memory engine uses
        (``patch_frontmatter_bytes``, also imported by
        ``skills/agent-memory/scripts/memory-engine.py``) via the same
        compute-bytes-then-``write_validated_vault_markdown`` pattern
        ``_archive_candidate`` already uses in this file, rather than the
        CAS-write wrapper ``patch_validated_vault_frontmatter`` -- both wrap
        the same underlying atomic write, so reusing this file's own
        established pattern keeps every write path here on one primitive.

        Automation never promotes past `warm` (never all the way to
        `core`) -- this only ever writes the literal string ``"warm"``.
        """
        with vault_write_lock(self.vault_path) as lock:
            try:
                current_bytes = note_path.read_bytes()
            except FileNotFoundError:
                return False
            fields = self._frontmatter_fields(
                current_bytes.decode("utf-8", errors="replace")
            )
            if fields.get("tier") != "archive":
                # Already promoted (e.g. an earlier source this same pass)
                # or not actually archive-tier any more -- nothing to do.
                return False
            new_bytes = patch_frontmatter_bytes(current_bytes, {"tier": "warm"})
            self._snapshot_pass_page(rel_path, before=current_bytes, after=new_bytes)
            write_validated_vault_markdown(
                self.vault_path,
                note_path,
                new_bytes,
                manifest=self._manifest(),
                existing_lock=lock,
            )
        return True

    def _record_non_enrichment_source(
        self,
        *,
        note_path: Path,
        rel_path: str,
        existing_text: str,
        original_fingerprint: bytes | None,
        source_rel_path: str,
        source_excerpt: str,
        requeueable_if_ambiguous: bool = False,
    ) -> BriefingUpsertResult:
        """ТЗ 6.1: add one marked, non-enrichment row to "Sources That
        Shaped This Page" -- no model call, no Verify, and (unlike a real
        enrichment) no ``enrichment_count`` bump and no monthly-budget
        effect (the monthly count filters ``NOT_ENRICHMENT_SOURCE_MARKER``
        rows out, see ``_upsert_briefing``).

        Shared by the `cold` tier (never enriched at all) and by a `warm`
        page whose speculative compile turned out insignificant (ТЗ 5.6
        "Значимый сигнал"; see ``_claims_are_significant``) -- both cases
        reduce to the same "acknowledge the source, do not enrich" write.

        Still calls ``_record_source_state`` with the real excerpt, so the
        ТЗ 5.5 invariant 4 idempotency-by-chunk guarantee holds here exactly
        like it does for a real enrichment: reprocessing the identical
        chunk is a no-op, but accumulating several *different* not-yet-seen
        chunks never blocks a later real enrichment once the page's tier
        moves on.

        Checks the human zone directly (rather than comparing the
        pre/post-``_replace_section`` text) before doing anything else: an
        ambiguous marker pair makes both ``_replace_section`` calls below
        silently no-op (fail-closed, see ``_human_zone_span``), and a plain
        before/after comparison cannot tell that failure apart from the
        legitimate case where this exact source/day is already reflected on
        the page and the section bodies simply have nothing new to add. Only
        the zone check is unambiguous, so this page is skipped -- frontmatter
        untouched, nothing written, and (crucially) ``_record_source_state``
        is not called, so the chunk is not marked applied and a later pass
        with a repaired zone can still pick it up.

        ``requeueable_if_ambiguous`` (code review defect 2): the caller's own
        promise that no model call happened before this check on this
        attempt, so putting the underlying queue event back for a free
        retry is safe. Only the `cold`-tier caller in ``_upsert_briefing``
        passes ``True`` -- the `warm`-tier "insignificant" caller reaches
        this same check *after* compiling and leaves it at the default
        ``False``, so a stuck `warm` page still only acks (as before) rather
        than re-burning a model call on every retry.
        """
        if self._human_zone_span(existing_text) == _AMBIGUOUS_HUMAN_ZONE:
            logger.warning(
                "Compiled briefing %s has ambiguous human-zone markers; "
                "skipping non-enrichment source write for %s so the source "
                "is not lost or falsely marked applied",
                rel_path,
                source_rel_path,
            )
            self._record_human_zone_ambiguous(rel_path)
            return BriefingUpsertResult(
                path=rel_path,
                written=False,
                requeueable=requeueable_if_ambiguous,
            )
        today = date.today().isoformat()
        shaped_rows = self._sources_shaped_rows(existing_text)
        existing_pairs = {(row[0], row[1]) for row in shaped_rows}
        if (today, source_rel_path) not in existing_pairs:
            shaped_rows = [
                *shaped_rows,
                (today, source_rel_path, NOT_ENRICHMENT_SOURCE_MARKER),
            ]
        old_sources = self._source_links_from_note(existing_text)
        all_sources = self._merge_paths(old_sources, [source_rel_path])
        new_text = self._replace_section(
            existing_text, "Sources", self._render_sources(all_sources)
        )
        new_text = self._replace_section(
            new_text,
            "Sources That Shaped This Page",
            self._render_sources_shaped_table(shaped_rows),
        )
        shaped_source_count = len(
            {
                source
                for _, source, what_added in shaped_rows
                if what_added != NOT_ENRICHMENT_SOURCE_MARKER
            }
        )
        new_bytes = patch_frontmatter_bytes(
            new_text.encode("utf-8"),
            {"updated": today, "source_count": shaped_source_count},
        )
        with vault_write_lock(self.vault_path) as lock:
            try:
                current_bytes = note_path.read_bytes()
            except FileNotFoundError:
                current_bytes = None
            if self._full_content_fingerprint(current_bytes) != original_fingerprint:
                raise CompiledBriefingWriteConflict(
                    f"compiled briefing changed during build: {rel_path}"
                )
            self._snapshot_pass_page(rel_path, before=current_bytes, after=new_bytes)
            write_validated_vault_markdown(
                self.vault_path,
                note_path,
                new_bytes,
                manifest=self._manifest(),
                existing_lock=lock,
            )
            self._record_source_state(
                rel_path,
                new_bytes.decode("utf-8"),
                source_rel_path=source_rel_path,
                source_excerpt=source_excerpt,
            )
        return BriefingUpsertResult(path=rel_path, written=True)

    def _warm_recent_source_signal(self, existing_text: str) -> bool:
        """ТЗ 5.6 "Значимый сигнал для уровня warm", cheap half: true when
        the sources table already has a row within the last
        ``WARM_SIGNAL_WINDOW_DAYS`` days -- together with the new source
        about to be recorded, that reaches the ">=2 sources in 7 days"
        signal without a model call.
        """
        cutoff = date.today() - timedelta(days=WARM_SIGNAL_WINDOW_DAYS)
        for row_date, _source, _what in self._sources_shaped_rows(existing_text):
            try:
                parsed = date.fromisoformat(row_date)
            except ValueError:
                continue
            if parsed >= cutoff:
                return True
        return False

    @staticmethod
    def _claims_are_significant(
        claims: list[dict[str, str]],
        conflicts: list[dict[str, str]],
    ) -> bool:
        """ТЗ 5.6 "Значимый сигнал для уровня warm", expensive half: a new
        decision/commitment or a conflict. The claim schema (ТЗ 4.2) has no
        separate "decision" kind -- "commitment" covers both a decision and
        an obligation.
        """
        if conflicts:
            return True
        return any(claim.get("kind") == "commitment" for claim in claims)

    @staticmethod
    def _full_content_fingerprint(content: bytes | None) -> bytes | None:
        """Fingerprint the exact briefing bytes; ``None`` represents absence."""
        return hashlib.sha256(content).digest() if content is not None else None

    def _manifest(self) -> VaultManifest:
        """Load the required project manifest before every Markdown write."""
        return load_manifest_for_vault(self.vault_path)

    # -- Compile-enrich pass snapshot/rollback (ТЗ 5.5 inv 8, G4) ----------

    def _pass_snapshot_dir(self, pass_id: str) -> Path:
        return self.vault_path / ".session" / "compile-enrich" / "snapshots" / pass_id

    def _pass_snapshot_manifest_path(self, pass_id: str) -> Path:
        return self._pass_snapshot_dir(pass_id) / "manifest.json"

    def _pass_snapshot_blob_path(self, pass_id: str, rel_path: str) -> Path:
        return self._pass_snapshot_dir(pass_id) / "blobs" / rel_path

    def _snapshot_pass_page(
        self,
        rel_path: str,
        *,
        before: bytes | None,
        after: bytes | None,
    ) -> None:
        """Snapshot one page's pre-pass state before a compile-enrich write
        (ТЗ 5.5 inv 8, G4). Called from ``_upsert_briefing`` before the page
        write and from ``_archive_candidate`` before the move/delete.

        A no-op with no active pass or with snapshots disabled for it. Only
        the first call for a given ``rel_path`` in one pass takes effect --
        the ТЗ wants the page's state before the PASS started, not before
        each individual write inside it, so a page enriched twice in one
        pass still rolls back to how it looked before the pass began.
        """
        pass_obj = self._active_pass
        if pass_obj is None or not pass_obj.snapshot_enabled:
            return
        if rel_path in pass_obj.snapshot_manifest:
            return
        if before is not None:
            _atomic_write_bytes(
                self._pass_snapshot_blob_path(pass_obj.pass_id, rel_path), before
            )
        fingerprint_before = self._full_content_fingerprint(before)
        fingerprint_after = self._full_content_fingerprint(after)
        pass_obj.snapshot_manifest[rel_path] = {
            "existed": before is not None,
            "fingerprint_before": (
                fingerprint_before.hex() if fingerprint_before is not None else None
            ),
            "fingerprint_after": (
                fingerprint_after.hex() if fingerprint_after is not None else None
            ),
        }
        _atomic_write_text(
            self._pass_snapshot_manifest_path(pass_obj.pass_id),
            json.dumps(pass_obj.snapshot_manifest, ensure_ascii=False, indent=2),
        )

    def _read_pass_snapshot_manifest(self, pass_id: str) -> dict[str, Any]:
        try:
            raw = self._pass_snapshot_manifest_path(pass_id).read_text(
                encoding="utf-8"
            )
            payload = json.loads(raw)
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _cleanup_old_pass_snapshots(self) -> None:
        """Delete pass snapshot directories older than
        ``SNAPSHOT_RETENTION_DAYS`` (ТЗ 5.5 inv 8 / 5.6). Runs once at the
        start of every pass, before any page is snapshotted."""
        root = self.vault_path / ".session" / "compile-enrich" / "snapshots"
        try:
            entries = list(root.iterdir())
        except FileNotFoundError:
            return
        cutoff = time.time() - SNAPSHOT_RETENTION_DAYS * 86400
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)

    def rollback_compile_enrich_pass(self, pass_id: str) -> dict[str, Any]:
        """Roll back one compile-enrich pass's page writes (ТЗ 5.5 inv 8).

        Reads the pass's on-disk snapshot manifest -- independent of
        whether that pass is still ``self._active_pass`` -- and, under the
        vault write lock, restores or removes every page whose CURRENT
        fingerprint still matches the fingerprint recorded right after the
        pass wrote it. A page something else touched since then is left
        alone and reported in ``skipped`` (ТЗ: "если файл изменился после
        прохода, откат этого файла не выполняется").

        The return value always carries ``manifest_found``: whether a
        snapshot manifest file for ``pass_id`` exists on disk at all. This
        tells apart two situations that otherwise both restore/skip
        nothing: a ``pass_id`` with no manifest on disk (a made-up id, a
        typo, or a snapshot already cleaned up by
        ``_cleanup_old_pass_snapshots``) -- ``manifest_found`` is ``False``
        -- versus a pass whose manifest exists but genuinely has nothing to
        restore, e.g. the effectiveness-gate rollback this method's own
        caller (``run_nightly_maintenance``) triggers for a pass that took
        work but never touched a single page, so no manifest was ever
        written for it either; that case is still reported with
        ``manifest_found`` set to ``False`` here, because from this
        method's point of view it is indistinguishable from an unknown id
        -- callers that already know why they are rolling back (like
        ``run_nightly_maintenance``) do not need to consult the flag, only
        a caller for an arbitrary externally supplied id (the CLI) does.
        """
        manifest_found = self._pass_snapshot_manifest_path(pass_id).exists()
        manifest = self._read_pass_snapshot_manifest(pass_id)
        restored: list[str] = []
        skipped: list[str] = []
        if not manifest:
            return {
                "restored": restored,
                "skipped": skipped,
                "manifest_found": manifest_found,
            }

        with vault_write_lock(self.vault_path):
            for rel_path, entry in manifest.items():
                if not isinstance(entry, dict):
                    continue
                file_path = self.vault_path / rel_path
                try:
                    current_bytes = file_path.read_bytes()
                except FileNotFoundError:
                    current_bytes = None
                current_fingerprint = self._full_content_fingerprint(current_bytes)
                after_hex = entry.get("fingerprint_after")
                after_fingerprint = bytes.fromhex(after_hex) if after_hex else None
                if current_fingerprint != after_fingerprint:
                    skipped.append(rel_path)
                    continue
                if entry.get("existed"):
                    blob_path = self._pass_snapshot_blob_path(pass_id, rel_path)
                    try:
                        original_bytes = blob_path.read_bytes()
                    except FileNotFoundError:
                        skipped.append(rel_path)
                        continue
                    _atomic_write_bytes(file_path, original_bytes)
                elif file_path.exists():
                    file_path.unlink()
                restored.append(rel_path)
        return {
            "restored": sorted(restored),
            "skipped": sorted(skipped),
            "manifest_found": True,
        }

    def _write_pass_journal(
        self,
        *,
        pass_id: str,
        started_at: str,
        status: str,
        error: str,
        rollback: dict[str, Any] | None = None,
    ) -> None:
        """Persist the ТЗ 5.2 step 6 pass journal to
        ``.session/compile-enrich.json`` (G6). Always called from
        ``run_nightly_maintenance``'s ``finally`` -- ТЗ 5.5 inv 6: the
        journal survives a rollback and survives the pass body raising.
        """
        pass_obj = self._active_pass
        payload = {
            "pass_id": pass_id,
            "started_at": started_at,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": status,
            "error": error,
            "sources_processed": list(pass_obj.sources_processed) if pass_obj else [],
            "touched_pages": sorted(pass_obj.touched_pages) if pass_obj else [],
            "model_calls_used": pass_obj.model_calls_used if pass_obj else 0,
            "verify_rejected": pass_obj.verify_rejected if pass_obj else 0,
            "verify_format_drift": (
                pass_obj.verify_format_drift if pass_obj else 0
            ),
            "trust_blocked": pass_obj.trust_blocked if pass_obj else 0,
            "conflicts_auto_resolved": (
                pass_obj.conflicts_auto_resolved if pass_obj else 0
            ),
            "queue_evictions": pass_obj.queue_evictions if pass_obj else 0,
            "budget_exhausted": (
                sorted(pass_obj.budget_exhausted) if pass_obj else []
            ),
            "human_zone_ambiguous_pages": (
                sorted(pass_obj.human_zone_ambiguous_pages) if pass_obj else []
            ),
            "rollback": rollback,
        }
        journal_path = self.vault_path / ".session" / "compile-enrich.json"
        _atomic_write_text(
            journal_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _run_model(self, prompt: str, *, timeout: int) -> str:
        """Single choke point for every model invocation (G1), gating the
        ТЗ 5.6 per-pass model-call budget around ``CliRunner.run``.

        With no active compile-enrich pass (``self._active_pass is None``),
        this is exactly the previous unlimited ``self.runner.run(...)``
        call -- only ``run_nightly_maintenance`` creates a pass, so a
        one-off manual refresh stays unbudgeted.
        """
        pass_obj = self._active_pass
        if (
            pass_obj is not None
            and pass_obj.model_calls_used >= MAX_MODEL_CALLS_PER_PASS
        ):
            pass_obj.budget_exhausted.add("model-calls-per-pass")
            raise CompiledBriefingPassBudgetExceededError(
                f"model-calls-per-pass budget exhausted ({MAX_MODEL_CALLS_PER_PASS})"
            )
        raw = self.runner.run(prompt, timeout=timeout)
        if pass_obj is not None:
            pass_obj.model_calls_used += 1
        return raw

    def _run_json_dict_prompt(
        self,
        *,
        prompt: str,
        timeout: int,
        error_context: str,
        json_example: str,
    ) -> dict[str, Any]:
        raw = self._run_model(prompt, timeout=timeout)
        try:
            return _strip_payload_surrogates(
                extract_first_json_dict(raw, error_context=error_context)
            )
        except ValueError as first_error:
            repaired = self._repair_json_dict_output(
                raw_output=raw,
                error_context=error_context,
                json_example=json_example,
            )
            if repaired is not None:
                return _strip_payload_surrogates(repaired)
            raise first_error

    def _repair_json_dict_output(
        self,
        *,
        raw_output: str,
        error_context: str,
        json_example: str,
    ) -> dict[str, Any] | None:
        source = str(raw_output or "").strip()
        if not source:
            return None

        repair_prompt = self._build_json_repair_prompt(
            raw_output=source,
            error_context=error_context,
            json_example=json_example,
        )
        try:
            repaired_raw = self._run_model(
                repair_prompt,
                timeout=JSON_REPAIR_TIMEOUT_SECONDS,
            )
        except (CliExecutionError, FileNotFoundError, TimeoutError):
            return None

        try:
            return extract_first_json_dict(repaired_raw, error_context=error_context)
        except ValueError:
            return None

    def _build_json_repair_prompt(
        self,
        *,
        raw_output: str,
        error_context: str,
        json_example: str,
    ) -> str:
        return (
            "You are repairing model output for a strict JSON-only runtime.\n"
            f"Return ONLY one valid JSON object for {error_context}.\n"
            "Do not add markdown fences, prose, or explanations.\n"
            "Do not invent facts that are not already present in the raw output.\n"
            "If the raw output is partial, salvage the structure conservatively.\n\n"
            "[EXPECTED_JSON_SHAPE]\n"
            f"{json_example}\n\n"
            "[RAW_OUTPUT]\n"
            f"{self._clip(raw_output, MAX_JSON_REPAIR_CHARS)}\n"
        )

    def _target_path(self, target: CompiledBriefingTarget) -> Path:
        existing_path = target.existing_path.strip()
        if existing_path.startswith("compiled/") and existing_path.endswith(".md"):
            candidate = (self.vault_path / existing_path).resolve()
            try:
                rel_path = candidate.relative_to(self.compiled_root)
            except ValueError:
                return self.compiled_root / target.domain / f"{target.slug}.md"
            if rel_path.parts and rel_path.parts[0] == "archive":
                return self.compiled_root / target.domain / f"{target.slug}.md"
            return candidate
        return self.compiled_root / target.domain / f"{target.slug}.md"

    def _semantic_resolve_target(
        self, target: CompiledBriefingTarget
    ) -> CompiledBriefingTarget | None:
        """Resolve stage 2 (ТЗ 5.2): semantic match against existing
        compiled/ pages in ``target.domain``. Only called by
        ``_upsert_briefing`` when stage 1 (``_target_path``, exact
        path/slug match) found no file on disk for this target.

        Returns a replacement target (new slug/path pointing at the
        matched page) when the best same-domain candidate clears the
        confident-match threshold; otherwise ``None``, and the caller
        keeps the original target so a new page gets created.

        Thin wrapper around ``_semantic_resolve_target_full`` -- kept as its
        own method (rather than inlined) because it is the one already
        covered by this module's own Resolve tests and reused directly by
        ``compiled_why``/other callers that only ever care about the
        same-page match, not the possible-duplicate signal below.
        """
        return self._semantic_resolve_target_full(target).target

    def _semantic_resolve_target_full(
        self, target: CompiledBriefingTarget
    ) -> SemanticResolveResult:
        """Full Resolve stage 2 outcome (ТЗ 5.2), including the
        possible-duplicate signal ``_semantic_resolve_target`` itself only
        logs and discards. See ``SemanticResolveResult`` for what each field
        means; ``_upsert_briefing`` is the only caller that needs the
        ``duplicate_candidate`` side of it, to queue an owner decision for
        the new page it is about to create.
        """
        if not any(
            candidate.domain == target.domain for candidate in self._iter_candidates()
        ):
            # Guard, not just an optimization: an empty domain means
            # recall() would still shell out to the real `qmd` binary
            # (subprocess, up to QMD_RECALL_TIMEOUT_SECONDS = 600s) for a
            # search that cannot possibly find anything.
            return SemanticResolveResult(target=None)

        query = target.title
        if target.description:
            query = f"{target.title}\n{target.description}"
        payload = self.qmd.recall(
            query,
            limit=RESOLVE_MAX_CANDIDATES_PER_SOURCE,
            raw=True,
        )

        # compiled/<domain>/ never nests under compiled/archive/ (archived
        # pages live at compiled/archive/<domain>/, see
        # _archive_candidate), so this one prefix check both scopes the
        # search to the target's domain and excludes the archive, per
        # ТЗ 5.2's "путь начинается с compiled/<домен>/ и не лежит в
        # архиве".
        domain_prefix = f"compiled/{target.domain}/"
        best_path = ""
        best_confidence = -1.0
        for item in payload.get("results", []):
            rel_path_hit = str(item.get("rel_path") or "")
            if not rel_path_hit.startswith(domain_prefix):
                continue
            confidence = float(item.get("confidence", 0.0) or 0.0)
            if confidence > best_confidence:
                best_confidence = confidence
                best_path = rel_path_hit

        if (
            not best_path
            or best_confidence < RESOLVE_POSSIBLE_DUPLICATE_CONFIDENCE_THRESHOLD
        ):
            return SemanticResolveResult(target=None)

        if best_confidence < RESOLVE_SAME_PAGE_CONFIDENCE_THRESHOLD:
            logger.info(
                "Compiled briefing Resolve: possible duplicate candidate "
                "target=%s/%s existing=%s confidence=%.4f (queued as a "
                "duplicate-candidate decision once the new page is written)",
                target.domain,
                target.slug,
                best_path,
                best_confidence,
            )
            if not (self.vault_path / best_path).exists():
                # Stale qmd index entry: a match was found, but there is no
                # existing page left to pair it with -- nothing to queue
                # (ТЗ: "в ветке «совпадение найдено, но файла нет» запись
                # не ставить").
                return SemanticResolveResult(target=None)
            return SemanticResolveResult(
                target=None,
                duplicate_candidate=best_path,
                duplicate_confidence=best_confidence,
            )

        if not (self.vault_path / best_path).exists():
            # Stale qmd index entry (page deleted/moved since the index was
            # last built) -- fall back to creating a new page instead of
            # crashing.
            return SemanticResolveResult(target=None)

        return SemanticResolveResult(
            target=replace(target, slug=Path(best_path).stem, existing_path=best_path)
        )

    def _queue_duplicate_candidate(
        self,
        *,
        page_rel_path: str,
        candidate_rel_path: str,
        confidence: float,
        existing_lock: VaultWriteLock,
    ) -> None:
        """Queue a newly created page and its possible-duplicate match as an
        owner decision (ТЗ 5.2 Resolve, possible-duplicate confidence zone).

        Must be called from inside ``_upsert_briefing``'s own
        ``vault_write_lock`` block, after the new page's write already
        succeeded, passing that same lock through as ``existing_lock``:
        ``append_decision_queue_entries`` never takes the vault lock itself
        (see its module docstring), and both pages must already exist on
        disk once the entry is queued.

        The import is deferred (function-local, not module-level):
        ``decisions_queue`` imports from this module at module scope
        (``CompiledBriefingCandidate``, ``CompiledBriefingService``,
        ``_atomic_write_text``), so importing it back at module scope here
        would be a circular import.
        """
        from d_brain.services.decisions_queue import append_decision_queue_entries

        summary = (
            f"Новая страница похожа на существующую ({candidate_rel_path}), "
            f"совпадение {confidence:.0%} — возможно, это дубль."
        )
        evicted = append_decision_queue_entries(
            self.vault_path,
            [
                {
                    "kind": "duplicate-candidate",
                    "page": page_rel_path,
                    "candidate_page": candidate_rel_path,
                    "summary": summary,
                    "since": date.today().isoformat(),
                }
            ],
            existing_lock=existing_lock,
        )
        self._record_queue_eviction(evicted)

    def _queue_monthly_drift(self, *, page_rel_path: str) -> None:
        """Queue a page that just hit ТЗ 5.6's monthly per-page enrichment
        cap (``MAX_ENRICHMENTS_PER_PAGE_PER_MONTH``) as an owner decision
        ("в очередь решений с пометкой о дрейфе").

        Same entry shape as ``_queue_duplicate_candidate``'s
        ``"duplicate-candidate"`` (``kind``, ``page``, ``summary``,
        ``since``) -- ТЗ names no extra fields for drift. Deduped by
        ``(kind, page)`` in ``append_decision_queue_entries``, so a page
        that keeps hitting the cap on every later pass this month produces
        only the one entry, not a new one per pass.

        Unlike ``_queue_duplicate_candidate``, called from a point in
        ``_upsert_briefing`` that is *not* already inside a
        ``vault_write_lock`` (the budget check runs before the page is
        compiled or written), so this opens its own, short-lived one --
        safe here because nothing on this call path holds the vault lock
        already (the surrounding queue drain is guarded by the separate
        ``_worker_lock``/``_state_lock`` files, not by this one).
        """
        from d_brain.services.decisions_queue import append_decision_queue_entries

        summary = (
            f"Страница обогащалась чаще {MAX_ENRICHMENTS_PER_PAGE_PER_MONTH} раз "
            "за календарный месяц — похоже на дрейф, нужна проверка владельца."
        )
        with vault_write_lock(self.vault_path) as lock:
            evicted = append_decision_queue_entries(
                self.vault_path,
                [
                    {
                        "kind": "drift",
                        "page": page_rel_path,
                        "summary": summary,
                        "since": date.today().isoformat(),
                    }
                ],
                existing_lock=lock,
            )
        self._record_queue_eviction(evicted)

    def _queue_verify_rejected(
        self,
        *,
        page_rel_path: str,
        source_rel_path: str = "",
        source_excerpt: str = "",
        max_updates: int = 3,
    ) -> None:
        """Queue a page whose Verify step has rejected a majority of its
        proposed claims on ``MAX_VERIFY_REJECTED_RETRIES`` consecutive
        attempts against the same source snapshot, as an owner decision
        (ТЗ 5.2 step 4 / 7.2: a source the verify step keeps rejecting
        "остаётся в очереди решений" -- until this fix, both call sites
        that catch ``CompiledBriefingVerificationRejectedError`` only
        logged a warning and, once the retry limit was hit, skipped the
        page forever with no trace for the owner).

        Mirrors ``_queue_monthly_drift``: called from a point that holds no
        vault lock yet, so it opens its own, short-lived one. Deduped by
        ``(kind, page)`` in ``append_decision_queue_entries``, so a page
        that keeps failing Verify on later passes (still the same exhausted
        source snapshot) only ever gets the one entry.
        """
        from d_brain.services.decisions_queue import append_decision_queue_entries

        summary = (
            f"Проверка утверждений отклонила страницу {MAX_VERIFY_REJECTED_RETRIES} "
            "раз(а) подряд по одному и тому же источнику — страница больше не "
            "обновляется, нужна проверка владельца."
        )
        with vault_write_lock(self.vault_path) as lock:
            evicted = append_decision_queue_entries(
                self.vault_path,
                [
                    {
                        "kind": "verify-rejected",
                        "page": page_rel_path,
                        "summary": summary,
                        "since": date.today().isoformat(),
                        "source_path": source_rel_path,
                        "source_excerpt": source_excerpt,
                        "max_updates": str(max_updates),
                    }
                ],
                existing_lock=lock,
            )
        self._record_queue_eviction(evicted)

    def _queue_undecided_conflict(
        self,
        *,
        page_rel_path: str,
        existing_claim: str,
        existing_source: str,
        new_claim: str,
        new_source: str,
    ) -> None:
        """Queue one conflict the adjudicator could not settle.

        This is a retry buffer, not a task for the owner. The pair is
        re-adjudicated by the nightly pass with the escalated prompt
        (``_adjudicate_conflict(attempt=2)``), which drops the "unclear"
        option entirely; the owner is never asked to arbitrate unless they
        open the queue screen themselves.

        Replaces the old ``blocked-action`` producer. That kind existed to
        explain why a date-based supersession did not happen automatically
        when the new source's trust was too weak -- trust no longer blocks
        anything, so the only thing left worth recording is "the model
        looked at this pair and did not reach a verdict".

        Mirrors ``_queue_verify_rejected``/``_queue_monthly_drift``: called
        from inside ``_apply_claims_and_conflicts``, a point that runs
        before ``_upsert_briefing`` takes its own vault write lock, so this
        opens its own, short-lived one rather than being handed a lock that
        does not exist yet at that call site.

        Deduped by ``(kind, page)`` in ``append_decision_queue_entries``, so
        a page carrying several undecided pairs gets one entry; the pairs
        themselves are all on the page's own Open Conflicts table, which is
        what the retry drain actually reads.
        """
        from d_brain.services.decisions_queue import (
            UNDECIDED_CONFLICT_KIND,
            append_decision_queue_entries,
        )

        summary = (
            "Модель не смогла решить, какая версия верна — обе оставлены на "
            f"странице до повторного разбора. Было: «{existing_claim}» "
            f"({existing_source}). Предлагалось: «{new_claim}» "
            f"({new_source})."
        )
        with vault_write_lock(self.vault_path) as lock:
            evicted = append_decision_queue_entries(
                self.vault_path,
                [
                    {
                        "kind": UNDECIDED_CONFLICT_KIND,
                        "page": page_rel_path,
                        "summary": summary,
                        "since": date.today().isoformat(),
                    }
                ],
                existing_lock=lock,
            )
        self._record_queue_eviction(evicted)

    def _record_queue_eviction(self, evicted: int) -> None:
        """Fold a decisions-queue eviction count into the active pass's
        journal (ТЗ 7.2: "факт вытеснения попадает в дайджест"), mirroring
        how ``budget_exhausted`` already reaches the owner via
        ``_write_pass_journal``/``compiled_enrich_report.py``. A no-op
        outside an active pass (a one-off manual call has no pass journal
        to record into, same as every other ``_active_pass is not None``
        guard in this module) or when nothing was evicted.
        """
        if evicted and self._active_pass is not None:
            self._active_pass.queue_evictions += evicted

    def _record_human_zone_ambiguous(self, rel_path: str) -> None:
        """Surface one page's ambiguous human-zone markers to the owner.

        Two destinations, on purpose. The pass journal (mirroring
        ``_record_queue_eviction`` above) is a no-op outside an active pass
        -- and ``_active_pass`` is set nowhere but
        ``run_nightly_maintenance``, so on this method's most common caller
        by far, the background drain, it recorded nothing at all. That was
        the whole owner-facing signal for a page the drain then *released
        for a free retry every 300s with ``attempts`` unchanged* (see
        ``_drain_queue_once``): the page retried forever and the owner was
        never told to go fix the markers. So the decisions queue gets the
        entry too, exactly as ``_queue_undecodable_page`` next door already
        does for the same reason.

        Every call site of this method (the ``HumanZoneMarkerError`` branch
        in ``_upsert_briefing``, ``_record_non_enrichment_source``, and
        ``_compress_cooled_pages``) is reached *before* that path takes its
        own ``vault_write_lock``, so ``_queue_human_zone_ambiguous`` can
        safely open one of its own rather than take an ``existing_lock``.
        """
        if self._active_pass is not None:
            self._active_pass.human_zone_ambiguous_pages.add(rel_path)
        self._queue_human_zone_ambiguous(rel_path)

    def _queue_human_zone_ambiguous(self, rel_path: str) -> None:
        """Queue a page with unresolvable <!-- human:start/end --> markers
        as an owner decision -- see ``_record_human_zone_ambiguous``, the
        only caller, for why the pass journal alone was not enough.

        Deduped by ``(kind, page)`` in ``append_decision_queue_entries``, so
        a page that stays broken across many retries only ever gets the one
        entry.
        """
        from d_brain.services.decisions_queue import (
            HUMAN_ZONE_AMBIGUOUS_KIND,
            append_decision_queue_entries,
        )

        summary = (
            "Маркеры личной зоны <!-- human:start --> / <!-- human:end --> "
            "на странице не складываются в один участок (обычно так выходит "
            "после копирования блока). Пока непонятно, какой текст твой, "
            "страница не обогащается и не сжимается — поправь маркеры "
            "вручную."
        )
        with vault_write_lock(self.vault_path) as lock:
            evicted = append_decision_queue_entries(
                self.vault_path,
                [
                    {
                        "kind": HUMAN_ZONE_AMBIGUOUS_KIND,
                        "page": rel_path,
                        "summary": summary,
                        "since": date.today().isoformat(),
                    }
                ],
                existing_lock=lock,
            )
        self._record_queue_eviction(evicted)

    def _queue_undecodable_page(
        self, rel_path: str, *, existing_lock: Any = None
    ) -> None:
        """Queue a page whose bytes on disk are not valid UTF-8 as an owner
        decision.

        Deliberately the decisions queue and not the pass journal, unlike
        ``_record_human_zone_ambiguous`` next to it: the journal is written
        by ``run_nightly_maintenance``, and ``_active_pass`` is set nowhere
        else, so anything recorded there from ``_upsert_briefing`` is a
        silent no-op on that method's most common caller by far -- the
        background drain (``spawn_background_drain`` ->
        ``run_compiled_maintenance --queue-only`` -> ``run_queue_worker``),
        which runs after every captured note, document, and web archive.
        That path would retry the same undecodable page three times, drop
        the refresh event for good, and tell the owner nothing. The queue
        does not depend on a pass being open, so it reaches the owner from
        either caller.

        Mirrors ``_queue_verify_rejected``/``_queue_blocked_action``, with
        one difference: ``_compress_cooled_pages`` calls this while already
        holding the vault write lock, so the lock is passed through instead
        of being taken twice. Deduped by ``(kind, page)`` in
        ``append_decision_queue_entries``, so a page that stays broken over
        many passes only ever gets one entry.
        """
        from d_brain.services.decisions_queue import (
            PAGE_ENCODING_BROKEN_KIND,
            append_decision_queue_entries,
        )

        summary = (
            "В файле страницы есть байты, не читаемые как UTF-8 (обычно так "
            "выглядит текст, сохранённый редактором в другой кодировке). "
            "Переписать такую страницу значит затереть эти байты, поэтому "
            "она не обновляется и не сжимается, пока кодировка файла не "
            "исправлена."
        )
        entry = [
            {
                "kind": PAGE_ENCODING_BROKEN_KIND,
                "page": rel_path,
                "summary": summary,
                "since": date.today().isoformat(),
            }
        ]
        if existing_lock is not None:
            evicted = append_decision_queue_entries(
                self.vault_path, entry, existing_lock=existing_lock
            )
        else:
            with vault_write_lock(self.vault_path) as lock:
                evicted = append_decision_queue_entries(
                    self.vault_path, entry, existing_lock=lock
                )
        self._record_queue_eviction(evicted)

    def _build_impact_prompt(
        self,
        *,
        source_rel_path: str,
        source_excerpt: str,
        signal: dict[str, Any] | None,
        catalog: list[dict[str, str]],
        max_updates: int,
    ) -> str:
        return (
            "You maintain an LLM-owned compiled markdown knowledge base for a "
            "personal assistant.\n"
            "Output metadata in "
            f"{prompt_language_name(self.content_language)} when natural.\n"
            "Return ONLY JSON.\n\n"
            "Decide whether one changed source note should refresh 0 to "
            f"{max_updates} compiled briefings.\n"
            "One source note may contain multiple unrelated durable threads.\n"
            "This is especially common in daily notes that bundle several meetings, "
            "incidents, decisions, and project updates in one file.\n"
            "First identify the durable threads inside the source, then map only the "
            "strongest recurring threads to compiled briefing updates.\n"
            "Allowed domains and intent:\n"
            + "\n".join(
                f"- {domain}: {hint}" for domain, hint in DOMAIN_HINTS.items()
            )
            + "\n\nRules:\n"
            "- Prefer updating an existing compiled note when clearly appropriate.\n"
            "- Create a new note only for durable entities or threads likely to "
            "matter again.\n"
            "- Do not create notes for one-off trivial mentions.\n"
            "- Decisions are only for consequential decisions or explicit "
            "commitments.\n"
            "- Meetings are only for recurring series, strategic negotiations, or "
            "threads with ongoing context.\n"
            "- Concepts must stay portable. If a candidate title carries a date, or "
            "names one specific project, client, or person (the catalog below lists "
            "the existing ones), it is a topic, not a concept -- you make that call, "
            "nothing downstream reroutes it for you.\n"
            "- If the source is mixed, split it mentally into 1-6 durable threads "
            "before deciding updates.\n"
            "- Prefer 0-2 strong updates over many weak updates when one daily note "
            "covers many topics.\n"
            "- Return an empty updates list if the source is too weak or too noisy, "
            "but STILL return a valid JSON object.\n"
            "- Use concise stable titles.\n"
            "- Slug must be lowercase kebab-case ASCII.\n\n"
            "Return JSON exactly like:\n"
            "{\n"
            '  "source_shape": "single|mixed|noisy",\n'
            '  "durable_threads": [\n'
            "    {\n"
            '      "label": "short thread label",\n'
            '      "why": "why this thread is durable or recurring"\n'
            "    }\n"
            "  ],\n"
            '  "updates": [\n'
            "    {\n"
            '      "domain": "projects|people|topics|decisions|meetings|concepts",\n'
            '      "title": "brief title",\n'
            '      "slug": "brief-slug",\n'
            '      "description": "one-line search snippet",\n'
            '      "reason": "why this briefing should refresh",\n'
            '      "existing_path": "compiled/<domain>/<slug>.md or empty"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Mixed daily-note example:\n"
            "{\n"
            '  "source_shape": "mixed",\n'
            '  "durable_threads": [\n'
            '    {"label": "db incident recovery", "why": "operational risk '
            'likely to matter again"},\n'
            '    {"label": "partner negotiation boundary", "why": "decision '
            'with ongoing consequences"}\n'
            "  ],\n"
            '  "updates": [\n'
            "    {\n"
            '      "domain": "projects",\n'
            '      "title": "Migration Incident Risk",\n'
            '      "slug": "migration-incident-risk",\n'
            '      "description": "Operational incident and recovery risk in '
            'migration track",\n'
            '      "reason": "Daily note contains a durable incident thread that '
            'changes project risk",\n'
            '      "existing_path": "compiled/projects/migration-incident-risk.md"\n'
            "    },\n"
            "    {\n"
            '      "domain": "decisions",\n'
            '      "title": "No self-estimates for partner options",\n'
            '      "slug": "no-self-estimates-for-partner-options",\n'
            '      "description": "Decision boundary on who owns estimates and '
            'commitments",\n'
            '      "reason": "Daily note records an explicit durable decision",\n'
            '      "existing_path": ""\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "If you are unsure, output:\n"
            '{ "source_shape": "noisy", "durable_threads": [], "updates": [] }\n\n'
            "[EXISTING_COMPILED_CATALOG]\n"
            f"{json.dumps(catalog, ensure_ascii=False, indent=2)}\n\n"
            "[SOURCE_PATH]\n"
            f"{source_rel_path}\n\n"
            "[SOURCE_MEMORY_SIGNAL]\n"
            f"{json.dumps(signal or {}, ensure_ascii=False, indent=2)}\n\n"
            "[SOURCE_EXCERPT]\n"
            f"{source_excerpt}\n"
        )

    def _build_compile_prompt(
        self,
        *,
        target: CompiledBriefingTarget,
        source_rel_path: str,
        source_excerpt: str,
        signal: dict[str, Any] | None,
        existing_text: str,
    ) -> str:
        return (
            "Maintain one compiled briefing for a personal assistant knowledge base.\n"
            "Write all generated text in "
            f"{prompt_language_name(self.content_language)}.\n"
            "Return ONLY JSON.\n\n"
            "You are given the current briefing markdown, if any, and one changed "
            "source note.\n"
            "Produce a concise, durable operational briefing. Keep stable facts, "
            "incorporate new evidence, and avoid invention.\n\n"
            "Required JSON schema:\n"
            "{\n"
            '  "description": "one-line snippet",\n'
            '  "status": "active|draft|pending|done|inactive",\n'
            '  "freshness_state": "fresh|watch|stale",\n'
            '  "confidence": "high|medium|low",\n'
            '  "current_state": "short paragraph",\n'
            '  "recent_changes": ["bullet", "..."],\n'
            '  "open_loops": ["bullet", "..."],\n'
            '  "key_decisions": ["bullet", "..."],\n'
            '  "record_kind": "decision|incident|briefing",\n'
            '  "decision_status": "proposed|accepted|rejected|superseded",\n'
            '  "decision_owner": "owner or empty",\n'
            '  "decision_date": "YYYY-MM-DD or empty",\n'
            '  "rationale": "decision rationale or empty",\n'
            '  "alternatives_considered": ["bullet", "..."],\n'
            '  "supersedes": ["decision identifier", "..."],\n'
            '  "superseded_by": "decision identifier or empty",\n'
            '  "decision_evidence": ["vault/relative/path.md", "..."],\n'
            '  "incident_date": "YYYY-MM-DD or empty",\n'
            '  "severity": "low|medium|high|critical or empty",\n'
            '  "timeline": ["timestamped event", "..."],\n'
            '  "root_cause": "root cause or empty",\n'
            '  "what_worked": ["bullet", "..."],\n'
            '  "what_did_not_work": ["bullet", "..."],\n'
            '  "corrective_actions": ["bullet", "..."],\n'
            '  "generalizable_learning": "reusable learning or empty",\n'
            '  "next_check": "short line",\n'
            '  "source_links": ["vault/relative/path.md", "..."],\n'
            '  "claims": [\n'
            "    {\n"
            '      "text": "utterance to record as a durable claim",\n'
            '      "kind": "fact|opinion|commitment"\n'
            "    }\n"
            "  ],\n"
            '  "conflicts": [\n'
            "    {\n"
            '      "existing_claim": "text already on this page",\n'
            '      "existing_source": "vault/relative/path.md of the existing '
            'claim",\n'
            '      "new_claim": "text from claims above, verbatim",\n'
            '      "type": "temporal|factual|contextual",\n'
            '      "context_note": "for type=contextual, how the two claims\' '
            'scopes differ, or empty"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Rules:\n"
            "- Be specific, short, and cumulative.\n"
            "- Do not claim certainty beyond the available evidence.\n"
            "- Preserve older still-valid context from the existing note.\n"
            "- Rewrite current_state as the concise current truth; do not append "
            "a second summary or repeat the same fact in several sections.\n"
            "- recent_changes contains only real changes of state from the changed "
            "source, not every extracted fact. Do not prefix bullets with a date; "
            "the renderer adds the source date.\n"
            "- Prefer 2-5 bullets per list. Empty lists are allowed when truly "
            "absent.\n"
            "- source_links must contain only vault-relative paths.\n"
            "- Include only sources that materially support content kept on the "
            "page. A merely inspected source does not belong in source_links.\n"
            "- If sources or speakers give incompatible dates, owners, statuses, "
            "or outcomes, do not silently choose one. Lower confidence, preserve "
            "the uncertainty, and add a focused open loop or a conflict when the "
            "conflict schema permits it. confidence=high is forbidden while such "
            "uncertainty remains.\n"
            "- For the decisions domain, choose record_kind decision for an ADR "
            "or incident for an operational debrief and fill the matching fields.\n"
            "- For a decision, key_decisions must state the decision named by the "
            "target title; distinguish proposed, accepted, and completed. Keep an "
            "owner unknown unless the source explicitly assigns that person for "
            "this decision and scope.\n"
            "- alternatives_considered contains only alternatives explicitly "
            "discussed in the changed source or preserved from an accepted existing "
            "decision. Never invent a plausible alternative.\n"
            "- Keep open_loops focused on this target; omit adjacent project work.\n"
            "- Accepted decisions are immutable. Preserve their decision, owner, "
            "date, rationale, and alternatives unless a new decision explicitly "
            "supersedes them; then set decision_status=superseded and "
            "superseded_by.\n"
            "- Incident debriefs should capture timeline, root cause, what worked, "
            "what did not work, corrective actions, and generalizable learning.\n"
            "- claims are individual facts, opinions, or commitments taken from "
            "the changed source excerpt only -- never from the existing briefing "
            "or from other compiled/summary pages.\n"
            f"- Extract 0-{MAX_CLAIMS_PER_PASS} claims. Every substantive new or "
            "changed fact, decision, commitment, owner, date, status, outcome, "
            "rationale, alternative, and open loop kept anywhere in the candidate "
            "page must be represented in claims. Return an empty claims list only "
            "when the source adds no substantive page content.\n"
            "- A conflict's new_claim must be copied verbatim from one entry in "
            "claims above; do not describe a conflict for text you did not also "
            "add to claims.\n"
            "- A conflict's existing_claim/existing_source must be copied "
            "verbatim from one entry in EXISTING_CLAIMS below; do not invent an "
            "existing claim that is not listed there.\n"
            "- Only propose a conflict when the new claim actually contradicts an "
            "existing one; do not restate agreement as a conflict.\n"
            "- For a type=contextual conflict, fill context_note with a short "
            "explanation of how the existing and new claims' scopes differ (e.g. "
            "different regions, time windows, or audiences) so both can stay on "
            "the page without looking like a plain contradiction; leave it empty "
            "if you cannot articulate the difference.\n"
            "- Trust is not yours to decide: never include a trust or confidence "
            "level on a claim or conflict.\n"
            "- Do not use markdown fences.\n\n"
            "[TARGET]\n"
            f"{json.dumps(asdict(target), ensure_ascii=False, indent=2)}\n\n"
            "[SOURCE_PATH]\n"
            f"{source_rel_path}\n\n"
            "[SOURCE_MEMORY_SIGNAL]\n"
            f"{json.dumps(signal or {}, ensure_ascii=False, indent=2)}\n\n"
            "[EXISTING_BRIEFING_MARKDOWN]\n"
            f"{self._clip(existing_text, MAX_EXISTING_NOTE_CHARS) or '(none)'}\n\n"
            "[EXISTING_CLAIMS]\n"
            f"{self._existing_claims_catalog(existing_text)}\n\n"
            "[CHANGED_SOURCE_EXCERPT]\n"
            f"{source_excerpt}\n"
        )

    def _render_briefing(
        self,
        *,
        target: CompiledBriefingTarget,
        payload: dict[str, Any],
        source_rel_path: str,
        existing_text: str,
        existing_meta: dict[str, str],
        signal: dict[str, Any] | None,
        source_excerpt: str = "",
        claims: list[dict[str, str]] | None = None,
        conflicts: list[dict[str, str]] | None = None,
        record_side_effects: bool = True,
    ) -> str:
        claims = claims or []
        conflicts = conflicts or []

        # Extract (and validate) the human zone before anything else: a
        # malformed marker pair must raise before the vault lock is taken, so
        # a bad page is skipped rather than partially rewritten. Passed the
        # full ``existing_text`` (frontmatter included), not just the body --
        # see _extract_human_zone / human_zone_populated below for why.
        human_zone = self._extract_human_zone(existing_text)
        human_zone_inner = human_zone[
            len(HUMAN_ZONE_START) : len(human_zone) - len(HUMAN_ZONE_END)
        ].strip()
        human_zone_tokens = (
            self._tokens(human_zone_inner) if human_zone_inner else set()
        )
        # Code review defect 1: sticky once true (see _HUMAN_ZONE_POPULATED_RE
        # / human_zone_markers_look_corrupted) -- sets the frontmatter flag
        # the first time this page's zone holds real text, and keeps it set
        # on every later render even if the owner clears their notes again,
        # so a future corruption that also destroys the "## Owner Notes"
        # heading is still caught.
        human_zone_populated = bool(human_zone_inner) or (
            existing_meta.get("human_zone_populated") == "true"
        )

        # A claim that just restates the owner's own human-zone text must
        # never be re-added as "new" machine-zone content (same rule as
        # recent_changes/open_loops/etc. below). Any conflict pointing at a
        # dropped claim becomes moot and is dropped with it.
        if claims and human_zone_tokens:
            kept_claim_texts = set(
                self._filter_list_duplicating_human_zone(
                    [claim["text"] for claim in claims], human_zone_tokens
                )
            )
            claims = [claim for claim in claims if claim["text"] in kept_claim_texts]
            conflicts = [
                conflict
                for conflict in conflicts
                if conflict["new_claim"] in kept_claim_texts
            ]

        today = date.today().isoformat()
        compiled_at = datetime.now().astimezone().isoformat(timespec="seconds")
        # Needed below only to queue a "blocked-action" owner decision when
        # ``_apply_claims_and_conflicts`` blocks a low-trust supersession
        # (ТЗ 4.4/7.2) -- computed once here rather than inside that method,
        # which otherwise has no notion of ``target``/the page's own path.
        page_rel_path = (
            self._target_path(target).relative_to(self.vault_path).as_posix()
        )
        description = self._clean_line(payload.get("description")) or target.description
        status = str(payload.get("status") or existing_meta.get("status") or "active")
        if status not in STATUS_VALUES:
            status = "active"
        freshness_state = str(
            payload.get("freshness_state")
            or existing_meta.get("freshness_state")
            or "watch"
        )
        if freshness_state not in FRESHNESS_VALUES:
            freshness_state = "watch"
        confidence = str(
            payload.get("confidence")
            or existing_meta.get("confidence")
            or "medium"
        )
        if confidence not in CONFIDENCE_VALUES:
            confidence = "medium"
        quality_status = existing_meta.get("quality_status", "").strip()
        quality_reason = existing_meta.get("quality_reason", "").strip()
        if payload.get("_quality_verification_completed") is True:
            quality_issues = payload.get("_quality_issues")
            if isinstance(quality_issues, list) and quality_issues:
                quality_status = "needs_review"
                quality_reason = "; ".join(
                    str(issue).strip()
                    for issue in quality_issues
                    if str(issue).strip()
                )
            else:
                quality_status = ""
                quality_reason = ""

        current_state = self._paragraph(payload.get("current_state"))
        def without_leading_date(items: list[str]) -> list[str]:
            cleaned = [
                re.sub(r"^\d{4}-\d{2}-\d{2}:\s*", "", item).strip()
                for item in items
            ]
            return [item for item in cleaned if item]

        recent_changes = without_leading_date(
            self._normalize_list(payload.get("recent_changes"))
        )
        open_loops = without_leading_date(
            self._normalize_list(payload.get("open_loops"))
        )
        # Filtered here, before any accepted-decision restoration below can
        # overwrite it with immutable existing-page content: the duplicate
        # filter must only ever see freshly generated model output, never
        # text that was already on the page (see the accepted-decision
        # branch further down).
        key_decisions = self._filter_list_duplicating_human_zone(
            self._normalize_list(payload.get("key_decisions")), human_zone_tokens
        )
        # The one _clean_line field rendered as a bare line of its own (see
        # "## Next Check" below), so unlike every other one it can forge a
        # heading -- and the forgery would sit above the real headings that
        # follow it, which is what the section readers find first.
        next_check = _defuse_embedded_headings(
            self._clean_line(payload.get("next_check"))
        )

        record_kind = ""
        decision_status = ""
        decision_owner = ""
        decision_date = ""
        rationale = ""
        alternatives: list[str] = []
        supersedes: list[str] = []
        superseded_by = ""
        decision_evidence: list[str] = []
        incident_date = ""
        severity = ""
        timeline: list[str] = []
        root_cause = ""
        what_worked: list[str] = []
        what_did_not_work: list[str] = []
        corrective_actions: list[str] = []
        generalizable_learning = ""
        if target.domain == "decisions":
            existing_record_kind = self._metadata_value(
                existing_meta.get("record_kind")
            )
            existing_decision_status = self._metadata_value(
                existing_meta.get("decision_status")
            )
            record_kind = self._metadata_value(
                payload.get("record_kind")
                or existing_record_kind
                or "decision"
            )
            if (
                existing_record_kind == "decision"
                and existing_decision_status == "accepted"
            ):
                record_kind = "decision"
            if record_kind not in RECORD_KIND_VALUES:
                record_kind = "decision"
            if record_kind == "decision":
                decision_status = self._metadata_value(
                    payload.get("decision_status")
                    or existing_meta.get("decision_status")
                    or "proposed"
                )
                if decision_status not in DECISION_STATUS_VALUES:
                    decision_status = "proposed"
                decision_owner = self._metadata_value(
                    payload.get("decision_owner")
                    or existing_meta.get("decision_owner")
                )
                decision_date = self._validated_date_field(
                    payload.get("decision_date")
                    or existing_meta.get("decision_date"),
                    field="decision_date",
                    page_path=f"compiled/{target.domain}/{target.slug}.md",
                )
                # Same reasoning as key_decisions above: filter the fresh
                # model output now, before the accepted-decision branch
                # below can replace it with immutable existing-page text.
                rationale = self._filter_text_duplicating_human_zone(
                    self._paragraph(payload.get("rationale")), human_zone_tokens
                )
                alternatives = self._filter_list_duplicating_human_zone(
                    self._normalize_list(payload.get("alternatives_considered")),
                    human_zone_tokens,
                )
                supersedes = self._normalize_list(payload.get("supersedes"))
                superseded_by = self._clean_line(payload.get("superseded_by"))
                decision_evidence = self._normalize_paths(
                    payload.get("decision_evidence")
                )

                explicit_supersession = (
                    decision_status == "superseded" and bool(superseded_by)
                )
                if existing_decision_status == "accepted" and not explicit_supersession:
                    decision_status = "accepted"
                    decision_owner = self._metadata_value(
                        existing_meta.get("decision_owner")
                    ) or decision_owner
                    decision_date = self._validated_date_field(
                        existing_meta.get("decision_date"),
                        field="decision_date",
                        page_path=f"compiled/{target.domain}/{target.slug}.md",
                    ) or decision_date
                    rationale = (
                        self._section_text(existing_text, "Rationale") or rationale
                    )
                    alternatives = self._section_bullets(
                        existing_text,
                        "Alternatives Considered",
                    ) or alternatives
                    key_decisions = self._section_bullets(
                        existing_text,
                        "Key Decisions",
                    ) or key_decisions
                if decision_status == "accepted" and not key_decisions:
                    key_decisions = [target.title]
            elif record_kind == "incident":
                incident_date = self._validated_date_field(
                    payload.get("incident_date")
                    or existing_meta.get("incident_date"),
                    field="incident_date",
                    page_path=f"compiled/{target.domain}/{target.slug}.md",
                )
                severity = self._metadata_value(
                    payload.get("severity") or existing_meta.get("severity")
                )
                if severity and severity not in INCIDENT_SEVERITY_VALUES:
                    logger.warning(
                        "Compiled briefing %s has invalid severity value %r; "
                        "clearing it",
                        f"compiled/{target.domain}/{target.slug}.md",
                        severity,
                    )
                    severity = ""
                timeline = self._normalize_list(payload.get("timeline"))
                root_cause = self._paragraph(payload.get("root_cause"))
                what_worked = self._normalize_list(payload.get("what_worked"))
                what_did_not_work = self._normalize_list(
                    payload.get("what_did_not_work")
                )
                corrective_actions = self._normalize_list(
                    payload.get("corrective_actions")
                )
                generalizable_learning = self._paragraph(
                    payload.get("generalizable_learning")
                )

        # Drop any freshly generated content that just restates the human
        # zone (>=80% token overlap) so the owner never sees their own note
        # echoed back as a "machine" bullet. key_decisions/rationale/
        # alternatives are filtered earlier, before the accepted-decision
        # branch can replace them with immutable existing-page text (see
        # above) -- filtering them again here would wrongly apply the
        # duplicate check to that inherited text.
        recent_changes = self._filter_list_duplicating_human_zone(
            recent_changes, human_zone_tokens
        )
        open_loops = self._filter_list_duplicating_human_zone(
            open_loops, human_zone_tokens
        )

        # ТЗ 6.3: Recent Changes / Open Loops accumulate across compile
        # passes (like "Sources That Shaped This Page" below), instead of
        # being overwritten with only this pass's items -- the ТЗ 6.3
        # invariant ("last 5 items, the rest to History") only makes sense
        # against an accumulated history. Rendering here stays unbounded on
        # purpose: capping to RECENT_CHANGES_KEEP / dropping stale Open
        # Loops is the dedicated nightly compression step's job
        # (``_compress_cooled_pages``), which only runs for warm/cold/
        # archive tiers -- a `core`/`active` page is expected to keep
        # growing while it stays hot.
        source_date_value = self.qmd._record_date_for_rel_path(
            source_rel_path, signal
        )
        source_date = (
            source_date_value.isoformat() if source_date_value is not None else today
        )
        recent_changes_rows = self._dated_rows(
            existing_text,
            "Recent Changes",
            empty_placeholder="No recent changes captured yet.",
        )
        existing_recent_pairs = {
            (row_text, row_source) for _, row_text, row_source in recent_changes_rows
        }
        for item in recent_changes:
            pair = (item, source_rel_path)
            if pair not in existing_recent_pairs:
                recent_changes_rows.append((source_date, item, source_rel_path))
                existing_recent_pairs.add(pair)

        open_loops_rows = self._dated_rows(
            existing_text,
            "Open Loops",
            empty_placeholder="No open loops captured yet.",
        )
        existing_open_pairs = {
            (row_text, row_source) for _, row_text, row_source in open_loops_rows
        }
        for item in open_loops:
            pair = (item, source_rel_path)
            if pair not in existing_open_pairs:
                open_loops_rows.append((source_date, item, source_rel_path))
                existing_open_pairs.add(pair)

        # History (ТЗ 6.3) is only ever written by ``_compress_candidate_text``
        # -- a normal compile pass just carries any existing rows forward
        # unchanged, the same way it carries forward claim history/open
        # conflicts below.
        history_rows = self._dated_rows(
            existing_text, "History", empty_placeholder="(nothing archived yet)"
        )

        timeline = self._filter_list_duplicating_human_zone(
            timeline, human_zone_tokens
        )
        root_cause = self._filter_text_duplicating_human_zone(
            root_cause, human_zone_tokens
        )
        what_worked = self._filter_list_duplicating_human_zone(
            what_worked, human_zone_tokens
        )
        what_did_not_work = self._filter_list_duplicating_human_zone(
            what_did_not_work, human_zone_tokens
        )
        corrective_actions = self._filter_list_duplicating_human_zone(
            corrective_actions, human_zone_tokens
        )
        generalizable_learning = self._filter_text_duplicating_human_zone(
            generalizable_learning, human_zone_tokens
        )

        old_sources = self._source_links_from_note(existing_text)
        new_sources = self._normalize_paths(payload.get("source_links"))
        all_sources = self._merge_paths(old_sources, [source_rel_path, *new_sources])
        created = existing_meta.get("created") or today
        relevance = self._merged_relevance(existing_meta, signal)
        tier = self._merged_tier(existing_meta, signal)

        # "Sources That Shaped This Page" accumulates one row per (date,
        # source) pair across compile passes; rows are only ever appended,
        # never re-sorted or dropped, and duplicates for the same day are
        # skipped as a hygiene measure. What-added is a heuristic fallback
        # for passes without claims (first recent_changes bullet, else
        # description, else target reason); when claims are present, each
        # verified claim becomes its own row instead (see
        # _apply_claims_and_conflicts).
        shaped_rows = self._sources_shaped_rows(existing_text)
        claim_history_rows = self._claim_history_rows(existing_text)
        open_conflict_rows_before = self._open_conflicts_rows(existing_text)
        open_conflict_rows = open_conflict_rows_before
        if claims:
            shaped_rows, claim_history_rows, open_conflict_rows = (
                self._apply_claims_and_conflicts(
                    claims=claims,
                    conflicts=conflicts,
                    shaped_rows=shaped_rows,
                    claim_history_rows=claim_history_rows,
                    open_conflict_rows=open_conflict_rows,
                    source_rel_path=source_rel_path,
                    source_excerpt=source_excerpt,
                    signal=signal,
                    today=today,
                    page_rel_path=page_rel_path,
                    page_state=self._section_text(existing_text, "Current State"),
                    record_side_effects=record_side_effects,
                )
            )
        else:
            what_added = (
                (recent_changes[0] if recent_changes else "")
                or description
                or target.reason
                or "(not captured)"
            )
            if what_added == NOT_ENRICHMENT_SOURCE_MARKER:
                # Cheap guard: this row is a real enrichment, so its text
                # must never collide with the sentinel
                # NOT_ENRICHMENT_SOURCE_MARKER used to mark a non-enrichment
                # row -- free-form model output landing on that exact
                # string would otherwise let this row silently escape the
                # monthly enrichment budget filter below.
                what_added += "."
            existing_shaped_pairs = {(row[0], row[1]) for row in shaped_rows}
            if (source_date, source_rel_path) not in existing_shaped_pairs:
                shaped_rows = [
                    *shaped_rows,
                    (source_date, source_rel_path, what_added),
                ]
        new_open_conflicts = len(open_conflict_rows) - len(open_conflict_rows_before)

        # Provenance/trust frontmatter fields (profile `derived`, optional --
        # not added to vault-manifest.json / KNOWN_FRONTMATTER_PROFILES
        # because `frontmatter_required` there is shared with summaries/ and
        # has no notion of optional fields; existing compiled pages simply
        # lack these fields until their next enrichment):
        #   sources_trust    - own|forwarded|integration|inferred; minimum
        #                       trust level among the page's sources. Set by
        #                       code, never by the model. Only recomputed
        #                       for passes that add claims (see below) --
        #                       a compile pass without claims leaves the
        #                       page's accumulated minimum untouched.
        #   last_verified    - YYYY-MM-DD or empty (renders as YAML null);
        #                       when the page's claims were last confirmed.
        #   enrichment_count - how many times this page has been enriched;
        #                       always existing value + 1, since _render_briefing
        #                       rebuilds frontmatter from scratch each pass.
        #   conflicts_open   - number of unresolved factual conflicts;
        #                       code-maintained counter, incremented by the
        #                       factual conflicts newly opened this pass.
        #   human_reviewed   - YYYY-MM-DD or empty (renders as YAML null);
        #                       when the owner last confirmed this page.
        #   human_zone_populated - present (``true``) once the owner's
        #                       ``<!-- human:start/end -->`` zone has ever
        #                       held real text; omitted otherwise. Sticky --
        #                       never cleared once set (see the
        #                       ``human_zone_populated`` local computed
        #                       above and ``human_zone_markers_look_corrupted``,
        #                       code review defect 1).
        existing_sources_trust = existing_meta.get("sources_trust", "").strip()
        if claims:
            current_trust = self._source_trust_level(source_rel_path, source_excerpt)
            # No prior accumulated value yet -- the current pass's trust
            # *is* the page's trust, not the minimum against the
            # "no data yet" placeholder (which would otherwise permanently
            # floor every new page's first claims-bearing pass at
            # DEFAULT_SOURCES_TRUST regardless of the real source).
            if existing_sources_trust in SOURCES_TRUST_VALUES:
                sources_trust = self._min_trust(existing_sources_trust, current_trust)
            else:
                sources_trust = current_trust
        elif existing_sources_trust in SOURCES_TRUST_VALUES:
            sources_trust = existing_sources_trust
        else:
            sources_trust = DEFAULT_SOURCES_TRUST
        page_path = f"compiled/{target.domain}/{target.slug}.md"
        last_verified = self._validated_date_field(
            existing_meta.get("last_verified"),
            field="last_verified",
            page_path=page_path,
        )
        if claims and quality_status != "needs_review":
            last_verified = today
        enrichment_count = (
            self._int_value(existing_meta.get("enrichment_count"), default=0) + 1
        )
        # The delta is negative when a pass closes more conflicts than it
        # opens (a superseded side leaves the ledger, see
        # _apply_claims_and_conflicts); a page that somehow starts with a
        # counter lower than its table must still never go below zero.
        conflicts_open = max(
            0,
            self._int_value(existing_meta.get("conflicts_open"), default=0)
            + new_open_conflicts,
        )
        if open_conflict_rows and confidence == "high":
            confidence = "medium"
        shaped_source_count = len(
            {
                source
                for _, source, what_added in shaped_rows
                if what_added != NOT_ENRICHMENT_SOURCE_MARKER
            }
        )
        human_reviewed = self._validated_date_field(
            existing_meta.get("human_reviewed"),
            field="human_reviewed",
            page_path=page_path,
        )

        lines = [
            "---",
            "type: compiled-briefing",
            f"domain: {target.domain}",
            f"description: {json.dumps(description, ensure_ascii=False)}",
            f"status: {status}",
            f"created: {created}",
            f"updated: {today}",
            f"last_compiled_at: {compiled_at}",
            f"freshness_state: {freshness_state}",
            f"confidence: {confidence}",
            f"source_count: {shaped_source_count}",
            f"last_accessed: {today}",
            f"relevance: {relevance:.2f}",
            f"tier: {tier}",
            f"sources_trust: {sources_trust}",
            f"last_verified: {last_verified}",
            f"enrichment_count: {enrichment_count}",
            f"conflicts_open: {conflicts_open}",
            f"human_reviewed: {human_reviewed}",
        ]
        if human_zone_populated:
            lines.append("human_zone_populated: true")
        if quality_status == "needs_review" and quality_reason:
            lines.extend(
                [
                    "quality_status: needs_review",
                    f"quality_reason: {json.dumps(quality_reason, ensure_ascii=False)}",
                ]
            )
        if record_kind:
            lines.append(f"record_kind: {record_kind}")
        if record_kind == "decision":
            lines.extend(
                [
                    f"decision_status: {decision_status}",
                    f"decision_owner: {json.dumps(decision_owner, ensure_ascii=False)}",
                    f"decision_date: {decision_date}",
                    f"supersedes: {json.dumps(supersedes, ensure_ascii=False)}",
                    f"superseded_by: {json.dumps(superseded_by, ensure_ascii=False)}",
                ]
            )
        elif record_kind == "incident":
            lines.extend(
                [
                    f"incident_date: {incident_date}",
                    f"severity: {severity}",
                ]
            )
        # This is a full frontmatter rebuild, not a patch -- any key the
        # owner layer wrote out-of-band (e.g. ``duplicate_of``, see
        # CORE_FRONTMATTER_FIELDS above) must be carried forward explicitly
        # here, or it silently vanishes on this page's very next recompile.
        # Goes through _extra_frontmatter_lines, not existing_meta -- the
        # latter flattens every value to a plain string, which is fine for
        # the fields this code owns (always simple scalars) but silently
        # changes an owner field's YAML type (number/bool/list/null) if
        # reused here. See _extra_frontmatter_lines for the fix.
        lines.extend(
            self._extra_frontmatter_lines(existing_text, page_path=page_path)
        )
        lines.extend(["---", "", f"# {target.title}", ""])
        if record_kind == "decision":
            lines.extend(
                [
                    "## Decision Record",
                    f"- Status: {decision_status}",
                    f"- Date: {decision_date or '(unknown)'}",
                    f"- Owner: {decision_owner or '(unknown)'}",
                    *([f"- Supersedes: {', '.join(supersedes)}"] if supersedes else []),
                    *([f"- Superseded by: {superseded_by}"] if superseded_by else []),
                    "",
                    "## Rationale",
                    rationale or "No rationale captured yet.",
                    "",
                    "## Alternatives Considered",
                    *self._render_bullets(
                        alternatives,
                        empty="No alternatives captured yet.",
                    ),
                    "",
                    "## Decision Evidence",
                    *self._render_sources(decision_evidence),
                    "",
                ]
            )
        elif record_kind == "incident":
            lines.extend(
                [
                    "## Incident Debrief",
                    f"- Date: {incident_date or '(unknown)'}",
                    f"- Severity: {severity or '(unknown)'}",
                    "",
                    "## Timeline",
                    *self._render_bullets(timeline, empty="No timeline captured yet."),
                    "",
                    "## Root Cause",
                    root_cause or "No reliable root cause captured yet.",
                    "",
                    "## What Worked",
                    *self._render_bullets(
                        what_worked,
                        empty="Nothing captured yet.",
                    ),
                    "",
                    "## What Did Not Work",
                    *self._render_bullets(
                        what_did_not_work,
                        empty="Nothing captured yet.",
                    ),
                    "",
                    "## Corrective Actions",
                    *self._render_bullets(
                        corrective_actions,
                        empty="No corrective actions captured yet.",
                    ),
                    "",
                    "## Generalizable Learning",
                    generalizable_learning or "No reusable learning captured yet.",
                    "",
                ]
            )
        lines.extend(
            [
                "## Current State",
                current_state or "No reliable current state yet.",
                "",
                "## Recent Changes",
                *self._render_dated_bullets(
                    recent_changes_rows,
                    empty="No recent changes captured yet.",
                ),
                "",
                "## Open Loops",
                *self._render_dated_bullets(
                    open_loops_rows,
                    empty="No open loops captured yet.",
                ),
                "",
                "## Key Decisions",
                *self._render_bullets(
                    key_decisions,
                    empty="No key decisions captured yet.",
                ),
                "",
                "## Next Check",
                next_check or "Review after the next meaningful update.",
                "",
                "## Sources",
                *self._render_sources(all_sources),
                "",
                "## Sources That Shaped This Page",
                *self._render_sources_shaped_table(shaped_rows),
                "",
                "## Open Conflicts",
                *self._render_open_conflicts_table(open_conflict_rows),
                "",
                "## Claim History",
                *self._render_claim_history(claim_history_rows),
                "",
            ]
        )
        # ТЗ 6.3: History is conditional -- only rendered once compression
        # has actually moved something into it, so a page that never cooled
        # never grows an empty section. Deliberately not added to
        # ``lint_notes``'s required-sections set (see there).
        if history_rows:
            lines.extend(
                [
                    "## History",
                    *self._render_dated_bullets(
                        history_rows, empty="(nothing archived yet)"
                    ),
                    "",
                ]
            )
        lines.extend(
            [
                "## Owner Notes",
                human_zone,
                "",
            ]
        )
        rendered = "\n".join(lines)
        # Last line of defence for the owner's zone, mirroring the check this
        # function opens with -- that one validates the page it reads, this
        # one the page it is about to hand to the writer. Every model-authored
        # field is already defused (see _defuse_human_zone_markers), so this
        # only fires if some future field reaches the body without passing
        # through _clean_line/_paragraph/_normalize_list. Raising here means
        # the pass skips the page (_upsert_briefing turns this into
        # _record_human_zone_ambiguous) and the page stays readable, instead
        # of being written with a second marker pair that would make every
        # later pass fail on reading it.
        if self._human_zone_span(rendered) is _AMBIGUOUS_HUMAN_ZONE:
            raise HumanZoneMarkerError(
                "rendered page would carry ambiguous human zone markers: "
                f"start={rendered.count(HUMAN_ZONE_START)} "
                f"end={rendered.count(HUMAN_ZONE_END)}"
            )
        return rendered

    def _catalog(self) -> list[dict[str, str]]:
        catalog: list[dict[str, str]] = []
        for candidate in self._iter_candidates():
            catalog.append(
                {
                    "path": candidate.rel_path,
                    "domain": candidate.domain,
                    "slug": candidate.slug,
                    "title": candidate.title,
                    "description": candidate.description,
                }
            )
        return catalog

    def _impact_catalog(
        self,
        *,
        source_rel_path: str,
        source_excerpt: str,
        max_items: int = IMPACT_CATALOG_MAX_ITEMS,
        max_chars: int = IMPACT_CATALOG_MAX_CHARS,
    ) -> list[dict[str, str]]:
        """Return a bounded, source-aware compiled catalog for impact routing."""
        query_tokens = self._tokens(f"{source_rel_path}\n{source_excerpt}")
        scored: list[tuple[tuple[int, int, float, str], dict[str, str]]] = []

        for candidate in self._iter_candidates():
            source_links = self._source_links_from_note(candidate.text)
            source_link_match = 1 if source_rel_path in source_links else 0
            haystack = " ".join(
                [
                    candidate.domain,
                    candidate.slug,
                    candidate.title,
                    candidate.description,
                    " ".join(source_links),
                ]
            )
            overlap = len(query_tokens & self._tokens(haystack))
            item = {
                "path": candidate.rel_path,
                "domain": candidate.domain,
                "slug": candidate.slug,
                "title": candidate.title,
                "description": candidate.description,
            }
            scored.append(
                (
                    (
                        source_link_match,
                        overlap,
                        round(candidate.relevance, 4),
                        candidate.rel_path,
                    ),
                    item,
                )
            )

        if not scored:
            return []

        scored.sort(key=lambda item: item[0], reverse=True)
        selected: list[dict[str, str]] = []
        current_chars = 2  # []
        for _, item in scored:
            encoded = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if selected and len(selected) >= max_items:
                break
            if selected and current_chars + encoded + 2 > max_chars:
                break
            selected.append(item)
            current_chars += encoded + 2

        return selected or [scored[0][1]]

    def _iter_candidates(self) -> list[CompiledBriefingCandidate]:
        if not self.compiled_root.exists():
            return []
        candidates: list[CompiledBriefingCandidate] = []
        for path in sorted(self.compiled_root.glob("**/*.md")):
            rel_path = path.relative_to(self.vault_path).as_posix()
            if rel_path.startswith("compiled/archive/"):
                continue
            text = self._read_page_text(path)
            fields = self._frontmatter_fields(text)
            title = self._title_from_text(text) or path.stem.replace("-", " ")
            candidates.append(
                CompiledBriefingCandidate(
                    rel_path=rel_path,
                    domain=str(fields.get("domain") or path.parent.name),
                    slug=path.stem,
                    title=title.strip(),
                    description=str(fields.get("description") or "").strip(),
                    freshness_state=str(fields.get("freshness_state") or ""),
                    confidence=str(fields.get("confidence") or ""),
                    relevance=self._float_value(fields.get("relevance"), default=0.0),
                    tier=str(fields.get("tier") or ""),
                    text=text,
                )
            )
        return candidates

    def _rank_candidates(
        self,
        question: str,
        *,
        limit: int,
        domain: str | None = None,
    ) -> list[CompiledBriefingCandidate]:
        """Rank compiled pages against a free-text query.

        ``domain`` restricts the field *before* scoring, which matters
        because the ``min_score`` cut below is relative to the best
        candidate found (code review): filtering a ranked whole-vault list
        afterwards let a strong page in another domain raise the bar high
        enough to drop a genuinely relevant page in the requested one, and
        the caller then reported "не нашёл" for a page sitting right there
        on disk. Left ``None`` by every caller that really does search all
        domains (a direct question, ``/why``), so their behavior is
        unchanged.
        """
        query = (question or "").strip()
        if not query:
            return []
        query_lower = query.lower()
        query_tokens = self._tokens(query_lower)
        scored: list[tuple[float, CompiledBriefingCandidate]] = []
        for candidate in self._iter_candidates():
            if domain is not None and candidate.domain != domain:
                continue
            title_lower = candidate.title.lower()
            description_lower = candidate.description.lower()
            body_lower = self._clip(
                self._body_without_frontmatter(candidate.text).lower(),
                MAX_BODY_SNIPPET_CHARS,
            )
            score = 0.0
            # Substring, not a whole-word match, and deliberately so (code
            # review, twice): titles here are nominative and questions are
            # not, and nothing in this pipeline stems or lemmatizes, so it is
            # the loose containment test that lets "Краснодар" match
            # "краснодарский офис" at all. Tightening it -- to a word
            # boundary, then to a boundary plus a short case ending -- each
            # time removed matches without adding any (the strict rule only
            # ever fires where the loose one already did), and the pages it
            # dropped scored a flat zero, fell to the ``score <= 0`` guard
            # below, and left the owner with an answer off some unrelated
            # page that merely shared the question's other words. The cost of
            # keeping it loose is the reverse and much smaller: a short title
            # sitting inside a longer word ("Крас" in "краснодар") ranks
            # where it should not, and /why offers the owner a choice it
            # could have made itself.
            if title_lower and title_lower in query_lower:
                score += 8.0
            if candidate.slug and candidate.slug.replace("-", " ") in query_lower:
                score += 5.0
            title_tokens = self._tokens(title_lower)
            description_tokens = self._tokens(description_lower)
            body_tokens = self._tokens(body_lower)
            score += 2.4 * len(query_tokens & title_tokens)
            score += 1.2 * len(query_tokens & description_tokens)
            score += 0.25 * len(query_tokens & body_tokens)
            if score <= 0:
                # Дефект 1: a query with zero text overlap (title/slug/
                # description/body tokens) must not match anything just
                # because a page happens to score well on metadata alone
                # (domain hint, freshness, confidence, relevance, tier).
                # Cut here, before any metadata bonus below is added. The
                # identical check further down stays as a second barrier
                # for the rarer case where a small text score is wiped out
                # by a negative metadata adjustment (stale/low-confidence).
                continue
            for hint in QUESTION_DOMAIN_HINTS.get(candidate.domain, ()):
                if hint in query_lower:
                    score += 1.2
                    break
            if candidate.freshness_state == "fresh":
                score += 0.7
            elif candidate.freshness_state == "watch":
                score += 0.25
            elif candidate.freshness_state == "stale":
                score -= 0.2
            if candidate.confidence == "high":
                score += 0.4
            elif candidate.confidence == "medium":
                score += 0.15
            elif candidate.confidence == "low":
                score -= 0.15
            score += max(candidate.relevance, 0.0) * 0.6
            score += TIER_RANK.get(candidate.tier, 0) * 0.05
            if score <= 0:
                continue
            scored.append((score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return []
        top_score = scored[0][0]
        min_score = max(1.5, top_score * 0.35)
        filtered = [
            candidate
            for score, candidate in scored
            if score >= min_score
        ]
        return filtered[:limit]

    def _ensure_dirs(self) -> None:
        for domain in COMPILED_BRIEFING_DOMAINS:
            (self.compiled_root / domain).mkdir(parents=True, exist_ok=True)

    def _ensure_state_dirs(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.answers_root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _launcher_lock(self, *, blocking: bool) -> Iterator[bool]:
        self._ensure_state_dirs()
        with self.launcher_lock_path.open("a+", encoding="utf-8") as handle:
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _worker_lock(self, *, blocking: bool) -> Iterator[bool]:
        self._ensure_state_dirs()
        with self.worker_lock_path.open("a+", encoding="utf-8") as handle:
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        """The one lock over every ``.compiled``/give-up state file: the
        refresh queue, ``source-state.json``, and the dropped-sources
        journal.

        These were three separate helpers over the same ``state.lock`` file,
        which read as three independent locks while behaving as one -- taking
        any of them inside another deadlocked on the spot, since ``flock`` on
        a second open file description of the same file blocks against the
        first even within one process. The three shared a file on purpose
        (their writes interleave at the same moments: a queue event is acked
        exactly when a source finally compiles), so they are now one lock
        that says so, and nesting is simply allowed.

        Re-entrancy is tracked per *thread*, not per instance: two threads
        sharing one service must still serialize against each other (see
        ``test_compiled_briefings_concurrent_give_up_writes_do_not_lose_each_other``),
        and they do -- each takes its own file description and blocks on
        ``flock`` as before. Only a nested take on the thread that already
        holds it passes straight through.
        """
        depth = getattr(self._state_lock_depth, "value", 0)
        if depth:
            self._state_lock_depth.value = depth + 1
            try:
                yield
            finally:
                self._state_lock_depth.value = depth
            return
        self._ensure_state_dirs()
        with self.state_lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._state_lock_depth.value = 1
            try:
                yield
            finally:
                self._state_lock_depth.value = 0
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _with_queue_lock(
        self,
        callback: Callable[[], QueueLockResult],
    ) -> QueueLockResult:
        with self._state_lock():
            return callback()

    def _load_source_state(self) -> dict[str, Any]:
        with self._state_lock():
            return self._load_source_state_unlocked()

    def _load_source_state_unlocked(self) -> dict[str, Any]:
        if not self.source_state_path.exists():
            return {"version": SOURCE_STATE_VERSION, "entries": {}}
        try:
            payload = json.loads(
                self.source_state_path.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError as exc:
            raise CompiledSourceStateError(
                f"invalid compiled source state: {self.source_state_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise CompiledSourceStateError(
                "compiled source state must be a JSON object"
            )
        if payload.get("version") != SOURCE_STATE_VERSION:
            raise CompiledSourceStateError(
                "unsupported compiled source state version"
            )
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            raise CompiledSourceStateError(
                "compiled source state entries must be a JSON object"
            )
        for rel_path, entry in entries.items():
            if not isinstance(rel_path, str) or not isinstance(entry, dict):
                raise CompiledSourceStateError("invalid compiled source state entry")
            if not isinstance(entry.get("sources"), dict):
                raise CompiledSourceStateError(
                    f"invalid compiled source state entry: {rel_path}"
                )
        return payload

    def _write_source_state_unlocked(self, state: dict[str, Any]) -> None:
        _atomic_write_text(
            self.source_state_path,
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _load_worker_state(self) -> dict[str, Any]:
        if not self.worker_state_path.exists():
            return {}
        try:
            payload = json.loads(self.worker_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning(
                "Invalid compiled worker state payload at %s",
                self.worker_state_path,
            )
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_worker_state(self, payload: dict[str, Any]) -> None:
        _atomic_write_text(
            self.worker_state_path,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _clear_worker_state_unlocked(self) -> None:
        self.worker_state_path.unlink(missing_ok=True)

    def _parse_iso_datetime(self, raw: str) -> datetime | None:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _pid_is_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _worker_state_is_live(
        self,
        state: dict[str, Any],
        *,
        stale_after_seconds: int = DEFAULT_WORKER_STALE_SECONDS,
    ) -> bool:
        pid = int(state.get("pid") or 0)
        heartbeat_raw = str(state.get("heartbeat_at") or state.get("started_at") or "")
        heartbeat_at = self._parse_iso_datetime(heartbeat_raw)
        if not self._pid_is_alive(pid):
            return False
        if heartbeat_at is None:
            return True
        age_seconds = (
            datetime.now().astimezone() - heartbeat_at.astimezone()
        ).total_seconds()
        return age_seconds <= stale_after_seconds

    def _write_worker_state(
        self,
        *,
        pid: int,
        status: str,
        started_at: str | None = None,
    ) -> None:
        now = datetime.now().astimezone().isoformat()

        def write_state() -> None:
            state = self._load_worker_state()
            existing_started_at = str(state.get("started_at") or "").strip()
            self._save_worker_state(
                {
                    "pid": pid,
                    "status": status,
                    "started_at": started_at or existing_started_at or now,
                    "heartbeat_at": now,
                }
            )

        with self._launcher_lock(blocking=True):
            write_state()

    def _touch_worker_state(self, pid: int) -> None:
        self._write_worker_state(pid=pid, status="running")

    def _clear_worker_state(self, *, pid: int | None = None) -> None:
        def clear_state() -> None:
            if pid is None:
                self._clear_worker_state_unlocked()
                return
            state = self._load_worker_state()
            current_pid = int(state.get("pid") or 0)
            if current_pid in {0, pid}:
                self._clear_worker_state_unlocked()

        with self._launcher_lock(blocking=True):
            clear_state()

    def _start_queue_worker_journal(
        self,
        *,
        pid: int,
        started_at: str,
        force: bool,
        max_events: int,
        refresh_qmd: bool,
        initial_queue_size: int,
    ) -> None:
        started = datetime.fromisoformat(started_at)
        filename = f"{started.strftime('%Y-%m-%d-%H%M%S')}-{pid}.json"
        self.queue_worker_history_root.mkdir(parents=True, exist_ok=True)
        self._active_queue_worker_journal_path = (
            self.queue_worker_history_root / filename
        )
        self._active_queue_worker_journal = {
            "pid": pid,
            "status": "running",
            "started_at": started_at,
            "parameters": {
                "force": force,
                "max_events": max_events,
                "refresh_qmd": refresh_qmd,
            },
            "initial_queue_size": initial_queue_size,
            "events": [],
        }
        self._write_active_queue_worker_journal()
        self._rotate_queue_worker_journals()

    def _write_active_queue_worker_journal(self) -> None:
        if (
            self._active_queue_worker_journal is None
            or self._active_queue_worker_journal_path is None
        ):
            return
        _atomic_write_text(
            self._active_queue_worker_journal_path,
            json.dumps(
                self._active_queue_worker_journal,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def _record_queue_worker_event(
        self,
        event: dict[str, Any],
        *,
        outcome: str,
        updated: list[str],
        errors: list[str],
        attempts: int | None = None,
    ) -> None:
        if self._active_queue_worker_journal is None:
            return
        source_path = str(event.get("source_path") or "")
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", source_path)
        self._active_queue_worker_journal["events"].append(
            {
                "source_path": source_path,
                "source_date": date_match.group(0) if date_match else None,
                "enqueued_at": event.get("enqueued_at"),
                "last_enqueued_at": event.get("last_enqueued_at"),
                "claimed_at": event.get("claimed_at"),
                "finished_at": datetime.now().astimezone().isoformat(),
                "attempts": (
                    attempts
                    if attempts is not None
                    else int(event.get("attempts") or 0)
                ),
                "outcome": outcome,
                "updated": list(updated),
                "errors": list(errors),
            }
        )
        self._write_active_queue_worker_journal()

    def _finish_queue_worker_journal(
        self,
        *,
        status: str,
        remaining_queue_size: int,
        totals: dict[str, int] | None = None,
        error: dict[str, str] | None = None,
    ) -> None:
        if self._active_queue_worker_journal is None:
            return
        self._active_queue_worker_journal["status"] = status
        self._active_queue_worker_journal["finished_at"] = (
            datetime.now().astimezone().isoformat()
        )
        self._active_queue_worker_journal["remaining_queue_size"] = (
            remaining_queue_size
        )
        if totals is not None:
            self._active_queue_worker_journal["totals"] = totals
        if error is not None:
            self._active_queue_worker_journal["error"] = error
        self._write_active_queue_worker_journal()
        self._rotate_queue_worker_journals()
        self._active_queue_worker_journal = None
        self._active_queue_worker_journal_path = None

    def _rotate_queue_worker_journals(self) -> None:
        journals = sorted(
            self.queue_worker_history_root.glob("*.json"),
            key=lambda path: path.name,
            reverse=True,
        )
        for path in journals[QUEUE_WORKER_HISTORY_LIMIT:]:
            path.unlink()

    def _write_queue_worker_crash_journal(
        self, *, pid: int, started_at: str, exc: Exception
    ) -> None:
        """Persist an owner-visible trace of an unexpected ``run_queue_worker``
        crash to ``.session/compile-queue-worker.json`` (resilience review
        defect 1). ``spawn_background_drain`` runs this worker detached with
        stdout/stderr sent to DEVNULL, so this file -- same ``.session/``
        journal convention as ``_write_pass_journal``'s
        ``.session/compile-enrich.json`` -- is the only surviving record of
        *why* the queue stopped draining. Always overwrites the previous
        crash, mirroring that journal's own "latest pass only" convention.

        Best-effort, same as ``decisions_queue.write_queue_document``: the
        crash this records has already happened and is about to be
        re-raised by the caller regardless, so a failure writing *this*
        trace (e.g. the same full disk that caused the original crash) must
        never replace that original exception with this one -- caught
        broadly and logged instead of raised (code review defect 2).
        """
        payload = {
            "pid": pid,
            "started_at": started_at,
            "crashed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "traceback": traceback.format_exc(),
        }
        journal_path = self.vault_path / ".session" / "compile-queue-worker.json"
        try:
            _atomic_write_text(
                journal_path,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
        except Exception as journal_exc:  # noqa: BLE001 - best-effort trace
            logger.warning(
                "Failed to write queue worker crash journal (original "
                "crash: %r): %s",
                exc,
                journal_exc,
            )

    def _dropped_sources_journal_path(self) -> Path:
        return self.vault_path / ".session" / "compile-dropped-sources.json"

    def _load_dropped_queue_sources(self) -> list[dict[str, Any]]:
        """Read the give-up journal, fail-safe like ``_load_queue``."""

        try:
            payload = json.loads(
                self._dropped_sources_journal_path().read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning(
                "Invalid dropped-sources journal at %s",
                self._dropped_sources_journal_path(),
            )
            return []
        if not isinstance(payload, dict):
            return []
        entries = payload.get("sources")
        if not isinstance(entries, list):
            return []
        return [item for item in entries if isinstance(item, dict)]

    def _record_dropped_queue_source(
        self, *, source_rel_path: str, errors: list[str], attempts: int
    ) -> None:
        """Persist an owner-visible trace of a source the queue gave up on.

        Once ``_drain_queue_once`` acks a failed event -- after the third
        attempt, or immediately for an error it will never retry -- the
        queue entry is deleted, and that entry was the only thing that would
        ever have compiled this source: nothing retries it and nothing else
        points at it. Until now the only report was the drain's returned
        ``errors`` list, and the drain that handles nearly every real event
        is the detached one ``spawn_background_drain`` starts with
        stdout/stderr on DEVNULL -- so in practice the owner's note simply
        never reached a compiled page and no trace of that survived
        anywhere.

        Same ``.session/`` journal convention as
        ``_write_queue_worker_crash_journal``; read back by
        ``compiled_enrich_report`` into the digest's "Требует решения"
        block. Entries accumulate, one per source with the newest write
        winning, and are cleared only by a later drain that runs the same
        source to a conclusion -- a compiled page, or the impact stage
        deciding it affects no page at all (``_clear_dropped_queue_source``;
        that second outcome is a finished answer, not a failure, so leaving
        the entry up would nag the owner about a note the system has already
        settled). Deliberately *not*
        expired by calendar day, unlike the fact-check journal: like
        ``human_zone_ambiguous_pages`` this is unresolved business rather
        than a dated event, and a source that is still missing from
        ``compiled/`` a week later is still missing.

        Best-effort, same reasoning as
        ``_write_queue_worker_crash_journal``: this records a failure that
        already happened, so failing to record it must not also take down
        the rest of the drain.
        """

        normalized = source_rel_path.strip()
        if not normalized:
            return
        try:
            with self._state_lock():
                entries = [
                    item
                    for item in self._load_dropped_queue_sources()
                    if str(item.get("source_path") or "") != normalized
                ]
                entries.append(
                    {
                        "source_path": normalized,
                        "dropped_at": datetime.now()
                        .astimezone()
                        .isoformat(timespec="seconds"),
                        "attempts": attempts,
                        "errors": [str(item) for item in errors],
                    }
                )
                _atomic_write_text(
                    self._dropped_sources_journal_path(),
                    json.dumps({"sources": entries}, ensure_ascii=False, indent=2)
                    + "\n",
                )
        except Exception as exc:  # noqa: BLE001 - best-effort trace
            logger.warning(
                "Failed to record dropped compiled source %s: %s", normalized, exc
            )

    def _clear_dropped_queue_source(
        self, source_rel_path: str, updated_paths: tuple[str, ...] = ()
    ) -> None:
        """Drop a source's give-up trace once a drain has run it to a
        conclusion -- a compiled page written, or the impact stage deciding
        the source affects no page at all. Both are finished business; only
        a failure is not.

        The counterpart to ``_record_dropped_queue_source``: that journal
        holds unresolved business, so leaving an entry behind after the
        source finally made it through would keep the digest asking the
        owner to fix something already fixed.

        Inside a pass the clear is deferred rather than applied (code
        review): ``run_nightly_maintenance``'s ТЗ 5.5 inv 5 gate can roll
        the whole pass back *after* this point -- e.g. when a page this
        pass wrote ends up with no source link at all -- which restores the
        compiled pages but not the queue entry this drain already acked.
        Clearing immediately would then leave the owner with no trace at
        all of a source that is once again uncompiled and no longer queued.
        ``updated_paths`` -- the pages this drain wrote, empty when it wrote
        none -- is carried along so that gate can tell which deferred clears
        its rollback actually invalidated; see
        ``CompileEnrichPass.dropped_sources_cleared``. The background drain
        (``run_queue_worker``) has no active pass and nothing that can undo
        its writes, so it still clears inline.
        """

        normalized = source_rel_path.strip()
        if not normalized:
            return
        if self._active_pass is not None:
            pending = self._active_pass.dropped_sources_cleared
            for index, (existing_source, existing_pages) in enumerate(pending):
                if existing_source != normalized:
                    continue
                # One source can be drained more than once in a pass, each
                # time writing different pages; the rollback check below has
                # to see all of them, so they merge rather than the second
                # call being dropped as a duplicate.
                merged = tuple(dict.fromkeys((*existing_pages, *updated_paths)))
                pending[index] = (normalized, merged)
                return
            pending.append((normalized, tuple(dict.fromkeys(updated_paths))))
            return
        self._forget_dropped_queue_source(normalized)

    def _forget_dropped_queue_source(self, normalized: str) -> None:
        """Actually remove one give-up entry -- see
        ``_clear_dropped_queue_source`` for when this is allowed to run."""

        try:
            with self._state_lock():
                entries = self._load_dropped_queue_sources()
                remaining = [
                    item
                    for item in entries
                    if str(item.get("source_path") or "") != normalized
                ]
                if len(remaining) == len(entries):
                    return
                _atomic_write_text(
                    self._dropped_sources_journal_path(),
                    json.dumps({"sources": remaining}, ensure_ascii=False, indent=2)
                    + "\n",
                )
        except Exception as exc:  # noqa: BLE001 - best-effort bookkeeping
            logger.warning(
                "Failed to clear dropped compiled source %s: %s", normalized, exc
            )

    def _load_queue(self) -> list[dict[str, Any]]:
        if not self.queue_path.exists():
            return []
        try:
            payload = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Invalid compiled queue payload at %s", self.queue_path)
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _save_queue(self, payload: list[dict[str, Any]]) -> None:
        _atomic_write_text(
            self.queue_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _recover_stale_claims(
        self,
        queue: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        now = datetime.now().astimezone()
        mutated = False
        for event in queue:
            if str(event.get("state") or "pending") != "in_flight":
                continue
            claim_token = str(event.get("claim_token") or "").strip()
            if not claim_token:
                event["state"] = "pending"
                event["claimed_at"] = ""
                event["claimed_pid"] = 0
                mutated = True
                continue
            claimed_at = self._parse_iso_datetime(str(event.get("claimed_at") or ""))
            claimed_pid = int(event.get("claimed_pid") or 0)
            stale_claim = (
                claimed_at is None
                or (now - claimed_at.astimezone()).total_seconds()
                > DEFAULT_QUEUE_CLAIM_STALE_SECONDS
                or not self._pid_is_alive(claimed_pid)
            )
            if not stale_claim:
                continue
            event["state"] = "pending"
            event["claim_token"] = ""
            event["claimed_at"] = ""
            event["claimed_pid"] = 0
            mutated = True
        return queue, mutated

    def _source_tier_ranks(self) -> dict[str, int]:
        """Best-known memory-tier rank (``TIER_RANK``) for each source path,
        used only to order the compile-enrich queue (ТЗ 6.2: "the queue is
        processed by tier priority -- core and active first").

        A queue event only carries the *source* that changed, not the
        compiled page(s) it affects -- that mapping is the Impact model
        call in ``_resolve_targets``, which is far too expensive to run
        just to pick a claim order. Instead this reuses two things already
        read elsewhere on this same code path, at no extra model cost: the
        page tiers from ``_iter_candidates()`` and the page->source
        associations already recorded in source-state.json by the last
        successful write to each page.
        """
        page_ranks = {
            candidate.rel_path: TIER_RANK.get(candidate.tier, 0)
            for candidate in self._iter_candidates()
        }
        ranks: dict[str, int] = {}
        try:
            state = self._load_source_state_unlocked()
        except CompiledSourceStateError:
            # Code review Finding 2: this read only feeds the queue's tier
            # sort (ТЗ 6.2), an ordering optimization -- not a condition for
            # the queue to make progress at all. A corrupt source-state file
            # must not crash the ordinary queue-only drain (unlike the
            # nightly path's own reads of the same file, e.g.
            # ``freshness_issues``/``initialize_source_state``, which keep
            # raising on purpose). Degrade to no known ranks: every event
            # falls back to ``default_rank`` in the caller and the sort's
            # stability keeps arrival order, same as an all-tied batch.
            logger.warning(
                "Повреждён файл состояния источников %s -- приоритет "
                "очереди по уровню памяти временно отключён, события "
                "разбираются в порядке поступления",
                self.source_state_path,
            )
            return ranks
        for page_rel_path, entry in state.get("entries", {}).items():
            if not isinstance(entry, dict):
                continue
            sources = entry.get("sources")
            if not isinstance(sources, dict):
                continue
            rank = page_ranks.get(page_rel_path, 0)
            for source_rel_path in sources:
                if rank > ranks.get(source_rel_path, -1):
                    ranks[source_rel_path] = rank
        return ranks

    def _claim_ready_queue_events(
        self,
        *,
        force: bool,
        max_events: int,
    ) -> list[dict[str, Any]]:
        now = datetime.now().astimezone()
        now_ts = now.timestamp()

        def claim_events() -> list[dict[str, Any]]:
            queue = self._load_queue()
            queue, recovered = self._recover_stale_claims(queue)
            ready_indices: list[int] = []
            for index, event in enumerate(queue):
                if str(event.get("state") or "pending") != "pending":
                    continue
                due_at = float(event.get("due_at") or 0)
                if force or due_at <= now_ts:
                    ready_indices.append(index)
            skipped_updated = False
            if len(ready_indices) > 1:
                # A source with no page citing it yet ranks as "active" --
                # new content should not be starved behind existing
                # cold/warm pages just because its eventual target is
                # still unresolved. `list.sort` is stable, so events tied
                # on rank keep arrival order (unchanged behavior).
                default_rank = TIER_RANK["active"]
                source_ranks = self._source_tier_ranks()
                ready_indices.sort(
                    key=lambda index: -source_ranks.get(
                        str(queue[index].get("source_path") or ""),
                        default_rank,
                    )
                )
                # Code review Finding 1 (starvation): rank alone can starve
                # a low-tier event forever if higher-tier sources keep
                # arriving ahead of it. Each cycle it is ready but not
                # claimed bumps ``skipped_cycles``; once it crosses
                # QUEUE_STARVATION_SKIP_LIMIT it is forced to the very front
                # for this claim, regardless of tier. This only reorders
                # among events already in ``ready_indices`` (i.e. already
                # due), so a not-yet-due event still cannot jump ahead, and
                # it leaves the tier sort untouched below the limit (ТЗ 6.2).
                starved = [
                    index
                    for index in ready_indices
                    if int(queue[index].get("skipped_cycles") or 0)
                    >= QUEUE_STARVATION_SKIP_LIMIT
                ]
                if starved:
                    rest = [index for index in ready_indices if index not in starved]
                    ready_indices = starved + rest
            claimed_indices = set(ready_indices[:max_events])
            selected: list[dict[str, Any]] = []
            for index in ready_indices[:max_events]:
                event = queue[index]
                claim_token = uuid4().hex
                event["state"] = "in_flight"
                event["claim_token"] = claim_token
                event["claimed_at"] = now.isoformat()
                event["claimed_pid"] = os.getpid()
                selected.append(dict(event))
            if len(ready_indices) > 1:
                for index in ready_indices:
                    if index in claimed_indices:
                        continue
                    queue[index]["skipped_cycles"] = (
                        int(queue[index].get("skipped_cycles") or 0) + 1
                    )
                    skipped_updated = True
            if recovered or selected or skipped_updated:
                self._save_queue(queue)
            return selected

        return self._with_queue_lock(claim_events)

    def _ack_claimed_queue_event(self, event: dict[str, Any]) -> None:
        source_path = str(event.get("source_path") or "")
        claim_token = str(event.get("claim_token") or "")

        def ack_event() -> None:
            queue = self._load_queue()
            updated = [
                item
                for item in queue
                if not (
                    str(item.get("source_path") or "") == source_path
                    and str(item.get("claim_token") or "") == claim_token
                    and str(item.get("state") or "") == "in_flight"
                )
            ]
            if len(updated) != len(queue):
                self._save_queue(updated)

        self._with_queue_lock(ack_event)

    def _release_claimed_queue_event(
        self,
        event: dict[str, Any],
        *,
        attempts: int,
        due_at: float,
        backoff: bool = False,
    ) -> None:
        """Release a claimed event back to "pending".

        ``backoff`` (code review defect 1): marks ``due_at`` as a retry
        backoff rather than a plain debounce wait. A single claim (this
        method's caller, or a fresh ``--force`` invocation) still honors
        ``force`` unconditionally, same as before -- the flag only
        matters to ``run_queue_worker``'s own ``while True`` loop, which
        reads it to decide whether to immediately re-drain again or fall
        back to the normal wait/poll path. Without it, an event released
        without incrementing ``attempts`` (see the `requeueable` caller in
        ``_drain_queue_once``) combined with that loop's own
        "force ignores due_at" check reclaimed the same event every loop
        iteration with no exit condition, all within one process.
        """
        source_path = str(event.get("source_path") or "")
        claim_token = str(event.get("claim_token") or "")

        def release_event() -> None:
            queue = self._load_queue()
            updated = False
            for item in queue:
                if str(item.get("source_path") or "") != source_path:
                    continue
                if str(item.get("claim_token") or "") != claim_token:
                    continue
                if str(item.get("state") or "") != "in_flight":
                    continue
                item["attempts"] = attempts
                item["due_at"] = due_at
                item["backoff"] = backoff
                item["state"] = "pending"
                item["claim_token"] = ""
                item["claimed_at"] = ""
                item["claimed_pid"] = 0
                updated = True
                break
            if updated:
                self._save_queue(queue)

        self._with_queue_lock(release_event)

    def _normalize_rel_path(self, value: str | Path) -> str:
        path = Path(value)
        try:
            rel_path = path.relative_to(self.vault_path).as_posix()
        except ValueError:
            rel_path = path.as_posix()
        return self._strip_relative_prefix(rel_path)

    def _resolve_lint_source_path(self, source: str) -> Path | None:
        raw = self._normalize_lint_source(source)
        if not raw:
            return None

        if Path(raw).is_absolute():
            return None

        candidates: list[Path] = []
        for source_variant in self._lint_source_variants(raw):
            source_path = Path(source_variant)
            for base_dir in self._lint_source_base_dirs(source_variant):
                candidate = (base_dir / source_path).resolve()
                try:
                    candidate.relative_to(base_dir)
                except ValueError:
                    continue
                candidates.append(candidate)
                if candidate.exists():
                    return candidate
                if not source_path.suffix:
                    md_candidate = candidate.with_suffix(".md")
                    candidates.append(md_candidate)
                    if md_candidate.exists():
                        return md_candidate
        return candidates[0] if candidates else None

    def _normalize_lint_source(self, source: str) -> str:
        raw = str(source or "").strip()
        raw = raw.split("#", 1)[0].strip()
        raw = self._strip_relative_prefix(raw)
        if raw.startswith("vault/"):
            raw = raw[len("vault/") :]
        if raw.startswith("claude/"):
            raw = f".{raw}"
        return raw

    @staticmethod
    def _lint_source_variants(raw: str) -> list[str]:
        variants = [raw]
        for visible_prefix, hidden_prefix in HIDDEN_STATE_SOURCE_PREFIXES.items():
            if raw.startswith(visible_prefix):
                variants.append(f"{hidden_prefix}{raw[len(visible_prefix) :]}")
        return variants

    def _lint_source_base_dirs(self, raw: str) -> tuple[Path, ...]:
        """Base dirs to probe for one cited source, most likely first.

        A compiled page cites vault notes and repository files alike, and the
        two namespaces overlap: ``.claude/`` exists both in the vault (rules,
        docs) and in the project root (the shared skills tree). Picking a
        single base dir by prefix therefore mis-resolved every path the
        prefix list did not name -- ``tests/``, ``AGENTS.md``, and
        ``.claude/skills/**`` all resolved under the vault, where they do not
        exist, and ``lint_notes`` reported them as broken source links. Both
        roots are probed now; the prefix only decides the order, and the
        first path that exists wins.
        """
        project_root = self.vault_path.parent
        if (
            raw.startswith(PROJECT_ROOT_SOURCE_PREFIXES)
            or raw in PROJECT_ROOT_SOURCE_FILES
        ):
            return (project_root, self.vault_path)
        return (self.vault_path, project_root)

    def _candidate_freshness_issue(
        self,
        candidate: CompiledBriefingCandidate,
        state_entry: Any,
    ) -> dict[str, str] | None:
        if candidate.freshness_state == "stale":
            return {
                "path": candidate.rel_path,
                "issue": "stale",
                "detail": "frontmatter freshness_state=stale",
            }
        if state_entry is None:
            return {
                "path": candidate.rel_path,
                "issue": "source-untracked",
                "detail": "source snapshot has not been initialized",
            }
        stored_sources = state_entry["sources"]
        current_sources = self._source_snapshot(candidate.text)
        if current_sources == stored_sources:
            return None
        changed_sources = sorted(
            source
            for source in set(current_sources) | set(stored_sources)
            if current_sources.get(source) != stored_sources.get(source)
        )
        return {
            "path": candidate.rel_path,
            "issue": "source-changed",
            "detail": f"changed_sources={','.join(changed_sources)}",
        }

    def _source_snapshot(self, note_text: str) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for source in self._source_links_from_note(note_text):
            raw = self._normalize_lint_source(source)
            if not raw or Path(raw).is_absolute():
                continue
            if raw.startswith(SOURCE_STATE_IGNORED_PATH_PREFIXES):
                continue
            resolved = self._resolve_lint_source_path(raw)
            key = self._source_state_key(raw, resolved)
            if not key:
                continue
            if resolved is None or not resolved.exists():
                snapshot[key] = "missing"
                continue
            snapshot[key] = self._semantic_source_digest(resolved)
        return dict(sorted(snapshot.items()))

    def _source_state_key(self, raw: str, resolved: Path | None) -> str:
        if resolved is None:
            return raw
        for root in (self.vault_path, self.vault_path.parent):
            try:
                return resolved.relative_to(root).as_posix()
            except ValueError:
                continue
        return raw

    def _semantic_source_digest(self, path: Path) -> str:
        content = path.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            normalized = content
        else:
            normalized = self._semantic_source_text(text).encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()

    @staticmethod
    def _semantic_source_text(text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n")
        match = FRONTMATTER_RE.match(normalized)
        if match is not None:
            kept_lines = []
            for line in match.group(1).splitlines():
                key = line.partition(":")[0].strip()
                if key in SOURCE_STATE_IGNORED_FRONTMATTER_FIELDS:
                    continue
                kept_lines.append(line.rstrip())
            body = normalized[match.end() :]
            normalized = "---\n" + "\n".join(kept_lines) + "\n---\n" + body
        return normalized.rstrip() + "\n"

    def _record_source_state(
        self,
        rel_path: str,
        note_text: str,
        *,
        source_rel_path: str | None = None,
        source_excerpt: str | None = None,
    ) -> None:
        snapshot = self._source_snapshot(note_text)
        with self._state_lock():
            state = self._load_source_state_unlocked()
            existing_entry = state["entries"].get(rel_path) or {}
            # Additive: `applied_chunks` (idempotency-by-chunk, ТЗ 5.5
            # invariant 4) is a different concern than `sources` (whole-file
            # freshness snapshot) above and must survive being overwritten
            # by unrelated calls to this method (e.g. the freshness-only
            # call from `_refresh_candidate`, which passes no chunk).
            # `verify_rejected` (see `_record_verify_rejection`) is instead
            # deliberately dropped here: reaching this method means the page
            # was written, so it is no longer stuck.
            stored_applied_chunks = existing_entry.get("applied_chunks") or {}
            applied_chunks = {
                source: list(hashes)
                for source, hashes in stored_applied_chunks.items()
                if isinstance(hashes, list)
            }
            if source_rel_path is not None and source_excerpt is not None:
                chunk_hash = self._source_chunk_hash(source_excerpt)
                hashes = applied_chunks.setdefault(source_rel_path, [])
                if chunk_hash not in hashes:
                    hashes.append(chunk_hash)
                if len(hashes) > SOURCE_STATE_MAX_APPLIED_CHUNK_HASHES:
                    del hashes[: len(hashes) - SOURCE_STATE_MAX_APPLIED_CHUNK_HASHES]
            entry: dict[str, Any] = {
                "evaluated_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "sources": snapshot,
            }
            if applied_chunks:
                entry["applied_chunks"] = applied_chunks
            state["entries"][rel_path] = entry
            self._write_source_state_unlocked(state)

    @classmethod
    def _sources_digest(cls, snapshot: dict[str, str]) -> str:
        return hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _verify_rejection_exhausted(self, state_entry: Any, note_text: str) -> bool:
        """True when this page already burned its Verify retries on exactly
        the source content it still has (see MAX_VERIFY_REJECTED_RETRIES)."""
        if not isinstance(state_entry, dict):
            return False
        rejection = state_entry.get("verify_rejected")
        if not isinstance(rejection, dict):
            return False
        current = self._sources_digest(self._source_snapshot(note_text))
        if rejection.get("sources") != current:
            return False
        return self._int_value(rejection.get("count"), default=0) >= (
            MAX_VERIFY_REJECTED_RETRIES
        )

    def _record_verify_rejection(self, rel_path: str, note_text: str) -> int:
        """Count one Verify rejection for a page, keyed on the source
        snapshot that produced it. Returns the new count so a caller can
        tell, without a second read, whether this rejection just exhausted
        ``MAX_VERIFY_REJECTED_RETRIES`` (see ``_queue_verify_rejected``).

        The snapshot is stored only as that key -- it is deliberately NOT
        written into `sources`, because the page was never actually written:
        marking its sources as seen would hide the freshness issue from the
        health report instead of merely stopping the retries.
        """
        digest = self._sources_digest(self._source_snapshot(note_text))
        with self._state_lock():
            state = self._load_source_state_unlocked()
            entry = state["entries"].get(rel_path)
            if not isinstance(entry, dict):
                entry = {
                    "evaluated_at": datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    ),
                    "sources": {},
                }
            rejection = entry.get("verify_rejected")
            previous = 0
            if isinstance(rejection, dict) and rejection.get("sources") == digest:
                previous = self._int_value(rejection.get("count"), default=0)
            new_count = previous + 1
            entry["verify_rejected"] = {"sources": digest, "count": new_count}
            state["entries"][rel_path] = entry
            self._write_source_state_unlocked(state)
            return new_count

    def _clear_verify_rejection(self, rel_path: str) -> None:
        """Reset a page's Verify-rejection retry count. Used by the
        decisions-queue "retry" response for a ``verify-rejected`` item
        (see ``decisions_queue._apply_verify_rejected_retry``): the owner
        asked for another attempt, so the next pass must not immediately
        skip the page as still exhausted."""
        with self._state_lock():
            state = self._load_source_state_unlocked()
            entry = state["entries"].get(rel_path)
            if not isinstance(entry, dict) or "verify_rejected" not in entry:
                return
            del entry["verify_rejected"]
            state["entries"][rel_path] = entry
            self._write_source_state_unlocked(state)

    def _duplicate_source_chunk(
        self,
        *,
        existing_text: str,
        source_rel_path: str,
        source_excerpt: str,
        page_rel_path: str,
    ) -> bool:
        """Idempotency gate for the pair "source x page" (ТЗ 5.5 invariant
        4, 4.2): true when this exact chunk was already applied to this
        page, so the caller can skip the model call and the write.

        Table-row presence alone is not a safe idempotency key:
        ``refresh_daily_fully`` calls ``refresh_after_write`` once per
        daily-entry chunk, reusing the SAME ``source_rel_path`` with a
        DIFFERENT excerpt each time. Keying only on the source path would
        silently drop every chunk after the first one for that path. So the
        source path must already be recorded in the "Sources That Shaped
        This Page" table *and* the excerpt hash must match one already
        recorded for this page/source pair in source-state.json.
        """
        if not any(
            row_source == source_rel_path
            for _, row_source, _ in self._sources_shaped_rows(existing_text)
        ):
            return False
        applied = self._applied_source_chunk_hashes(page_rel_path, source_rel_path)
        return self._source_chunk_hash(source_excerpt) in applied

    def _applied_source_chunk_hashes(
        self, page_rel_path: str, source_rel_path: str
    ) -> list[str]:
        state = self._load_source_state()
        entry = state["entries"].get(page_rel_path)
        if not entry:
            return []
        applied_chunks = entry.get("applied_chunks")
        if not isinstance(applied_chunks, dict):
            return []
        hashes = applied_chunks.get(source_rel_path)
        return hashes if isinstance(hashes, list) else []

    @staticmethod
    def _source_chunk_hash(source_excerpt: str) -> str:
        return hashlib.sha256(source_excerpt.encode("utf-8")).hexdigest()

    def _source_excerpt(self, source_rel_path: str, source_excerpt: str) -> str:
        clipped = self._clip(source_excerpt, MAX_SOURCE_EXCERPT_CHARS)
        if clipped:
            return clipped
        source_path = self._resolve_lint_source_path(source_rel_path)
        if source_path is None or not source_path.exists():
            return ""
        return self._clip(
            source_path.read_text(encoding="utf-8", errors="replace"),
            MAX_SOURCE_EXCERPT_CHARS,
        )

    def _daily_source_chunks(self, source_rel_path: str, source_text: str) -> list[str]:
        text = str(source_text or "").strip()
        if not text:
            return []
        if not source_rel_path.startswith("daily/"):
            return [self._clip(text, MAX_SOURCE_EXCERPT_CHARS)]

        blocks = self._daily_entry_blocks(text)
        if not blocks:
            return self._chunk_text_full(text, MAX_SOURCE_EXCERPT_CHARS)

        title = self._daily_chunk_title(source_rel_path, text)
        body_limit = max(200, MAX_SOURCE_EXCERPT_CHARS - len(title) - 2)
        chunks: list[str] = []
        # Security: DAILY_ENTRY_SPLIT_RE matches any "## HH:MM [...]"-shaped
        # line, including one that a forwarded/lower-trust entry's OWN body
        # merely contains as text (source_links.py sanitizes a forwarder's
        # *name*, never the forwarded message body). That embedded line
        # still opens its own block below, exactly like a genuine entry
        # would. On the live per-write path this is harmless: the whole
        # entry -- its real header *and* the embedded fake one -- travels to
        # _source_trust_level as ONE excerpt, and its header-aggregation
        # already rates an excerpt by its weakest header (see
        # _excerpt_entry_headers and
        # test_compiled_briefings_whole_day_excerpt_is_rated_by_its_weakest_entry).
        # Per-entry chunking defeats that aggregation by handing the fake
        # header its own, isolated excerpt, so it gets judged only by its
        # own (attacker-chosen) marker -- e.g. a fake "## 08:05 [voice]"
        # buried in a forwarded message earning "own" trust for whatever
        # follows it. Fix: track the weakest real header seen so far in file
        # order, and if a later block's OWN marker claims to be MORE
        # trusted than that floor, carry the floor's real header into its
        # excerpt too, so _source_trust_level sees both headers together
        # and still picks the weaker one -- the same aggregation the
        # live/whole-day path already relies on. A block only ever RAISES
        # the floor's bar when its own marker is genuinely at or below the
        # current floor, so a normal multi-entry day where every entry is
        # the owner's own is untouched (see
        # test_compiled_briefings_applied_chunk_cap_covers_a_full_active_day).
        floor_trust = "own"
        floor_header = ""
        for block in blocks:
            block_text = block.strip()
            if not block_text:
                continue
            own_trust = self._source_trust_level(source_rel_path, block_text)
            guard_header = ""
            if TRUST_RANK.get(own_trust, 0) > TRUST_RANK.get(floor_trust, 0):
                guard_header = floor_header
            else:
                floor_trust = own_trust
                floor_header = block_text.splitlines()[0].strip()
            body_chunks = self._chunk_text_full(block_text, body_limit)
            for piece in body_chunks:
                chunk = "\n\n".join(
                    part
                    for part in (title, guard_header, piece.strip())
                    if part
                ).strip()
                if chunk:
                    chunks.append(chunk)
        return chunks

    @staticmethod
    def _daily_entry_blocks(text: str) -> list[str]:
        stripped = str(text or "").strip()
        if not stripped:
            return []
        parts = DAILY_ENTRY_SPLIT_RE.split(stripped)
        blocks: list[str] = []
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("## "):
                blocks.append(candidate)
        return blocks

    @staticmethod
    def _daily_chunk_title(source_rel_path: str, source_text: str) -> str:
        first_line = (
            str(source_text or "").splitlines()[0].strip() if source_text else ""
        )
        if first_line.startswith("# "):
            return first_line
        stem = Path(source_rel_path).stem
        return f"# {stem}"

    def _chunk_text_full(self, text: str, limit: int) -> list[str]:
        source = str(text or "").strip()
        if not source:
            return []
        if limit <= 0 or len(source) <= limit:
            return [source]

        paragraphs = [
            part.strip()
            for part in re.split(r"\n{2,}", source)
            if part.strip()
        ]
        if len(paragraphs) <= 1:
            return self._chunk_lines_full(source, limit)

        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            candidate = paragraph if not current else f"{current}\n\n{paragraph}"
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(paragraph) <= limit:
                current = paragraph
                continue
            chunks.extend(self._chunk_lines_full(paragraph, limit))
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _chunk_lines_full(text: str, limit: int) -> list[str]:
        source = str(text or "").strip()
        if not source:
            return []
        if limit <= 0 or len(source) <= limit:
            return [source]

        lines = source.splitlines()
        chunks: list[str] = []
        current = ""
        for line in lines:
            clean = line.rstrip()
            candidate = clean if not current else f"{current}\n{clean}"
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(clean) <= limit:
                current = clean
                continue
            chunks.extend(CompiledBriefingService._split_long_line(clean, limit))
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _split_long_line(text: str, limit: int) -> list[str]:
        source = str(text or "").strip()
        if not source:
            return []
        if limit <= 0 or len(source) <= limit:
            return [source]

        chunks: list[str] = []
        rest = source
        while len(rest) > limit:
            window = rest[: limit + 1]
            split_at = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
            if split_at >= limit // 2:
                split_at += 1
            else:
                split_at = max(window.rfind(" "), window.rfind("\t"))
            if split_at < limit // 2:
                split_at = limit
            piece = rest[:split_at].rstrip()
            if piece:
                chunks.append(piece)
            rest = rest[split_at:].lstrip()
        if rest:
            chunks.append(rest)
        return chunks

    def _resolve_ai_cli(self, explicit_value: str | None) -> str:
        if explicit_value:
            return normalize_ai_cli(explicit_value)
        env_path = self.vault_path.parent / ".env"
        if env_path.exists():
            for raw_line in env_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() != "AI_CLI":
                    continue
                candidate = value.strip().strip('"').strip("'")
                if candidate:
                    try:
                        return normalize_ai_cli(candidate)
                    except ValueError:
                        logger.warning("Invalid AI_CLI in .env: %s", candidate)
                        break
        return "claude"

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        source = str(text or "").strip()
        if limit <= 0 or len(source) <= limit:
            return source
        return source[:limit].rstrip() + "\n...[truncated]"

    @staticmethod
    def _clean_line(value: Any) -> str:
        text = " ".join(str(value or "").split())
        return _defuse_human_zone_markers(text.strip())

    @staticmethod
    def _paragraph(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        # Line endings first: MULTILINE "^" reacts to "\n" alone, so a bare
        # "\r" is not a line start for _defuse_embedded_headings below --
        # but it is one for anything that reads the page with translated
        # line endings, which would resurrect the forged heading it just
        # let through. Model output has no reason to carry "\r" anyway.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return _defuse_embedded_headings(
            _defuse_human_zone_markers(re.sub(r"\n{3,}", "\n\n", text))
        )

    @staticmethod
    def _slugify(value: str) -> str:
        source = str(value or "").strip().lower()
        translit = source.maketrans(
            {
                "а": "a",
                "б": "b",
                "в": "v",
                "г": "g",
                "д": "d",
                "е": "e",
                "ё": "e",
                "ж": "zh",
                "з": "z",
                "и": "i",
                "й": "y",
                "к": "k",
                "л": "l",
                "м": "m",
                "н": "n",
                "о": "o",
                "п": "p",
                "р": "r",
                "с": "s",
                "т": "t",
                "у": "u",
                "ф": "f",
                "х": "h",
                "ц": "ts",
                "ч": "ch",
                "ш": "sh",
                "щ": "sch",
                "ъ": "",
                "ы": "y",
                "ь": "",
                "э": "e",
                "ю": "yu",
                "я": "ya",
            }
        )
        normalized = source.translate(translit)
        normalized = re.sub(r"[^0-9a-z]+", "-", normalized).strip("-")
        return normalized[:80]

    @staticmethod
    def _normalize_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        lines: list[str] = []
        for item in value:
            cleaned = " ".join(str(item or "").split()).strip()
            if cleaned:
                lines.append(_defuse_human_zone_markers(cleaned))
        return lines[:7]

    def _normalize_paths(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        paths: list[str] = []
        project_root = self.vault_path.parent.resolve()
        for item in value:
            raw = str(item or "").strip()
            if not raw:
                continue
            if raw.startswith("/"):
                absolute_path = Path(raw)
                try:
                    raw = absolute_path.relative_to(self.vault_path).as_posix()
                except ValueError:
                    try:
                        raw = absolute_path.relative_to(project_root).as_posix()
                    except ValueError:
                        continue
            raw = self._strip_relative_prefix(raw)
            if raw.startswith("claude/"):
                raw = f".{raw}"
            if raw.startswith("vault/"):
                raw = raw[len("vault/") :]
            if raw.endswith(".md") or "/" in raw:
                paths.append(raw)
        return paths

    @staticmethod
    def _strip_relative_prefix(raw: str) -> str:
        value = str(raw or "").strip()
        while value.startswith("./"):
            value = value[2:]
        return value

    @staticmethod
    def _merge_paths(*groups: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for item in group:
                path = str(item or "").strip()
                if not path or path in seen:
                    continue
                seen.add(path)
                merged.append(path)
        return merged

    @staticmethod
    def _render_bullets(lines: list[str], *, empty: str) -> list[str]:
        if not lines:
            return [f"- {empty}"]
        return [f"- {line}" for line in lines]

    @staticmethod
    def _render_sources(paths: list[str]) -> list[str]:
        if not paths:
            return ["- (none)"]
        return [f"- [[{path}]]" for path in paths]

    @staticmethod
    def _sections_from_text(text: str) -> set[str]:
        return set(re.findall(r"^##\s+(.+?)\s*$", text or "", re.MULTILINE))

    @staticmethod
    def _source_links_from_note(text: str) -> list[str]:
        if not text:
            return []
        match = re.search(
            r"^##\s+Sources\s*$\n([\s\S]*?)(?:^##\s+|\Z)",
            text,
            re.MULTILINE,
        )
        if match is None:
            return []
        return [
            path.strip()
            for path in WIKILINK_RE.findall(match.group(1))
            if path.strip()
        ]

    @classmethod
    def _sources_shaped_rows(cls, text: str) -> list[tuple[str, str, str]]:
        """Parse existing "Sources That Shaped This Page" table rows.

        Rows are only ever appended by ``_render_briefing``, in the order
        they first appeared; this parser preserves that order so a second
        compile pass never loses or reorders an earlier row.
        """
        section = cls._section_text(text, "Sources That Shaped This Page")
        rows: list[tuple[str, str, str]] = []
        for line in section.splitlines():
            match = SOURCES_SHAPED_TABLE_ROW_RE.match(line.strip())
            if match is None:
                continue
            date_value, source_value, what_value = match.groups()
            source_text = cls._unescape_table_cell(source_value)
            what_text = cls._unescape_table_cell(what_value)
            if what_text == NOT_ENRICHMENT_SOURCE_MARKER:
                continue
            rows.append((date_value, source_text, what_text))
        return rows

    @classmethod
    def _render_sources_shaped_table(
        cls,
        rows: list[tuple[str, str, str]],
    ) -> list[str]:
        rows = [row for row in rows if row[2] != NOT_ENRICHMENT_SOURCE_MARKER]
        if not rows:
            return ["(no sources recorded yet)"]
        lines = ["| Date | Source | What Added |", "| --- | --- | --- |"]
        for date_value, source_value, what_value in rows:
            source_cell = cls._escape_table_cell(source_value)
            what_cell = cls._escape_table_cell(what_value)
            lines.append(f"| {date_value} | [[{source_cell}]] | {what_cell} |")
        return lines

    @classmethod
    def _dated_rows(
        cls,
        text: str,
        heading: str,
        *,
        empty_placeholder: str,
    ) -> list[tuple[str, str, str]]:
        """Parse a dated-bullet section (Recent Changes / Open Loops /
        History, ТЗ 6.3) into ``(date, text, source)`` rows, oldest first.

        Rows are only ever appended by ``_render_briefing`` (new items) or
        moved in by ``_compress_candidate_text`` (Recent Changes overflow,
        stale Open Loops), so this parser preserves order the same way
        ``_sources_shaped_rows`` does. A bullet written before this dated
        format existed (a plain ``"- text"`` line) is kept as a best-effort
        row -- dated with this page's own ``updated`` field, no source --
        instead of being silently dropped when a page first accumulates
        under the new format.
        """
        section = cls._section_text(text, heading)
        fallback_date = (
            cls._frontmatter_fields(text).get("updated") or date.today().isoformat()
        )
        rows: list[tuple[str, str, str]] = []
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- ") or stripped == f"- {empty_placeholder}":
                continue
            match = DATED_BULLET_ROW_RE.match(stripped)
            if match is not None:
                row_date, row_text, row_source = match.groups()
                rows.append((row_date, row_text.strip(), (row_source or "").strip()))
                continue
            plain_text = stripped[2:].strip()
            if plain_text:
                rows.append((fallback_date, plain_text, ""))
        return rows

    @staticmethod
    def _render_dated_bullets(
        rows: list[tuple[str, str, str]],
        *,
        empty: str,
    ) -> list[str]:
        if not rows:
            return [f"- {empty}"]
        lines = []
        for row_date, row_text, row_source in rows:
            if row_source:
                lines.append(f"- {row_date}: {row_text} (source: [[{row_source}]])")
            else:
                lines.append(f"- {row_date}: {row_text}")
        return lines

    @classmethod
    def _existing_claims_catalog(cls, existing_text: str) -> str:
        """Existing "Sources That Shaped This Page" rows as JSON, shown to
        the model so ``conflicts[].existing_claim``/``existing_source`` can
        reference real page content (see ``_build_compile_prompt``) instead
        of being invented. This table is the sole storage location for
        claims (ТЗ 4.2), so it doubles as the existing-claims catalog.
        """
        rows = cls._sources_shaped_rows(existing_text)
        catalog = [{"source": source, "text": what} for _, source, what in rows]
        return json.dumps(catalog, ensure_ascii=False, indent=2)

    @classmethod
    def _claim_history_rows(cls, text: str) -> list[tuple[str, str, str, str]]:
        """Parse existing "Claim History" table rows: (date, old source,
        claim, new/superseding source). Same append-only contract as
        ``_sources_shaped_rows``.
        """
        section = cls._section_text(text, "Claim History")
        rows: list[tuple[str, str, str, str]] = []
        for line in section.splitlines():
            match = CLAIM_HISTORY_ROW_RE.match(line.strip())
            if match is None:
                continue
            date_value, source_value, claim_value, superseded_value = match.groups()
            rows.append(
                (
                    date_value,
                    cls._unescape_table_cell(source_value),
                    cls._unescape_table_cell(claim_value),
                    cls._unescape_table_cell(superseded_value),
                )
            )
        return rows

    @classmethod
    def _render_claim_history(cls, rows: list[tuple[str, str, str, str]]) -> list[str]:
        if not rows:
            return ["(no superseded claims yet)"]
        lines = [
            "| Date | Source | Claim | Superseded By |",
            "| --- | --- | --- | --- |",
        ]
        for date_value, source_value, claim_value, superseded_value in rows:
            source_cell = cls._escape_table_cell(source_value)
            claim_cell = cls._escape_table_cell(claim_value)
            superseded_cell = cls._escape_table_cell(superseded_value)
            lines.append(
                f"| {date_value} | [[{source_cell}]] | {claim_cell} | "
                f"[[{superseded_cell}]] |"
            )
        return lines

    @classmethod
    def _open_conflicts_rows(
        cls, text: str
    ) -> list[tuple[str, str, str, str, str]]:
        """Parse existing "Open Conflicts" table rows: (date, existing
        claim, existing source, new claim, new source). Same append-only
        contract as ``_sources_shaped_rows``.
        """
        section = cls._section_text(text, "Open Conflicts")
        rows: list[tuple[str, str, str, str, str]] = []
        for line in section.splitlines():
            match = OPEN_CONFLICTS_ROW_RE.match(line.strip())
            if match is None:
                continue
            date_value, existing_claim, existing_source, new_claim, new_source = (
                match.groups()
            )
            rows.append(
                (
                    date_value,
                    cls._unescape_table_cell(existing_claim),
                    cls._unescape_table_cell(existing_source),
                    cls._unescape_table_cell(new_claim),
                    cls._unescape_table_cell(new_source),
                )
            )
        return rows

    @classmethod
    def _render_open_conflicts_table(
        cls, rows: list[tuple[str, str, str, str, str]]
    ) -> list[str]:
        if not rows:
            return ["(no open conflicts)"]
        lines = [
            "| Date | Existing Claim | Existing Source | New Claim | New Source |",
            "| --- | --- | --- | --- | --- |",
        ]
        for date_value, existing_claim, existing_source, new_claim, new_source in rows:
            existing_claim_cell = cls._escape_table_cell(existing_claim)
            existing_source_cell = cls._escape_table_cell(existing_source)
            new_claim_cell = cls._escape_table_cell(new_claim)
            new_source_cell = cls._escape_table_cell(new_source)
            lines.append(
                f"| {date_value} | {existing_claim_cell} | "
                f"[[{existing_source_cell}]] | {new_claim_cell} | "
                f"[[{new_source_cell}]] |"
            )
        return lines

    @staticmethod
    def _escape_table_cell(value: str) -> str:
        return str(value or "").replace("|", "\\|")

    @staticmethod
    def _unescape_table_cell(value: str) -> str:
        return str(value or "").replace("\\|", "|")

    @staticmethod
    def _human_zone_span(text: str) -> tuple[int, int] | None:
        """Byte span of the owner's human zone within ``text``, or ``None``.

        ``_section_text``/``_replace_section``/``_insert_section_before``
        find ``## heading`` boundaries with a plain textual regex. The human
        zone is only designed to live inside ``## Owner Notes`` (always the
        last section), but nothing stops an owner from relocating the
        marker pair elsewhere by hand -- and if the note they write inside
        it happens to contain a line that reads exactly like one of the
        system headings those three functions search for, the naive
        first-match search would treat that line as a real section boundary
        and let a point-edit write clobber text inside the zone. This
        returns the zone's span so callers can skip any heading match that
        falls inside it.

        Mirrors ``add_links.py::human_zone_span`` / ``fix_links.py::
        _protect_human_zone``: a well-ordered single marker pair returns its
        span. Zero exact markers with no ``## Owner Notes`` heading either
        (see ``human_zone_markers_look_corrupted``) means there is nothing
        to protect (matches ``_extract_human_zone``'s "no zone yet" case)
        and returns ``None``. Anything else -- two or more markers of
        either kind, a single pair in reversed order, a single lone marker
        with no pair, or zero exact markers alongside the heading this
        code always renders together with them (see
        ``human_zone_markers_look_corrupted``) -- is ambiguous
        (``_extract_human_zone`` itself raises ``HumanZoneMarkerError`` on
        the same cases) -- which START pairs with which END (or whether a
        real pair exists at all) can't be guessed without risking silent
        corruption -- and returns the shared ``_AMBIGUOUS_HUMAN_ZONE``
        sentinel instead so the caller can fail closed.
        """
        starts = text.count(HUMAN_ZONE_START)
        ends = text.count(HUMAN_ZONE_END)
        if starts == 0 and ends == 0:
            if human_zone_markers_look_corrupted(text):
                return _AMBIGUOUS_HUMAN_ZONE
            return None
        if starts != 1 or ends != 1:
            return _AMBIGUOUS_HUMAN_ZONE
        start_index = text.find(HUMAN_ZONE_START)
        end_index = text.find(HUMAN_ZONE_END)
        if end_index < start_index:
            return _AMBIGUOUS_HUMAN_ZONE
        return (start_index, end_index + len(HUMAN_ZONE_END))

    @staticmethod
    def _heading_match(
        text: str,
        pattern: re.Pattern[str],
        zone: tuple[int, int] | None,
        *,
        start: int = 0,
    ) -> re.Match[str] | None:
        """First match of ``pattern`` at or after ``start``, skipping any
        match that starts inside ``zone`` (see ``_human_zone_span``).
        """
        for match in pattern.finditer(text, start):
            if zone is not None and zone[0] <= match.start() < zone[1]:
                continue
            return match
        return None

    @classmethod
    def _section_text(cls, text: str, heading: str) -> str:
        text = text or ""
        zone = cls._human_zone_span(text)
        if zone == _AMBIGUOUS_HUMAN_ZONE:
            # Fail closed (see _human_zone_span): can't tell what's inside
            # the zone and what isn't, so treat the section as unreadable
            # rather than risk surfacing text the owner meant to protect.
            return ""
        heading_pattern = re.compile(
            rf"^##\s+{re.escape(heading)}\s*$\n", re.MULTILINE
        )
        heading_match = cls._heading_match(text, heading_pattern, zone)
        if heading_match is None:
            return ""
        boundary_pattern = re.compile(r"^##\s+", re.MULTILINE)
        boundary_match = cls._heading_match(
            text, boundary_pattern, zone, start=heading_match.end()
        )
        body_end = boundary_match.start() if boundary_match is not None else len(text)
        # The zone has no heading of its own to stop at -- if it sits
        # inside this body span (e.g. relocated between two system
        # sections, with no real "## ..." line between the target heading
        # and the zone), the plain next-heading search above walks straight
        # through it to whatever real heading comes after. Clamp the body
        # to end where the zone starts instead, so the zone is never read
        # as part of this section's text.
        if zone is not None and heading_match.end() <= zone[0] < body_end:
            body_end = zone[0]
        return text[heading_match.end() : body_end].strip()

    @classmethod
    def _section_bullets(cls, text: str, heading: str) -> list[str]:
        section = cls._section_text(text, heading)
        return [
            line[2:].strip()
            for line in section.splitlines()
            if line.startswith("- ") and line[2:].strip()
        ]

    @classmethod
    def _replace_section(cls, text: str, heading: str, new_lines: list[str]) -> str:
        """Replace one ``## heading`` section's body in place.

        Every other section -- including the human zone -- is left
        byte-identical. Used by the ТЗ 6.3 compression step and by the ТЗ
        6.1 non-enrichment source write, both of which must only ever touch
        the one section they own. A no-op (returns ``text`` unchanged) when
        the heading is not present at all -- callers that need to create the
        section use ``_insert_section_before`` instead. Also a no-op when
        the human zone's markers are ambiguous (see ``_human_zone_span``):
        refusing to guess a heading's true position beats risking a write
        into the zone.
        """
        zone = cls._human_zone_span(text)
        if zone == _AMBIGUOUS_HUMAN_ZONE:
            return text
        heading_pattern = re.compile(
            rf"^##\s+{re.escape(heading)}\s*$\n", re.MULTILINE
        )
        heading_match = cls._heading_match(text, heading_pattern, zone)
        if heading_match is None:
            return text
        boundary_pattern = re.compile(r"^##\s+", re.MULTILINE)
        boundary_match = cls._heading_match(
            text, boundary_pattern, zone, start=heading_match.end()
        )
        body_end = boundary_match.start() if boundary_match is not None else len(text)
        # Same clamp as _section_text: without a heading of its own, a zone
        # relocated between this section and the next real heading is part
        # of the naive body span. Stop the replaced range at the zone
        # instead of at the next real heading, or the splice below
        # (text[:heading_match.end()] + replacement + text[body_end:])
        # would drop the zone along with the body it is replacing.
        if zone is not None and heading_match.end() <= zone[0] < body_end:
            body_end = zone[0]
        replacement_body = "\n".join(new_lines) + "\n\n"
        return text[: heading_match.end()] + replacement_body + text[body_end:]

    @classmethod
    def _has_section(cls, text: str, heading: str) -> bool:
        """Whether ``## heading`` exists on this page, outside the human zone.

        ``_replace_section`` is a documented no-op on a missing heading, so
        anything that moves content *between* two sections has to ask first:
        otherwise the removal half of the move lands and the write half
        silently does not, and the content is simply gone. Retiring a losing
        claim into "Claim History" is exactly that kind of move.
        """
        zone = cls._human_zone_span(text)
        if zone == _AMBIGUOUS_HUMAN_ZONE:
            return False
        pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$\n", re.MULTILINE)
        return cls._heading_match(text, pattern, zone) is not None

    @classmethod
    def _ensure_claim_history_section(cls, text: str) -> str:
        """Put an empty "Claim History" section back on a page that lost it.

        Every page ``_render_page`` writes carries one, so a page without it
        was edited by hand. Refusing to retire a claim there (see
        ``_has_section``) protects the claim, but on its own it would also
        park that page's conflicts forever -- the one dead end left in an
        otherwise self-draining queue. So the section is restored, empty, in
        its canonical slot, and the caller re-checks.

        A no-op -- and the caller then still declines to retire anything --
        when the section is already there, when the human zone's markers are
        ambiguous, or when neither anchor heading exists on the page.
        """
        if cls._has_section(text, "Claim History"):
            return text
        anchor = "History" if cls._has_section(text, "History") else "Owner Notes"
        return cls._insert_section_before(
            text,
            heading="Claim History",
            before_heading=anchor,
            new_lines=cls._render_claim_history([]),
        )

    @classmethod
    def _insert_section_before(
        cls,
        text: str,
        *,
        heading: str,
        before_heading: str,
        new_lines: list[str],
    ) -> str:
        """Insert a brand-new ``## heading`` section right before an
        existing ``## before_heading`` section, matching the one-blank-line
        spacing every other section uses.

        Used only to add the ТЗ 6.3 History section the first time a page
        compresses -- once present, later passes go through
        ``_replace_section`` instead. A no-op when ``before_heading`` is not
        present (should not happen: ``## Owner Notes`` is always rendered)
        or when the human zone's markers are ambiguous (see
        ``_human_zone_span``): a real ``before_heading`` line living inside
        a relocated, malformed zone must not be mistaken for the anchor.
        """
        zone = cls._human_zone_span(text)
        if zone == _AMBIGUOUS_HUMAN_ZONE:
            return text
        before_pattern = re.compile(
            rf"^##\s+{re.escape(before_heading)}\s*$", re.MULTILINE
        )
        before_match = cls._heading_match(text, before_pattern, zone)
        if before_match is None:
            return text
        block = f"## {heading}\n" + "\n".join(new_lines) + "\n\n"
        return text[: before_match.start()] + block + text[before_match.start() :]

    @staticmethod
    def _metadata_value(value: Any) -> str:
        raw = str(value or "").strip()
        if raw.startswith('"') and raw.endswith('"'):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return raw.strip('"')
            return str(decoded).strip()
        return raw

    @classmethod
    def _validated_date_field(
        cls,
        raw_value: Any,
        *,
        field: str,
        page_path: str,
    ) -> str:
        """Return a plain YYYY-MM-DD value, or "" (renders as YAML null).

        ``last_verified``/``human_reviewed`` render as a bare
        ``field: {value}`` line, unlike description/decision_owner which go
        through ``json.dumps``. A value containing a colon (e.g. a stray
        "unverified: check again") would therefore produce invalid YAML,
        which fails the write validation on every subsequent compile pass
        and freezes the page silently. Anything that is not a plain date is
        dropped instead, with a warning so the loss is visible.
        """
        value = cls._metadata_value(raw_value)
        if not value:
            return ""
        if DATE_ONLY_RE.match(value):
            return value
        logger.warning(
            "Compiled briefing %s has invalid %s value %r; clearing it",
            page_path,
            field,
            value,
        )
        return ""

    @classmethod
    def _frontmatter_fields(cls, text: str) -> dict[str, str]:
        # CRLF line endings (e.g. from Windows' `git config core.autocrlf
        # true`) break FRONTMATTER_RE's literal `\n` right after the
        # opening `---`, same as _semantic_source_text normalizes above --
        # without this every field, including `tier`, would read as absent.
        match = FRONTMATTER_RE.match((text or "").replace("\r\n", "\n"))
        if match is None:
            return {}
        fields: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            # Point-edit write paths (see _promote_archive_tier,
            # _record_non_enrichment_source) patch single fields with
            # patch_frontmatter_bytes, which always JSON-quotes string
            # values (e.g. `tier: "warm"`) -- unlike _render_briefing's own
            # plain `f"tier: {tier}"` convention used for the same fields.
            # Decode through ``_metadata_value`` -- the one place that
            # unescapes a JSON-quoted value -- instead of naively stripping
            # one wrapping pair of quotes: a naive strip leaves any escaped
            # inner quote untouched, so a value like `"Иван \"Ваня\"
            # Петров"` comes out with the backslashes still in it, and each
            # later json.dumps re-escapes what is already escaped. Every
            # reader of this dict (and every direct caller of
            # ``_metadata_value`` that later re-wraps one of these values)
            # sees the same fully-decoded value regardless of which write
            # path produced it.
            fields[key.strip()] = cls._metadata_value(value)
        return fields

    @classmethod
    def _extra_frontmatter_lines(
        cls, existing_text: str, *, page_path: str
    ) -> list[str]:
        """Frontmatter lines for owner-layer fields this code does not own
        (the passthrough loop in ``_render_briefing``, e.g. ``duplicate_of``).

        Deliberately re-parses the real frontmatter YAML instead of reusing
        ``existing_meta``/``_frontmatter_fields``: that dict flattens every
        value to a plain Python string, which is harmless for the fields
        this code owns (always simple scalars it wrote itself) but silently
        changes an *owner*-written field's YAML type on the very next
        render -- a number or boolean becomes a quoted string, a list
        becomes its bracketed text, a null becomes the literal string
        "null", and a multi-line block scalar loses everything past its
        first line. Re-parsing with the real loader keeps each value's
        actual type, so ``json.dumps`` re-emits it losslessly (numbers and
        booleans as themselves, null as ``null``, lists/dicts as flow
        syntax, multi-line strings as one escaped quoted line -- all valid
        YAML). A value the safe loader could not construct at all, or
        frontmatter that fails to reparse, is dropped with a warning
        instead of written wrong.
        """
        try:
            document = parse_frontmatter_bytes(existing_text.encode("utf-8"))
        except FrontmatterError as exc:
            logger.warning(
                "Compiled briefing %s has frontmatter this rebuild cannot "
                "reparse (%s); owner-layer fields cannot be carried "
                "forward this pass",
                page_path,
                exc,
            )
            return []
        lines: list[str] = []
        for key, value in document.fields.items():
            if key in CORE_FRONTMATTER_FIELDS:
                continue
            try:
                encoded = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                logger.warning(
                    "Compiled briefing %s carries owner-layer field %r "
                    "whose value cannot be preserved without corrupting "
                    "it; dropping it instead of writing it wrong",
                    page_path,
                    key,
                )
                continue
            lines.append(f"{key}: {encoded}")
        return lines

    @staticmethod
    def _decode_page_bytes(raw: bytes) -> str:
        """Decode one compiled page's bytes the way every reader here does."""

        return raw.decode("utf-8", errors="replace")

    @classmethod
    def _read_page_text(cls, path: Path) -> str:
        """Read one compiled page the way ``_upsert_briefing`` writes it.

        Deliberately not ``read_text``: its universal-newline translation
        turns every "\\r" on the page into "\\n", so the same file parses
        into different sections depending on which reader opened it, and
        the text compares unequal to the bytes on disk -- which is how
        ``_compress_cooled_pages``'s compare-and-swap decides the page
        changed under it and silently skips the page forever. The owner
        only has to write a note with CRLF endings inside their own zone
        for that to happen.
        """

        return cls._decode_page_bytes(path.read_bytes())

    @staticmethod
    def _title_from_text(text: str) -> str:
        match = TITLE_RE.search(text or "")
        return str(match.group(1)).strip() if match else ""

    @staticmethod
    def _body_without_frontmatter(text: str) -> str:
        # Code review defect 2: the body this returns feeds
        # _extract_human_zone, whose output must survive recompilation
        # byte-for-byte -- so the slice below always comes from the
        # *original* ``text``, never from a CRLF-normalized copy (a
        # normalized copy would silently turn every "\r\n" inside the
        # owner's zone into "\n"). ``_FRONTMATTER_BOUNDARY_RE`` tolerates
        # "\r\n" directly (see _frontmatter_fields for why plain "\n"
        # alone isn't enough), so it can match ``text`` as-is with no
        # normalization step at all.
        text = text or ""
        match = _FRONTMATTER_BOUNDARY_RE.match(text)
        if match is None:
            return text
        return text[match.end() :]

    @classmethod
    def _extract_human_zone(cls, text: str) -> str:
        """Extract the human-owned zone verbatim, or build an empty scaffold.

        Fail-closed by design: a page must carry exactly one well-ordered
        marker pair, or none at all. Anything else (missing end marker,
        duplicated markers, reversed order) raises ``HumanZoneMarkerError``
        rather than guessing, so the caller skips the page instead of
        risking the owner's text.

        Zero exact markers is not automatically "no zone yet" -- see
        ``human_zone_markers_look_corrupted``: a page carrying the ``##
        Owner Notes`` heading this code always renders together with the
        markers, or one whose frontmatter says the zone has held real text
        before (``human_zone_populated``), but no exact markers, has had
        them corrupted since (any typo, homoglyph, stray dash, or other
        mangling that drives the exact count to zero -- possibly alongside
        damage to the heading too, see ``human_zone_markers_look_corrupted``
        for why the frontmatter flag exists). Treating that as "never had a
        zone" would silently discard the owner's text between them by
        returning an empty scaffold, so that case also raises rather than
        guessing.

        Takes the full page text (frontmatter included), not just the
        body -- unlike ``_body_without_frontmatter``'s other caller, this
        one needs ``human_zone_markers_look_corrupted`` to see the
        frontmatter too, so the corresponding body is derived here instead
        of by the caller.
        """
        body = cls._body_without_frontmatter(text)
        starts = body.count(HUMAN_ZONE_START)
        ends = body.count(HUMAN_ZONE_END)
        if starts == 0 and ends == 0:
            if human_zone_markers_look_corrupted(text):
                raise HumanZoneMarkerError(
                    "human zone markers look corrupted: '## Owner Notes' "
                    "heading or human_zone_populated frontmatter flag found "
                    "but no exact marker present"
                )
            return f"{HUMAN_ZONE_START}\n{HUMAN_ZONE_END}"
        if starts != 1 or ends != 1:
            raise HumanZoneMarkerError(
                "expected exactly one human zone marker pair, found "
                f"start={starts} end={ends}"
            )
        start_index = body.index(HUMAN_ZONE_START)
        end_index = body.index(HUMAN_ZONE_END)
        if end_index < start_index:
            raise HumanZoneMarkerError(
                "human zone end marker appears before the start marker"
            )
        return body[start_index : end_index + len(HUMAN_ZONE_END)]

    @staticmethod
    def _float_value(value: str | None, *, default: float) -> float:
        try:
            return float(str(value or "").strip())
        except ValueError:
            return default

    @staticmethod
    def _int_value(value: str | None, *, default: int) -> int:
        raw = str(value or "").strip()
        try:
            parsed = int(raw)
        except ValueError:
            if raw:
                logger.warning(
                    "Compiled briefing has non-numeric counter value %r; "
                    "using default %d",
                    raw,
                    default,
                )
            return default
        # Counters (enrichment_count, conflicts_open) are never meaningful
        # below zero; clamp instead of letting a corrupted negative value
        # keep counting further away from zero on every write.
        return max(parsed, 0)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token.lower() for token in TOKEN_RE.findall(text or "")}

    @classmethod
    def _text_duplicates_human_zone(
        cls,
        text: str,
        human_zone_tokens: set[str],
    ) -> bool:
        if not human_zone_tokens or not text:
            return False
        text_tokens = cls._tokens(text)
        if len(text_tokens) < HUMAN_ZONE_DUPLICATE_MIN_TOKENS:
            return False
        overlap = len(text_tokens & human_zone_tokens)
        return (
            overlap / len(text_tokens)
        ) >= HUMAN_ZONE_DUPLICATE_OVERLAP_THRESHOLD

    @classmethod
    def _filter_list_duplicating_human_zone(
        cls,
        lines: list[str],
        human_zone_tokens: set[str],
    ) -> list[str]:
        if not human_zone_tokens:
            return lines
        return [
            line
            for line in lines
            if not cls._text_duplicates_human_zone(line, human_zone_tokens)
        ]

    @classmethod
    def _filter_text_duplicating_human_zone(
        cls,
        text: str,
        human_zone_tokens: set[str],
    ) -> str:
        if cls._text_duplicates_human_zone(text, human_zone_tokens):
            return ""
        return text

    def _merged_relevance(
        self,
        existing_meta: dict[str, str],
        signal: dict[str, Any] | None,
    ) -> float:
        existing = self._float_value(existing_meta.get("relevance"), default=0.0)
        if signal is None:
            return max(existing, 0.72)
        try:
            source_relevance = float(signal.get("relevance", 0) or 0)
        except (TypeError, ValueError):
            source_relevance = 0.0
        return max(existing, source_relevance, 0.72)

    def _merged_tier(
        self,
        existing_meta: dict[str, str],
        signal: dict[str, Any] | None,
    ) -> str:
        """Tier-authority split (ТЗ 6, plan point 5): the memory engine
        (``skills/agent-memory/scripts/memory-engine.py``) is authoritative
        for DOWNGRADES (decay) and for its own usage-based promotion
        (``touch``); this compiled layer only ever merges in a tier that is
        the same or higher than what is already on disk, and never lowers
        it -- exactly like ``_promote_archive_tier`` below, the layer's
        other upward-only tier edit. Neither may write a tier ranked below
        ``existing``.
        """
        existing = str(existing_meta.get("tier") or "").strip().lower()
        source = str((signal or {}).get("tier") or "").strip().lower()
        existing_rank = TIER_RANK.get(existing, 0)
        source_rank = TIER_RANK.get(source, 0)
        if existing_rank >= source_rank and existing:
            return existing
        if source:
            return source
        return "active"

    # --- Claims/conflicts (ТЗ 4.4, 5.3, 5.4) --------------------------------

    @staticmethod
    def _excerpt_entry_headers(excerpt: str) -> list[str]:
        """Every daily-entry header line in a source excerpt.

        A chunked excerpt (``_daily_source_chunks``) holds exactly one entry
        plus a synthetic ``"# <stem>"`` day title, but the freshness backfill
        path (``_refresh_candidate``) re-reads whole daily files, so an
        excerpt can just as well hold a full day of entries.

        A line is only a header where ``DAILY_ENTRY_SPLIT_RE`` would also
        cut, i.e. with "##" at the true start of the line. Matching against
        a stripped line instead would re-promote exactly what
        ``escape_embedded_daily_headers`` defuses by indenting it: a body
        line someone else's forwarded text merely contains. That matters
        most where the excerpt carries no genuine header of its own -- a
        ``upsert_daily_block`` block, e.g. a PLAUD meeting summary -- since
        there the forged header would be the only one this rating sees.

        Split on "\\n" alone, not with ``str.splitlines()``: the latter also
        breaks on "\\v", "\\f", U+2028 and friends, none of which
        ``DAILY_ENTRY_SPLIT_RE``'s MULTILINE "^" treats as a line start.
        A "## HH:MM [forward from: ...]" sitting behind one of those is
        body text, not a header, and counting it here rated a genuine
        entry of the owner's by a header no splitter would ever cut on --
        text pasted from a PDF carries U+2028 without anyone noticing.
        """
        return [
            line.rstrip()
            for line in str(excerpt or "").split("\n")
            if DAILY_ENTRY_MARK_RE.match(line)
        ]

    @classmethod
    def _source_trust_level(cls, source_rel_path: str, source_excerpt: str) -> str:
        """Provenance trust level, determined by code only (ТЗ 4.4). The
        model never participates and its self-reported ``claims[].source``
        is never consulted -- only the pipeline's own ``source_rel_path``.
        Fails closed to ``"inferred"`` (the weakest level) whenever the path
        and excerpt do not clearly match a stronger rule.

        A daily excerpt is rated by the weakest of ALL the entries it holds,
        not by its first one: an excerpt covering a whole day mixes the
        owner's own entries with forwarded ones, and there is no way to tell
        which entry a given claim came from. Reading only the first header
        would let a morning ``[voice]`` entry lend its "own" trust to a
        message someone else forwarded that afternoon.

        PLAUD meeting recordings (``imports/plaud/``) and documents the owner
        forwarded (``imports/documents/forwarded/``) are capped at
        ``"forwarded"`` rather than the general ``imports/`` rule -- see
        ``IMPORTS_PLAUD_PREFIX`` above.
        """
        if source_rel_path.startswith("thoughts/"):
            return "own"
        if source_rel_path.startswith(IMPORTS_PLAUD_PREFIX):
            return "forwarded"
        if source_rel_path.startswith(IMPORTS_DOCUMENTS_FORWARDED_PREFIX):
            return "forwarded"
        if source_rel_path.startswith("imports/"):
            return "integration"
        if source_rel_path.startswith("daily/"):
            headers = cls._excerpt_entry_headers(source_excerpt)
            if not headers:
                return "inferred"
            level = "own"
            for header in headers:
                if FORWARD_MARK_RE.match(header):
                    level = cls._min_trust(level, "forwarded")
                elif OWN_ENTRY_MARK_RE.match(header):
                    level = cls._min_trust(level, "own")
                else:
                    level = cls._min_trust(level, "inferred")
            return level
        return "inferred"

    @staticmethod
    def _min_trust(first: str, second: str) -> str:
        """Weaker of two trust levels (page-level trust is the fail-closed
        minimum across all of a page's sources, ТЗ 4.3/4.4)."""
        if TRUST_RANK.get(second, 0) < TRUST_RANK.get(first, 0):
            return second
        return first

    @classmethod
    def _trust_allows_consequential_action(cls, trust: str) -> bool:
        """ТЗ 4.4: whether a trust level is, alone, strong enough to justify
        an automatic action with consequences (task creation, CRM edit,
        silently superseding an existing claim)."""
        return trust in CONSEQUENTIAL_ACTION_TRUST_LEVELS

    @classmethod
    def _normalize_claims(
        cls,
        raw_claims: Any,
        *,
        source_rel_path: str,
    ) -> list[dict[str, str]]:
        """Validate/clean the model's ``claims`` list.

        ``source`` is always the pipeline's own ``source_rel_path``, never
        the model's self-reported value -- trust is code-determined (ТЗ
        4.4) and a claim can only ever come from the one source excerpt
        actually being processed this pass.
        """
        if not isinstance(raw_claims, list):
            return []
        claims: list[dict[str, str]] = []
        seen_texts: set[str] = set()
        for item in raw_claims:
            if not isinstance(item, dict):
                continue
            text = cls._clean_line(item.get("text"))
            if not text or text in seen_texts:
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in CLAIM_KIND_VALUES:
                kind = "fact"
            claims.append({"text": text, "source": source_rel_path, "kind": kind})
            seen_texts.add(text)
        # Sort before truncating, so that both the page's row order and
        # *which* claims survive MAX_CLAIMS_PER_PASS depend only on the claim
        # texts themselves -- never on the order the model happened to emit
        # them in. The same source must always compile to the same page bytes
        # (ТЗ 7, идемпотентность).
        claims.sort(key=lambda claim: claim["text"])
        return claims[:MAX_CLAIMS_PER_PASS]

    @classmethod
    def _normalize_conflicts(
        cls,
        raw_conflicts: Any,
        *,
        claims: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Validate/clean the model's ``conflicts`` list.

        Deliberately diverges from the ТЗ 5.3 literal example in two ways
        (see final report): ``existing_source`` is required (without it
        there is no way to look up the existing claim's date for the
        temporal override), and ``new_claim`` must verbatim-match one of
        this pass's own ``claims`` -- the model cannot flag a conflict
        against text it did not also propose adding.

        ``context_note`` (ТЗ 5.4 contextual explanation): cleaned the same
        way as every other free-text field here; an empty value is allowed
        -- ``_apply_claims_and_conflicts`` silently skips appending nothing
        rather than treating it as a validation failure.
        """
        if not isinstance(raw_conflicts, list):
            return []
        claim_texts = {claim["text"] for claim in claims}
        conflicts: list[dict[str, str]] = []
        for item in raw_conflicts:
            if not isinstance(item, dict):
                continue
            existing_claim = cls._clean_line(item.get("existing_claim"))
            existing_source = cls._clean_line(item.get("existing_source"))
            new_claim = cls._clean_line(item.get("new_claim"))
            if not existing_claim or not existing_source or not new_claim:
                continue
            if new_claim not in claim_texts:
                continue
            conflict_type = str(item.get("type") or "").strip().lower()
            if conflict_type not in CONFLICT_TYPE_VALUES:
                conflict_type = ""
            context_note = cls._clean_line(item.get("context_note"))
            conflicts.append(
                {
                    "existing_claim": existing_claim,
                    "existing_source": existing_source,
                    "new_claim": new_claim,
                    "type": conflict_type,
                    "context_note": context_note,
                }
            )
        # Same reason as in ``_normalize_claims``: resolution order decides
        # the order of the rows appended to Claim History / Open Conflicts,
        # so it must come from the conflict contents, not from the model's
        # array order.
        conflicts.sort(
            key=lambda conflict: (
                conflict["existing_source"],
                conflict["existing_claim"],
                conflict["new_claim"],
                conflict["type"],
            )
        )
        return conflicts

    @staticmethod
    def _verify_sample_size(claim_count: int, page_tier: str) -> int:
        """ТЗ 5.6: core/active pages verify every new claim; every other
        tier (including warm, and any unset/unrecognized tier -- fail
        closed rather than skipping Verify) samples 25%, minimum one."""
        if claim_count <= 0:
            return 0
        if page_tier in ("core", "active"):
            return claim_count
        return max(1, math.ceil(claim_count * VERIFY_WARM_SAMPLE_FRACTION))

    def _build_verify_prompt(
        self,
        *,
        claims: list[dict[str, str]],
        source_rel_path: str,
        source_excerpt: str,
        target_title: str = "",
        candidate_payload: dict[str, Any] | None = None,
        candidate_markdown: str = "",
        existing_claims: str = "[]",
    ) -> str:
        claims_payload = [
            {"index": index, "text": claim["text"]}
            for index, claim in enumerate(claims)
        ]
        return (
            "You are verifying claims extracted from one source note, with a "
            "clean, read-only context (a separate pass from the one that "
            "proposed them).\n"
            "Return ONLY JSON.\n\n"
            "For each claim, decide whether it follows from the given source "
            "excerpt alone. Do not use outside knowledge or assumptions, and do "
            "not fetch or reference anything outside the excerpt below.\n"
            "A claim is supported only if the source excerpt states it or "
            "clearly implies it. Also inspect the complete candidate page Markdown "
            "for blocking quality problems. The JSON describes the new model "
            "proposal; the Markdown is the final page after it is merged with "
            "existing content.\n\n"
            "Required JSON schema:\n"
            f"{VERIFY_JSON_EXAMPLE}\n\n"
            "Rules:\n"
            "- Return exactly one verdict per claim.\n"
            '- Copy "index" back exactly as given for the claim -- it is how '
            "your verdict is matched back to the claim, so it must be "
            "correct even if you paraphrase the claim text.\n"
            '- Echo the claim text back exactly as given in "text".\n'
            "- Reject a claim that selects one side of contradictory or "
            "ambiguous source statements without preserving that uncertainty.\n"
            "- Reject a claim that upgrades proposed or planned work to accepted, "
            "completed, or confirmed.\n"
            "- Reject any owner, date, status, outcome, or alternative that is not "
            "explicitly supported by the source excerpt.\n"
            "- Set source_coverage=true only when every substantive new or changed "
            "statement anywhere in the candidate is represented by a supported "
            "claim, or is preserved from EXISTING_VERIFIED_CLAIMS.\n"
            "- Set target_scope=true only when every candidate statement directly "
            "belongs to TARGET_TITLE; adjacent projects, people, or topics fail "
            "this check.\n"
            "- Set timeline_consistency=true only when dates, owners, statuses, "
            "outcomes, and confidence agree across the candidate and evidence, or "
            "the uncertainty/conflict is explicitly preserved.\n"
            "- page_issues must be an empty list when the candidate is coherent "
            "and supported. Otherwise list each blocking issue briefly.\n"
            "- A blocking page issue is: an internal contradiction; a substantive "
            "statement not supported by the changed source or EXISTING_VERIFIED_"
            "CLAIMS; a repeated claim in multiple sections; a decision named by "
            "TARGET_TITLE missing from key_decisions; status inflation; an owner "
            "whose assignment or scope is unsupported; an invented alternative; "
            "or adjacent work unrelated to the target.\n"
            "- EXISTING_VERIFIED_CLAIMS may support preserved older context, but "
            "never use it to pretend the changed source supplied new evidence.\n"
            "- Do not report wording preferences as page issues.\n"
            "- Keep \"reason\" short.\n\n"
            "[TARGET_TITLE]\n"
            f"{target_title}\n\n"
            "[CANDIDATE_PAGE_JSON]\n"
            f"{json.dumps(candidate_payload or {}, ensure_ascii=False, indent=2)}\n\n"
            "[CANDIDATE_PAGE_MARKDOWN]\n"
            f"{candidate_markdown}\n\n"
            "[EXISTING_VERIFIED_CLAIMS]\n"
            f"{existing_claims}\n\n"
            "[SOURCE_PATH]\n"
            f"{source_rel_path}\n\n"
            "[SOURCE_EXCERPT]\n"
            f"{source_excerpt}\n\n"
            "[CLAIMS]\n"
            f"{json.dumps(claims_payload, ensure_ascii=False, indent=2)}\n"
        )

    def _mark_verify_unavailable(
        self,
        candidate_payload: dict[str, Any],
        *,
        source_rel_path: str,
        reason: str,
    ) -> None:
        """Record that Verify produced no usable verdict for this page.

        Used only for a reply broken as a *format*, never for an honest
        rejection: the model said nothing about the claims, so they are kept
        and the page carries ``quality_status: needs_review`` with this
        reason instead (``_render_briefing`` reads both ``_quality_*`` keys).
        Because the mark is set, ``last_verified`` stays where it was -- the
        page is written, but it is not claimed to be verified.
        """
        if self._active_pass is not None:
            self._active_pass.verify_format_drift += 1
        logger.warning(
            "Compiled briefing Verify unusable for %s: %s; страница будет "
            "сохранена с пометкой needs_review",
            source_rel_path,
            reason,
        )
        candidate_payload["_quality_verification_completed"] = True
        candidate_payload["_quality_issues"] = [
            f"{reason} — утверждения не проверены"
        ]

    def _verify_claims_batch(
        self,
        *,
        claims: list[dict[str, str]],
        source_rel_path: str,
        source_excerpt: str,
        page_tier: str,
        target_title: str = "",
        candidate_payload: dict[str, Any] | None = None,
        candidate_markdown: str = "",
        existing_claims: str = "[]",
    ) -> list[dict[str, str]]:
        """Batched Verify (ТЗ 5.2 step 4): one model call per page, sampling
        per ``_verify_sample_size``. Claims outside the sample pass through
        unchecked. A rejected sampled claim is dropped (ТЗ 5.2: "утверждение
        не попадает на страницу"); a majority-rejected sample no longer
        aborts the write, it marks the page ``quality_status: needs_review``
        instead, so the owner gets a page with a stated reason rather than
        nothing at all.

        A Verify reply that is broken *as a format* -- unparseable JSON, or
        missing ``page_checks``/``page_issues`` -- is a different case from
        an honest rejection: the model said nothing about these claims, so
        there is no ground to drop them. They pass through unverified and
        the page is marked ``needs_review`` with that reason (see
        ``_mark_verify_unavailable``), which also keeps ``last_verified``
        from moving.
        """
        sample_size = self._verify_sample_size(len(claims), page_tier)
        sample = claims[:sample_size]
        if not sample:
            return claims

        prompt = self._build_verify_prompt(
            claims=sample,
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
            target_title=target_title,
            candidate_payload=candidate_payload,
            candidate_markdown=candidate_markdown,
            existing_claims=existing_claims,
        )
        try:
            payload = self._run_json_dict_prompt(
                prompt=prompt,
                timeout=VERIFY_TIMEOUT_SECONDS,
                error_context="compiled briefing claim verification",
                json_example=VERIFY_JSON_EXAMPLE,
            )
        except ValueError as exc:
            # A Verify reply that never parses as the required structure --
            # even after the JSON-repair retry inside
            # ``_run_json_dict_prompt`` -- says nothing about the claims,
            # so it is not treated as a rejection of them. The page is
            # written with ``quality_status: needs_review`` and this reason
            # instead; without a candidate payload to carry that mark there
            # is nowhere to record it, so that (today unreachable) path
            # still escalates as before.
            if candidate_payload is None:
                raise CompiledBriefingVerificationRejectedError(
                    f"Verify response for {source_rel_path} did not parse as "
                    f"JSON even after repair; treated as a full rejection ({exc})"
                ) from exc
            self._mark_verify_unavailable(
                candidate_payload,
                source_rel_path=source_rel_path,
                reason=f"Verify не вернул разбираемый JSON ({exc})",
            )
            return claims
        if candidate_payload is not None:
            candidate_payload.pop("_quality_verification_completed", None)
            candidate_payload.pop("_quality_issues", None)
            raw_page_checks = payload.get("page_checks")
            if not isinstance(raw_page_checks, dict) or any(
                not isinstance(raw_page_checks.get(key), bool)
                for key in VERIFY_PAGE_CHECK_KEYS
            ):
                self._mark_verify_unavailable(
                    candidate_payload,
                    source_rel_path=source_rel_path,
                    reason="Verify не вернул корректное поле page_checks",
                )
                return claims
            failed_page_checks = [
                key for key in VERIFY_PAGE_CHECK_KEYS if not raw_page_checks[key]
            ]
            raw_page_issues = payload.get("page_issues")
            if not isinstance(raw_page_issues, list) or any(
                not isinstance(issue, str) for issue in raw_page_issues
            ):
                self._mark_verify_unavailable(
                    candidate_payload,
                    source_rel_path=source_rel_path,
                    reason="Verify не вернул корректный список page_issues",
                )
                return claims
            page_issues = [issue.strip() for issue in raw_page_issues if issue.strip()]
            candidate_payload["_quality_verification_completed"] = True
            candidate_payload["_quality_issues"] = [
                *(
                    ["failed checks: " + ", ".join(failed_page_checks)]
                    if failed_page_checks
                    else []
                ),
                *page_issues,
            ]

        raw_verdicts = payload.get("verdicts")
        # Matched by position (the "index" each claim was given in the
        # prompt), not by echoed-back text: text matching is fragile to
        # truncation, skipped items, or the model paraphrasing instead of
        # quoting verbatim, and any of those previously made a claim fall
        # through as silently "not rejected". Index is trivial for the model
        # to copy back correctly even when it does not quote the claim text
        # exactly.
        verdicts_by_index: dict[int, dict[str, Any]] = {}
        if isinstance(raw_verdicts, list):
            for verdict in raw_verdicts:
                if not isinstance(verdict, dict):
                    continue
                index = verdict.get("index")
                if isinstance(index, bool) or not isinstance(index, int):
                    continue
                verdicts_by_index[index] = verdict

        if raw_verdicts and not verdicts_by_index:
            # Code review Finding 3 (operational risk): a non-empty
            # ``verdicts`` list that matched nothing by index means the
            # model ignored the index-echo format entirely (e.g. it
            # dropped "index" or used a different key) -- every claim below
            # then fails closed as unsupported, exactly like an honest
            # majority rejection would. Without this, the two are
            # indistinguishable in the logs: a full format drift would
            # look, and trip the "reject > half" abort below, exactly like
            # a real content rejection on every single page.
            logger.warning(
                "Verify для %s вернул %d вердикт(ов), но ни один не "
                "сопоставился по полю \"index\" -- похоже на дрейф формата "
                "ответа модели, а не на честное массовое отклонение; все "
                "%d сэмплированных утверждений будут отклонены как "
                "неподтверждённые",
                source_rel_path,
                len(raw_verdicts),
                len(sample),
            )
            if self._active_pass is not None:
                self._active_pass.verify_format_drift += 1

        rejected_positions: set[int] = set()
        rejected_count = 0
        for position, claim in enumerate(sample):
            verdict = verdicts_by_index.get(position)
            # Fail closed (ТЗ 5.2 step 4 requires a rejection to actually
            # count): a claim with no matching verdict, or a verdict that
            # omits/mistypes "supported", is treated as unsupported --
            # never guessed at as passing.
            supported = verdict is not None and verdict.get("supported") is True
            if not supported:
                rejected_count += 1
                rejected_positions.add(position)
                reason = (
                    str(verdict.get("reason", ""))
                    if verdict
                    else "no verdict returned"
                )
                if self._active_pass is not None:
                    # G7: count every claim Verify rejects for the pass
                    # journal (ТЗ 5.2 step 6), not just aborted pages.
                    self._active_pass.verify_rejected += 1
                logger.info(
                    "Compiled briefing Verify rejected claim for %s: %r (%s)",
                    source_rel_path,
                    claim["text"],
                    reason,
                )

        if rejected_count * 2 > len(sample):
            if candidate_payload is None:
                raise CompiledBriefingVerificationRejectedError(
                    f"Verify rejected {rejected_count}/{len(sample)} sampled claims "
                    f"for {source_rel_path}; page write aborted"
                )
            candidate_payload.setdefault("_quality_issues", []).append(
                f"Verify rejected {rejected_count}/{len(sample)} sampled claims"
            )

        # Filtered by position in `claims`, not by claim text (code review
        # Finding 4): `rejected_positions` only ever holds indices below
        # `sample_size`, so a claim past the sample boundary is never
        # dropped by coincidence of sharing text with a rejected sampled
        # claim -- it always passes through unchecked, per this method's
        # own docstring.
        return [
            claim
            for position, claim in enumerate(claims)
            if position not in rejected_positions
        ]

    def _extract_and_verify_claims(
        self,
        *,
        payload: dict[str, Any],
        target: CompiledBriefingTarget,
        source_rel_path: str,
        source_excerpt: str,
        existing_claims: str,
        existing_text: str,
        existing_meta: dict[str, str],
        signal: dict[str, Any] | None,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Claims/conflicts extraction entry point, called from
        ``_upsert_briefing`` before rendering. Returns ``([], [])`` when the
        payload carries no claims -- existing compile passes without a
        ``claims`` key are entirely unaffected (see final report).
        """
        raw_claims = payload.get("claims")
        if not raw_claims:
            return [], []
        # Invariant 2 (запрет самоусиления): a source already marked
        # superseded in the QMD memory signal must not seed new claims --
        # its content has already been replaced, so re-extracting from it
        # would resurrect a fact epistemic_memory has moved past.
        source_epistemic_state = (
            str(signal.get("epistemic_state") or "") if signal is not None else ""
        )
        if source_epistemic_state == "superseded":
            return [], []
        claims = self._normalize_claims(raw_claims, source_rel_path=source_rel_path)
        if not claims:
            return [], []
        conflicts = self._normalize_conflicts(payload.get("conflicts"), claims=claims)
        page_tier = self._merged_tier(existing_meta, signal)
        candidate_markdown = self._render_briefing(
            target=target,
            payload=payload,
            source_rel_path=source_rel_path,
            existing_text=existing_text,
            existing_meta=existing_meta,
            signal=signal,
            source_excerpt=source_excerpt,
            claims=claims,
            conflicts=conflicts,
            record_side_effects=False,
        )
        verified_claims = self._verify_claims_batch(
            claims=claims,
            source_rel_path=source_rel_path,
            source_excerpt=source_excerpt,
            page_tier=page_tier,
            target_title=target.title,
            candidate_payload=payload,
            candidate_markdown=candidate_markdown,
            existing_claims=existing_claims,
        )
        verified_texts = {claim["text"] for claim in verified_claims}
        conflicts = [
            conflict
            for conflict in conflicts
            if conflict["new_claim"] in verified_texts
        ]
        return verified_claims, conflicts

    def _build_conflict_adjudication_prompt(
        self,
        *,
        page_rel_path: str,
        page_state: str,
        existing_claim: str,
        existing_source: str,
        existing_date: str,
        new_claim: str,
        new_source: str,
        new_date: str,
        new_trust: str,
        claim_kind: str,
        model_conflict_type: str,
        attempt: int,
    ) -> str:
        """Prompt for one conflict adjudication.

        Dates and trust are stated as evidence, not as a verdict: the code
        that used to turn them into one (``_effective_conflict_type`` and the
        ``_trust_allows_consequential_action`` gate) is gone, and the model
        is expected to weigh them against the claims themselves.

        ``attempt`` > 1 means this pair already came back ``unclear`` at
        least once and is being re-adjudicated from the decisions queue. That
        retry drops ``unclear`` from the menu: the point of a second pass is
        to reach a decision, and leaving the escape hatch open would let the
        same pair bounce between passes forever.
        """
        trust_gloss = TRUST_LEVEL_EXPLANATIONS.get(new_trust, new_trust)
        outcomes = [
            '- "new_supersedes" -- новое утверждение заменяет старое; старое '
            "уедет в историю утверждений со ссылкой на источник.",
            '- "existing_stands" -- старое утверждение остаётся текущим; '
            "новое не попадёт на страницу.",
            '- "both_valid" -- оба верны, но относятся к разным контекстам '
            "(разные системы, команды, периоды); заполни context_note.",
        ]
        if attempt <= 1:
            outcomes.append(
                '- "unclear" -- решить нельзя даже после внимательного '
                "чтения; обе версии останутся на странице."
            )
        header = (
            "Ты ведёшь скомпилированную базу знаний личного ассистента.\n"
            "Верни ТОЛЬКО JSON.\n\n"
            "На одной странице столкнулись два утверждения. Реши, что "
            "страница должна утверждать сейчас.\n"
        )
        if attempt > 1:
            header += (
                "\nЭТО ПОВТОРНЫЙ ЗАХОД. Эту же пару уже показывали, и "
                "решение принято не было. В этот раз решение принять "
                "обязательно: выбери один из трёх исходов ниже, варианта "
                '"не знаю" больше нет.\n'
            )
        return (
            header
            + "\nВозможные исходы:\n"
            + "\n".join(outcomes)
            + "\n\nЧем руководствоваться:\n"
            "- Даты источников — сильный довод за более свежую версию, но "
            "не закон: более свежая запись может быть пересказом старого "
            "или чужой репликой.\n"
            "- Уровень доверия говорит, откуда взялись слова, а не насколько "
            "они верны.\n"
            "- Утверждения-мнения владельца законно меняются со временем.\n"
            "- Если оба утверждения об одном и том же и одно явно отменяет "
            'другое — это не "both_valid".\n\n'
            f"[СТРАНИЦА] {page_rel_path}\n"
            f"{self._clip(page_state, MAX_BODY_SNIPPET_CHARS) or '(пусто)'}\n\n"
            "[СТАРОЕ УТВЕРЖДЕНИЕ]\n"
            f"текст: {existing_claim}\n"
            f"источник: {existing_source}\n"
            f"дата источника: {existing_date or 'неизвестна'}\n\n"
            "[НОВОЕ УТВЕРЖДЕНИЕ]\n"
            f"текст: {new_claim}\n"
            f"источник: {new_source}\n"
            f"дата источника: {new_date or 'неизвестна'}\n"
            f"доверие к источнику: {new_trust} — {trust_gloss}\n"
            f"вид утверждения: {claim_kind}\n"
            f"как назвала конфликт модель-составитель: {model_conflict_type}\n\n"
            "Верни JSON строго такого вида:\n"
            f"{ADJUDICATE_JSON_EXAMPLE}"
        )

    def _adjudicate_conflict(
        self,
        *,
        page_rel_path: str,
        page_state: str,
        existing_claim: str,
        existing_source: str,
        existing_date: str,
        new_claim: str,
        new_source: str,
        new_date: str,
        new_trust: str,
        claim_kind: str,
        model_conflict_type: str,
        attempt: int = 1,
    ) -> tuple[str, str]:
        """Decide one conflict's outcome. Returns ``(outcome, context_note)``.

        Every failure mode -- model unreachable, unparseable JSON, an answer
        outside ``CONFLICT_OUTCOME_VALUES`` -- returns ``("unclear", "")``,
        which the caller renders as the same both-claims-kept Open Conflicts
        row the whole path used to fail closed into. There is deliberately no
        louder failure: a page must never end up asserting one side of a
        conflict because a network call flaked.

        ``CompiledBriefingPassBudgetExceededError`` is the one exception
        allowed through. It is not a failure to decide -- it ends the pass's
        work on this source cleanly and leaves the source queued, so letting
        it propagate is what makes "retry on the next pass" happen without
        any bookkeeping of our own.
        """
        prompt = self._build_conflict_adjudication_prompt(
            page_rel_path=page_rel_path,
            page_state=page_state,
            existing_claim=existing_claim,
            existing_source=existing_source,
            existing_date=existing_date,
            new_claim=new_claim,
            new_source=new_source,
            new_date=new_date,
            new_trust=new_trust,
            claim_kind=claim_kind,
            model_conflict_type=model_conflict_type,
            attempt=attempt,
        )
        try:
            payload = self._run_json_dict_prompt(
                prompt=prompt,
                timeout=ADJUDICATE_TIMEOUT_SECONDS,
                error_context="compiled briefing conflict adjudication",
                json_example=ADJUDICATE_JSON_EXAMPLE,
            )
        except CompiledBriefingPassBudgetExceededError:
            raise
        except Exception:
            logger.exception(
                "Compiled briefing conflict adjudication failed for %s -- "
                "keeping both claims",
                page_rel_path,
            )
            return "unclear", ""

        outcome = str(payload.get("outcome") or "").strip().lower()
        if outcome not in CONFLICT_OUTCOME_VALUES:
            logger.info(
                "Compiled briefing conflict adjudication returned an "
                "unsupported outcome %r for %s -- keeping both claims",
                outcome,
                page_rel_path,
            )
            return "unclear", ""
        if outcome == "unclear" and attempt > 1:
            # The retry prompt does not offer "unclear"; an answer that uses
            # it anyway is the model ignoring the instruction, not a real
            # verdict. Honour it as "still undecided" rather than pretending
            # the escalation worked.
            logger.info(
                "Compiled briefing conflict adjudication still undecided for "
                "%s on attempt %d",
                page_rel_path,
                attempt,
            )
        context_note = ""
        if outcome == "both_valid":
            context_note = self._clean_line(payload.get("context_note"))
        return outcome, context_note

    def _resolve_open_conflicts(self, *, limit: int) -> list[str]:
        """Re-adjudicate conflicts already standing open on compiled pages.

        The write path settles every conflict it creates, so a row only
        reaches this table when the adjudicator answered ``"unclear"``.
        This is the second attempt at exactly those pairs: same call, but
        ``attempt=2``, whose prompt states outright that this pair has been
        seen before and drops the "unclear" option from the menu.

        Bounded by ``limit`` conflicts per pass. Failing to reach a verdict
        again is not an error -- the row simply stays, and the next pass
        tries it again, which is the whole design: a conflict is retried
        until it resolves, never handed to the owner as a task.

        Returns the pages actually rewritten.
        """
        resolved_pages: list[str] = []
        if limit <= 0:
            return resolved_pages
        budget = limit
        for candidate in self._iter_candidates():
            if budget <= 0:
                break
            rows = self._open_conflicts_rows(candidate.text)
            if not rows:
                continue
            page_text = self._ensure_claim_history_section(candidate.text)
            if not self._has_section(page_text, "Claim History"):
                # Nowhere to retire a losing claim to, and nowhere to put the
                # section back either. Removing a claim from the live ledger
                # anyway would delete it outright, so this page keeps its
                # conflicts until its sections are whole again -- and it
                # costs no model call to find that out.
                logger.warning(
                    "Compiled briefing %s has open conflicts but no Claim "
                    "History section; leaving them for the owner",
                    candidate.rel_path,
                )
                continue
            try:
                new_text, settled = self._settle_page_conflicts(
                    rel_path=candidate.rel_path,
                    text=page_text,
                    rows=rows,
                    limit=budget,
                )
            except CompiledBriefingPassBudgetExceededError:
                # The pass ran out of model calls mid-page. Whatever it had
                # already decided is discarded rather than half-written --
                # the rows are still on the page, so the next pass starts
                # this page over from a consistent state.
                logger.info(
                    "Compiled briefing conflict retry stopped on %s: pass "
                    "model-call budget exhausted",
                    candidate.rel_path,
                )
                break
            budget -= settled
            if new_text == candidate.text:
                continue
            if not self._write_settled_page(candidate, new_text):
                continue
            resolved_pages.append(candidate.rel_path)
            if self._active_pass is not None:
                self._active_pass.conflicts_auto_resolved += len(rows) - len(
                    self._open_conflicts_rows(new_text)
                )
            self._drop_undecided_conflict_entries(candidate.rel_path, new_text)
        return resolved_pages

    def _settle_page_conflicts(
        self,
        *,
        rel_path: str,
        text: str,
        rows: list[tuple[str, str, str, str, str]],
        limit: int,
    ) -> tuple[str, int]:
        """Adjudicate up to ``limit`` of one page's open conflicts.

        Returns the page's new text and how many pairs were adjudicated
        (including the ones that came back undecided -- they cost a model
        call all the same). Pure apart from the model calls: the caller
        writes.
        """
        page_state = self._section_text(text, "Current State")
        shaped_rows = self._sources_shaped_rows(text)
        history_rows = self._claim_history_rows(text)
        date_lookup = {
            (source, what): row_date for row_date, source, what in shaped_rows
        }
        kept_rows: list[tuple[str, str, str, str, str]] = []
        settled = 0
        for row in rows:
            if settled >= limit:
                kept_rows.append(row)
                continue
            since, existing_claim, existing_source, new_claim, new_source = row
            settled += 1
            outcome, context_note = self._adjudicate_conflict(
                page_rel_path=rel_path,
                page_state=page_state,
                existing_claim=existing_claim,
                existing_source=existing_source,
                existing_date=date_lookup.get((existing_source, existing_claim), ""),
                new_claim=new_claim,
                new_source=new_source,
                new_date=date_lookup.get((new_source, new_claim), since),
                new_trust=self._source_trust_level(new_source, ""),
                claim_kind="fact",
                model_conflict_type="factual",
                attempt=2,
            )
            if outcome == "unclear":
                kept_rows.append(row)
                continue
            if outcome == "both_valid":
                shaped_rows = self._annotate_shaped_row(
                    shaped_rows,
                    source=new_source,
                    claim=new_claim,
                    note=context_note,
                )
                continue
            keep_existing = outcome == "existing_stands"
            loser_claim, loser_source = (
                (new_claim, new_source) if keep_existing
                else (existing_claim, existing_source)
            )
            winner_source = existing_source if keep_existing else new_source
            remaining = [
                shaped
                for shaped in shaped_rows
                if not (shaped[1] == loser_source and shaped[2] == loser_claim)
            ]
            if len(remaining) == len(shaped_rows):
                # The losing claim is not in the ledger any more (a hand
                # edit, or an earlier resolution). Closing the row is still
                # right -- the page no longer asserts both sides.
                continue
            loser_date = date_lookup.get((loser_source, loser_claim), since)
            shaped_rows = remaining
            history_row = (loser_date, loser_source, loser_claim, winner_source)
            # Same dedup contract as the write path: the date column is
            # ignored, so re-retiring the same claim adds no second row.
            if not any(existing[1:] == history_row[1:] for existing in history_rows):
                history_rows = [*history_rows, history_row]

        text = self._replace_section(
            text,
            "Sources That Shaped This Page",
            self._render_sources_shaped_table(shaped_rows),
        )
        text = self._replace_section(
            text, "Claim History", self._render_claim_history(history_rows)
        )
        text = self._replace_section(
            text, "Open Conflicts", self._render_open_conflicts_table(kept_rows)
        )
        return text, settled

    @staticmethod
    def _annotate_shaped_row(
        rows: list[tuple[str, str, str]],
        *,
        source: str,
        claim: str,
        note: str,
    ) -> list[tuple[str, str, str]]:
        """Append the adjudicator's "these are different scopes" note to the
        row of the claim it was written about.

        The write path can only ever append such a note to the row it is
        adding right then (ТЗ 5.4 keeps that table append-only); here the
        row is already on the page, so the note is folded into its text.
        Skipped when the note is empty or already present, which keeps a
        repeat verdict on the same pair from stacking duplicates.
        """
        if not note:
            return rows
        suffix = f" ({note})"
        return [
            (row_date, row_source, row_what + suffix)
            if row_source == source
            and row_what == claim
            and not row_what.endswith(suffix)
            else (row_date, row_source, row_what)
            for row_date, row_source, row_what in rows
        ]

    def _write_settled_page(
        self, candidate: CompiledBriefingCandidate, new_text: str
    ) -> bool:
        """Write one page whose conflicts were just re-adjudicated.

        Same freshness/encoding guards as ``_compress_cooled_pages``: the
        page is skipped if it changed since this pass scanned it, and skipped
        (loudly) if its bytes are not valid UTF-8, because this rewrites the
        whole page from decoded text.
        """
        note_path = self.vault_path / candidate.rel_path
        with vault_write_lock(self.vault_path) as lock:
            try:
                current_bytes = note_path.read_bytes()
            except FileNotFoundError:
                return False
            if self._decode_page_bytes(current_bytes) != candidate.text:
                return False
            if current_bytes != candidate.text.encode("utf-8"):
                logger.warning(
                    "Compiled briefing %s has bytes that are not valid UTF-8; "
                    "leaving its conflicts open rather than rewriting them as "
                    "replacement characters",
                    candidate.rel_path,
                )
                self._queue_undecodable_page(candidate.rel_path, existing_lock=lock)
                return False
            new_bytes = patch_frontmatter_bytes(
                new_text.encode("utf-8"),
                {
                    "conflicts_open": len(
                        self._open_conflicts_rows(new_text)
                    )
                },
            )
            self._snapshot_pass_page(
                candidate.rel_path, before=current_bytes, after=new_bytes
            )
            write_validated_vault_markdown(
                self.vault_path,
                note_path,
                new_bytes,
                manifest=self._manifest(),
                existing_lock=lock,
            )
        return True

    def _drop_undecided_conflict_entries(self, rel_path: str, text: str) -> None:
        """Clear a page's retry entries once it has no open conflicts left.

        The entry is a pointer at the page, not at one pair (it is deduped
        by ``(kind, page)``), so it stays meaningful until the page's last
        conflict is settled. The retired ``"blocked-action"`` kind is
        cleared alongside it: those entries are still on disk from before
        adjudication existed and describe the same, now-settled situation.
        """
        if self._open_conflicts_rows(text):
            return
        from d_brain.services.decisions_queue import (
            BLOCKED_ACTION_KIND,
            UNDECIDED_CONFLICT_KIND,
            remove_queue_entries_for_page,
        )

        with vault_write_lock(self.vault_path) as lock:
            remove_queue_entries_for_page(
                self.vault_path,
                rel_path,
                kinds=(UNDECIDED_CONFLICT_KIND, BLOCKED_ACTION_KIND),
                existing_lock=lock,
            )

    def _adjudicate_drift_entries(self, *, limit: int) -> list[str]:
        """Judge queued drift suspicions instead of asking the owner to.

        A ``"drift"`` entry means one thing only: the page hit
        ``MAX_ENRICHMENTS_PER_PAGE_PER_MONTH`` this month. That counter
        cannot tell a page slowly losing its shape from a project page that
        is simply busy, which is why the entry existed -- somebody had to
        look. Here the model looks: it reads the page and everything added
        to it this month, and answers whether the page actually drifted.

        Real drift is recorded where it belongs, on the page itself
        (``quality_status: needs_review`` plus the reason), which is the
        same flag Verify raises and which the next successful Verify pass
        clears. Either way the queue entry goes: the question has been
        answered. The monthly enrichment budget is untouched by all this --
        it is a cost control, not a drift verdict.

        Returns the pages marked as drifted.
        """
        from d_brain.services.decisions_queue import (
            DRIFT_KIND,
            list_json_queue_items,
        )

        marked: list[str] = []
        if limit <= 0:
            return marked
        pending = [
            item
            for item in list_json_queue_items(self.vault_path)
            if item.kind == DRIFT_KIND
        ][:limit]
        for item in pending:
            page_path = self.vault_path / item.page
            try:
                text = self._read_page_text(page_path)
            except FileNotFoundError:
                text = ""
            if not text:
                # The page is gone; the suspicion about it cannot be
                # answered and no longer means anything.
                self._drop_drift_entry(item.page)
                continue
            month_prefix = (item.since or date.today().isoformat())[:7]
            verdict, reason = self._judge_page_drift(
                page_rel_path=item.page,
                text=text,
                month_prefix=month_prefix,
            )
            if verdict is None:
                # Unreachable model, unparseable answer: leave the entry so
                # the next pass asks again.
                continue
            if verdict and self._flag_page_drift(item.page, text, reason):
                marked.append(item.page)
            self._drop_drift_entry(item.page)
        return marked

    def _judge_page_drift(
        self, *, page_rel_path: str, text: str, month_prefix: str
    ) -> tuple[bool | None, str]:
        """One model call. Returns ``(drifted, reason)``; ``(None, "")`` when
        the call or its answer was unusable, which the caller treats as "ask
        again next pass" rather than as a verdict either way."""
        month_rows = "\n".join(
            f"- {row_date} {source}: {what}"
            for row_date, source, what in self._sources_shaped_rows(text)
            if row_date.startswith(month_prefix)
            and what != NOT_ENRICHMENT_SOURCE_MARKER
        )
        page_state = self._clip(
            self._section_text(text, "Current State"), MAX_BODY_SNIPPET_CHARS
        )
        prompt = (
            "Ты ведёшь скомпилированную базу знаний личного ассистента.\n"
            "Верни ТОЛЬКО JSON.\n\n"
            "Эта страница обновлялась в этом месяце необычно часто. Само по "
            "себе это ничего не значит: у активного проекта так и должно "
            "быть. Дрейф — это другое: страница перестала быть про один "
            "предмет, в неё стекается материал из разных тем, или её "
            "утверждения накопились в кашу.\n\n"
            "Реши, дрейф ли это.\n"
            '- "drift": true — страница потеряла предмет или смешала темы; '
            'в "reason" одной фразой скажи, что именно расползлось.\n'
            '- "drift": false — просто активная работа по одной теме.\n\n'
            f"[СТРАНИЦА] {page_rel_path}\n"
            f"{page_state or '(пусто)'}\n\n"
            f"[ЧТО ДОБАВЛЯЛОСЬ В {month_prefix}]\n"
            f"{self._clip(month_rows, MAX_BODY_SNIPPET_CHARS) or '(ничего)'}\n\n"
            "Верни JSON строго такого вида:\n"
            f"{DRIFT_JSON_EXAMPLE}"
        )
        try:
            payload = self._run_json_dict_prompt(
                prompt=prompt,
                timeout=ADJUDICATE_TIMEOUT_SECONDS,
                error_context="compiled briefing drift judgement",
                json_example=DRIFT_JSON_EXAMPLE,
            )
        except CompiledBriefingPassBudgetExceededError:
            raise
        except Exception:
            logger.exception(
                "Compiled briefing drift judgement failed for %s -- leaving "
                "the queue entry for the next pass",
                page_rel_path,
            )
            return None, ""
        drifted = payload.get("drift")
        if not isinstance(drifted, bool):
            logger.info(
                "Compiled briefing drift judgement for %s returned no usable "
                "verdict (%r)",
                page_rel_path,
                drifted,
            )
            return None, ""
        return drifted, self._clean_line(payload.get("reason"))

    def _flag_page_drift(self, rel_path: str, text: str, reason: str) -> bool:
        """Write the drift verdict onto the page as ``quality_status``.

        Uses the same two frontmatter fields Verify uses for a
        content-quality problem, rather than inventing a drift-specific one:
        both mean "a human should look at this page", both are cleared by
        the next Verify pass that comes back clean, and the digest and page
        schema already know about them.
        """
        note_path = self.vault_path / rel_path
        with vault_write_lock(self.vault_path) as lock:
            try:
                current_bytes = note_path.read_bytes()
            except FileNotFoundError:
                return False
            if self._decode_page_bytes(current_bytes) != text:
                return False
            new_bytes = patch_frontmatter_bytes(
                current_bytes,
                {
                    "quality_status": "needs_review",
                    "quality_reason": reason
                    or "страница расползлась по темам за месяц",
                },
            )
            if new_bytes == current_bytes:
                return False
            self._snapshot_pass_page(rel_path, before=current_bytes, after=new_bytes)
            write_validated_vault_markdown(
                self.vault_path,
                note_path,
                new_bytes,
                manifest=self._manifest(),
                existing_lock=lock,
            )
        return True

    def _drop_drift_entry(self, rel_path: str) -> None:
        from d_brain.services.decisions_queue import (
            DRIFT_KIND,
            remove_queue_entries_for_page,
        )

        with vault_write_lock(self.vault_path) as lock:
            remove_queue_entries_for_page(
                self.vault_path,
                rel_path,
                kinds=(DRIFT_KIND,),
                existing_lock=lock,
            )

    def _apply_claims_and_conflicts(
        self,
        *,
        claims: list[dict[str, str]],
        conflicts: list[dict[str, str]],
        shaped_rows: list[tuple[str, str, str]],
        claim_history_rows: list[tuple[str, str, str, str]],
        open_conflict_rows: list[tuple[str, str, str, str, str]],
        source_rel_path: str,
        source_excerpt: str,
        signal: dict[str, Any] | None,
        today: str,
        page_rel_path: str,
        page_state: str = "",
        record_side_effects: bool = True,
    ) -> tuple[
        list[tuple[str, str, str]],
        list[tuple[str, str, str, str]],
        list[tuple[str, str, str, str, str]],
    ]:
        """Resolve this pass's verified claims/conflicts against the page's
        existing claim ledger ("Sources That Shaped This Page", ТЗ 4.2) and
        return the updated (shaped_rows, claim_history_rows,
        open_conflict_rows).

        Every conflict here is settled by ``_adjudicate_conflict``, one
        model call per pair; ``page_rel_path``/``page_state`` are what that
        call needs to see the page the pair sits on, and ``page_rel_path``
        also names the page in the ``"undecided-conflict"`` retry entry a
        verdict of ``"unclear"`` leaves behind.

        ТЗ 5.4 note on decisions supersession ("для решений выполняется
        замещение через epistemic_memory"): deliberately NOT done here.
        Verified by reading epistemic_memory.py itself and its only caller
        (memory-engine.py's manual ``supersede`` CLI command) rather than
        taking the old comment at face value. Not a lock problem -- this
        method runs before ``_upsert_briefing`` takes the vault write lock,
        so a call from here would not deadlock. Three other things block
        it, and none are fixable from inside this method:

        1. ``supersede_notes(vault_path, old, new)`` reads both notes off
           disk and requires each to already carry valid namespaced
           ``epistemic_confidence``/``epistemic_scope``/``epistemic_state``
           frontmatter. The "new" page here is still mid-render -- it has
           no on-disk content yet to hash, let alone epistemic metadata.
        2. A decision page's ``supersedes``/``superseded_by`` is free-form
           "decision identifier" text straight from the model's JSON
           (unlike ``decision_evidence``, which IS normalized to vault
           paths via ``_normalize_paths``) -- there is no vault-relative
           path here to resolve the old/new pair from.
        3. Structural, not local: giving a compiled page any
           ``epistemic_``-prefixed field reroutes its manifest validation
           from the ``derived`` profile to the ``epistemic`` one
           (``frontmatter.route_profile``), which has no knowledge of this
           page's own required fields and would reject the write. This is
           the same reason the monthly-enrichment counter above avoids an
           ``epistemic_`` field instead of adding one.

        Making decision pages epistemic_memory-compatible needs a
        validation-profile change plus a path-resolution scheme for
        supersedes/superseded_by, not a call added here. Deferred as an
        explicit gap; the losing claim still moves to Claim History with
        its source.

        ТЗ 4.4 note: trust no longer gates supersession here. It used to --
        a temporal win by a `forwarded` source was downgraded to `factual`
        so the owner decided instead -- which in practice meant every
        date-based supersession coming out of a PLAUD recording piled into
        the decisions queue. Trust is now stated to the adjudicator as
        evidence about where the words came from, and it weighs that
        against the claims themselves.
        """
        if not claims:
            return shaped_rows, claim_history_rows, open_conflict_rows

        new_date_value = self.qmd._record_date_for_rel_path(source_rel_path, signal)
        new_date = new_date_value.isoformat() if new_date_value is not None else today
        current_trust = self._source_trust_level(source_rel_path, source_excerpt)

        existing_lookup: dict[tuple[str, str], str] = {
            (row_source, row_what): row_date
            for row_date, row_source, row_what in shaped_rows
        }
        claim_kind_by_text = {claim["text"]: claim["kind"] for claim in claims}

        dropped_existing: set[tuple[str, str]] = set()
        blocked_new_texts: set[str] = set()
        # ТЗ 5.4 contextual explanation: keyed by new_claim text so that one
        # new claim caught in two separate contextual conflicts (against two
        # different existing claims) gets both explanations glued together,
        # not overwritten -- see the row-building loop below.
        contextual_notes_by_claim: dict[str, list[str]] = {}
        # A claim text that also won a *factual* conflict this pass must
        # keep its row text byte-identical to the ``new_claim`` recorded in
        # ``open_conflict_rows`` -- the "is this conflict still live" check
        # further down matches purely on claim text against the shaped-rows
        # ledger. Appending a contextual note to that same row would desync
        # the two tables and silently "resolve" a still-open factual
        # conflict. Guard against the (unusual) case of one new claim
        # carrying both a factual and a contextual conflict at once.
        factual_new_claim_texts: set[str] = set()

        for conflict in conflicts:
            key = (conflict["existing_source"], conflict["existing_claim"])
            existing_date = existing_lookup.get(key)
            if existing_date is None:
                # Reference to a claim that is not actually recorded on this
                # page (hallucinated or stale existing_claim/existing_source
                # pairing) -- nothing to resolve.
                continue
            claim_kind = claim_kind_by_text.get(conflict["new_claim"], "fact")
            outcome, context_note = self._adjudicate_conflict(
                page_rel_path=page_rel_path,
                page_state=page_state,
                existing_claim=conflict["existing_claim"],
                existing_source=conflict["existing_source"],
                existing_date=existing_date,
                new_claim=conflict["new_claim"],
                new_source=source_rel_path,
                new_date=new_date,
                new_trust=current_trust,
                claim_kind=claim_kind,
                model_conflict_type=conflict["type"],
            )
            effective_type = CONFLICT_OUTCOME_TO_TYPE[outcome]
            winner_is_new = outcome == "new_supersedes"
            if context_note:
                # The adjudicator's own explanation of how the two scopes
                # differ replaces the compile stage's ``context_note`` for
                # this pair: it was produced by the model that actually
                # decided they coexist.
                conflict = {**conflict, "context_note": context_note}
            if outcome == "unclear" and record_side_effects:
                # Undecided is the one outcome that still costs the owner a
                # queue entry -- but as a retry buffer, not a task: the
                # nightly pass re-adjudicates it with the escalated prompt
                # (``attempt`` > 1). Counted on the pass journal under the
                # old ``trust_blocked`` field, which now means "conflicts
                # this pass could not settle" rather than "blocked by trust"
                # -- trust no longer blocks anything.
                if self._active_pass is not None:
                    self._active_pass.trust_blocked += 1
                self._queue_undecided_conflict(
                    page_rel_path=page_rel_path,
                    existing_claim=conflict["existing_claim"],
                    existing_source=conflict["existing_source"],
                    new_claim=conflict["new_claim"],
                    new_source=source_rel_path,
                )
            if effective_type == "temporal":
                if winner_is_new:
                    dropped_existing.add(key)
                    history_row = (
                        existing_date,
                        conflict["existing_source"],
                        conflict["existing_claim"],
                        source_rel_path,
                    )
                    # Dedup ignores the date column: the same supersession
                    # re-derived on a later day is the same fact, not a
                    # second one.
                    if not any(
                        row[1:] == history_row[1:] for row in claim_history_rows
                    ):
                        claim_history_rows = [*claim_history_rows, history_row]
                else:
                    # The existing claim is still current; the new claim
                    # loses and is simply never added (it was never live
                    # content, so there is nothing to move to history).
                    blocked_new_texts.add(conflict["new_claim"])
            elif effective_type == "factual":
                # Both statements are kept (ТЗ 5.4); flagged for the owner
                # via the Open Conflicts table rather than auto-resolved.
                factual_new_claim_texts.add(conflict["new_claim"])
                conflict_row = (
                    today,
                    conflict["existing_claim"],
                    conflict["existing_source"],
                    conflict["new_claim"],
                    source_rel_path,
                )
                # Dedup ignores the date column, as in Claim History above:
                # re-deriving the same conflict from the same two claims must
                # not add a second row, and must not bump `conflicts_open`
                # again (that counter is the length delta of this table).
                if not any(row[1:] == conflict_row[1:] for row in open_conflict_rows):
                    open_conflict_rows = [*open_conflict_rows, conflict_row]
            elif effective_type == "contextual":
                # Both statements are valid in their own scope (ТЗ 5.4); no
                # replacement and no owner-decision entry needed -- but the
                # model's explanation of *how* the scopes differ (when it
                # gave one) is worth keeping next to the new claim itself,
                # so it is appended to that claim's own new row below
                # rather than dropped on the floor.
                note = conflict.get("context_note", "").strip()
                if note:
                    contextual_notes_by_claim.setdefault(
                        conflict["new_claim"], []
                    ).append(note)

        if dropped_existing:
            shaped_rows = [
                row for row in shaped_rows if (row[1], row[2]) not in dropped_existing
            ]

        existing_triples = {
            (row_date, row_source, row_what)
            for row_date, row_source, row_what in shaped_rows
        }
        for claim in claims:
            if claim["text"] in blocked_new_texts:
                continue
            row_what = claim["text"]
            notes = contextual_notes_by_claim.get(claim["text"])
            if notes and claim["text"] not in factual_new_claim_texts:
                # Append-only per ТЗ 5.4: this only ever extends the text of
                # the NEW row being added right now, never rewrites a row
                # already on the page.
                row_what = f"{row_what} ({'; '.join(notes)})"
            row = (today, claim["source"], row_what)
            if row in existing_triples:
                continue
            shaped_rows = [*shaped_rows, row]
            existing_triples.add(row)

        # An open conflict stops being open once either of its two sides
        # leaves the claim ledger (a later pass superseded it): there is
        # nothing left for the owner to choose between. Rows whose both sides
        # are still live stay until the owner resolves them.
        live_claims = {
            (row_source, row_what) for _, row_source, row_what in shaped_rows
        }
        open_conflict_rows = [
            row
            for row in open_conflict_rows
            if (row[2], row[1]) in live_claims and (row[4], row[3]) in live_claims
        ]

        return shaped_rows, claim_history_rows, open_conflict_rows

    def _archive_stale_notes(self, *, limit: int) -> list[str]:
        """Archive via either trigger: the existing one (stale freshness +
        status done/inactive) or the ТЗ 6.4 tier-based one (tier `archive`,
        idle >=``ARCHIVE_TIER_IDLE_DAYS`` days, no incoming links). Both
        reuse the same ``_archive_candidate`` move, which never deletes and
        never overrides a tier the page already has -- see there.

        Do not confuse the memory tier `archive` (candidate's own
        ``tier`` field, page still lives in its normal ``compiled/<domain>``
        folder) with the physical ``compiled/archive/`` folder
        ``_archive_candidate`` moves it into.
        """
        archived: list[str] = []
        if limit <= 0:
            return archived
        link_index = self._load_incoming_link_index()
        for candidate in self._iter_candidates():
            if len(archived) >= limit:
                break
            status = self._frontmatter_fields(candidate.text).get("status", "")
            stale_trigger = (
                candidate.freshness_state == "stale"
                and status in {"done", "inactive"}
            )
            if not stale_trigger and not self._archive_tier_idle_trigger(
                candidate, link_index
            ):
                continue
            archived_path = self._archive_candidate(candidate)
            if not archived_path:
                continue
            archived.append(archived_path)
        return archived

    def _archive_tier_idle_trigger(
        self,
        candidate: CompiledBriefingCandidate,
        link_index: dict[str, int] | None,
    ) -> bool:
        """ТЗ 6.4: "a page that stayed in `archive` longer than 180 days
        with no incoming links moves to compiled/archive/". Simplified, per
        plan point 3, to "tier is `archive` and `last_accessed` is at least
        that old" -- there is no separate since-when-on-this-tier field to
        check the transition date itself against.
        """
        if candidate.tier != "archive":
            return False
        last_accessed_raw = self._frontmatter_fields(candidate.text).get(
            "last_accessed"
        )
        try:
            last_accessed = (
                date.fromisoformat(last_accessed_raw) if last_accessed_raw else None
            )
        except ValueError:
            last_accessed = None
        if last_accessed is None:
            return False
        if (date.today() - last_accessed).days < ARCHIVE_TIER_IDLE_DAYS:
            return False
        return not self._has_incoming_links(candidate, link_index)

    def _load_incoming_link_index(self) -> dict[str, int] | None:
        """ТЗ 6.4 "no incoming links": read incoming-link counts from the
        daily graph-builder artifact
        (``skills/graph-builder/scripts/analyze.py``, built before nightly
        maintenance runs). Returns ``None`` when the artifact is missing or
        unreadable, which ``_has_incoming_links`` treats as "fall back to a
        lightweight scan" rather than guessing from a stale/partial file.
        """
        graph_path = self.vault_path / ".graph" / "vault-graph.json"
        try:
            data = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        links_to = data.get("links_to")
        if not isinstance(links_to, dict):
            return None
        return {
            str(note_key): len(targets)
            for note_key, targets in links_to.items()
            if isinstance(targets, list)
        }

    def _has_incoming_links(
        self,
        candidate: CompiledBriefingCandidate,
        link_index: dict[str, int] | None,
    ) -> bool:
        if link_index is not None:
            note_key = (
                candidate.rel_path[:-3]
                if candidate.rel_path.endswith(".md")
                else candidate.rel_path
            )
            return link_index.get(note_key, 0) > 0
        # No graph artifact: fall back to a lightweight scan of the other
        # compiled candidates' own text for a wikilink to this page (not a
        # full vault scan -- that would turn one archival decision into an
        # O(vault) text scan every night). Deliberately conservative: never
        # deletes and the ТЗ wants archival only on confidence, so when even
        # this lightweight signal is unavailable the safe default (assume
        # linked, do not archive) lives in ``_archive_tier_idle_trigger``
        # simply never getting here without a candidate list to scan.
        target_key = (
            candidate.rel_path[:-3]
            if candidate.rel_path.endswith(".md")
            else candidate.rel_path
        )
        target_names = {target_key, candidate.slug, candidate.title}
        for other in self._iter_candidates():
            if other.rel_path == candidate.rel_path:
                continue
            for link in WIKILINK_RE.findall(other.text):
                link_clean = link.strip()
                link_key = link_clean[:-3] if link_clean.endswith(".md") else link_clean
                if link_key in target_names or link_clean in target_names:
                    return True
        return False

    def _backfill_freshness_notes(self, *, limit: int) -> list[str]:
        refreshed: list[str] = []
        if limit <= 0 or not self.is_available():
            return refreshed
        source_state = self._load_source_state()
        state_entries = source_state["entries"]
        attempts = 0
        for candidate in self._iter_candidates():
            if attempts >= limit:
                break
            state_entry = state_entries.get(candidate.rel_path)
            issue = self._candidate_freshness_issue(candidate, state_entry)
            if issue is None or issue["issue"] == "source-untracked":
                continue
            if self._verify_rejection_exhausted(state_entry, candidate.text):
                continue
            source_paths = self._freshness_source_paths(candidate.text, state_entry)
            if not source_paths:
                continue
            if issue["issue"] == "stale":
                source_paths = source_paths[:1]
            attempts += 1
            try:
                refresh_result = self._refresh_candidate(
                    candidate,
                    source_paths=source_paths,
                )
            except CompiledBriefingVerificationRejectedError as exc:
                logger.warning(
                    "Compiled briefing freshness refresh rejected by Verify "
                    "for %s: %s",
                    candidate.rel_path,
                    exc,
                )
                rejection_count = self._record_verify_rejection(
                    candidate.rel_path, candidate.text
                )
                if rejection_count >= MAX_VERIFY_REJECTED_RETRIES:
                    # ТЗ 7.2: retries are exhausted -- hand the page to the
                    # owner instead of silently skipping it forever (see
                    # ``_queue_verify_rejected``).
                    self._queue_verify_rejected(page_rel_path=candidate.rel_path)
                continue
            except CompiledBriefingPassBudgetExceededError:
                # ТЗ 5.5 inv 7 / G3: stop the whole backfill loop, not just
                # this candidate -- the pass has no budget left for anyone
                # else either.
                break
            except (
                CliExecutionError,
                CompiledBriefingWriteConflict,
                FileNotFoundError,
                TimeoutError,
                ValueError,
            ) as exc:
                logger.warning(
                    "Compiled briefing freshness refresh failed for %s: %s",
                    candidate.rel_path,
                    exc,
                )
                continue
            if refresh_result.written and refresh_result.path not in refreshed:
                refreshed.append(refresh_result.path)
        return refreshed

    def _freshness_source_paths(
        self,
        note_text: str,
        state_entry: Any,
    ) -> list[str]:
        current = self._source_snapshot(note_text)
        stored = state_entry["sources"] if state_entry is not None else {}
        changed = {
            source
            for source in set(current) | set(stored)
            if current.get(source) != stored.get(source)
        }
        existing_changed = [
            source
            for source in sorted(changed)
            if current.get(source) not in {None, "missing"}
        ]
        if existing_changed:
            return existing_changed
        return [
            source
            for source, digest in current.items()
            if digest != "missing"
        ]

    def _refresh_candidate(
        self,
        candidate: CompiledBriefingCandidate,
        *,
        source_paths: list[str],
    ) -> BriefingUpsertResult:
        updated_path = ""
        written = False
        for source_rel_path in source_paths:
            excerpt = self._source_excerpt(source_rel_path, "")
            if not excerpt:
                continue
            target = CompiledBriefingTarget(
                domain=candidate.domain,
                title=candidate.title,
                slug=candidate.slug,
                description=candidate.description,
                reason="semantic source snapshot changed",
                existing_path=candidate.rel_path,
            )
            upsert_result = self._upsert_briefing(
                target=target,
                source_rel_path=source_rel_path,
                source_excerpt=excerpt,
                signal=self.qmd._memory_signal_for_rel_path(source_rel_path),
                record_source_state=False,
            )
            updated_path = upsert_result.path
            written = written or upsert_result.written
        if not updated_path:
            raise ValueError("no readable source for freshness refresh")
        updated_text = self._read_page_text(self.vault_path / updated_path)
        self._record_source_state(updated_path, updated_text)
        return BriefingUpsertResult(path=updated_path, written=written)

    def _archive_candidate(self, candidate: CompiledBriefingCandidate) -> str:
        source_path = self.vault_path / candidate.rel_path
        archive_dir = self.compiled_root / "archive" / candidate.domain
        with vault_write_lock(self.vault_path) as lock:
            # Resilience review defect 2: the existence check used to run
            # *before* this lock, with the read still inside it -- a
            # concurrent duplicate response (double tap, or a retried
            # Telegram callback) racing the same page's archival could pass
            # both processes' checks and then have the loser's read raise
            # FileNotFoundError from inside the lock. Checking by reading,
            # under the one lock, closes that window: "gone" is now reported
            # the same way whether it was gone from the very start or
            # vanished a moment ago, matching the empty-string "nothing to
            # archive" contract every caller (decisions_queue.py's
            # _apply_fact_check_reject) already treats as an idempotent
            # no-op.
            try:
                original_content = source_path.read_bytes()
            except FileNotFoundError:
                return ""
            source_content = original_content
            document = parse_frontmatter_bytes(source_content)
            today = date.today().isoformat()
            metadata_updates = {
                key: value
                for key, value in {
                    "type": "compiled-briefing",
                    "description": candidate.description or candidate.title,
                    "last_accessed": today,
                    "relevance": 1.0,
                    "tier": "active",
                }.items()
                if document.fields.get(key) in (None, "", [])
            }
            if metadata_updates:
                source_content = patch_frontmatter_bytes(
                    source_content, metadata_updates
                )
            archive_dir.mkdir(parents=True, exist_ok=True)
            target_path = archive_dir / source_path.name
            suffix = 2
            while target_path.exists():
                target_path = (
                    archive_dir / f"{source_path.stem}-{suffix}{source_path.suffix}"
                )
                suffix += 1
            target_rel_path = target_path.relative_to(self.vault_path).as_posix()
            # G4: snapshot both sides of the move -- the source page
            # disappears (before=original bytes, after=None) and the
            # archive copy is newly created (before=None, after=its bytes)
            # -- so a rollback can undo the whole archival, not just half.
            self._snapshot_pass_page(
                candidate.rel_path, before=original_content, after=None
            )
            self._snapshot_pass_page(
                target_rel_path, before=None, after=source_content
            )
            write_validated_vault_markdown(
                self.vault_path,
                target_path,
                source_content,
                manifest=self._manifest(),
                existing_lock=lock,
            )
            source_path.unlink()
        return target_path.relative_to(self.vault_path).as_posix()

    # --- Compression of cooled pages (ТЗ 6.3) --------------------------

    def _compress_cooled_pages(self, *, limit: int) -> list[str]:
        """Idempotent nightly-maintenance step (ТЗ 6.3): code only, no
        model calls. Brings each warm/cold/archive-tier page to the
        invariant "<=RECENT_CHANGES_KEEP items in Recent Changes; Open
        Loops older than OPEN_LOOP_ABANDON_DAYS days marked abandoned and
        moved to History" and leaves a page untouched -- no write at all --
        once it already satisfies that invariant, so a repeat pass is a
        guaranteed no-op.

        Deliberately not a tier-change-event handler (plan point 2): there
        is no such event and no prior-tier field stored, so this instead
        just re-checks the invariant for every candidate at the eligible
        tiers on every nightly pass. Has its own ``limit`` budget so it
        never competes with the enrichment page budget
        (``MAX_PAGES_PER_PASS``).
        """
        compressed: list[str] = []
        if limit <= 0:
            return compressed
        for candidate in self._iter_candidates():
            if len(compressed) >= limit:
                break
            if candidate.tier not in {"warm", "cold", "archive"}:
                continue
            if self._human_zone_span(candidate.text) == _AMBIGUOUS_HUMAN_ZONE:
                # _compress_candidate_text reads its sections through
                # _section_text, which fails closed to "" on an ambiguous
                # human zone (see _human_zone_span) -- so this candidate
                # would silently look like it has nothing to compress
                # (returns None below) rather than raise or signal
                # anything. Unlike _render_briefing's _extract_human_zone
                # call, nothing on this path -- or on the `cold`-tier path,
                # which never reaches _render_briefing at all -- would
                # otherwise put this in the logs.
                logger.warning(
                    "Compiled briefing %s has ambiguous human-zone markers; "
                    "skipping compression check this pass",
                    candidate.rel_path,
                )
                self._record_human_zone_ambiguous(candidate.rel_path)
                continue
            new_text = self._compress_candidate_text(candidate.text)
            if new_text is None:
                continue
            note_path = self.vault_path / candidate.rel_path
            with vault_write_lock(self.vault_path) as lock:
                try:
                    current_bytes = note_path.read_bytes()
                except FileNotFoundError:
                    continue
                # Compared as text, not as re-encoded bytes: _read_page_text
                # decodes with errors="replace", so a page carrying one
                # invalid byte never re-encodes to what is on disk and the
                # two questions "did this change since the scan?" and "do
                # these bytes survive a decode?" got silently answered as
                # one. Decoding the current bytes the same way the candidate
                # was decoded keeps the freshness check in one space.
                if self._decode_page_bytes(current_bytes) != candidate.text:
                    # Changed since _iter_candidates() ran this pass (e.g.
                    # enriched earlier in the same pass) -- skip rather than
                    # risk clobbering it with a stale compression diff.
                    continue
                if current_bytes != candidate.text.encode("utf-8"):
                    # These bytes are not valid UTF-8, so decoding replaced
                    # something with U+FFFD -- and this step rewrites the
                    # whole page from that decoded text. Writing it back
                    # would burn the replacement character into the file
                    # permanently, human zone included (an owner note saved
                    # in another encoding looks exactly like this). Skipping
                    # is the same outcome the old re-encode comparison gave,
                    # but said out loud instead of silently forever.
                    logger.warning(
                        "Compiled briefing %s has bytes that are not valid "
                        "UTF-8; skipping compression rather than rewriting "
                        "them as replacement characters",
                        candidate.rel_path,
                    )
                    self._queue_undecodable_page(
                        candidate.rel_path, existing_lock=lock
                    )
                    continue
                new_bytes = new_text.encode("utf-8")
                self._snapshot_pass_page(
                    candidate.rel_path, before=current_bytes, after=new_bytes
                )
                write_validated_vault_markdown(
                    self.vault_path,
                    note_path,
                    new_bytes,
                    manifest=self._manifest(),
                    existing_lock=lock,
                )
            compressed.append(candidate.rel_path)
        return compressed

    @classmethod
    def _compress_candidate_text(cls, text: str) -> str | None:
        """Pure text -> text (or ``None`` when nothing needs to change).

        ТЗ 6.3: "Источники, сформировавшие страницу" (the sources table),
        Open Conflicts, and the human zone are never compressed -- this
        only ever touches Recent Changes, Open Loops, and History.
        """
        recent_rows = cls._dated_rows(
            text, "Recent Changes", empty_placeholder="No recent changes captured yet."
        )
        open_rows = cls._dated_rows(
            text, "Open Loops", empty_placeholder="No open loops captured yet."
        )

        kept_recent = recent_rows[-RECENT_CHANGES_KEEP:]
        overflow_recent = recent_rows[:-RECENT_CHANGES_KEEP] if len(
            recent_rows
        ) > RECENT_CHANGES_KEEP else []

        today = date.today()
        kept_open: list[tuple[str, str, str]] = []
        abandoned_open: list[tuple[str, str, str]] = []
        for row_date, row_text, row_source in open_rows:
            try:
                parsed = date.fromisoformat(row_date)
            except ValueError:
                parsed = None
            if parsed is not None and (today - parsed).days > OPEN_LOOP_ABANDON_DAYS:
                abandoned_open.append((row_date, row_text, row_source))
            else:
                kept_open.append((row_date, row_text, row_source))

        if not overflow_recent and not abandoned_open:
            return None

        history_rows = cls._dated_rows(
            text, "History", empty_placeholder="(nothing archived yet)"
        )
        history_rows = [
            *history_rows,
            *(
                (row_date, f"[Recent Changes] {row_text}", row_source)
                for row_date, row_text, row_source in overflow_recent
            ),
            *(
                (row_date, f"[Open Loop, abandoned] {row_text}", row_source)
                for row_date, row_text, row_source in abandoned_open
            ),
        ]

        new_text = cls._replace_section(
            text,
            "Recent Changes",
            cls._render_dated_bullets(
                kept_recent, empty="No recent changes captured yet."
            ),
        )
        new_text = cls._replace_section(
            new_text,
            "Open Loops",
            cls._render_dated_bullets(kept_open, empty="No open loops captured yet."),
        )
        return cls._upsert_history_section(new_text, history_rows)

    @classmethod
    def _upsert_history_section(
        cls, text: str, rows: list[tuple[str, str, str]]
    ) -> str:
        rendered = cls._render_dated_bullets(rows, empty="(nothing archived yet)")
        # _sections_from_text is a plain regex with no human-zone awareness:
        # a decoy "## History" line inside the zone would make it report a
        # section that is not really there. _replace_section would then
        # correctly find no real heading and no-op, silently dropping
        # `rows` instead of archiving them. _heading_match (zone-aware, the
        # same check _replace_section itself makes) is used here so the two
        # stay in agreement about whether a real "## History" exists.
        history_pattern = re.compile(r"^##\s+History\s*$", re.MULTILINE)
        zone = cls._human_zone_span(text)
        if cls._heading_match(text, history_pattern, zone) is not None:
            return cls._replace_section(text, "History", rendered)
        return cls._insert_section_before(
            text,
            heading="History",
            before_heading="Owner Notes",
            new_lines=rendered,
        )

    def _refresh_qmd_index(self) -> None:
        result = self.qmd.refresh_after_searchable_write()
        if not result["available"]:
            logger.warning("qmd refresh skipped: %s", " | ".join(result["errors"]))
            return
        if result["errors"]:
            logger.warning("qmd refresh failed: %s", " | ".join(result["errors"]))

    @staticmethod
    def _artifact_title(request: str) -> str:
        first = " ".join(str(request or "").split()).strip()
        if not first:
            return "Assistant Output"
        return first[:80].rstrip(" .!?") or "Assistant Output"

    def _render_output_artifact(
        self,
        *,
        title: str,
        request: str,
        output_markdown: str,
        artifact_type: str,
        created_at: datetime,
    ) -> str:
        body = self._clip(output_markdown.strip(), MAX_OUTPUT_ARTIFACT_CHARS)
        return (
            "---\n"
            f"date: {created_at.date().isoformat()}\n"
            "type: assistant-output\n"
            f"description: {json.dumps(title, ensure_ascii=False)}\n"
            f"artifact_type: {artifact_type}\n"
            f"created: {created_at.date().isoformat()}\n"
            f"updated: {created_at.date().isoformat()}\n"
            f"last_accessed: {created_at.date().isoformat()}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            f"# {title}\n\n"
            "## Request\n"
            f"{request.strip()}\n\n"
            "## Output\n"
            f"{body}\n"
        )

    def _render_batch_consolidation(
        self,
        *,
        headline: str,
        payload: dict[str, Any],
        events: list[CompiledBatchConsolidationEvent],
        created_at: datetime,
    ) -> str:
        summary = (
            self._paragraph(payload.get("summary"))
            or "No durable cross-source pattern captured yet."
        )
        themes = self._normalize_list(payload.get("themes"))
        follow_ups = self._normalize_list(payload.get("follow_ups"))
        source_paths = self._merge_paths([event.source_rel_path for event in events])
        updated_paths = self._merge_paths(
            *[list(event.updated_paths) for event in events]
        )
        themes_block = "\n".join(
            self._render_bullets(themes, empty="No strong recurring themes yet.")
        )
        follow_ups_block = "\n".join(
            self._render_bullets(
                follow_ups,
                empty="No immediate follow-ups captured.",
            )
        )
        updated_block = "\n".join(self._render_sources(updated_paths))
        sources_block = "\n".join(self._render_sources(source_paths))
        return (
            "---\n"
            f"date: {created_at.date().isoformat()}\n"
            "type: compiled-consolidation\n"
            f"description: {json.dumps(headline, ensure_ascii=False)}\n"
            "artifact_type: compiled-batch\n"
            f"created: {created_at.date().isoformat()}\n"
            f"updated: {created_at.date().isoformat()}\n"
            f"last_accessed: {created_at.date().isoformat()}\n"
            "relevance: 1.0\n"
            "tier: active\n"
            "---\n\n"
            f"# {headline}\n\n"
            "## Summary\n"
            f"{summary}\n\n"
            "## Themes\n"
            f"{themes_block}\n\n"
            "## Follow-ups\n"
            f"{follow_ups_block}\n\n"
            "## Updated Briefings\n"
            f"{updated_block}\n\n"
            "## Sources\n"
            f"{sources_block}\n"
        )
