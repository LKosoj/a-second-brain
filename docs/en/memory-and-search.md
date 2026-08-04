# Memory, notes, and search

[Русский](../ru/memory-and-search.md) | [Documentation index](../index.md)

This page explains how A Second Brain stores notes, selects context, finds old
material, and keeps memory useful as the vault grows.

## The short version

The private vault's Markdown files, attachments, and imported originals are the
source of truth. Search indexes, compiled briefings, memory scores, and session
files help the assistant work with those sources, but do not replace them.

For each question, the assistant uses the smallest useful combination of:

1. Curated core context such as `MEMORY.md` and current goals.
2. Compiled briefings for fast status and history answers.
3. QMD semantic search for notes with related meaning.
4. Exact text search for literal values such as an ID, path, or date.

This avoids loading every note into every prompt. It also keeps old material
available without letting it dominate routine answers.

## What is stored where

| Path | Role | Source or derived |
|---|---|---|
| `vault/MEMORY.md` | Curated facts, preferences, and decisions that should remain useful for weeks or months | Source |
| `vault/daily/` | Raw chronological capture from Telegram and integrations | Source |
| `vault/goals/` | Three-year vision and yearly, monthly, and weekly focus | Source |
| `vault/thoughts/` | Reusable ideas, learnings, project notes, and reflections | Source |
| `vault/business/` | CRM, professional network, and event context | Source |
| `vault/projects/` | Clients, leads, and active projects | Source |
| `vault/imports/` | Archived documents, web pages, YouTube material, and PLAUD recordings | Source |
| `vault/compiled/` | Short operational briefings assembled from source notes | Derived |
| `vault/summaries/` | Periodic reports and reusable assistant outputs | Derived |
| `vault/MOC/` | Maps of Content: navigation notes that link a domain together | Source or maintained navigation |
| `vault/attachments/` | Images and other uploaded binary files | Source |
| `vault/.qmd/` | Project-local QMD configuration, database, model cache, and locks | Derived runtime state |
| `vault/.memory-entries.json` | Memory metadata for individual entries inside daily files | Derived runtime state |
| `vault/.compiled/`, `.graph/`, `.session/` | Briefing state, graph reports, and current session handoff | Derived runtime state |

`vault-manifest.json` defines the allowed user-content roots, infrastructure
paths, QMD index name, context budget, and required note metadata. The runtime
uses this manifest when validating writes.

Deleting derived state does not intentionally delete the Markdown sources, but
do not remove runtime directories casually. Follow the backup and restore
procedure if rebuilding an instance.

## How incoming material becomes knowledge

The first write preserves the input. Voice is transcribed, text is appended to
the current daily file, photos are saved under attachments, and documents are
saved on disk before extraction. Supported external sources are archived under
`imports/` with enough provenance to return to the original.

The full daily cycle then runs three phases in separate agent contexts:

1. **Capture** reads the day's entries and classifies them into structured JSON.
2. **Execute** creates the notes and context updates selected during
   classification, plus optional Todoist tasks.
3. **Reflect** writes the report, updates the rolling handoff, runs memory and
   vault-health maintenance, and refreshes QMD.

`/process` stops after a read-only preview. `/process_full` runs the write-heavy
cycle immediately. The installed timer runs the same full cycle at 21:00.

Not every daily entry needs a separate card. A durable card is useful when a
fact, decision, relationship, lesson, or project state should be found and
updated independently. Transient chronology can remain in `daily/`.

## How context is selected

### 1. Bounded core context

The assistant eagerly loads a small set of high-value files:

- `MEMORY.md`;
- weekly, monthly, and latest yearly goals;
- today's and yesterday's daily notes;
- business and project indexes;
- the previous session handoff;
- bounded vault, Git, graph, and hygiene summaries.

The default total budget is `200000` bytes and is set in
`vault-manifest.json`. If the pack is too large, lower-priority sections are
replaced by explicit omission markers. `MEMORY.md` and weekly goals are kept.
The assistant therefore knows the current focus without reading the full
archive.

Vault text is treated as evidence, not as executable instructions. A command
written inside a note does not gain authority merely because it was retrieved.

### 2. Compiled briefings

Compiled briefings are concise operational summaries across projects, people,
meetings, topics, and decisions. They are useful for questions such as “What is
the current status?” or “What changed since the last review?”

Each briefing links back to its sources and records freshness and confidence.
It is still derived data. When an answer depends on a precise fact, the runtime
can verify the briefing against curated context or the source note.

Relevant source writes enqueue a briefing refresh. Post-cycle maintenance drains
that queue, checks required sections and source links, refreshes stale
briefings, archives obsolete ones, and updates QMD when searchable content
changed.

A reusable direct answer can also be filed under `summaries/answers/`. The
current filter rejects outputs shorter than 180 characters. Longer outputs are
filed only when they contain a list, reach 350 characters, or the request
contains one of the current cues: `что`, `status`, `статус`, `решили`,
`изменил`, or `summary`. This prevents every short reply from becoming another
note.

