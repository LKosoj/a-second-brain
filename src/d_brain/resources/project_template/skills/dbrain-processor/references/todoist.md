---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
---
# Todoist Integration

## Task Links Format

Use the current Todoist task URL format:

`https://app.todoist.com/app/task/{task_id}`

If the MCP response already contains `url`, prefer it directly.

## Todoist via mcp-cli

Always use `mcp-cli`. Do not rely on CLI-specific MCP wrappers.

### Reading

```bash
mcp-cli call todoist get-overview '{}'
mcp-cli call todoist find-tasks '{"searchText": "query"}'
mcp-cli call todoist find-tasks-by-date '{"startDate": "today", "daysCount": 7}'
```

### Writing

```bash
mcp-cli call todoist add-tasks '{"tasks": [{"content": "Task", "dueString": "tomorrow", "priority": "p2"}]}'
mcp-cli call todoist update-tasks '{"tasks": [{"id": "task_id", "content": "Updated"}]}'
mcp-cli call todoist complete-tasks '{"ids": ["task_id"]}'
```

## Pre-Creation Checklist

1. Check workload for the next 7 days.
2. Check duplicates for the same underlying commitment.
3. Route to a Todoist project using the live catalog when the fit is clear.
4. Use Inbox only as fallback.

## Priority Rules

Assign priority from consequence, commitment, and timing, not from standalone words.

Consider:
- external deadline or delivery risk,
- impact on current goals,
- dependency chains blocking other work,
- explicit personal responsibility,
- and reversibility if delayed.

Suggested interpretation:
- `p1` — hard external commitment or clear near-term risk
- `p2` — important committed work that should move soon
- `p3` — useful operational follow-up
- `p4` — low-pressure or exploratory work

## Due-Date Rules

Translate explicit timing intent into `dueString`.

- Preserve concrete dates when present.
- Preserve short relative intent when the source is clear enough.
- If workload is overloaded, shift flexible tasks, but never move a real deadline.
- If no time intent exists, use a conservative fallback instead of leaving the task undated.

## Task Title Style

Task titles should be direct, specific, and executable:
- start with a verb,
- name the object,
- include enough context to act without rereading the source.
- For delegated work, phrase the task as the owner's control action: check status, follow up, confirm delivery, unblock, or escalate.

Avoid vague placeholders.

## Project Routing

Project selection should come from the live Todoist project catalog plus source context.

Do not infer project placement from static word maps.
If several projects are plausible and confidence is low, use Inbox as fallback.

## Labels

Add labels only when they improve later retrieval or workflow.
Do not generate decorative labels.

## Error Handling

If task creation fails:
1. keep the exact error,
2. do not mark the source as processed,
3. continue with the rest of the pipeline.

Never replace a real error with “add manually”.

## Recurring Tasks

Create recurring tasks only for explicit process commitments or recurring plans.
Do not invent recurring cadence when the source does not support it.
