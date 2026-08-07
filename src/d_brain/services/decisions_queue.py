"""Owner decisions queue: listing and responses (задача L, ТЗ 7.2).

This module is the single place that defines two things: what a queue item
*is*, and what an owner response to it physically does to the vault.

Eight item kinds exist in production today:

- ``"fact-check-rejected"`` -- written by ``compiled_fact_check.py`` into
  ``.session/decisions-queue.json`` when a page fails its monthly re-check.
  A response either confirms the page (bumps ``last_verified``) or rejects
  it (archives the page via ``CompiledBriefingService._archive_candidate``);
  either way the queue entry is removed.
- ``"conflict"`` -- not stored in the JSON file at all. It lives only in a
  page's own "Open Conflicts" table (ТЗ 5.4: a factual conflict keeps both
  sides forever in the "Sources That Shaped This Page" table, which never
  shrinks). ``list_queue_items`` derives one item per open-conflict row by
  reading every ``compiled/**`` page directly, the same way
  ``compiled_enrich_report._collect_conflicts`` already does for the digest.
  A response means picking which side is "active": the row is removed from
  "Open Conflicts" and ``conflicts_open`` is decremented (as an int). Both
  claims stay recorded in the sources table regardless of which side was
  picked -- there is nowhere else to record a per-claim "winner", so the
  choice is only preserved in the response journal below, for audit.

- ``"duplicate-candidate"`` (задача N) -- written by a producer elsewhere
  (out of scope for this module) into ``.session/decisions-queue.json`` when
  two compiled pages look like they describe the same thing. ``page`` is the
  newer page, ``candidate_page`` the existing one it may duplicate. A
  response is deliberately conservative: this module never merges pages
  (data loss risk, not safe without a model) -- "link" marks a mutual
  ``duplicate_of`` frontmatter pointer on *both* pages (each patched
  independently, so one page vanishing does not block marking the other),
  "distinct" only clears the queue entry and touches neither page. The queue
  entry is deduped by ``(kind, page)`` like every other kind (see the
  30-item-cap note below) -- a second duplicate-candidate proposal for the
  same ``page`` is dropped by that dedup before it ever reaches this module,
  a known limitation this module does not widen the key to fix.

- ``"drift"`` (ТЗ 5.6 table: "Максимум обогащений одной страницы за
  календарный месяц: 8 (при превышении — в очередь решений с пометкой о
  дрейфе)") -- written by
  ``CompiledBriefingService._queue_monthly_drift`` when a page's monthly
  enrichment budget (``MAX_ENRICHMENTS_PER_PAGE_PER_MONTH``) is reached.
  Same shape as ``"duplicate-candidate"``: ``kind``, ``page``, ``summary``,
  ``since``. Deduped by ``(kind, page, month-of-since)`` -- ТЗ 5.6 counts
  per calendar month, so a page that keeps hitting the cap this month only
  ever gets one queue entry, while drifting again next month replaces that
  entry instead of being swallowed by it (see ``_dedup_key``). This module
  offers no dedicated response for it (no known
  automatic remedy exists) -- only the generic ``reject``/``defer`` below.

- ``"verify-rejected"`` (ТЗ 5.2 step 4 / 7.2) -- written by
  ``CompiledBriefingService._queue_verify_rejected`` once a page's Verify
  step has rejected a majority of its proposed claims
  ``MAX_VERIFY_REJECTED_RETRIES`` times in a row against the same source
  snapshot (both the incremental ``refresh_after_write`` path and the
  nightly ``_backfill_freshness_notes`` path count toward the same
  retry counter and can trigger this). Same shape as ``"drift"``: ``kind``,
  ``page``, ``summary``, ``since``. "Повторить проверку" clears the page's
  retry counter (``CompiledBriefingService._clear_verify_rejection``) so
  the next pass attempts Verify again instead of skipping the page as
  still exhausted; "Отклонить" (the generic reject below) just drops the
  queue entry and leaves the page exhausted as-is.

- ``"blocked-action"`` (ТЗ 4.4/7.1/7.2) -- written by
  ``CompiledBriefingService._queue_blocked_action`` when a low-trust source
  (``forwarded``/``inferred``) would otherwise have won a temporal
  supersession by date alone: ``_trust_allows_consequential_action`` blocks
  applying it automatically, so both claims stay on the page (the same
  "factual" Open Conflicts row the page itself gets) and this entry tells
  the owner *why* -- page, the two claims, the blocking source, its trust
  level. Same shape as ``"drift"``: ``kind``, ``page``, ``summary``,
  ``since``. Deduped by ``(kind, page)``, so a page that keeps getting
  blocked on later passes only ever gets the one entry; the conflict itself
  is still (separately) re-recorded on the page every time, so nothing
  about it is lost by not requeuing here. Protected from eviction
  (``_PROTECTED_QUEUE_KINDS``) per ТЗ 7.2. No dedicated response exists for
  it (the actual choice between the two claims happens via the page's own
  ``"conflict"`` item above) -- only the generic ``reject``/``defer`` below.

- ``"page-encoding-broken"`` -- written by
  ``CompiledBriefingService._queue_undecodable_page`` when a compiled
  page's bytes on disk are not valid UTF-8. Every writer in
  ``compiled_briefings.py`` refuses such a page rather than rewrite it and
  commit U+FFFD over the owner's real bytes, so it stops being updated and
  compressed until the owner re-saves that file as UTF-8. Same shape as
  ``"drift"``: ``kind``, ``page``, ``summary``, ``since``. Deduped by
  ``(kind, page)``, so a page that stays broken across many passes only
  ever gets the one entry. Not protected from eviction, like ``"drift"``
  and ``"verify-rejected"`` and unlike ``"blocked-action"``: ТЗ 7.2 names
  the protected kinds explicitly and this is not one of them. Note that
  the eviction an entry like this causes on a full queue is *not* counted
  for the owner on its most common path: ``_record_queue_eviction`` folds
  the count into the active pass, and the background drain that produces
  most of these entries runs with no pass open. That gap is not specific
  to this kind -- ``"duplicate-candidate"`` is written from the same
  method on the same pathless path -- and is recorded as a known
  limitation rather than papered over here. No dedicated response exists
  for this kind either: the remedy is outside the vault entirely (re-save
  the file as UTF-8 in an editor), so only the generic ``reject``/
  ``defer`` below are offered.

- ``"human-zone-ambiguous"`` -- written by
  ``CompiledBriefingService._queue_human_zone_ambiguous`` when a compiled
  page's <!-- human:start/end --> markers cannot be resolved to exactly one
  span. Every writer fails closed on such a page rather than guess which
  text is the owner's, so the page stops being enriched and compressed
  until the owner fixes the markers by hand. Same shape as ``"drift"``:
  ``kind``, ``page``, ``summary``, ``since``, deduped by ``(kind, page)``,
  not protected from eviction -- and, like ``"page-encoding-broken"``
  above, written to the queue rather than only to the pass journal
  (``CompileEnrichPass.human_zone_ambiguous_pages``, which the digest also
  reports): the journal exists only inside ``run_nightly_maintenance``, so
  on the background drain -- the far more common producer -- it is a silent
  no-op. That silence mattered more here than anywhere else: the drain
  *releases* such an event for a free retry every 300s with ``attempts``
  deliberately left unchanged (see ``_drain_queue_once``), so without a
  queue entry the page retried forever and told the owner nothing. No
  dedicated response exists for this kind either -- the remedy is editing
  the page's markers by hand -- so only the generic ``reject``/``defer``
  below are offered.

Any other ``kind`` found in the JSON queue file is treated honestly as
unsupported: it is never guessed at, only ``reject`` (drop it from the
queue) and ``defer`` (leave it) are offered.

Human-readable queue file and the weekly review (задача N)
------------------------------------------------------------
``write_queue_document`` renders the same ``QueueItem`` lines the bot's
"Очередь" screen shows (via the shared ``queue_kind_label``/
``format_queue_item_line`` below -- ``bot/dashboard.py`` imports both
instead of keeping its own copy) to a human-readable file under
``summaries/compile/``, so the two surfaces can never drift apart. It is
called from three places, always with the caller's already-open
``vault_write_lock`` passed through as ``existing_lock`` -- never a second,
nested lock (see the "Writes go through..." note above): once at the end of
every mutating response handler below, once from
``_regenerate_queue_document_after_append`` right after
``append_decision_queue_entries`` writes a new entry (the producer's own
already-held lock, passed through explicitly as ``append_decision_queue_
entries``'s ``existing_lock`` argument -- not rediscovered from a registry,
see that function's docstring), and once nightly from
``processor._run_compiled_digest_cycle`` as a safety net, right after that
cycle's own digest write and inside the same lock. It is deliberately
best-effort (catches and logs rather than raising): by the time any caller
reaches it, the actual decision has already committed, so a failure to
refresh this mirror file must never roll back or fail an already-successful
response.

``mark_page_human_reviewed`` is a separate, single-purpose writer for the
weekly-review screen's "confirm as reviewed" button (bot-layer, not a queue
response): it patches the code-only ``human_reviewed`` frontmatter field to
today's date, modeled on ``_apply_fact_check_confirm`` below (same
lock, same graceful handling of a page that vanished). It does not touch
the decisions queue at all, so it does not call ``write_queue_document``.

Writes go through the same two techniques already used elsewhere in this
codebase: ``patch_validated_vault_frontmatter``/``write_validated_vault_markdown``
for anything that touches a page (both already take the vault lock
internally and check identity/hash before writing), and the plain
tempfile+``os.replace`` ``_atomic_write_text`` (imported from
``compiled_briefings``) for the JSON queue file, nested inside a
``vault_write_lock`` this module holds when a page write and a queue-file
write must happen as one unit (ТЗ: "все записи атомарны, под vault-lock, с
fingerprint-защитой от гонок").

One exception: ``CompiledBriefingService._archive_candidate`` acquires its
*own* fresh ``vault_write_lock`` internally and has no way to accept one
already held by a caller (flock is not safely re-entrant across two open
file descriptors to the same file -- a second ``vault_write_lock(vault_path)``
call from inside a lock this module already holds would self-deadlock). So
a "reject" response for a ``fact-check-rejected`` item calls it as an
unlocked, top-level step, then removes the queue entry in a second,
separate lock acquisition -- two atomic steps instead of one combined
transaction, not a race: the archive call is atomic on its own, and a
process crashing between the two steps only leaves a queue entry pointing
at an already-archived page, which the confirm/reject handlers below treat
as an idempotent no-op rather than an error.

Response journal
-----------------
Every response (including "defer") is appended to
``.session/decisions-queue-responses.jsonl`` -- one JSON object per line,
append-only, distinct from the nightly compile-enrich pass journal
(``.session/compile-enrich.json``, overwritten whole every pass) and from
the fact-check pass journal (``.session/compile-fact-check.json``). This is
a lower-stakes audit trail, not authoritative vault state, so it is a plain
append (mirroring ``SessionStore.append``'s json-line convention) rather
than its own separate lock file; when the response also mutates a page or
the queue, the journal append happens inside that same ``vault_write_lock``
so it is still serialized against concurrent responses.

30-item cap (ТЗ 7.2, ТЗ 5.6 "Размер очереди решений: 30")
-----------------------------------------------------------
``append_decision_queue_entries`` is the shared writer both
``compiled_fact_check.py`` and any future producer call. On overflow it
evicts the oldest (by ``since``) entries first, except entries whose
``kind`` is in ``_PROTECTED_QUEUE_KINDS``. ``"conflict"`` items are
protected by kind even though they never actually reach this file (they
live on pages) -- documented as a deliberate, currently-vacuous safety
margin rather than reworking this into a tier lookup for a case that cannot
occur yet. ``"blocked-action"`` items DO reach this file (produced by
``CompiledBriefingService._queue_blocked_action``) and are protected for
real.

Eviction candidates are drawn from entries already on file first, never
shrinking a small ``new_entries`` call just because protected entries
already on file have piled up to ``QUEUE_CAP`` or beyond -- plausible once a
real ``"blocked-action"`` producer exists, one distinct page at a time, each
never auto-resolving. In that situation there is nothing evictable in
``existing`` at all, and the combined list is simply allowed to grow past
``QUEUE_CAP`` rather than dropping the entry a caller just asked to queue
before the owner ever saw it once.

That guarantee is about *this* call's own entries never being sacrificed for
an *existing* backlog, not about ``new_entries`` being cap-exempt on its own:
a single call can itself carry more entries than ``QUEUE_CAP`` (a bulk
producer, e.g. ``compiled_fact_check.py`` with a ``--page-limit`` above 30 --
see ``run_compiled_monthly_verify.py``), and nothing on file yet can absorb
that. ``_trim_new_entries_to_cap`` (code review) handles exactly that case:
when ``new_entries`` alone already exceeds ``QUEUE_CAP``, its own oldest (by
``since``) non-protected entries are trimmed down first, before ``existing``
is even considered, keeping the newest ones and every protected one. A
single call's batch that is itself no larger than ``QUEUE_CAP`` is never
touched by this step (room is always >= its own non-protected count in that
case), so the "existing protected backlog" guarantee above is unaffected.
Protected entries are never trimmed from ``new_entries`` either -- if they
alone already fill or exceed ``QUEUE_CAP``, the batch passes through
untouched, the same "let it grow past cap" lesser evil as the
``existing``-side case. The function's ``int`` return value is how many
entries that call evicted in total (from ``existing``, from ``new_entries``,
or both), so a caller can fold it into the pass journal's
``queue_evictions`` and get it in front of the owner (ТЗ 7.2: "факт
вытеснения попадает в дайджест").
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from d_brain.manifest import VaultManifest, load_manifest_for_vault
from d_brain.services.compiled_briefings import (
    CompiledBriefingCandidate,
    CompiledBriefingService,
    _atomic_write_text,
)
from d_brain.services.frontmatter import (
    UnsafeVaultPathError,
    patch_frontmatter_bytes,
    patch_validated_vault_frontmatter,
    read_vault_file_bytes,
    write_validated_vault_markdown,
)
from d_brain.services.vault_lock import VaultWriteLock, vault_write_lock

logger = logging.getLogger(__name__)

# Public: reused by compiled_enrich_report.py instead of hardcoding the same
# path a second time (задача L code review, defect 5).
DECISIONS_QUEUE_RELATIVE_PATH = Path(".session") / "decisions-queue.json"
_RESPONSE_JOURNAL_RELATIVE_PATH = Path(".session") / "decisions-queue-responses.jsonl"

# ТЗ 5.6 "Размер очереди решений: 30".
QUEUE_CAP = 30
# See module docstring: "conflict" is protected even though no producer
# ever writes one to the JSON file today (ТЗ names it as needing protection,
# but it lives on pages, not here); "blocked-action" is the other kind ТЗ
# 7.2 names as needing protection, and does have a real producer
# (``CompiledBriefingService._queue_blocked_action``).
_PROTECTED_QUEUE_KINDS = frozenset({"conflict", "blocked-action"})

FACT_CHECK_REJECTED_KIND = "fact-check-rejected"
CONFLICT_KIND = "conflict"
BLOCKED_ACTION_KIND = "blocked-action"
DUPLICATE_CANDIDATE_KIND = "duplicate-candidate"
# ТЗ 5.6 monthly-per-page enrichment budget overrun. No dedicated response
# exists for it (see module docstring) -- it only ever gets the generic
# reject/defer.
DRIFT_KIND = "drift"
# ТЗ 5.2 step 4 / 7.2: a page Verify keeps rejecting on the same source
# snapshot (see module docstring and ``_apply_verify_rejected_retry``).
VERIFY_REJECTED_KIND = "verify-rejected"
# A page whose bytes on disk are not valid UTF-8 (see module docstring and
# ``CompiledBriefingService._queue_undecodable_page``).
PAGE_ENCODING_BROKEN_KIND = "page-encoding-broken"
# A page whose <!-- human:start/end --> markers are ambiguous (see module
# docstring and ``CompiledBriefingService._queue_human_zone_ambiguous``).
HUMAN_ZONE_AMBIGUOUS_KIND = "human-zone-ambiguous"


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One entry on the owner's decisions queue, from either source.

    ``conflict_row`` is only set for ``kind == "conflict"``: the exact
    5-tuple (date, existing_claim, existing_source, new_claim, new_source)
    ``CompiledBriefingService._open_conflicts_rows`` parsed it from, kept so
    a later response can re-locate (and verify the continued presence of)
    this exact row rather than a row index that could shift under a
    concurrent edit.

    ``candidate_page`` is only set for ``kind == "duplicate-candidate"``
    (задача N): the existing page ``page`` might duplicate.
    """

    kind: str
    page: str
    summary: str
    since: str
    conflict_row: tuple[str, str, str, str, str] | None = None
    candidate_page: str = ""


