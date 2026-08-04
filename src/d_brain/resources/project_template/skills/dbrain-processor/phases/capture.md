---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
---
# Phase 1: CAPTURE

Read one daily file, classify each entry conservatively, and output structured JSON.

## Input
- injected core context — target daily, goals and owner indexes

## Task

1. Use the injected target daily in chronological order.
2. Treat each `## HH:MM [type]` block as one entry.
3. Classify every entry into exactly one class:
   - `task`
   - `idea`
   - `reflection`
   - `learning`
   - `project`
   - `crm_update`
   - `skip`
4. Use the supplied references to ground judgement:
   - owner context from `about.md`
   - goal alignment from the injected weekly/monthly/yearly goals
   - business/project context from the injected owner indexes
5. Detect only real entities that are strongly supported by the vault context.
6. Prefer conservative output:
   - if there is no clear next action, do not force a `task`
   - if a company is mentioned casually, do not force `crm_update`
   - if an entity match is weak, leave `entities` empty

## Output Format

Print ONLY valid JSON (no markdown, no explanation).

```json
{
  "date": "2026-02-19",
  "one_big_thing": "Current weekly focus",
  "entries": [
    {
      "time": "10:30",
      "type": "voice",
      "content": "Source entry text",
      "classification": "task",
      "task_content": "Concrete next action",
      "task_priority": 2,
      "task_due": "tomorrow",
      "entities": ["Example Studio"],
      "goal_alignment": "weekly"
    }
  ],
  "stats": {
    "total_entries": 5,
    "tasks": 2,
    "thoughts": 2,
    "crm_updates": 1,
    "skipped": 0
  }
}
```

## Classification Rules

Classify semantically, not by keyword triggers.

### `task`
- there is a concrete obligation, follow-up, or committed next action

### `idea`
- the value is in preserving a concept, not immediate execution

### `reflection`
- the value is in personal meaning or judgement

### `learning`
- the value is in reusable knowledge, comparison, or insight

### `project`
- the entry describes a durable initiative or workstream larger than one action

### `crm_update`
- the entry materially changes business/client/relationship state

### `skip`
- the entry is already processed, duplicate, empty, or not valuable for downstream work

## Thought Titles

For `idea`, `reflection`, `learning`, and `project`, generate titles as claims, decisions, or concrete insights, not vague topic labels.

## Entity Rules

- `entities` should contain canonical human-readable names, not invented file paths.
- Use an empty list when no strong entity match exists.
- Prefer existing vault entities over new guesses.

## Goal Alignment

Allowed values:
- `weekly`
- `monthly`
- `yearly`
- `operational`
- `none`

Use `operational` when the entry matters but does not clearly support a current goal.

## Important

- Mark entries with `<!-- ✓ processed -->` as `skip`
- Mark entries with `<!-- d-brain:entry-status: already_processed -->` as `skip`
- Preserve any runtime-owned `d-brain:entry-status:*` comment semantics even when the visible entry text still contains action language
- Preserve original timestamps and source wording in `content`
- Output ONLY JSON
