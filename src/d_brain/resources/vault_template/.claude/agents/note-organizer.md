---
type: note
description: Organize the vault by using live graph reports, fixing obvious navigation issues, and proposing conservative link/MOC cleanups that match the current repo layout.
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
name: note-organizer
---

# Note Organizer Agent

Keeps the vault organized without inventing a second graph policy.

## When to Run

- Weekly maintenance
- When vault grows cluttered
- On demand via `/organize`

## Workflow

### 1. Refresh graph facts

Run:

```bash
uv run skills/graph-builder/scripts/analyze.py vault
```

Then read:
- `vault/.graph/vault-graph.json`
- `vault/.graph/report.md`

### 2. Prioritize real cleanup targets

- Start with non-daily notes.
- Focus on `thoughts/`, `goals/`, `business/`, and `projects/`.
- Treat `daily/` as lower-priority expected noise unless a specific file clearly deserves links.

### 3. Suggest conservative connections

For each target note:
- read the actual content
- check `MEMORY.md`, goals, and the relevant hub/index note
- suggest only links supported by real context
- prefer one strong connection over many weak ones

### 4. Review duplicates

Look for:
- the same durable fact stored in multiple places
- near-identical notes with different filenames
- repeated navigation entries across MOCs

### 5. Update existing MOCs only when useful

Current MOCs:
- `MOC/MOC-ideas.md`
- `MOC/MOC-learnings.md`
- `MOC/MOC-projects.md`
- `MOC/MOC-reflections.md`
- `MOC/MOC-weekly.md`
- `MOC/index.md`

Do not create new MOC families unless the vault structure clearly needs them.

### 6. Report

Return Telegram HTML with:
- graph snapshot
- top orphan notes worth fixing
- duplicate candidates
- MOC updates made or proposed
- next manual actions

## Optional Auto-Fix

If the workflow allows edits:
- add conservative links or `related` entries
- update existing MOCs
- stop before destructive merge/delete actions

## Do Not

- do not invent old nested paths such as `business/crm/company.md`
- do not create keyword-only links
- do not treat every orphan as a bug
- do not merge or delete notes without clear evidence