def queue_item_fingerprint(item: QueueItem) -> str:
    r"""8-hex-char identity fingerprint of ``(kind, page, since)`` (задача L
    code review, defect 4).

    A queue button only carries the item's *index* into the session's
    ``queue_items`` page slice (same convention as the file browser's
    ``menu:filesentry:{index}``). Applying a response re-reads and can
    shorten that list, so a second click that still lands in range (e.g. a
    duplicated tap, or the owner tapping a stale screen) can silently
    resolve to a *different but still valid* item at the same index --
    unlike the file browser, applying a decision to the wrong page is not
    easily reversible. The caller embeds this fingerprint in the button and
    must refuse to act when it does not match the item currently at that
    index.

    ``candidate_page`` is folded into the hash too (задача N): without it,
    two different ``duplicate-candidate`` proposals about the same ``page``
    reported on the same ``since`` date would fingerprint identically, so a
    stale button could silently resolve to the wrong candidate.

    ``conflict_row`` is folded in for the same reason (code review): a
    single compiled page can have two *different* open conflicts dated the
    same day (e.g. two sources landing the same day), and ``candidate_page``
    is always empty for ``kind == "conflict"``, so without the row content
    itself in the hash both conflicts would fingerprint identically -- a
    stale button could then silently apply the owner's choice to the wrong
    conflict row.

    All 9 fields (kind, page, since, candidate_page, and the 5 row fields)
    are combined with a length-prefixed, "netstring-style" encoding --
    each field becomes ``f"{len(field)}:{field}"`` before concatenation --
    instead of being joined with a plain separator (code review, defect:
    separator injection). A plain separator, even a control character like
    ``\x1f``, is not enough on its own: if the separator can also occur
    *inside* a field's value, two different field tuples can still
    concatenate to the same string -- e.g. ``("a", "b\x1fc")`` and
    ``("a\x1fb", "c")`` both join to ``"a\x1fb\x1fc"``. ``conflict_row``
    values come from the "Open Conflicts" table on a compiled page --
    ultimately model output -- so a claim or source string containing a
    stray ``\x1f`` is not something this module can rule out upstream.
    Length-prefixing sidesteps the problem entirely: decoding is
    unambiguous by construction (read digits up to the next ``:`` for the
    length, consume exactly that many characters as the field, repeat), so
    no field's content -- ``\x1f``, digits, colons, or anything else -- can
    ever be mistaken for a boundary. Two different field tuples can
    therefore never produce the same encoded string, which is also why
    ``("a", "bc")`` and ``("ab", "c")`` (the original concatenation-without-
    separator concern) still cannot collide either: they encode as
    ``"1:a2:bc"`` and ``"2:ab1:c"``, which differ.
    """
    row = item.conflict_row if item.conflict_row is not None else ("", "", "", "", "")
    fields = (item.kind, item.page, item.since, item.candidate_page, *row)
    encoded = "".join(f"{len(field)}:{field}" for field in fields)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return digest[:8]


