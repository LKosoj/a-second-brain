---
type: note
description: Analyze the vault graph, inspect orphan and broken-link reports, and add only justified links or frontmatter relations aligned with the current flat business/projects layout.
last_accessed: 2026-06-13
relevance: 0.56
tier: cold
name: graph-builder
allowed-tools: Bash(uv run:*), Bash(rg:*), Read, Edit
depends_on: []
---

# Graph Builder

Routing doc for vault graph analysis and targeted link-building.

## What This Skill Owns

- deterministic graph analysis via `analyze.py`
- targeted semantic links after reading the report
- conservative frontmatter `related` updates

## Current Workflow

1. Run:
   ```bash
   uv run skills/graph-builder/scripts/analyze.py vault
   ```
2. Read:
   - `vault/.graph/vault-graph.json`
   - `vault/.graph/report.md`
3. Prioritize non-daily orphan or weakly connected files.
4. Read `MOC/index.md` and the nearest domain hub before proposing links.
5. Add only links you can justify from actual content.

## Current Domains

### Personal
- `thoughts/`
- `goals/`
- `daily/`
- Hub: `MEMORY.md`

### Business
- `business/_index.md`
- `business/crm.md`
- `business/network.md`
- `business/events.md`
- Hub: `business/_index.md`

### Projects
- `projects/_index.md`
- `projects/clients.md`
- `projects/leads.md`
- `projects/projects.md`
- Hub: `projects/_index.md`

## Link Quality Rules

- link from meaning, not from name similarity alone
- prefer existing hub/index notes when entity structure is still shallow
- use frontmatter `related` only when it improves retrieval
- keep 2-5 strong links instead of 10 weak ones
- daily files are low-priority for manual graph cleanup
- prefer one targeted cleanup pass over bulk relinking

## Do Not

- do not change file bodies unless the task explicitly requires it
- do not add links to non-existent files
- do not invent old nested paths such as `business/crm/*` or `projects/leads/*`
- do not create new files without a strong retrieval reason

## Relevant Skills

- [[skills/vault-health/SKILL|vault-health]] — health scoring, MOC generation, link repair
- [[skills/dbrain-processor/SKILL|dbrain-processor]] — daily processing pipeline
