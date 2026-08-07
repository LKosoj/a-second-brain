---
type: note
last_accessed: 2026-04-04
relevance: 0.84
tier: warm
---
# Agent Second Brain

Voice-first personal assistant for capturing thoughts and managing tasks via Telegram.

Общение всегда на русском.

## Public repository rules

### Privacy boundary

- `vault/` is private runtime data and must remain ignored.
- Public examples and fixtures must use fictional people, organizations, and
  projects.
- Never commit `.env`, credentials, imports, transcripts, generated summaries,
  sessions, locks, caches, backups, or attachments.
- Add distributable vault content under
  `src/d_brain/resources/vault_template/` and public project skills under
  `src/d_brain/resources/project_template/skills/`.

### Source of truth

- Workflow contracts: `src/d_brain/control_plane/registry.py`
- Runtime prompt construction: `src/d_brain/services/`
- Distributed vault template: `src/d_brain/resources/vault_template/`
- Distributed project skills: `src/d_brain/resources/project_template/skills/`
- Public installation: `install.sh` and `scripts/install-systemd-user.sh`

### Quality gates

Run before committing:

```bash
uv run --frozen --group dev pytest -q
uv run --frozen --group dev ruff check src tests
uv run --frozen --group dev mypy src
git diff --check
```

Tests must build temporary vaults and must not depend on a developer's runtime
`vault/`.

## EVERY SESSION BOOTSTRAP

**Before doing anything else, read these files in order:**

1. `vault/MEMORY.md` — curated long-term memory (preferences, decisions, context)
2. `vault/.memory-config.json` — memory decay configuration
3. `vault/daily/YYYY-MM-DD.md` — today's entries
4. `vault/daily/YYYY-MM-DD.md` — yesterday's entries (for continuity)
5. `vault/goals/3-weekly.md` — this week's ONE Big Thing
6. `vault/.session/handoff.md` — previous session context (if exists)

**Don't ask permission, just do it.** This ensures context continuity across sessions.

---

## SESSION END PROTOCOL

**Before ending a significant session, write to today's daily:**

```markdown
## HH:MM [text]
Session summary: [what was discussed/decided/created]
- Key decision: [if any]
- Created: [[link]] [if any files created]
- Next action: [if any]
```

**Also update `vault/MEMORY.md` only if the new information is likely to remain useful weeks or months later:**
- New durable key decision was made
- Stable user preference discovered
- Important durable fact about the user, project, or business context was learned
- Active context changed significantly in a way that should survive beyond the current session

**Default to NOT updating `vault/MEMORY.md` when unsure. Prefer `daily` or `handoff` for:**
- Step-by-step implementation progress
- One-off fixes, smoke tests, and verification results
- Temporary debugging forensics or migration status
- File-by-file change logs
- Short-lived next steps or work-in-progress notes

**Update `vault/.session/handoff.md`:**
- Last Session: what was done
- Key Decisions: if any
- In Progress: unfinished work
- Next Steps: what to do next
- Observations: friction signals, patterns, ideas (type: `[friction]`, `[pattern]`, `[idea]`)
- Keep exactly one rolling copy of these sections; overwrite status fields instead of appending new session blocks
- Keep `Observations` deduplicated and compact; weekly reflection clears processed items

---

## Mission

Help user stay aligned with goals, capture valuable insights, and maintain clarity.

## Directory Structure

| Folder | Purpose |
|--------|---------|
| `daily/` | Raw daily entries (YYYY-MM-DD.md) |
| `goals/` | Goal cascade (3y → yearly → monthly → weekly) |
| `thoughts/` | Processed notes by category |
| `MOC/` | Maps of Content indexes |
| `attachments/` | Photos by date |
| `business/` | Business data (CRM, network, events) |
| `projects/` | Side projects (clients, leads) |

## Business Context

**Entry point:** `business/_index.md`

```
business/
├── _index.md       ← Start here (overview + links)
├── crm.md          ← CRM and deal context
├── network.md      ← Company structure and partner context
└── events.md       ← Events and conferences
```