@dataclass(frozen=True, slots=True)
class ResponseOutcome:
    """What ``apply_response`` did, for the caller (bot handler / CLI) to
    relay to the owner. ``ok=False`` only for a request this module cannot
    carry out at all -- today that is one case: a page whose bytes are not
    valid UTF-8, which no handler here may rewrite (see
    ``_apply_conflict_choice``). Every other outcome, including an
    idempotent "already resolved" no-op, is ``ok=True`` with an explanatory
    message. An ``action_id`` not offered for this item's kind is not one
    of these at all -- ``apply_response`` raises ``ValueError`` for it
    before any ``ResponseOutcome`` exists.

    Callers must not treat ``ok`` as "was there anything to say": the bot
    handler shows ``message`` either way, which is what makes the ``False``
    case land in front of the owner at all."""

    ok: bool
    message: str


def _read_raw_queue(path: Path) -> list[dict[str, Any]]:
    """Tolerant read: a missing or corrupt file is an empty queue, not an
    error (ТЗ: "Отсутствие или порча файла — это пустой список")."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _trim_new_entries_to_cap(
    new_entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """When a single call's own ``new_entries`` batch is already larger than
    ``QUEUE_CAP`` by itself (a bulk producer -- see module docstring), there
    is no ``existing``-side room to borrow from, so the batch must be
    trimmed on its own terms before ``_evict_overflow`` even looks at
    ``existing``. Drops the oldest (by ``since``) non-protected entries
    first, keeping the newest ones -- same eviction rule as the
    ``existing`` side. Protected entries are never dropped here: if they
    alone already fill or exceed ``QUEUE_CAP``, the batch is returned
    unchanged (lesser evil, same as ``existing`` overflowing on protected
    entries alone -- see module docstring).

    A batch no larger than ``QUEUE_CAP`` is always returned unchanged: this
    is strictly about a single call's own entries exceeding the cap by
    themselves, never about squeezing room for an ``existing`` backlog (that
    stays ``_evict_overflow``'s job).
    """
    if len(new_entries) <= QUEUE_CAP:
        return new_entries, 0
    protected_count = sum(
        1 for entry in new_entries if entry.get("kind") in _PROTECTED_QUEUE_KINDS
    )
    room = QUEUE_CAP - protected_count
    if room <= 0:
        # The protected entries alone already fill the cap, so there is no
        # room left to fit anything else into -- but dropping every
        # non-protected entry of this batch is not "keeping the newest
        # ones", it is losing all of them. Let the queue grow past the cap
        # instead, the same lesser evil the ``existing`` side already takes
        # when its own protected backlog overflows (see module docstring).
        return new_entries, 0
    evictable_indices = sorted(
        (
            index
            for index, entry in enumerate(new_entries)
            if entry.get("kind") not in _PROTECTED_QUEUE_KINDS
        ),
        key=lambda index: str(new_entries[index].get("since") or ""),
    )
    # Always positive here: both counts are the batch's own size minus the
    # same ``protected_count``, so this is exactly ``len(new_entries) -
    # QUEUE_CAP``, and the two returns above already took every batch that
    # is not over the cap or has no room at all.
    overflow = len(evictable_indices) - room
    drop = set(evictable_indices[:overflow])
    kept = [entry for index, entry in enumerate(new_entries) if index not in drop]
    return kept, len(drop)


def _evict_overflow(
    existing: list[dict[str, Any]], new_entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Drop the oldest (by ``since``) non-protected entries from ``existing``
    until ``existing + new_entries`` fits ``QUEUE_CAP``, or until nothing
    evictable is left in ``existing``. Returns ``(combined_list,
    evicted_count)``.

    ``new_entries`` must already have been trimmed by
    ``_trim_new_entries_to_cap`` (its only caller does that, before working
    out drift supersession from the survivors -- see there). Eviction
    candidates for the remaining room are drawn only from ``existing``: an
    item that survived the batch-level trim must still survive at least
    until the owner has seen it once (code review, ТЗ 7.2 -- "новые пункты
    [не должны] перестанут помещаться вовсе"). Without this split, a backlog
    of protected entries alone reaching ``QUEUE_CAP`` made every subsequent
    call's brand-new entry the *only* evictable item (nothing else in the
    combined list is not protected), so it was dropped in the very call that
    added it -- the owner would never see it even once. Letting the combined
    list grow past ``QUEUE_CAP`` in that situation is the lesser evil.

    Protected entries are never evicted from either side, even if that
    leaves the combined list over cap -- see module docstring.
    """
    room_needed = len(existing) + len(new_entries) - QUEUE_CAP
    if room_needed <= 0:
        return [*existing, *new_entries], 0
    evictable_indices = sorted(
        (
            index
            for index, entry in enumerate(existing)
            if entry.get("kind") not in _PROTECTED_QUEUE_KINDS
        ),
        key=lambda index: str(existing[index].get("since") or ""),
    )
    drop = set(evictable_indices[:room_needed])
    kept_existing = [entry for index, entry in enumerate(existing) if index not in drop]
    return [*kept_existing, *new_entries], len(drop)


def _dedup_key(entry: dict[str, Any]) -> tuple[object, ...]:
    """The key that decides whether an entry is "already queued".

    ``(kind, page)`` for every kind: one live item per page and per kind, so
    a producer re-running does not pile up copies. ``drift`` additionally
    keys on the calendar month of ``since``, because ТЗ 5.6 counts
    enrichments *per calendar month* -- a page drifting again in a later
    month is a genuinely new event, and keying it the same way would let a
    stale entry the owner never got round to swallow it silently (found in
    review).
    """
    kind = entry.get("kind")
    page = entry.get("page")
    if kind == DRIFT_KIND:
        since = entry.get("since")
        month = since[:7] if isinstance(since, str) else None
        return (kind, page, month)
    return (kind, page)


def append_decision_queue_entries(
    vault_path: Path,
    entries: list[dict[str, str]],
    *,
    existing_lock: VaultWriteLock | None = None,
) -> int:
    """Append new entries to ``.session/decisions-queue.json``, skipping any
    already on file by ``_dedup_key``, then enforce ``QUEUE_CAP`` by
    eviction (see module docstring). A fresh ``drift`` entry also replaces
    that page's stale drift entry from an earlier month, so the owner sees
    one current drift signal per page rather than one per month it ever
    drifted. That replacement is computed from the entries that survive
    the batch trim, never from ones trimmed away: a stale signal must not
    be retired on behalf of a fresh one nobody will ever see.

    Supersession only reaches entries already on file, though. Two drift
    entries for one page from two different months, passed in a single
    call, both survive: ``_dedup_key`` deliberately keys drift by month so
    a new month is not swallowed by the old one, and nothing collapses
    them afterwards. No producer does that today (``_queue_monthly_drift``
    sends one entry per call), so this is left as a documented limit for
    whichever bulk drift producer appears first, not guessed at here.

    Returns the number of entries evicted by this call (0 when nothing was
    evicted, including when nothing new was actually appended) -- code
    review, ТЗ 7.2: the fact of eviction must reach the owner's digest, so a
    caller needs to know it happened. Callers within ``compiled_briefings.py``
    fold this into the active pass's ``queue_evictions`` counter (mirroring
    ``budget_exhausted``), which ``_write_pass_journal``/``compiled_enrich_
    report.py`` surface as one "Требует решения" digest line.

    Extracted from ``compiled_fact_check.py``'s former private
    ``_append_decision_queue_entries`` (задача L) -- the 30-item cap and the
    drift month key are new; everything else (tolerant read, callers hold
    the vault write lock) is unchanged. This is the shared writer for any
    current or future producer of JSON-backed queue entries.

    Also regenerates the human-readable mirror (ТЗ 7.2: "перегенерируемый
    при каждом изменении очереди") when an entry actually got appended --
    see ``_regenerate_queue_document_after_append``. ``existing_lock`` must
    be the exact lock object the caller already holds (every real producer
    does, see the module docstring) -- passed through explicitly rather
    than rediscovered, since searching a process-wide lock registry for
    "something open for this path" cannot tell whether it is *this* call
    stack's own lock or an unrelated one another thread happens to be
    holding at the same moment (defect found in review: a thread that never
    took the lock could still borrow another thread's live capability and
    write under it). ``None`` when the caller holds no lock -- the mirror
    is then simply left stale rather than opening a fresh one, which risks
    self-deadlock (see below).
    """
    path = vault_path / DECISIONS_QUEUE_RELATIVE_PATH
    existing = _read_raw_queue(path)
    seen_keys = {_dedup_key(item) for item in existing}
    new_entries: list[dict[str, str]] = []
    for entry in entries:
        # Dedup against keys already on file AND against ones already kept
        # from earlier in this same call -- a caller can legitimately pass
        # more than one entry per call (this is a general-purpose shared
        # writer, not one written only for today's one-item-at-a-time
        # producers), and the file-only check above let same-call
        # duplicates straight through (code review defect 4).
        key = _dedup_key(entry)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        new_entries.append(entry)
    if not new_entries:
        return 0
    # Trim the batch before working out what it supersedes, not after: a
    # fresh drift entry only earns the right to replace that page's stale
    # one if it actually survives into the file. Computing supersession
    # from the untrimmed batch dropped the old entry on behalf of a new one
    # that was itself then trimmed away, leaving the page with *no* drift
    # signal at all -- the opposite of the "one current drift signal per
    # page" this supersession exists to give the owner.
    new_entries, batch_evicted = _trim_new_entries_to_cap(new_entries)
    superseded = {
        (entry.get("kind"), entry.get("page"))
        for entry in new_entries
        if entry.get("kind") == DRIFT_KIND
    }
    kept = [
        item
        for item in existing
        if (item.get("kind"), item.get("page")) not in superseded
    ]
    combined, existing_evicted = _evict_overflow(kept, new_entries)
    evicted_count = batch_evicted + existing_evicted
    _atomic_write_text(
        path, json.dumps(combined, ensure_ascii=False, indent=2) + "\n"
    )
    _regenerate_queue_document_after_append(vault_path, existing_lock=existing_lock)
    return evicted_count


def _regenerate_queue_document_after_append(
    vault_path: Path, *, existing_lock: VaultWriteLock | None
) -> None:
    """Refresh the human-readable queue mirror right after the JSON queue
    file itself changed, instead of leaving it to lag until the owner
    answers something or the nightly safety net runs (ТЗ 7.2 code review).

    Every current caller of ``append_decision_queue_entries``
    (``compiled_fact_check.run_monthly_fact_check``,
    ``CompiledBriefingService._queue_duplicate_candidate``,
    ``CompiledBriefingService._queue_monthly_drift``) already holds the
    vault write lock and passes it through as ``existing_lock`` -- see the
    module docstring. Using that lock directly, instead of opening a second
    ``vault_write_lock`` here, is mandatory, not a nicety: flock is not
    reentrant across two open file descriptors to the same file, so a
    second acquisition from inside a lock already held would self-deadlock
    (this module has hit that exact bug before, see the "Writes go
    through..." docstring section above). If no lock was passed -- should
    not happen given the documented contract, but a future caller might get
    it wrong -- the mirror is simply left stale rather than risking that
    deadlock.
    """
    if existing_lock is None:
        logger.warning(
            "append_decision_queue_entries called without a held vault "
            "lock for %s; decisions queue mirror not regenerated",
            vault_path,
        )
        return
    try:
        manifest = load_manifest_for_vault(vault_path)
    except Exception as exc:  # noqa: BLE001 - best-effort mirror, see write_queue_document
        logger.warning(
            "Failed to load manifest for decisions queue mirror: %s", exc
        )
        return
    # No second try/except around this call: ``write_queue_document`` already
    # swallows and logs everything itself, by documented contract. The reason
    # that contract matters here is the same one behind the manifest guard
    # above -- by this point the JSON queue is on disk, evictions and all, and
    # the caller is still waiting on the eviction count ТЗ 7.2 puts in the
    # owner's digest. A stale mirror must not take that count with it; it
    # catches up on the next queue change or on the nightly safety net.
    write_queue_document(vault_path, manifest=manifest, existing_lock=existing_lock)


def _remove_queue_entry(vault_path: Path, *, kind: str, page: str) -> None:
    """Idempotent: removing an already-absent entry is a no-op, not an
    error (a concurrent response, or a page archived by other means, may
    have already cleared it)."""
    path = vault_path / DECISIONS_QUEUE_RELATIVE_PATH
    existing = _read_raw_queue(path)
    remaining = [
        entry
        for entry in existing
        if not (entry.get("kind") == kind and entry.get("page") == page)
    ]
    if len(remaining) == len(existing):
        return
    _atomic_write_text(
        path, json.dumps(remaining, ensure_ascii=False, indent=2) + "\n"
    )


def _json_backed_items(vault_path: Path) -> list[QueueItem]:
    path = vault_path / DECISIONS_QUEUE_RELATIVE_PATH
    items: list[QueueItem] = []
    for entry in _read_raw_queue(path):
        page = str(entry.get("page") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        if not page or not summary:
            continue
        kind = str(entry.get("kind") or "").strip() or "queued"
        since = str(entry.get("since") or "").strip()
        candidate_page = str(entry.get("candidate_page") or "").strip()
        items.append(
            QueueItem(
                kind=kind,
                page=page,
                summary=summary,
                since=since,
                candidate_page=candidate_page,
            )
        )
    return items


def _conflict_items(candidates: list[CompiledBriefingCandidate]) -> list[QueueItem]:
    items: list[QueueItem] = []
    for candidate in candidates:
        rows = CompiledBriefingService._open_conflicts_rows(candidate.text)
        for row in rows:
            since, existing_claim, existing_source, new_claim, new_source = row
            summary = (
                f"«{existing_claim}» ({existing_source}) "
                f"vs «{new_claim}» ({new_source})"
            )
            items.append(
                QueueItem(
                    kind=CONFLICT_KIND,
                    page=candidate.rel_path,
                    summary=summary,
                    since=since,
                    conflict_row=row,
                )
            )
    return items


def list_queue_items(vault_path: Path) -> list[QueueItem]:
    """All current decisions queue items: JSON-backed entries first (file
    order), then one item per open-conflict row across ``compiled/**``
    (candidate order, i.e. sorted by path -- see
    ``CompiledBriefingService._iter_candidates``).

    Pure and read-only: never writes, never calls a model.
    """
    vault_path = Path(vault_path)
    service = CompiledBriefingService(vault_path)
    candidates = service._iter_candidates()
    return [*_json_backed_items(vault_path), *_conflict_items(candidates)]


def action_button_specs(item: QueueItem) -> tuple[tuple[str, str], ...]:
    """``(action_id, button_label)`` pairs for one item, in display order.

    ``conflict`` labels are the actual claim text (ТЗ: "покажи две кнопки с
    реальным текстом обеих версий", not abstract "confirm"/"reject" words).
    A kind without a dedicated branch below only offers reject/defer -- this
    module never guesses what "confirm" would mean for a kind it does not
    understand.
    """
    if item.kind == FACT_CHECK_REJECTED_KIND:
        return (
            ("confirm", "Подтвердить: страница актуальна"),
            ("reject", "Архивировать страницу"),
            ("defer", "Отложить"),
        )
    if item.kind == CONFLICT_KIND and item.conflict_row is not None:
        _since, existing_claim, _existing_source, new_claim, _new_source = (
            item.conflict_row
        )
        return (
            ("keep_existing", f"«{existing_claim}»"),
            ("keep_new", f"«{new_claim}»"),
            ("defer", "Отложить"),
        )
    if item.kind == VERIFY_REJECTED_KIND:
        return (
            ("retry", "Повторить проверку"),
            ("reject", "Прекратить попытки"),
            ("defer", "Отложить"),
        )
    if item.kind == DUPLICATE_CANDIDATE_KIND:
        return (
            ("link", "Связать как похожие"),
            ("distinct", "Пометить как разные"),
            ("defer", "Отложить"),
        )
    return (
        ("reject", "Отклонить (удалить из очереди)"),
        ("defer", "Отложить"),
    )


# --- Shared queue-line rendering (задача N) --------------------------------
#
# Both the bot's "Очередь" screen (bot/dashboard.py) and the human-readable
# queue file below (``write_queue_document``) call these instead of keeping
# their own copies, so the two surfaces can never say something different
# about the same item.

QUEUE_KIND_LABELS = {
    FACT_CHECK_REJECTED_KIND: "проверка фактов",
    CONFLICT_KIND: "конфликт",
    DUPLICATE_CANDIDATE_KIND: "возможный дубликат",
    DRIFT_KIND: "дрейф",
    VERIFY_REJECTED_KIND: "отклонено проверкой",
    BLOCKED_ACTION_KIND: "действие заблокировано",
    PAGE_ENCODING_BROKEN_KIND: "кодировка файла страницы",
    HUMAN_ZONE_AMBIGUOUS_KIND: "маркеры личной зоны",
}

_QUEUE_DOCUMENT_RELATIVE_PATH = Path("summaries") / "compile" / "decisions-queue.md"
_EMPTY_QUEUE_DOCUMENT_TEXT = "Очередь пуста — открытых пунктов нет."


def queue_kind_label(kind: str) -> str:
    """Honest label for a queue item's kind -- an unsupported kind (задача L,
    ТЗ 7.2: no producer exists for one today, but the UI must not hide or
    guess at it) still gets a readable, truthful label instead of a blank
    or a made-up one."""
    return QUEUE_KIND_LABELS.get(kind, f"неизвестный тип: {kind}")


def format_queue_item_line(item: QueueItem) -> str:
    """One line for one queue item: ``- [kind] page — summary``."""
    return f"- [{queue_kind_label(item.kind)}] {item.page} — {item.summary}"


def queue_document_path(vault_path: Path) -> Path:
    """Human-readable mirror of the decisions queue (задача N, missed
    acceptance criterion) -- one file, always overwritten in place, unlike
    the dated digest/brief files, since it reflects live queue state rather
    than a point-in-time snapshot. Lives at ``summaries/compile/`` next to
    the daily digest (ТЗ 7.2), not directly under ``summaries/`` -- both
    route to the same ``derived`` frontmatter profile regardless of depth
    (``frontmatter.route_profile`` matches on ``parts[0] == "summaries"``)."""
    return Path(vault_path) / _QUEUE_DOCUMENT_RELATIVE_PATH


def render_queue_document(items: list[QueueItem]) -> str:
    """Render the whole human-readable queue document body (no frontmatter).

    An empty queue gets an explicit placeholder line, matching the bot
    screen's own empty-queue text (``build_queue_text`` in
    ``bot/dashboard.py``) -- not an empty file (задача N requirement).
    """
    lines = ["# Очередь решений", ""]
    if not items:
        lines.append(_EMPTY_QUEUE_DOCUMENT_TEXT)
        return "\n".join(lines) + "\n"
    lines.extend(format_queue_item_line(item) for item in items)
    return "\n".join(lines) + "\n"


def render_queue_document_note(items: list[QueueItem]) -> bytes:
    """Wrap the queue document body in the frontmatter the ``derived``
    profile requires for anything under ``summaries/`` (mirrors
    ``compiled_enrich_report.render_digest_note``)."""
    today = date.today().isoformat()
    header = (
        "---\n"
        "type: compiled-decisions-queue\n"
        'description: "Человекочитаемая очередь решений владельца"\n'
        f"last_accessed: {today}\n"
        "relevance: 1.0\n"
        "tier: active\n"
        "---\n\n"
    )
    return (header + render_queue_document(items)).encode("utf-8")


def write_queue_document(
    vault_path: Path,
    *,
    manifest: VaultManifest,
    existing_lock: VaultWriteLock,
) -> None:
    """Regenerate the human-readable queue mirror file (задача N).

    Called at the end of every mutating response handler below, inside the
    lock the handler already holds -- ``existing_lock`` is always passed
    through, never a second, nested ``vault_write_lock`` (flock is not
    safely re-entrant across two open file descriptors to the same file --
    see module docstring). Also called once more, nightly, from
    ``processor._run_compiled_digest_cycle`` as a safety net, right after
    that cycle's digest write and inside the same lock.

    Best-effort by design: by the time any caller reaches this, the actual
    decision (a page patch, a queue-file edit) has already committed, so a
    failure to refresh this mirror must never roll back or fail an
    already-successful response -- caught broadly and logged instead of
    raised.
    """
    try:
        items = list_queue_items(vault_path)
        write_validated_vault_markdown(
            vault_path,
            queue_document_path(vault_path),
            render_queue_document_note(items),
            manifest=manifest,
            existing_lock=existing_lock,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort mirror, see docstring
        logger.warning("Failed to write decisions queue document: %s", exc)


def _append_response_journal(
    vault_path: Path,
    *,
    kind: str,
    page: str,
    action_id: str,
    chosen_label: str,
    at: datetime,
) -> None:
    path = vault_path / _RESPONSE_JOURNAL_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "at": at.isoformat(timespec="seconds"),
            "kind": kind,
            "page": page,
            "action": action_id,
            "chosen": chosen_label,
        },
        ensure_ascii=False,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _find_candidate_by_path(
    service: CompiledBriefingService, rel_path: str
) -> CompiledBriefingCandidate | None:
    for candidate in service._iter_candidates():
        if candidate.rel_path == rel_path:
            return candidate
    return None


def _apply_fact_check_confirm(
    vault_path: Path, item: QueueItem, *, today: date
) -> ResponseOutcome:
    manifest = load_manifest_for_vault(vault_path)
    page_path = vault_path / item.page
    patched = True
    with vault_write_lock(vault_path) as lock:
        try:
            patch_validated_vault_frontmatter(
                vault_path,
                page_path,
                {"last_verified": today.isoformat()},
                manifest=manifest,
                existing_lock=lock,
            )
        except UnsafeVaultPathError:
            # Page already gone (missing file or missing parent directory --
            # both surface as UnsafeVaultPathError, not FileNotFoundError,
            # from patch_validated_vault_frontmatter) -- still clear the
            # stale queue entry. Nothing was actually patched, so the
            # outcome message below must not claim the date was updated.
            patched = False
        _remove_queue_entry(vault_path, kind=item.kind, page=item.page)
        _append_response_journal(
            vault_path,
            kind=item.kind,
            page=item.page,
            action_id="confirm",
            chosen_label="страница актуальна",
            at=datetime.now().astimezone(),
        )
        write_queue_document(vault_path, manifest=manifest, existing_lock=lock)
    if patched:
        message = f"Подтверждено: {item.page} — дата проверки обновлена."
    else:
        message = (
            f"Страница {item.page} уже отсутствовала — обновлять нечего, "
            "запись удалена из очереди."
        )
    return ResponseOutcome(ok=True, message=message)


def _apply_fact_check_reject(vault_path: Path, item: QueueItem) -> ResponseOutcome:
    service = CompiledBriefingService(vault_path)
    candidate = _find_candidate_by_path(service, item.page)
    archived_rel_path = ""
    if candidate is not None:
        # Unlocked, top-level call on purpose -- see module docstring on
        # why this cannot be nested inside a lock this module holds.
        archived_rel_path = service._archive_candidate(candidate)
    manifest = load_manifest_for_vault(vault_path)
    with vault_write_lock(vault_path) as lock:
        _remove_queue_entry(vault_path, kind=item.kind, page=item.page)
        _append_response_journal(
            vault_path,
            kind=item.kind,
            page=item.page,
            action_id="reject",
            chosen_label="страница устарела",
            at=datetime.now().astimezone(),
        )
        write_queue_document(vault_path, manifest=manifest, existing_lock=lock)
    # ``candidate is None`` or an empty ``archived_rel_path`` both mean
    # ``_archive_candidate`` found nothing on disk to move (задача L code
    # review, defect 6) -- the message must not claim an archive happened
    # when it did not.
    if archived_rel_path:
        message = (
            f"Страница {item.page} отправлена в архив "
            f"(в архиве: {archived_rel_path})."
        )
    else:
        message = (
            f"Страница {item.page} уже отсутствовала — архивировать нечего, "
            "запись удалена из очереди."
        )
    return ResponseOutcome(ok=True, message=message)


def _apply_conflict_choice(
    vault_path: Path, item: QueueItem, action_id: str
) -> ResponseOutcome:
    assert item.conflict_row is not None
    _since, existing_claim, _existing_source, new_claim, _new_source = (
        item.conflict_row
    )
    chosen_label = existing_claim if action_id == "keep_existing" else new_claim

    manifest = load_manifest_for_vault(vault_path)
    page_path = vault_path / item.page
    with vault_write_lock(vault_path) as lock:
        try:
            source = read_vault_file_bytes(vault_path, page_path)
        except FileNotFoundError:
            # Page vanished (missing file or missing parent directory)
            # between listing and responding -- a graceful negative result,
            # not an uncaught exception (задача L code review, defect 3).
            return ResponseOutcome(
                ok=True,
                message=f"Страница {item.page} пропала — конфликт больше не актуален.",
            )
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError:
            # list_queue_items builds this item from _read_page_text, which
            # decodes with errors="replace" -- so a page whose bytes are not
            # valid UTF-8 reaches the owner's queue and their tap lands here.
            # Decoding it the same lenient way is not an option: this handler
            # rewrites the whole page, so it would burn U+FFFD over the real
            # bytes. Fail closed with something the owner can act on instead
            # of a bare UnicodeDecodeError, which the bot handler catches as
            # a ValueError and reports as "action unavailable" forever.
            return ResponseOutcome(
                ok=False,
                message=(
                    f"Страница {item.page} содержит байты, не читаемые как "
                    "UTF-8 — конфликт не тронут, почините кодировку файла."
                ),
            )
        current_rows = CompiledBriefingService._open_conflicts_rows(text)
        try:
            row_index = current_rows.index(item.conflict_row)
        except ValueError:
            # Already resolved (or the table changed) -- idempotent no-op.
            return ResponseOutcome(
                ok=True,
                message=f"Конфликт на {item.page} уже разрешён.",
            )
        remaining_rows = current_rows[:row_index] + current_rows[row_index + 1 :]
        new_text = CompiledBriefingService._replace_section(
            text,
            "Open Conflicts",
            CompiledBriefingService._render_open_conflicts_table(remaining_rows),
        )
        new_bytes = patch_frontmatter_bytes(
            new_text.encode("utf-8"), {"conflicts_open": len(remaining_rows)}
        )
        write_validated_vault_markdown(
            vault_path,
            page_path,
            new_bytes,
            manifest=manifest,
            existing_lock=lock,
            expected_full_sha256=hashlib.sha256(source).hexdigest(),
        )
        _append_response_journal(
            vault_path,
            kind=item.kind,
            page=item.page,
            action_id=action_id,
            chosen_label=chosen_label,
            at=datetime.now().astimezone(),
        )
        write_queue_document(vault_path, manifest=manifest, existing_lock=lock)
    return ResponseOutcome(
        ok=True,
        message=f"Конфликт на {item.page} разрешён в пользу «{chosen_label}».",
    )


def _apply_unknown_reject(vault_path: Path, item: QueueItem) -> ResponseOutcome:
    # Was missing a vault_write_lock entirely (задача N code review): every
    # sibling handler wraps its queue-file mutation in one, this one didn't.
    manifest = load_manifest_for_vault(vault_path)
    with vault_write_lock(vault_path) as lock:
        _remove_queue_entry(vault_path, kind=item.kind, page=item.page)
        _append_response_journal(
            vault_path,
            kind=item.kind,
            page=item.page,
            action_id="reject",
            chosen_label="",
            at=datetime.now().astimezone(),
        )
        write_queue_document(vault_path, manifest=manifest, existing_lock=lock)
    return ResponseOutcome(
        ok=True,
        message=f"Запись «{item.kind}» для {item.page} удалена из очереди.",
    )


def _apply_duplicate_link(vault_path: Path, item: QueueItem) -> ResponseOutcome:
    """Mark a mutual "confirmed similar" relationship on both pages'
    frontmatter (задача N). Deliberately conservative: no automatic merge
    (data loss risk, not safe without a model) -- both pages are kept, each
    patched independently with its own soft-fail handling for a vanished
    page, so one page disappearing does not block marking the other."""
    manifest = load_manifest_for_vault(vault_path)
    with vault_write_lock(vault_path) as lock:
        linked: list[str] = []
        for page, other in (
            (item.page, item.candidate_page),
            (item.candidate_page, item.page),
        ):
            try:
                patch_validated_vault_frontmatter(
                    vault_path,
                    vault_path / page,
                    {"duplicate_of": other},
                    manifest=manifest,
                    existing_lock=lock,
                )
                linked.append(page)
            except UnsafeVaultPathError:
                # This page vanished -- soft-fail independently of the other
                # page, same convention as _apply_fact_check_confirm above.
                pass
        _remove_queue_entry(vault_path, kind=item.kind, page=item.page)
        _append_response_journal(
            vault_path,
            kind=item.kind,
            page=item.page,
            action_id="link",
            chosen_label=item.candidate_page,
            at=datetime.now().astimezone(),
        )
        write_queue_document(vault_path, manifest=manifest, existing_lock=lock)
    if len(linked) == 2:
        message = f"Страницы {item.page} и {item.candidate_page} связаны как похожие."
    elif linked:
        message = f"Связь отмечена только на {linked[0]} — вторая страница пропала."
    else:
        message = "Обе страницы пропали — связывать нечего, запись удалена из очереди."
    return ResponseOutcome(ok=True, message=message)


def _apply_duplicate_distinct(vault_path: Path, item: QueueItem) -> ResponseOutcome:
    """Owner confirmed the two pages are not duplicates -- no page
    frontmatter changes, only the queue entry is cleared (задача N)."""
    manifest = load_manifest_for_vault(vault_path)
    with vault_write_lock(vault_path) as lock:
        _remove_queue_entry(vault_path, kind=item.kind, page=item.page)
        _append_response_journal(
            vault_path,
            kind=item.kind,
            page=item.page,
            action_id="distinct",
            chosen_label="",
            at=datetime.now().astimezone(),
        )
        write_queue_document(vault_path, manifest=manifest, existing_lock=lock)
    return ResponseOutcome(
        ok=True,
        message=f"Отмечено: {item.page} и {item.candidate_page} — разные страницы.",
    )


def _apply_verify_rejected_retry(vault_path: Path, item: QueueItem) -> ResponseOutcome:
    """Owner asked to retry a page Verify gave up on (ТЗ 5.2 step 4):
    clears the page's Verify-rejection retry counter
    (``CompiledBriefingService._clear_verify_rejection``) so the next pass
    attempts Verify again on this source snapshot instead of immediately
    skipping it as still exhausted."""
    service = CompiledBriefingService(vault_path)
    manifest = load_manifest_for_vault(vault_path)
    with vault_write_lock(vault_path) as lock:
        service._clear_verify_rejection(item.page)
        _remove_queue_entry(vault_path, kind=item.kind, page=item.page)
        _append_response_journal(
            vault_path,
            kind=item.kind,
            page=item.page,
            action_id="retry",
            chosen_label="повторная проверка",
            at=datetime.now().astimezone(),
        )
        write_queue_document(vault_path, manifest=manifest, existing_lock=lock)
    return ResponseOutcome(
        ok=True,
        message=(
            f"Счётчик отклонений сброшен для {item.page} — "
            "страница будет проверена снова."
        ),
    )


def mark_page_human_reviewed(
    vault_path: Path, rel_path: str, *, today: date | None = None
) -> ResponseOutcome:
    """Record that the owner reviewed one compiled page during the weekly
    review (задача N, missed acceptance criterion) -- patches the code-only
    ``human_reviewed`` frontmatter field (page-schema.md: "when the owner
    last confirmed this page") to today's date.

    Modeled on ``_apply_fact_check_confirm`` above: same targeted
    frontmatter patch with the vault lock passed through, same graceful
    handling of a page that vanished since the weekly screen was rendered.
    This is not a decisions-queue response -- it does not touch the queue,
    so it does not call ``write_queue_document``.
    """
    resolved_today = today or date.today()
    manifest = load_manifest_for_vault(vault_path)
    page_path = vault_path / rel_path
    with vault_write_lock(vault_path) as lock:
        try:
            patch_validated_vault_frontmatter(
                vault_path,
                page_path,
                {"human_reviewed": resolved_today.isoformat()},
                manifest=manifest,
                existing_lock=lock,
            )
        except UnsafeVaultPathError:
            return ResponseOutcome(
                ok=True,
                message=f"Страница {rel_path} пропала — отметить нечего.",
            )
    return ResponseOutcome(
        ok=True,
        message=f"Отмечено как просмотрено: {rel_path}.",
    )


def _defer_outcome(item: QueueItem) -> ResponseOutcome:
    # Best-effort, unlocked: deferring changes nothing in the vault, so this
    # never needs to race against a concurrent mutation of the same item.
    return ResponseOutcome(ok=True, message=f"Отложено: {item.page}.")


def apply_response(
    vault_path: Path,
    item: QueueItem,
    action_id: str,
    *,
    today: date | None = None,
) -> ResponseOutcome:
    """Apply one owner response to one queue item (задача L, ТЗ 7.2).

    ``action_id`` must be one of the ids ``action_button_specs(item)``
    offered for this item -- anything else is a caller/UI bug, not a normal
    outcome, and raises ``ValueError`` rather than silently doing nothing.
    """
    valid_ids = {action_id for action_id, _label in action_button_specs(item)}
    if action_id not in valid_ids:
        raise ValueError(
            f"action {action_id!r} is not offered for queue item kind "
            f"{item.kind!r} (offered: {sorted(valid_ids)})"
        )
    vault_path = Path(vault_path)

    if action_id == "defer":
        outcome = _defer_outcome(item)
        try:
            _append_response_journal(
                vault_path,
                kind=item.kind,
                page=item.page,
                action_id="defer",
                chosen_label="",
                at=datetime.now().astimezone(),
            )
        except OSError as exc:  # pragma: no cover - best-effort audit trail
            logger.warning("Failed to journal deferred response: %s", exc)
        return outcome

    if item.kind == FACT_CHECK_REJECTED_KIND:
        resolved_today = today or date.today()
        if action_id == "confirm":
            return _apply_fact_check_confirm(vault_path, item, today=resolved_today)
        return _apply_fact_check_reject(vault_path, item)

    if item.kind == CONFLICT_KIND:
        return _apply_conflict_choice(vault_path, item, action_id)

    if item.kind == DUPLICATE_CANDIDATE_KIND:
        if action_id == "link":
            return _apply_duplicate_link(vault_path, item)
        return _apply_duplicate_distinct(vault_path, item)

    if item.kind == VERIFY_REJECTED_KIND and action_id == "retry":
        return _apply_verify_rejected_retry(vault_path, item)

    return _apply_unknown_reject(vault_path, item)
