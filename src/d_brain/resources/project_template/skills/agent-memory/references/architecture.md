---
type: note
title: Memory Architecture
last_accessed: 2026-02-26
relevance: 0.19
tier: archive
---
# Memory Architecture

## The Problem

AI agents forget everything between sessions. Common solutions (dump everything into context, vector-search everything) create bloat and noise. Human memory works differently: it has layers, it decays, and it surprises you with random connections.

## Design Principles

### Single Source of Truth (DRY)
Each fact lives in ONE place. If a client, project, or durable note already has a canonical home, don't duplicate it in a separate "state" note. Instead, reference the existing source of truth: `see business/crm.md` or the relevant project/note card.

**Violation test:** Search for any person/project name. If it appears in 3+ files with substantive detail, you have a DRY violation.

### Context is a Shared Resource (KISS)
Every byte loaded per turn costs tokens. An agent loading 40KB of context files is burning ~10,000 tokens before the conversation starts. Measure your context budget:
- State file (volatile): target <4KB
- Config files: target <15KB total
- Per-turn overhead = sum of all always-loaded files

### Build Only What You Use (YAGNI)
Don't create "knowledge graphs," "daily digests," or "weekly reviews" unless you actively query them. If a cron job generates a report nobody reads, it's waste.

## Three-Layer Architecture

```
Layer 1: HOT CONTEXT (always loaded, <4KB)
  └── State file — volatile: active focus, blockers, reminders
      NOT: contacts, history, reference, tools

Layer 2: SEARCHABLE VAULT (on-demand, unlimited)
  └── Cards with YAML frontmatter — one file per entity
      Searched via: semantic search, grep, graph traversal
      Organized by: domain directories + index files (MOC)

Layer 3: ARCHIVE (deep search only)
  └── Old daily logs, completed projects, cold contacts
      Still searchable but excluded from default queries
```

### Layer 1: Hot Context

This is what's loaded every turn. Must be ruthlessly slim.

**Include:** Current week's focus, active blockers, 3-5 pending items, security rules, navigation hints.

**Exclude:** Contact lists (use vault), tool docs (separate file), event history (daily files), anything searchable.

**Audit:** If your state file exceeds 80 lines, something belongs in Layer 2.

### Layer 2: Searchable Vault

One markdown file per entity (person, project, company, idea). YAML frontmatter enables filtering and automation.

**Directory structure:**
```
vault/
├── business/      # flat business hubs and relationship context
├── projects/      # flat project hubs and project context
├── thoughts/      # durable note cards by category
├── daily/         # episodic daily logs
├── MOC/           # Maps of Content (index files)
└── .graph/        # computed graph data
```

**Why one file per entity:** Enables granular decay, individual relevance scoring, and precise search results. A single "contacts.md" with 200 entries can't decay — the whole file is either loaded or not.

### Layer 2b: Daily Files (Episodic Memory)

Daily logs (`daily/YYYY-MM-DD.md`) sit between vault and archive. They capture the raw narrative of each day — conversations, decisions, reasoning, context that doesn't fit neatly into entity cards.

**Never delete daily files.** The decay system handles visibility:
- Active (0-7 days): loaded at session start
- Warm (8-21 days): searchable, not auto-loaded
- Cold (22-60 days): deep search only
- Archive (60+): creative mode, explicit queries

**Why keep everything:**
- Disk: 365 daily files ≈ 4MB. Irrelevant cost.
- Context: knowledge graphs capture entities but lose reasoning and tone.
- Search: semantic search finds old dailies just as well as new ones.
- Source rebuild: re-reading 100+ Telegram messages is far more expensive than keeping a 10KB summary.

**Compression:** If a daily file exceeds 20KB, extract key facts into vault cards and trim the daily to a 20-line summary. Keep the YAML frontmatter intact.

### Layer 3: Archive

One-off analysis reports. Completed project retrospectives. Generated artifacts that served a temporary purpose. These are still searchable but excluded from default queries. Daily files are NOT archive — they stay in `daily/` with tier-based visibility.

## Forgetting Curve

Inspired by Ebbinghaus (1885): memory strength decays over time without reinforcement.

### Relevance Score

Each card has `relevance: 0.0-1.0` in its frontmatter. Decays linearly:

```
relevance = max(floor, 1.0 - days_since_access × rate)

Default: rate=0.015, floor=0.1
→ After 7 days:  0.90
→ After 21 days: 0.69
→ After 33 days: 0.50
→ After 60 days: 0.10 (floor)
```

### Tier Assignment

Based on days since `last_accessed`:

| Tier | Days | Description |
|------|------|-------------|
| core | manual | Never auto-assigned. Identity, security, pricing. |
| active | 0-7 | Hot context. Searched in all modes. |
| warm | 8-21 | Default search radius. Gradually fading. |
| cold | 22-60 | Deep search only. Mostly forgotten. |
| archive | 60+ | Creative mode or explicit recall only. |

### Touch Protocol

When an agent reads or references a card, it should promote the card one tier at a time toward active and refresh its metadata:
```bash
uv run skills/agent-memory/scripts/memory-engine.py touch vault/<filepath>
```
This updates `last_accessed` to today, refreshes `relevance`, and promotes recall gradually (`archive → cold → warm → active`) instead of blindly resetting everything to `active`.

**When to touch:**
- Agent reads card content to answer a question
- Agent updates card with new information
- User explicitly mentions the entity

**When NOT to touch:**
- Card appears in search results but isn't opened
- Automated scan (decay script itself)
- Bulk operations

## Search Protocols

Different tasks need different search depths:

### Heartbeat Mode (fast checks)
Search: core + active only.
Use for: quick status checks, routine monitoring, simple questions.
Cost: minimal — only hot cards in scope.

### Normal Mode (default)
Search: core + active + warm.
Use for: most questions, task execution, lookups.
Cost: moderate — includes fading but recent cards.

### Deep Mode (complex tasks)
Search: all tiers.
Use for: strategy, OSINT, complex analysis, "find everything about X."
Cost: high — full vault scan.

### Creative Mode (divergent thinking)
Method: random sample from cold + archive tiers.
Use for: brainstorming, finding unexpected connections, "what if" scenarios.
Not semantic search — deliberately random to surface forgotten associations.

```bash
uv run skills/agent-memory/scripts/memory-engine.py creative 5 vault
```

## Multi-Agent Shared Memory

When multiple agents share a vault:

1. **Shared YAML schema** — all agents use same frontmatter fields
2. **Each agent touches on read** — keeps decay accurate across agents
3. **One vault, one truth** — don't fork the vault per agent
4. **Conflict resolution** — last write wins for metadata; append-only for history sections

## Context Budget Calculator

Measure your actual per-turn cost:

```
Always-loaded files:
  STATE.md         _____ bytes
  CONFIG.md        _____ bytes
  IDENTITY.md      _____ bytes
  RULES.md         _____ bytes
  ─────────────────────────
  Total context:   _____ bytes ÷ 4 ≈ _____ tokens

Target: <25KB (6,000 tokens) for always-loaded context
```

Every 1KB saved = ~250 tokens freed per turn for actual conversation.
