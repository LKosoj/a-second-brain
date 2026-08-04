# Control Plane

Human-facing guide to the d-brain control plane.

The control plane decides:
- which workflow handles one input;
- which context is required and in what order;
- which side effects are allowed;
- which runtime entrypoint actually executes the work.

The execution plane stays in Python services, the Telegram bot, `qmd`, compiled briefings, nightly processing, and ingest pipelines.

## Canonical Sources

The canonical control-plane sources are:
- `src/d_brain/control_plane/contracts.py`
- `src/d_brain/control_plane/registry.py`
- `src/d_brain/control_plane/router.py`

Supporting governance assets:
- `vault-manifest.json` (vault contract; it is not a workflow registry)
- `vault/.claude/settings.json`
- `vault/.claude/hooks/protect-control-plane.sh`
- `vault/.claude/hooks/validate-frontmatter.sh`
- `vault/.claude/docs/prompt-source-map.md`

Everything else should explain these files, not compete with them.

## Adoption Runtime Boundaries

`vault-manifest.json` is loaded fail-fast by `src/d_brain/manifest.py`. It owns
the vault root, QMD index name, context byte budget, content/infrastructure
roots, and required frontmatter fields. It does not define workflows; the
registry remains the workflow source of truth.

`src/d_brain/services/context_pack.py` builds the one UTF-8 byte-budget-aware eager context
injected into applicable runtime prompts. The runtime-loaded retrieval contract
is `skills/vault-retrieval/SKILL.md`; it tells the model how to
use QMD and `rg`, while `qmd.py` and `recall_planner.py` own execution.
`compiled_briefings.py` owns compiled prompt builders and the derived briefing
block injected before automatic recall by the default direct-question route.

`frontmatter.py`, `frontmatter_migration.py`, `epistemic_memory.py`, and
`vault_lock.py` own durable vault metadata changes. Migration is a guarded CLI
operation with inventory, backup/proof, WAL and validation; epistemic
supersession is a separate namespaced metadata operation. Neither mechanism
adds a workflow definition to the registry.

## Workflow Kinds

The registry currently models these workflow kinds:
- `capture`
- `question`
- `maintenance`
- `integration`

Typical examples:
- `capture.daily-entry`
- `question.planning`
- `question.fact-lookup`
- `maintenance.compiled-nightly`
- `maintenance.vault-health`
- `integration.documents.archive`
- `integration.web.archive`
- `integration.youtube.archive`
- `integration.plaud.sync`

## Ownership Model

### Repo-Managed Control-Plane Assets

These files are part of product/runtime governance and should be changed only through repo edits:
- `src/d_brain/control_plane/*`
- `vault/.claude/settings.json`
- `vault/.claude/hooks/*`
- `vault/.claude/docs/prompt-source-map.md`
- `docs/control-plane.md`
- `docs/control-plane-security-profile-template.md`
- `scripts/setup_control_plane.sh`
- `scripts/update_control_plane.sh`

Runtime writes must not silently mutate this layer.

### Manifest Content Surface

`user_content_roots` enumerates the content surface; membership does not make
every path canonical:

- curated canonical records: `vault/MEMORY.md`, `vault/goals/*`,
  `vault/business/*`, `vault/projects/*`, `vault/thoughts/*`;
- canonical captured records: `vault/daily/*`, `vault/imports/*`;
- user-facing derived notes: `vault/compiled/*`, `vault/summaries/*`,
  `vault/MOC/*`.

Runtime may read this surface broadly, but writes remain narrow and
policy-bound. Derived notes may be rebuilt; canonical records are not treated
as disposable indexes.

### Manifest Infrastructure Surface

Every declared infrastructure root has a separate role:

- config/governance: `vault/.claude/*`, `vault/.codex/*`,
  `vault/.obsidian/*`, `vault/templates/*`;
- rebuildable derived state: `vault/.graph/*`, `vault/.qmd/*`;
- runtime state: `vault/.compiled/*`, `vault/.session/*`;
- recoverable trash: `vault/.trash/*`.

`vault/.sessions/*` and `vault/.locks/*` are additional service-owned runtime
state outside the manifest infrastructure list. Runtime state can be cleared
only according to the owning workflow's recovery contract; it is not durable
user knowledge.

## Setup And Update

Initial bootstrap:

```bash
./scripts/setup_control_plane.sh
```

Routine refresh after pulling repo changes:

```bash
./scripts/update_control_plane.sh
```

Both scripts are intentionally narrow. They do not rewrite user vault content. They only validate the presence of control-plane assets, refresh hook permissions, and print the current managed surface.

## Guardrails

Current deterministic guardrails:
- `protect-control-plane.sh` blocks `Edit`/`Write` targets and direct Bash
  mutations that lexically reference repo-managed `.claude` assets; ordinary
  commands, read-only inspection, `a-second-brain qmd`, and memory-engine remain usable
- `validate-frontmatter.sh` flags obvious malformed frontmatter after markdown writes

The vault is not exposed as an MCP server. External integration authority is
declared by the control-plane registry, not by a generic vault tool.

The Bash guard is defense in depth, not a sandbox against deliberately
obfuscated same-UID shell code. The intended posture is warning-first for soft
issues and hard-block only for control-plane invariants.

## Security Profiles

Future risky integrations should not invent trust rules ad hoc.

Use:
- `docs/control-plane-security-profile-template.md`

Every risky integration must define:
- trust boundary
- allowed actions
- forbidden actions
- read-only fallback
- write boundary
- audit trail expectations

## Current Status

The control plane registry is the canonical source of truth for all workflow definitions. It drives:
- question routing (5 route strategies: planning, relationship, status-history, fact-lookup, general)
- capture workflow metadata (`capture.daily-entry`)
- scheduled maintenance orchestration (`maintenance.scheduled-cycle`, `maintenance.compiled-nightly`, `maintenance.vault-health`)
- integration workflow contracts (`integration.documents.archive`, `integration.web.archive`, `integration.youtube.archive`, `integration.plaud.sync`)
- top-level text workflow resolution via `router.resolve_text_workflow()`

Navigation docs (`CLAUDE.md`, skill docs, this file) reference the registry as their upstream source. They should never redefine workflow behavior independently.
