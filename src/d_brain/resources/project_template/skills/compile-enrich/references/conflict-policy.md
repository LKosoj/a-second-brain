---
type: note
description: Trust levels, claim kinds, conflict-type resolution, Verify sampling, and pass budgets for the compile-enrich pipeline. Mirrors src/d_brain/services/compiled_briefings.py; do not restate it as an independent spec.
last_accessed: 2026-08-05
relevance: 0.6
tier: warm
---

# Conflict And Trust Policy

## Source Trust

Four levels, `SOURCES_TRUST_VALUES`: `own > integration > forwarded >
inferred` (`TRUST_RANK`: own=4, integration=3, forwarded=2, inferred=1).
Trust is decided **by code, never by the model** (`_source_trust_level`),
and it is never used to pick a conflict winner (only dates and claim kind do
that, see below). The rule depends on where the source lives, not just its
entry marker:

- `thoughts/` -- always `own`.
- `imports/plaud/` (dictaphone meeting recordings) -- always capped at
  `forwarded`, regardless of any marker.
- `imports/` (other integrations) -- always `integration`.
- `daily/` -- read off the excerpt's own entry headers (`[voice]`,
  `[text]`, `[photo]` = own; `[forward from: ...]` = forwarded; any other
  marker = inferred); an excerpt spanning more than one entry takes the
  weakest level across all of them.
- anything else -- `inferred` (fails closed).

Only `own` and `integration` are strong enough, alone, to justify an
automatic action with consequences (creating a task, editing CRM data,
silently superseding an existing claim). `forwarded`/`inferred` alone must
never trigger one.

## Claim Kinds

`CLAIM_KIND_VALUES`: `fact | opinion | commitment`.

## Conflict Types

`CONFLICT_TYPE_VALUES`: `temporal | factual | contextual`. The type actually
used is decided deterministically (`_effective_conflict_type`), not by
trusting the model's label outright:

1. An **opinion**-kind new claim is always `temporal` -- an owner's opinion
   legitimately changes over time, so a disagreement there is a
   supersession, not something to flag for review.
2. Otherwise, if **both** the existing and the new claim's source dates are
   known and differ, the conflict is `temporal` regardless of what the model
   labeled it.
3. Otherwise the model's label is honored if it is one of the three valid
   values; an invalid or missing label falls back to `factual` (fail-closed:
   keep both statements, flag for owner review).

**Temporal** conflicts resolve automatically: the later source date wins
(`_temporal_winner_is_new`); ties or unparseable dates default to the new
claim. Trust never enters this decision.

**Factual** conflicts do not resolve automatically. Both sides stay
recorded forever in "Sources That Shaped This Page"; the disagreement is
logged as one row in "Open Conflicts" for the owner. A response through the
decisions queue (`services/decisions_queue.py`) picks the active side,
removes the row, and decrements the page's `conflicts_open` counter -- both
claims still stay in the sources table either way, since there is nowhere
else to record "which one won" per claim.

**Contextual** conflicts resolve automatically, with no "Open Conflicts"
row and no owner decision: both claims are treated as valid, each in its
own scope. The new claim is recorded in "Sources That Shaped This Page" as
usual; if the model supplied an explanation of how the two scopes differ
(`context_note`), it is appended to that new claim's own row rather than
touching the existing claim's row.

## Verify

One batched, read-only, clean-context model call per page, checking whether
each sampled claim actually follows from the source excerpt given (no
network access, so it can only judge entailment, not look anything up).
The same call must explicitly pass three page-wide checks before a write:
every substantive statement has source coverage, all content belongs to the
target topic, and dates/statuses/owners/outcomes are internally consistent or
retain the source's uncertainty. A missing or failed check rejects the page.
Sampling rate: 100% of new claims for `core`/`active` tier pages, 25%
(minimum 1) for everything else -- `warm`, `cold`, `archive`, or an
unset/unknown tier all fall back to the warm fraction (fail-closed rather
than skipping Verify for a tier the source ТЗ never named).

If Verify rejects a majority of one page's newly proposed claims, the whole
page write is skipped for this pass -- not partially applied. A single
rejection does not queue anything by itself: only once
`MAX_VERIFY_REJECTED_RETRIES` consecutive rejections land against the same
source snapshot does the page reach the decisions queue as a
`verify-rejected` item (`_queue_verify_rejected`) -- earlier rejections just
retry on the next pass. See "Decisions Queue Item Kinds" below.

## Pass Budgets (`run_nightly_maintenance`)

- `MAX_PAGES_PER_PASS` = 40 -- distinct compiled pages one pass may write.
- `MAX_MODEL_CALLS_PER_PASS` = 200 -- every impact/compile/verify/JSON-repair
  call shares this one budget.
- `MAX_ENRICHMENTS_PER_PAGE_PER_MONTH` = 8 -- beyond this, further source
  material for that page waits for the decisions queue instead of
  compounding unattended drift onto one page.
- `MAX_CLAIMS_PER_PASS` = 20 -- claims accepted from one model response.

Exhausting a budget ends the pass normally: the remainder stays queued, and
the exhaustion itself is reported in the owner's daily digest -- it is never
treated as a failure.

