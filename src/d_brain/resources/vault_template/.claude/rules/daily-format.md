---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
paths: "daily/**/*.md"
---

# Daily Notes Format

Navigation rule for capture files in `daily/`.

This file defines the note shape, not the full processing contract.
Runtime processing behavior lives in:
- `skills/dbrain-processor/phases/capture.md`
- `skills/dbrain-processor/references/classification.md`
- `vault/.claude/docs/prompt-source-map.md`

## File Naming

- Format: `YYYY-MM-DD.md`
- Example: `2024-12-20.md`
- One file per day

## Entry Format

Each entry follows this structure:

```markdown
## HH:MM [type]
Content of the entry
```

### Entry Types

| Type | Description |
|------|-------------|
| `[voice]` | Transcribed voice message |
| `[text]` | Direct text message |
| `[forward from: Name]` | Forwarded message with source |
| `[photo]` | Image with embed |

### Photo Entries

Photos include Obsidian embed:

```markdown
## 14:30 [photo]
![[attachments/2024-12-20/img-143025.jpg]]

Optional description or transcribed text from photo
```

## Processing Rules

- Read entries chronologically.
- Preserve source text, timestamps, and ordering.
- Keep one entry per `## HH:MM [type]` block.
- Let runtime decide classification, skipping, entity extraction, and downstream actions.

## Do NOT

- Modify original entries
- Delete entries before archiving
- Change timestamps
- Add content between entries

## Processing Markers

- Current runtime may add entry-level HTML comments such as `<!-- ✓ processed -->`.
- Current canonical entry-status marker is `<!-- d-brain:entry-status: already_processed -->`; future statuses may reuse the same `d-brain:entry-status:*` shape.
- These markers are runtime-owned. Do not manage them manually in this rule.
- The older file-level YAML footer (`processed: ...`, `thoughts: ...`, `tasks: ...`) is legacy and is not the canonical contract anymore.

## When Behavior Changes

If daily processing behavior changes, edit the canonical runtime files first instead of expanding this rule into another full spec.