Current repo shape is flat at the top level of `business/`.
Only create deeper linked notes when the live structure clearly calls for them.

## Projects Context

**Entry point:** `projects/_index.md`

```
projects/
├── _index.md       ← Start here
├── clients.md      ← Client project context
├── leads.md        ← Lead pipeline context
└── projects.md     ← Active projects
```

## Current Focus

See [[goals/3-weekly]] for this week's ONE Big Thing.
See [[goals/2-monthly]] for monthly priorities.

## Goals Hierarchy

```
goals/0-vision-3y.md    → 3-year vision by life areas
goals/1-yearly-YYYY.md  → Annual goals + quarterly breakdown
goals/2-monthly.md      → Current month's top 3 priorities
goals/3-weekly.md       → This week's focus + ONE Big Thing
```

## Entry Format

```markdown
## HH:MM [type]
Content
```

Types: `[voice]`, `[text]`, `[forward from: Name]`, `[photo]`

## Processing Workflow

Use `/process` for a quick interactive preview. The full write-heavy daily processing runs automatically at 21:00.

All workflows are declared in `src/d_brain/control_plane/registry.py` — the canonical catalog of capture, question, maintenance, and integration workflows. The registry defines entrypoints, context requirements, allowed writes, and fallback behavior for each workflow.

### 3-Phase Pipeline:
1. **CAPTURE** — Read daily entries → classify → JSON
2. **EXECUTE** — Create Todoist tasks, save thoughts, update CRM → JSON
3. **REFLECT** — Generate markdown report, update MEMORY, record observations

Each phase = fresh agent context for better quality.
The important invariant is isolation between phases, not a specific vendor CLI.

## Card Template (agent-memory)

**Skill:** `skills/agent-memory/SKILL.md`

All new vault cards follow the agent-memory template:

```yaml
---
type: crm|lead|contact|project|personal|note
description: >-
  One line — what a searcher will see in results
tags: [tag1, tag2]        # 2-5 tags, lowercase
status: active|draft|pending|done|inactive
industry: FMCG            # for CRM/leads
region: US                 # ISO codes
created: YYYY-MM-DD
updated: YYYY-MM-DD
# Auto fields (don't edit manually):
last_accessed: YYYY-MM-DD
relevance: 0.85
tier: active
---
```

**Rules:**
- `description` — REQUIRED. Write as a search snippet, NOT "contact" or "crm"
- `tags` — REQUIRED. 2-5 tags, lowercase, hyphen-separated
- `status` ≠ `tier`: status = business status, tier = memory (automatic)
- One fact = one place (DRY). References via [[wikilinks]]
- Decay engine: `uv run skills/agent-memory/scripts/memory-engine.py decay vault`

## Skills & References

### Source of truth hierarchy

1. **Control-plane registry** (`src/d_brain/control_plane/registry.py`) — canonical workflow catalog: every capture, question, maintenance, and integration workflow with its entrypoint, context, and write permissions.
2. **Runtime prompt builders** (`src/d_brain/services/*.py`) — inline prompt construction.
3. **Phase and reference files** (`skills/dbrain-processor/phases/`, `references/`) — loaded by runtime builders.
4. **Navigation docs** (this file, skill docs, `docs/control-plane.md`) — explain the above, never override them.

Use `vault/.claude/docs/prompt-source-map.md` for the full source-of-truth map.

### Public and private skills

- Public skills are exposed at `skills/<name>` and stored in the public package
  at `src/d_brain/resources/project_template/skills/<name>`.
- Private skills are stored only in
  `vault/skills/private/<name>` and must not enter Git or the public package.
- `skills/private` points to that private directory. Every private skill has a
  relative discovery symlink: `skills/<name> -> private/<name>`.
- `.claude/skills`, `.agents/skills`, `.codex/skills`, and `.qwen/skills` point
  to the same root `skills` tree. Do not keep separate CLI copies.