## Monthly Fact-Check (`compiled_fact_check.py`)

A separate, narrower, fully deterministic pass (**zero model calls**) from
the nightly compile-enrich pass above. It only ever patches two frontmatter
fields, `last_verified` and `confidence`. A page that fails its check is
left unadvanced and queued as a `fact-check-rejected` decision rather than
being silently patched or archived.

"Monthly" is the per-page cadence, not the schedule: the pass itself runs
every night, in the same scheduled cycle as everything else here
(`maintenance.compiled-fact-check`, `triggers=("scheduled-post",)`). Each
run takes only the stalest `DEFAULT_FACT_CHECK_PAGE_LIMIT` = 20
core/active/warm pages, which works out to revisiting any single page
roughly once a month. There is no separate monthly timer.

## Decisions Queue Item Kinds

Eight kinds exist today (`services/decisions_queue.py`):

- `fact-check-rejected` -- written into `.session/decisions-queue.json` by
  `compiled_fact_check.py`. A response either confirms the page (bumps
  `last_verified`) or rejects it (archives the page); either way the queue
  entry is removed.
- `conflict` -- not stored in the JSON file at all. It is derived live, one
  item per open-conflict row, by reading every `compiled/**` page's own
  "Open Conflicts" table directly.
- `blocked-action` -- written into `.session/decisions-queue.json` by
  `CompiledBriefingService._queue_blocked_action` (ТЗ 4.4/7.1/7.2) when a
  new claim would otherwise have won a temporal supersession by source date
  alone, but its trust level (`forwarded`/`inferred`) is not strong enough,
  alone, to apply that replacement automatically
  (`_trust_allows_consequential_action`). The conflict type is downgraded
  to `factual` instead, so both claims stay recorded and the page's own
  Open Conflicts table gets the usual row; this queue item additionally
  explains to the owner *why* the normally-automatic date-based
  supersession did not just happen. No dedicated response exists for it --
  only the generic `reject`/`defer` below. Protected from eviction under
  `QUEUE_CAP`, same as `conflict`.
- `duplicate-candidate` -- written into `.session/decisions-queue.json` when
  Resolve (ТЗ 5.2) creates a new page whose confidence against an existing
  same-domain page falls in the possible-duplicate zone (0.85-0.95, see
  `RESOLVE_POSSIBLE_DUPLICATE_CONFIDENCE_THRESHOLD` /
  `RESOLVE_SAME_PAGE_CONFIDENCE_THRESHOLD` in `compiled_briefings.py`). A
  response either "link"s the two pages (sets a mutual `duplicate_of`
  frontmatter pointer on both, patched independently) or "distinct"s them
  (clears the queue entry, touches neither page) -- this module never
  merges pages itself; a real merge is always the owner's call.
- `drift` -- written into `.session/decisions-queue.json` by
  `CompiledBriefingService._queue_monthly_drift` when a page's monthly
  enrichment budget (`MAX_ENRICHMENTS_PER_PAGE_PER_MONTH`) is reached. No
  dedicated response exists for it -- only the generic `reject`/`defer`
  below.
- `verify-rejected` -- written by
  `CompiledBriefingService._queue_verify_rejected` once a page's Verify step
  has rejected a majority of its proposed claims
  `MAX_VERIFY_REJECTED_RETRIES` times in a row against the same source
  snapshot (both the incremental refresh path and the nightly freshness
  backfill count toward the same retry counter). Incremental entries retain
  the source event, including for a page that has not been created yet.
  "Повторить проверку" clears the page's retry counter and returns that
  source to the refresh queue; the generic `reject` below just drops the
  queue entry and leaves the page exhausted as-is.
- `page-encoding-broken` -- written by
  `CompiledBriefingService._queue_undecodable_page` when a compiled page's
  bytes on disk are not valid UTF-8. Every writer refuses such a page rather
  than rewrite it and commit U+FFFD over the owner's real bytes, so it stops
  being updated and compressed until the owner re-saves the file as UTF-8.
  No dedicated response -- the remedy is outside the vault entirely.
- `human-zone-ambiguous` -- written by
  `CompiledBriefingService._queue_human_zone_ambiguous` when a page's
  `<!-- human:start -->` / `<!-- human:end -->` markers do not resolve to
  exactly one span. Every writer fails closed rather than guess which text
  is the owner's, and the background drain then releases the refresh event
  for a free retry every 300s indefinitely, so the queue entry is the only
  thing that tells the owner to go fix the markers. No dedicated response --
  the remedy is editing the markers by hand.

Both of the last two are written to the queue rather than only to the pass
journal on purpose: the journal exists only inside `run_nightly_maintenance`,
so on the background drain -- the far more common producer of both -- a
journal-only signal is a silent no-op.

Any other `kind` found in the JSON file is treated honestly as unsupported:
only `reject` (drop it) and `defer` (leave it) are offered for it.

## Human-Readable Queue Mirror

`decisions_queue.write_queue_document` renders the same queue items the
bot's "Очередь" screen shows to `summaries/compile/decisions-queue.md`,
regenerated on every mutating response and, as a safety net, once more
nightly from `maintenance.compiled-digest`. Both surfaces read the same
data, so they cannot disagree about what is open.
