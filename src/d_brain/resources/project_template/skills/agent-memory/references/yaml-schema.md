---
type: note
title: YAML Frontmatter Schema
last_accessed: 2026-02-26
relevance: 0.19
tier: archive
---
# YAML Frontmatter Schema

## Canonical Schema (all card types)

Every card MUST have this structure. Fields marked (auto) are managed by memory-engine.py.
Fields marked (required) must be written by the agent when creating a card.

```yaml
---
# ── identity ──
type: crm                    # (required) crm | lead | contact | project | personal | daily | note
description: >-              # (required) One-line summary. What is this card about?
  Key client in [industry], active deal $XXK, [project description]

# ── classification ──
tags: [fmcg, active-deal, uz] # (required) 2-5 freeform tags for grep filtering
status: active                # (required) universal: active|draft|pending|done|inactive; CRM-only: prospect|negotiation|won|lost
industry: FMCG                # (optional) For CRM/leads
region: UZ                    # (optional) ISO country codes
source: referral              # (optional) How this entity entered the system
priority: High                # (optional) High | Medium | Low

# ── ownership ──
owner: [Your Name]                  # (optional) Who owns this relationship
responsible: [Your Name]             # (optional) Who is doing the work

# ── dates ──
created: 2026-01-15           # (recommended) When card was first created
updated: 2026-02-20           # (recommended) When content was last meaningfully changed

# ── deal tracking ──
deal_status: negotiation       # (optional) For active deals
deal_deadline: 2026-03-15     # (optional) Deal close date

# ── memory system (auto) ──
last_accessed: 2026-02-25     # (auto) When card was last read/touched
relevance: 0.85               # (auto) 0.0-1.0, decays over time
tier: active                  # (auto) core | active | warm | cold | archive
---
```

## Required Fields Explained

### `description` (string, one line)
The single most important field for search quality. Write a concise summary that answers:
"If someone searches for this entity, what should they see in results?"

Good: `"Event Specialist, [Company] [Country], tender 2026"`
Bad: `"контакт"` (too vague)
Bad: (empty — defeats the purpose of the entire system)

### `tags` (list, 2-5 items)
Cross-cutting labels for fast grep filtering. Use lowercase, hyphens.
```yaml
tags: [hot-lead, ai-training, enterprise, follow-up]
```
Search: `rg -l "hot-lead" vault/business vault/projects vault/thoughts`

### `type` (enum)
| Type | When |
|------|------|
| crm | Existing client/company |
| lead | Potential client |
| contact | Person (not a lead/client) |
| project | Active or past project |
| personal | Family, friends |
| daily | Daily log file |
| note | Everything else |

### `status` (enum, normalized)
Domain-specific lifecycle. NOT the same as `tier` (which is memory-system lifecycle).

**Universal (all card types):**

| Status | Meaning |
|--------|---------|
| `active` | Currently relevant, in use, engaged |
| `draft` | Work in progress, not finalized |
| `pending` | Waiting for external input or decision |
| `done` | Completed, kept for reference |
| `inactive` | Was active, went quiet or outdated |

**CRM-specific (only for type: crm, lead, crm-lead, client):**

| Status | Meaning |
|--------|---------|
| `prospect` | Identified lead, no deep engagement yet |
| `negotiation` | Proposal/KP sent, in talks |
| `won` | Deal closed positively |
| `lost` | Rejected, didn't pursue, conflict |

**ONLY these 9 values.** No Russian, no mixed case, no free-text.

Typical lifecycle by type:
- **crm/lead:** prospect → active → negotiation → won/lost
- **project:** draft → active → done
- **contact:** active → inactive
- **note/knowledge:** draft → active → inactive (outdated)
- **personal:** active → inactive
- **daily:** no status needed (has `date` field)

When in doubt: CRM → `prospect`, everything else → `active`.

## Memory System Fields (auto-managed)

### `relevance` (float, 0.0-1.0)
Computed by decay engine. Do not manually edit unless marking as `core`.
- 1.0 = just accessed
- 0.5 = ~33 days old
- 0.1 = floor (60+ days)

### `tier` (enum)
Computed by decay engine based on `last_accessed`.
- `core` — only manually assigned, never auto-demoted. Use for: identity, security rules, pricing.
- `active` — 0-7 days since access. Searched in all modes.
- `warm` — 8-21 days. Searched in normal+ modes.
- `cold` — 22-60 days. Deep search only.
- `archive` — 60+ days. Creative mode or explicit queries.

### `last_accessed` (ISO date)
Updated by `touch` command (graduated: +1 tier per touch).

## Agent Protocol for New Cards

When creating ANY new card:
1. ALWAYS include: `type`, `description`, `tags`, `status`
2. Write `description` as if it's a search result snippet — concise, informative
3. Add 2-5 `tags` that cross-cut directory structure
4. Run `memory-engine.py touch vault/<file>` after creation
5. The engine will auto-add `relevance`, `last_accessed`, `tier` on next decay run

## Field Semantics

### `relevance` (float, 0.0-1.0)
Computed by decay engine. Do not manually edit unless marking as `core`.
- 1.0 = just accessed
- 0.5 = ~33 days old
- 0.1 = floor (60+ days)

### `tier` (enum)
Computed by decay engine based on `last_accessed`.
- `core` — only manually assigned, never auto-demoted. Use for: identity, security rules, pricing, critical reference.
- `active` — 0-7 days since access. Searched in all modes.
- `warm` — 8-21 days. Searched in normal+ modes.
- `cold` — 22-60 days. Deep search only.
- `archive` — 60+ days. Creative mode or explicit queries.

### `last_accessed` (ISO date)
Updated by `touch` command when agent reads the card. Also updated by decay engine on first run (inferred from `updated`, `created`, git log, or file mtime).

### `type` (string)
Auto-inferred from directory path if not present. Configurable via `type_inference` in `.memory-config.json`.

### `tags` (list)
Freeform. Useful for cross-cutting concerns that don't fit directory structure.
```yaml
tags: [hot-lead, ai-training, enterprise, follow-up]
```
Search: `rg -l "hot-lead" vault/business vault/projects vault/thoughts`

## Configuration

Type inference mapping in `.memory-config.json` is project-specific and should follow the live flat vault layout instead of old nested CRM trees. A minimal example:
```json
{
  "type_inference": {
    "projects/": "project"
  }
}
```

When `memory-engine.py init` encounters a file without `type`, it checks the file path against these patterns.

## Frontmatter Tips

1. **Don't duplicate content.** If the H1 heading says "# Acme Corp", you don't need `title: Acme Corp` — the engine infers it.
2. **Tags > nested directories.** Flat domain notes plus tags are more flexible than reviving deep directory trees like `crm/hot/enterprise/ai/`.
3. **Status is domain-specific.** The memory system uses `tier` for lifecycle; `status` is for your business logic (active/won/lost/churned).
4. **`updated` vs `last_accessed`**: `updated` = when content changed; `last_accessed` = when anyone read it. Both matter for decay; the engine uses whichever is most recent.
