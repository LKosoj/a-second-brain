---
type: note
last_accessed: 2026-04-04
relevance: 0.26
tier: archive
---
# Prompt Source Map

Explicit source-of-truth map for the project's prompt corpus.

## Goal

Prevent prompt drift between:
- runtime prompt builders in code
- phase/reference prompt files
- repo-level skills
- repo-level rules
- repo-level agents

## Canonical Runtime Prompt Layer

These files define actual behavior and should be edited first when product behavior changes.

### Control-Plane Runtime Layer

- `src/d_brain/control_plane/contracts.py`
- `src/d_brain/control_plane/registry.py`
- `src/d_brain/control_plane/router.py`

### Inline Prompt Builders

- `src/d_brain/services/processor.py`
- `src/d_brain/services/plaud.py`
- `src/d_brain/services/documents.py` (`DocumentArchiveService._llm_summary()`)
- `src/d_brain/services/todoist_projects.py`
- `src/d_brain/services/link_summary.py`
- `src/d_brain/services/image_analysis.py`
- `src/d_brain/services/compiled_briefings.py`
- `src/d_brain/services/reflection_digest.py`
- `src/d_brain/services/recall_planner.py`

### Runtime-Provided Context

- `src/d_brain/services/context_pack.py` builds UTF-8 byte-budget-aware eager context for the
  runtime prompt paths that opt into it.
- `src/d_brain/run_context_pack.py` and
  `vault/.claude/hooks/context-pack.sh` are the narrow manual/session hook
  entrypoint; they do not become a second prompt source.
- `vault-manifest.json` and `src/d_brain/manifest.py` provide the context
  budget and vault contract. They are configuration, not prompt policy.

### Runtime-Loaded Retrieval Policy

- `skills/vault-retrieval/SKILL.md` is loaded directly by
  `src/d_brain/services/processor.py` for execute, reflect, direct-question, and
  `/do` prompts. It is the agent retrieval contract; retrieval planning and QMD
  execution remain implemented in `src/d_brain/services/recall_planner.py` and
  `src/d_brain/services/qmd.py`.

### Durable Metadata Policy (Not a Prompt Source)

- `src/d_brain/services/frontmatter.py` validates and patches vault metadata.
- `src/d_brain/services/frontmatter_migration.py` and
  `src/d_brain/run_frontmatter_migration.py` own guarded migration state.
- `src/d_brain/services/epistemic_memory.py` owns namespaced epistemic
  supersession; `src/d_brain/services/vault_lock.py` coordinates cooperative
  writers.

These modules govern persistence and must not be copied into phase prompts.

### Phase Files Loaded Directly By Runtime

- `skills/dbrain-processor/phases/capture.md`
- `skills/dbrain-processor/phases/preview.md`
- `skills/dbrain-processor/phases/execute.md`
- `skills/dbrain-processor/phases/reflect.md`

### Reference Files Loaded Directly By `processor.py`

- `skills/dbrain-processor/references/about.md`
- `skills/dbrain-processor/references/classification.md`
- `skills/dbrain-processor/references/goals.md`
- `skills/dbrain-processor/references/intake-intent.md`
- `skills/dbrain-processor/references/links.md`
- `skills/dbrain-processor/references/ownership.md`
- `skills/dbrain-processor/references/process-goals.md`
- `skills/dbrain-processor/references/question-answer.md`
- `skills/dbrain-processor/references/todoist.md`
- `skills/dbrain-processor/references/todoist-project-routing.md`

### Reference Files Loaded Directly By Other Runtime Code

- `skills/dbrain-processor/references/plaud.md` via `src/d_brain/services/plaud.py`

### Canonical Support References

These files are part of the maintained prompt policy layer, even if a given runtime path may not inject them on every call.

- `skills/dbrain-processor/references/about.md`
- `skills/dbrain-processor/references/classification.md`
- `skills/dbrain-processor/references/goals.md`
- `skills/dbrain-processor/references/intake-intent.md`
- `skills/dbrain-processor/references/links.md`
- `skills/dbrain-processor/references/ownership.md`
- `skills/dbrain-processor/references/plaud.md`
- `skills/dbrain-processor/references/process-goals.md`
- `skills/dbrain-processor/references/question-answer.md`
- `skills/dbrain-processor/references/todoist.md`
- `skills/dbrain-processor/references/todoist-project-routing.md`

## Secondary Navigation Layer

These files may explain the system, but should not redefine runtime behavior independently.

- `docs/control-plane.md`
- `docs/control-plane-security-profile-template.md`
- `scripts/setup_control_plane.sh`
- `scripts/update_control_plane.sh`
- `vault/.claude/CLAUDE.md`
- `vault/.claude/settings.json`
- `vault/.claude/hooks/*.sh`
- `skills/dbrain-processor/SKILL.md`
- `vault/.claude/rules/*.md`
- `vault/.claude/agents/*.md`
- `skills/graph-builder/SKILL.md`
- `skills/todoist-ai/SKILL.md`
- `skills/agent-memory/SKILL.md`
- `skills/vault-health/SKILL.md`

## Bundled Utility Skills Outside Product Runtime

These prompt files may exist in the repo tree as reusable utilities, but they are not part of the product's day-to-day runtime prompt surface unless a workflow explicitly invokes them:

- `skills/doc-coauthoring/SKILL.md`
- `skills/docx/SKILL.md`
- `skills/pptx/SKILL.md`
- `skills/youtube-transcript/SKILL.md`
- `skills/datetime/SKILL.md`

## Removed Legacy Prompt Files

The former low-trust refs below were deleted after consolidation into the canonical runtime layer:

- `skills/dbrain-processor/references/business-context.md`
- `skills/dbrain-processor/references/contacts.md`
- `skills/dbrain-processor/references/report-template.md`
- `skills/dbrain-processor/references/rules.md`

Do not recreate them as alternate specs. If new behavior is needed, add it to the canonical runtime files instead.

## Editing Rules

### When changing product behavior

1. Edit the canonical runtime prompt builder and/or canonical phase/reference file.
2. Update or add tests.
3. Then update navigation docs:
   - `skills/dbrain-processor/SKILL.md`
   - `vault/.claude/CLAUDE.md`
   - relevant rules/agents

### When changing only documentation

You may edit the secondary navigation layer directly, but do not create a new competing runtime contract there.

## Drift Checks

Prompt corpus is unhealthy if any of the following becomes true:

- repo agents encode direct `mcp__todoist__...` while runtime uses `mcp-cli`
- a prompt file references missing paths
- multiple files claim to be the authoritative HTML report contract
- secondary docs silently describe a vault topology that no longer exists
- legacy prompt files contain newer policy than the canonical runtime files
- control-plane registry and runtime route behavior diverge silently

## Current Cleanup Status

1. Runtime canon stays in code plus the control-plane registry
2. Orchestration truth lives in `src/d_brain/control_plane/registry.py` — all workflow definitions, question routes, and integration contracts
3. Navigation docs (`CLAUDE.md`, `docs/control-plane.md`, skill docs) reference the registry as upstream source of truth
4. `dbrain-processor/SKILL.md` is a thin routing document
5. Repo agents use the `mcp-cli` contract
6. Rules were reconciled with live runtime behavior
7. Lightweight drift tests cover the managed prompt surface
