---
type: note
title: Search Protocols
last_accessed: 2026-02-26
relevance: 0.19
tier: archive
---
# Search Protocols

## Protocol Selection

Before searching, classify the task:

| Signal | Mode | Search Radius |
|--------|------|---------------|
| Quick status check, monitoring | heartbeat | core + active |
| Normal question, lookup | normal | active + warm |
| "Find everything about X" | deep | all tiers |
| Strategy, OSINT, complex analysis | deep | all tiers |
| Brainstorm, "what if", ideation | creative | random from cold+archive |

## Search Order (All Modes)

1. **Index files first** — MOC or `_index.md` files are pre-built navigation. Check them before searching.
2. **Follow links** — If an index points to `[[business/crm]]` or another concrete note, read it directly before searching by keywords.
3. **Semantic search** — Only when indexes don't cover the query.
4. **Grep** — Last resort for exact strings, IDs, phone numbers.

## Tier Filtering

### Pre-filter with `rg`
```bash
# Find all active business/project/thought cards
rg -l "^tier: active" vault/business vault/projects vault/thoughts

# Find all cold+ cards for deep search
rg -l "^tier: (cold|archive)" vault/business vault/projects vault/thoughts

# Find high-relevance cards
rg -l "^relevance: 0\\.9" vault/business vault/projects vault/thoughts
```

### Combine with semantic search
1. Grep for tier → get file list
2. Read relevant files → answer question

This is cheaper than searching all 400+ cards semantically.

## Heartbeat Protocol

Heartbeats are frequent, automated checks. Minimize cost:

1. Read state file (always loaded)
2. Check only `core` + `active` tier cards matching current projects
3. Skip warm/cold/archive entirely
4. Total reads: 0-5 files per heartbeat

## Normal Protocol

Default for user questions:

1. Check index/MOC for direct link
2. If not found: semantic search with default radius (active + warm)
3. Read top 3-5 results
4. Touch any card you read: `uv run skills/agent-memory/scripts/memory-engine.py touch vault/<file>`

## Deep Protocol

For complex, multi-step analysis:

1. Check indexes
2. Semantic search across ALL tiers (no filtering)
3. Read all relevant results (up to 10-15 files)
4. Cross-reference cards for connections
5. Touch all read cards

## Creative Protocol

Deliberately non-directed. Goal: surface unexpected connections.

```bash
uv run skills/agent-memory/scripts/memory-engine.py creative 5 vault
```

1. Get 5 random cards from cold/archive
2. Read each one
3. For each card, ask: "How could this connect to my current task?"
4. If a connection exists → touch the card (promotes it one tier toward active)
5. If no connection → leave it (continues to decay)

### When to use creative mode
- Stuck on a problem
- Looking for new business angles
- User says "brainstorm" or "what if"
- Exploring forgotten knowledge
- Weekly reflection sessions

### What makes it work
Human creativity often comes from random associations — shower thoughts, dreams, serendipitous encounters. Creative mode simulates this by pulling forgotten cards back into working memory. The randomness is the feature, not a bug.

## Touch Discipline

Touching a card resets its decay. Over-touching defeats the purpose.

**Touch when:**
- You read the card's content to answer a question
- You update the card with new information
- User explicitly discusses the entity

**Don't touch when:**
- Card appears in search results but you didn't open it
- Running automated scans or reports
- Bulk migration or formatting changes
- Card mentioned in passing but not substantively used

## Cost Awareness

| Mode | Typical reads | Token cost |
|------|---------------|------------|
| Heartbeat | 0-5 files | ~2K tokens |
| Normal | 3-8 files | ~5K tokens |
| Deep | 10-20 files | ~15K tokens |
| Creative | 5 files | ~3K tokens |

Optimize by reading index files first — they're cheap navigation that prevents expensive full-text searches.
