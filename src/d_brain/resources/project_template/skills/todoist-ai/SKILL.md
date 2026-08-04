---
type: note
description: Thin project-facing Todoist routing doc. Use Bash plus mcp-cli for task creation, updates, workload checks, and process-goal maintenance.
last_accessed: 2026-06-11
relevance: 0.54
tier: cold
name: todoist-ai
depends_on: []
---

# todoist-ai

Project-facing routing doc for Todoist access.

## Core Contract

- Always use `mcp-cli`.
- Do not rely on agent-specific MCP tool names.
- Use the absolute `MCP_CONFIG_PATH` already supplied by the runtime; it must point to the repo-local `mcp-config.json`, and must not be overwritten from `$PWD`.
- Verify the connection before declaring Todoist broken.
- Retry failed calls up to 3 times before surfacing the error.

```bash
mcp-cli call todoist user-info '{}'
mcp-cli call todoist find-tasks-by-date '{"startDate": "today"}'
mcp-cli call todoist add-tasks '{"tasks": [{"content": "Task", "dueString": "tomorrow", "priority": "p2"}]}'
mcp-cli call todoist update-tasks '{"tasks": [{"id": "123", "content": "Updated"}]}'
mcp-cli call todoist complete-tasks '{"ids": ["task_id"]}'
mcp-cli call todoist get-overview '{}'
```

## When To Use

- Create, update, or complete tasks in Todoist
- Check workload or task status
- Support scheduled processing and `/do` task actions
- Inspect process-goal tasks and weekly workload

## Task Quality Rules

- Create the owner's controllable next step.
- Check duplicates and near-term workload before creating new tasks.
- Inbox is fallback only.
- Prefer named projects from the live Todoist catalog when the fit is clear.
- Use recurring tasks for stable process commitments.
- If all retries fail, preserve the exact error text instead of inventing success.

## Relevant Skills

- [[skills/dbrain-processor/SKILL|dbrain-processor]] — Daily entry processing
