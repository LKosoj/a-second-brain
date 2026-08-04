---
type: note
description: Card-template and memory-operations routing doc for this vault. Use it for decay, touch, creative recall, and for keeping durable knowledge in one place.
last_accessed: 2026-07-27
relevance: 0.99
tier: active
name: agent-memory
Use when: (1) touching notes after meaningful reads, (2) running decay, (3) maintaining card frontmatter, (4) diagnosing memory drift
Triggers: ["memory management", "organize vault", "memory decay", "forgetting curve"]
---

# Agent Memory

Project-facing routing doc for the vault memory system.

## What This Skill Owns

- card frontmatter conventions
- touch/decay workflow
- creative recall commands
- keeping durable facts in one place
- epistemic confidence, scope, and correction links for durable facts

## What This Skill Does Not Own

- daily processing prompt behavior
- goal logic
- graph maintenance
- Telegram report formatting

Those contracts live in their own runtime files. See `.claude/docs/prompt-source-map.md`.

## Card Contract

All durable vault cards should follow the agent-memory template described in:
- `vault/.claude/CLAUDE.md`
- `vault/.claude/rules/thoughts-format.md`

Minimum expectations:
- meaningful `description`
- sparse lowercase `tags`
- real `status`
- automatic `last_accessed`, `relevance`, `tier`
- no duplicate facts across multiple notes

## Operational Commands

```bash
uv run skills/agent-memory/scripts/memory-engine.py scan vault
uv run skills/agent-memory/scripts/memory-engine.py init vault --dry-run
uv run skills/agent-memory/scripts/memory-engine.py decay vault
uv run skills/agent-memory/scripts/memory-engine.py daily vault
uv run skills/agent-memory/scripts/memory-engine.py touch vault/path/to/note.md
uv run skills/agent-memory/scripts/memory-engine.py supersede thoughts/old-fact.md thoughts/corrected-fact.md --vault vault
uv run skills/agent-memory/scripts/memory-engine.py recover-supersession vault
uv run skills/agent-memory/scripts/memory-engine.py creative 5 vault
uv run skills/agent-memory/scripts/memory-engine.py stats vault
```

All mutation commands require a valid `vault-manifest.json` and use the shared
vault write lock. `daily` changes only `daily/*.md`; use `init` or `decay` for
other Markdown notes.

## Session Rules

- Bootstrap reads today + yesterday, not the whole archive.
- If you read a note directly to answer a question, touch it afterwards.
- Use creative recall for deep history or idea generation, not for routine execution.
- Let decay lower visibility over time instead of deleting useful notes.
- For a durable fact with epistemic metadata, use only the namespaced fields:
  `epistemic_confidence` (`verified|inferred|unverified`), `epistemic_scope`,
  `epistemic_state` (`active|superseded`), and, for verified facts,
  non-empty `epistemic_verification`.
- Correct a fact by creating a successor note, then run `supersede OLD NEW --vault .`.
  Never silently rewrite or delete the old fact. The command writes reciprocal
  `superseded_by` (scalar) and `supersedes` (list) links safely.
- Before `supersede` or `recover-supersession`, quiesce non-cooperative vault
  editors and writers. The shared `vault/.locks/vault-write.lock` serializes
  cooperating services: VaultStorage daily writes, memory-engine mutation
  commands, graph reports, processor session/handoff/thought writes, compiled
  briefing renders, and vault-health description patches. It is not an
  absolute filesystem-wide CAS guarantee; frontmatter migration additionally
  requires its fresh full quiesce proof and external backup gate.
- `init`, `decay`, `daily`, and `touch` load the manifest and keep every
  Markdown mutation for one command inside one shared vault lock. Supersession
  commands delegate their own lock to avoid nested-lock deadlocks.
- `supersede` and `recover-supersession` delegate lock ownership to their
  epistemic-memory operation; do not wrap that delegation in another vault lock.
- Do not use compiled `confidence`, card `status`, or `decision_status` as
  epistemic metadata; they are separate contracts.

## Anti-Patterns

- duplicating the same fact in multiple cards
- touching every file during bulk maintenance
- loading the whole vault when indexes and tier-aware search are enough
- creating a parallel "memory system" outside the vault itself
- changing an existing fact in place when the correction should preserve its history
