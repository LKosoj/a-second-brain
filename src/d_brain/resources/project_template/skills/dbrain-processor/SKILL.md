---
type: note
description: Routing doc for the daily-processing prompt corpus. The runtime source of truth lives in `src/d_brain/control_plane/*.py`, `src/d_brain/services/*.py`, `phases/`, and selected `references/`, while this file only explains how the pieces fit together.
last_accessed: 2026-07-09
relevance: 0.78
tier: warm
name: dbrain-processor
allowed-tools: Bash(mcp-cli:*)
depends_on: [graph-builder, todoist-ai, agent-memory, vault-health]
---

# d-brain Processor

Runtime pipeline for daily capture, Todoist execution, and Telegram reports.

## What This File Is

This file is a **routing and source-map document** for the prompt corpus.

Use it to understand:
- where the real runtime contract lives
- which prompt files are canonical
- which files are secondary or legacy
- where to edit behavior safely

## What This File Is Not

This file is **not** the full behavior spec for daily processing.

Do not change runtime behavior in this file alone.

If you need to change product behavior, edit the canonical runtime files first:
- `src/d_brain/control_plane/*.py`
- prompt builders in `src/d_brain/services/*.py`
- phase files in `skills/dbrain-processor/phases/`
- active reference files in `skills/dbrain-processor/references/`

Then update this file so navigation stays accurate.

## Execution Modes

- `/process` in Telegram = quick preview only, no side effects
- scheduled/full processing = `capture -> execute -> reflect` with writes

## Runtime Canon

### Control-Plane Runtime Layer

- `src/d_brain/control_plane/contracts.py`
- `src/d_brain/control_plane/registry.py`
- `src/d_brain/control_plane/router.py`

### Routing-Relevant Prompt Builder Examples (Non-Exhaustive)

The complete catalog of runtime prompt builders is maintained only in
`.claude/docs/prompt-source-map.md`. The examples below exist for routing from
this skill and must not be treated as a complete inventory.

- `src/d_brain/services/processor.py`
  - `_build_capture_prompt()`
  - `_build_preview_prompt()`
  - `_build_execute_prompt()`
  - `_build_text_intent_prompt()`
  - `_build_question_answer_prompt()`
  - `_build_reflect_prompt()`
  - `execute_prompt()`
  - `generate_weekly()`
- `src/d_brain/services/plaud.py`
  - `_build_classification_prompt()`
- `src/d_brain/services/todoist_projects.py`
  - `_build_prompt()`

### Canonical Phase Files

- `skills/dbrain-processor/phases/capture.md`
- `skills/dbrain-processor/phases/preview.md`
- `skills/dbrain-processor/phases/execute.md`
- `skills/dbrain-processor/phases/reflect.md`

### Canonical Reference Files

- `skills/dbrain-processor/references/about.md`
- `skills/dbrain-processor/references/classification.md`
- `skills/dbrain-processor/references/goals.md`
- `skills/dbrain-processor/references/links.md`
- `skills/dbrain-processor/references/ownership.md`
- `skills/dbrain-processor/references/plaud.md`
- `skills/dbrain-processor/references/intake-intent.md`
- `skills/dbrain-processor/references/process-goals.md`
- `skills/dbrain-processor/references/question-answer.md`
- `skills/dbrain-processor/references/todoist.md`
- `skills/dbrain-processor/references/todoist-project-routing.md`

### Runtime Retrieval Contract

- `skills/vault-retrieval/SKILL.md` is loaded by `processor.py` for
  execute, reflect, direct-answer, and `/do` prompts. It is the single normative
  contract for QMD routing and memory access tracking.

## Prompt Source Map

See `.claude/docs/prompt-source-map.md` for the complete prompt-builder catalog,
the explicit source-of-truth map, and current legacy status of prompt files.

For a human-facing explanation of orchestration and ownership boundaries, see:
- `docs/control-plane.md`

## Editing Protocol

### If you are changing processing behavior

1. Decide which runtime path changes:
   - daily capture
   - preview
   - execute
   - reflect
   - direct question answer
   - `/do`
   - PLAUD classification
   - Todoist routing
2. Edit the canonical runtime prompt builder and/or the canonical phase/reference file.
3. Update tests.
4. Only then update this file and other navigation docs.

### If you are changing only documentation

You may edit:
- this file
- `.claude/docs/prompt-source-map.md`
- `vault/.claude/CLAUDE.md`

But do not introduce a second competing runtime contract here.

## Quick Routing

### Daily `/process`

- wrapper: `src/d_brain/services/processor.py::_build_capture_prompt()`
- phase: `phases/capture.md`
- preview path: `phases/preview.md`

### Full scheduled processing

- wrapper: `src/d_brain/services/processor.py`
- phases:
  - `capture.md`
  - `execute.md`
  - `reflect.md`

### Plain-text direct question

- router: `src/d_brain/control_plane/registry.py` + `src/d_brain/control_plane/router.py`
- intake intent prompt: `references/intake-intent.md`
- answer rules: `references/question-answer.md`

### Scheduled maintenance

- scheduler/orchestrator: `src/d_brain/services/processor.py::run_scheduled_cycle()`
- workflow contracts: `src/d_brain/control_plane/registry.py`

### Telegram `/do`

- wrapper: `src/d_brain/services/processor.py::execute_prompt()`
- scope rules live in code, not in this file

### PLAUD

- wrapper: `src/d_brain/services/plaud.py::_build_classification_prompt()`
- policy: `references/plaud.md`

### Todoist project routing

- wrapper: `src/d_brain/services/todoist_projects.py::_build_prompt()`
- policy: `references/todoist-project-routing.md`

## Supporting Skills

- [[skills/graph-builder/SKILL|graph-builder]] — graph analysis and link maintenance
- [[skills/todoist-ai/SKILL|todoist-ai]] — Todoist access conventions
- [[skills/agent-memory/SKILL|agent-memory]] — memory decay, touch, creative recall
- [[skills/vault-health/SKILL|vault-health]] — deterministic vault maintenance around reflect
