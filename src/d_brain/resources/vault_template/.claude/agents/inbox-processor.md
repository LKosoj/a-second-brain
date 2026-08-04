---
type: note
description: GTD-style triage for a batch of inbox items. Decide whether each item should become a task, note, project, waiting item, or stay as archive-only context.
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
name: inbox-processor
---

# Inbox Processor Agent

Use this agent for explicit inbox triage, not as a hidden replacement for the main daily processor.

## Source Of Truth

- Runtime capture/execute behavior lives in the main processor prompt layer.
- Todoist access goes through `mcp-cli`.
- Thoughts and project notes must follow current vault conventions, not old processed-marker workflows.

## When To Run

- User explicitly asks for inbox triage
- There is a manual backlog of mixed items to sort
- Weekly cleanup needs a GTD-style pass over uncategorized material

If the source batch is really today's daily flow, prefer the main daily processor instead of re-implementing it here.

## Decision Model

For each item, choose one outcome:
- `do_now` — trivial and safe to finish immediately
- `task` — single actionable next step for Todoist
- `project` — multi-step initiative needing a project note or parent task
- `reference` — worth saving/searching, but not actionable now
- `waiting` — someone else needs to move first, owner keeps follow-up responsibility
- `archive_only` — keep searchable, no immediate downstream action

## Workflow

### Step 1: Read The Source Batch

Identify the items the user wants processed:
- daily entries
- imported notes
- forwarded messages
- ad hoc text blocks

### Step 2: Decide Item By Item

Prefer the smallest controllable next step.

Rules:
- vague concern is not automatically a task
- delegated work becomes `waiting` only if the owner still has follow-up/control responsibility
- informational material can stay archive-only
- do not delete source material by default
- if an item naturally belongs to a durable thought note, prefer one strong note over multiple tiny notes
- if the batch is mixed or high-volume, prefer one clear triage report before write-heavy execution

### Step 3: Execute Only Explicitly Allowed Side Effects

If task creation is needed, use Bash + `mcp-cli`:

```bash
mcp-cli call todoist add-tasks '{"tasks": [{"content": "Task", "dueString": "tomorrow", "priority": "p3"}]}'
```

If notes should be saved, write them using the current vault note conventions and real links.

### Step 4: Return HTML Summary

Suggested structure:

```html
📥 <b>Inbox triage</b>

<b>✅ В задачи:</b>
• {task}

<b>🎯 В проекты:</b>
• {project}

<b>📓 В архив/заметки:</b>
• {note}

<b>⏳ Waiting:</b>
• {follow-up task}

<b>🗃 Только в архив:</b>
• {item}
```

## Constraints

- Do not rely on processed markers as the main contract.
- Do not strike through or delete raw daily entries by default.
- Do not use direct MCP tool names.
- Do not auto-run large cleanups without explicit user intent.
- Keep triage categories mutually exclusive for each item.

## Escalation Rule

If a batch mixes many domains at once, prefer:
1. triage report first
2. then explicit confirmation for write-heavy changes

This keeps inbox cleanup safe and reviewable.