- Kimi Code discovers that same shared tree through `.agents/skills`; do not
  create a separate Kimi skills copy.
- The `.gitignore` skill allowlist keeps private discovery symlinks local. When
  adding a public skill, add its root path to that allowlist.
- Every `SKILL.md` must contain `name` and `description`.

| Skill | Purpose |
|-------|---------|
| `dbrain-processor` | Main daily processing (3-phase pipeline) |
| `graph-builder` | Vault link analysis and building |
| `vault-health` | Health scoring, MOC generation, link repair |
| `agent-memory` | Card template, decay engine, tiered search |
| `todoist-ai` | Todoist task management via MCP |
| `compile-enrich` | Compiled-page enrichment: domains, trust, conflicts, aging |

- **Processing:** `skills/dbrain-processor/SKILL.md`
- **Graph Builder:** `skills/graph-builder/SKILL.md`
- **Vault Health:** `skills/vault-health/SKILL.md`
- **Agent Memory:** `skills/agent-memory/SKILL.md`
- **Todoist:** `skills/todoist-ai/SKILL.md`
- **Compile Enrich:** `skills/compile-enrich/SKILL.md`
- **Rules:** `vault/.claude/rules/` (daily, thoughts, goals, obsidian-markdown, weekly-reflection)
- **Docs:** `vault/.claude/docs/`
- **Control Plane:** `docs/control-plane.md`, `scripts/setup_control_plane.sh`, `scripts/update_control_plane.sh`

## Graph Builder

**Purpose:** Analysis and maintenance of vault link structure.

**Architecture:**
1. `scripts/analyze.py` — deterministic vault traversal
2. `scripts/add_links.py` — batch link addition
3. Agent — semantic links for orphan files

**Usage:**
```bash
# Analyze vault
uv run skills/graph-builder/scripts/analyze.py vault

# Result
vault/.graph/vault-graph.json  # JSON graph with stats
vault/.graph/report.md         # Human-readable report
```

**Domains:**
| Domain | Path | Hub |
|--------|------|-----|
| Personal | thoughts/, goals/, daily/ | MEMORY.md |
| Business | business/*.md | business/_index.md |
| Projects | projects/*.md | projects/_index.md |

## Available Agents

| Agent | Purpose |
|-------|---------|
| `weekly-digest` | Weekly review with goal progress |
| `goal-aligner` | Check task-goal alignment |
| `note-organizer` | Organize vault, fix links |
| `inbox-processor` | GTD-style inbox processing |

## Path-Specific Rules

See `vault/.claude/rules/` for format requirements:
- `daily-format.md` — daily files format
- `thoughts-format.md` — thought notes format
- `goals-format.md` — goals format
- `telegram-report.md` — Telegram markdown report format
- `obsidian-markdown.md` — Obsidian syntax rules
- `weekly-reflection.md` — weekly reflection template

## Report Format

Reports use Telegram MarkdownV2 at delivery time:
- prompts should return markdown, not HTML
- runtime converts short messages to MarkdownV2 and long messages to HTML files when needed

## Quick Commands

| Command | Action |
|---------|--------|
| `/process` | Quick interactive preview of daily processing |
| `/process_full` | Full write-heavy daily cycle |
| `/do` | Execute arbitrary request |
| `/stats` | Show entry statistics |
| `/files` | Browse vault files |
| `/menu` | Open the persistent dashboard menu |
| `/why` | Explain why a compiled page states a claim, with sources |

## Customization

For personal overrides: create `CLAUDE.local.md`

## Learnings (from experience)

1. **Don't rewrite working code** without reason (KISS, DRY, YAGNI)
2. **Don't add checks** that weren't there — let the agent decide
3. **Don't propose solutions** without studying git log/diff first
4. **Don't break architecture** (entrypoint → Python orchestrator → runtime prompt layer is correct)
5. **Problems are usually simple** (e.g., sed one-liner for HTML fix)

---

*System Version: 3.0*
