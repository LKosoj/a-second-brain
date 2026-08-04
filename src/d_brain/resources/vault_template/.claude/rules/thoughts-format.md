---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
paths: "thoughts/**/*.md"
---

# Thoughts Format

Navigation rule for notes in `thoughts/` and its subfolders.

This file should stay lightweight.
Runtime save behavior lives in:
- `skills/dbrain-processor/phases/execute.md`
- `skills/agent-memory/SKILL.md`
- `vault/.claude/docs/prompt-source-map.md`

## Folder Structure

```
thoughts/
├── ideas/        # Runtime-created idea notes
├── reflections/  # Runtime-created reflection notes
├── projects/     # Runtime-created project notes
├── learnings/    # Runtime-created learning notes
├── tasks/        # Secondary/manual bucket when needed
└── ...           # Other manual buckets may exist
```

The runtime currently writes durable non-task notes into folders derived from capture classification.

## File Naming

- Format: `YYYY-MM-DD-slug.md`
- Slug: lowercase, hyphens, descriptive
- Example: `2024-12-20-voice-agent-architecture.md`

## Frontmatter (Required)

```yaml
---
type: note | personal | project
description: >-
  One-line retrieval snippet
tags: [relevant, tags]
status: active | draft | pending | done | inactive
created: 2024-12-20
updated: 2024-12-20
related: []
source: daily/2024-12-20.md
# auto fields:
last_accessed: 2024-12-20
relevance: 1.0
tier: active
---
```

Use the agent-memory card template as the canonical frontmatter contract.
The folder already carries capture category, so the older `type: idea|reflection|project|learning` enum is not primary runtime truth anymore.

## Content Structure

```markdown
# Title

## Summary
One paragraph summary of the key insight.

## Details
Full content of the thought.

## Related
- [[Link to related note]]
- [[goals/current-yearly#Section]]
```

The body may stay shorter when the note is simple; structure is a guide, not a second runtime template.

## Writing Rules

- Use titles as claims, decisions, or concrete insights, not topic labels.
- `description` is required and should read like a search result snippet.
- Prefer 2-5 lowercase tags.
- Add source and related links when they improve retrieval.
- Keep one durable insight per note.
- If the same durable claim already exists, update or link that note instead of creating a sibling duplicate.

## Wiki-Links

When saving a thought:

1. **Search for related notes** in thoughts/
2. **Check MOC indexes** for topic clusters
3. **Link to relevant goals** in goals/
4. **Add backlinks** to source daily note

Example:
```markdown
Extracted from [[daily/2024-12-20]].
Related to [[Voice Agents]] and [[goals/current-yearly#Relevant Section]].
```

## When Behavior Changes

If runtime save behavior changes, update `phases/execute.md` and the relevant agent-memory docs first.