### 3. QMD semantic retrieval

Semantic search means search by meaning rather than only by matching words.
QMD turns note chunks into embeddings, which are numeric representations used
to compare meaning. A reranker is a second model that reorders the best
candidates for the specific query.

The project keeps its QMD index under `vault/.qmd` and does not use the user's
global QMD state. It indexes core notes, daily entries, thoughts, compiled
briefings, imported sources, goals, business, projects, summaries, and MOCs.

With empty `EMB_MODEL` and `RERANK_MODEL` settings, QMD uses local
`Qwen3-Embedding-0.6B` and `Qwen3-Reranker-0.6B` models on CPU. Remote endpoints
are optional. See [Integrations](integrations.md#qmd) for installation and
model settings.

### 4. Exact lookup

Semantic search is the wrong tool for an exact environment variable, order ID,
filesystem path, code symbol, or literal phrase. For those questions, the agent
or operator uses exact text search such as `rg` against the relevant vault
source or derived directory. There is no separate `a-second-brain` exact-search
subcommand. Exact search does not silently replace a failed semantic search,
because the two searches answer different questions.

### 5. Links and Maps of Content

Wiki-links and MOCs provide stable human navigation. The graph tools can find
broken links, isolated notes, and weak metadata. New links should express a
real relationship, not just a shared word. A few strong links are more useful
than many speculative ones.

The current domain hubs are:

| Domain | Main paths | Hub |
|---|---|---|
| Personal | `thoughts/`, `goals/`, `daily/` | `MEMORY.md` |
| Business | `business/*.md` | `business/_index.md` |
| Projects | `projects/*.md` | `projects/_index.md` |

## Memory metadata

Markdown notes can start with frontmatter, a YAML metadata block between `---`
lines. A durable knowledge card normally includes:

```yaml
---
type: project
description: "One useful sentence shown in search results"
tags: [client-work, launch]
status: active
created: 2026-08-04
updated: 2026-08-04
last_accessed: 2026-08-04
relevance: 1.0
tier: active
---
```

`status` describes the real-world lifecycle of the item, such as an active or
completed project. `tier` describes only its memory visibility. They are not
interchangeable.

The default memory configuration is in `vault/.memory-config.json`:

```json
{
  "tiers": {"active": 7, "warm": 21, "cold": 60},
  "decay_rate": 0.015,
  "relevance_floor": 0.1
}
```

Relevance follows a floor-adjusted exponential curve:

```text
relevance = floor + (1 - floor) * exp(-decay_rate * days_since_access)
```

The default tiers are:

| Tier | Effective memory age | Normal recall |
|---|---:|---|
| `core` | Not automatically demoted | Always eligible and strongly preferred |
| `active` | 0–7 days | Eligible |
| `warm` | 8–21 days | Eligible |
| `cold` | 22–60 days | Hidden unless the semantic match is exceptionally strong |
| `archive` | More than 60 days | Hidden unless the semantic match is exceptionally strong |

`deep-recall` includes every tier. It keeps the same memory, recency, and
supersession score adjustments; it only removes the normal tier-visibility
filter. Decay changes metadata and ranking but never deletes the note.

## Recall ranking

`a-second-brain qmd recall` asks QMD for semantic candidates, then applies
memory-aware ranking:

- core, active, and warm tiers receive progressively smaller boosts;
- cold and archive receive small penalties;
- relevance contributes a smaller positive adjustment;
- recent dated notes receive a recency boost, while old dated notes can be
  penalized;
- evergreen paths such as `MEMORY.md`, goals, MOCs, and the business and project
  indexes are not penalized for age;
- a superseded fact receives a strong penalty but remains traceable.

Normal recall allows core, active, warm, and unclassified notes. A cold or
archived note can still pass when its base semantic score is at least `0.9`.
The final list is sorted by effective score and exposes a confidence value
clamped to the `0`–`1` range.

These adjustments affect discovery order, not truth. Open the selected source
before relying on its exact wording.

## Meaningful access and promotion

Using a note reinforces it. `a-second-brain qmd get` records access
automatically. If an operator or agent reads a Markdown file directly, it
should run the memory engine's `touch` command afterward.

A touch promotes only one tier:

```text
archive -> cold -> warm -> active -> active refresh
```

For a one-tier promotion, the engine moves `last_accessed` to a representative
date inside the target tier instead of always setting it to today. This
prevents one accidental lookup from making an old note immediately dominant.
Core notes stay core. Bulk maintenance must not touch every file, because that
would destroy the access signal.

## Daily entries age independently

One daily Markdown file can contain a morning decision and an evening reminder.
They should not necessarily have the same memory age. The runtime therefore
tracks sections whose headings follow this form:

```markdown
## 09:15 [voice]
...

## 18:40 [text]
...
```

Per-entry metadata is stored in `vault/.memory-entries.json`. QMD uses the
hottest entry as the daily file's memory signal. Touching the daily file
promotes all tracked entries in that file. The JSON store is derived private
state, not a second source of note content.

## Correcting durable facts

Important facts can carry epistemic metadata: metadata about how the fact is
known.

| Field | Meaning |
|---|---|
| `epistemic_confidence` | `verified`, `inferred`, or `unverified` |
| `epistemic_scope` | The person, project, period, or situation to which the fact applies |
| `epistemic_state` | `active` or `superseded` |
| `epistemic_verification` | Required evidence description for a verified fact |

Do not silently overwrite an important old fact when the correction itself is
useful history. Create a successor note and use `supersede`. The command marks
the old note and writes reciprocal `superseded_by` and `supersedes` links. Search
then penalizes the old version while preserving the chain.

## Search and memory commands

Run these from the project or wheel-created instance directory:

```bash
# Search by meaning, preferring current memory
a-second-brain qmd recall "launch decisions"

# Include cold and archived material
a-second-brain qmd deep-recall "old launch experiments"

# Return structured results or change the result limit
a-second-brain qmd recall --json --limit 10 "client promises"

# Read a selected source and record access
a-second-brain qmd get projects/projects.md

# Refresh text inventory, build embeddings, and inspect status
a-second-brain qmd update
a-second-brain qmd embed
a-second-brain qmd status
```

Memory maintenance commands live in the public `agent-memory` skill:

```bash
uv run skills/agent-memory/scripts/memory-engine.py stats vault
uv run skills/agent-memory/scripts/memory-engine.py decay vault
uv run skills/agent-memory/scripts/memory-engine.py daily vault
uv run skills/agent-memory/scripts/memory-engine.py touch vault/thoughts/example.md
uv run skills/agent-memory/scripts/memory-engine.py creative 5 vault
uv run skills/agent-memory/scripts/memory-engine.py supersede thoughts/old.md thoughts/new.md --vault vault
```

`creative` samples warm, cold, and archived cards for idea generation. It is
not the routine retrieval path.

## Rules for creating and updating notes

- Search for a possible duplicate before creating a durable card.
- Open the source result before quoting it or changing a related note.
- Keep one durable fact in one primary place; connect other contexts with
  wiki-links.
- Write a useful `description`, sparse lowercase tags, and the real domain
  `status`.
- Add only links that express a meaningful relationship.
- Preserve source URLs and imported originals where the integration supports
  them.
- Put durable context in `MEMORY.md` only when it should remain useful for weeks
  or months. Put implementation progress and one-off events in daily notes or
  the rolling handoff.

## What runs automatically

| Mechanism | Automatic condition |
|---|---|
| Capture persistence | On each accepted Telegram message |
| Direct question retrieval | When the text classifier returns `high` confidence for the question route |
| Full daily processing | At 21:00 in the host's local time when the supplied timer is enabled, or manually with `/process_full` |
| Memory decay and creative recall | During the full reflection phase |
| Vault-health and compiled-briefing maintenance | During the full reflection phase and scheduled post-cycle maintenance |
| QMD refresh after searchable writes | Best effort; the separate QMD timer can also refresh the index |
| Weekly, monthly, and yearly reviews | During the scheduled cycle when the relevant boundary is due |
| PLAUD import | Only when configured and its timer or manual sync is used |
| Encrypted vault snapshot | Before scheduled processing only when backup settings are configured |

Automatic means the relevant service and timer are installed, enabled, and
healthy. A source checkout does not enable user services until
`./scripts/install-systemd-user.sh --enable` succeeds.

The calendar checks run on the date when the scheduled cycle actually starts:
the weekly digest on Friday, system reflection and goal rollover on Sunday,
monthly review on the last day of the month, and yearly review on December 31.
An overdue weekly goals file can trigger rollover on a later day. The other
periodic reviews do not have a general missed-run catch-up mechanism.

“Best effort” QMD refresh means a failed index update does not roll back the
source note. The error is logged, and a later write or the separate QMD timer
can retry the refresh.

## Limits and failure behavior

- Without QMD, capture and ordinary vault operation still work, but semantic
  retrieval and memory-aware recall are unavailable.
- Empty QMD model variables do not disable search; they select the local CPU
  models. Remote embedding and reranking require explicit settings.
- If semantic retrieval fails, the assistant should report that evidence gap
  instead of presenting an exact-text scan as an equivalent search.
- Compiled briefings and scores can become stale. The source note remains the
  authority.
- Memory tiers measure recent use, not importance or truth.
- Graph repair cannot infer every relationship reliably. Review proposed links
  when meaning is ambiguous.
- Self-hosting keeps the vault under your filesystem control, but configured
  providers may receive the data described in the [privacy boundary](../../README.md#privacy-boundary).

Back up the entire private vault, including hidden runtime state, by following
[Backup and restore](backup-and-restore.md). For search failures, see
[Troubleshooting](troubleshooting.md).
