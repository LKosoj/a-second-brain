# Architecture

[Русский](../ru/architecture.md) | [Documentation index](../index.md)

## Public and private boundaries

```text
public source or wheel               private instance
├── Python application               ├── .env
├── anonymized vault template  init →├── vault/
├── systemd templates                ├── .vault-backups/
└── tests and documentation          └── logs and runtime state
```

The public repository is canonical for code. The generated vault is canonical
for user knowledge and must remain outside public Git history.

## Main components

| Component | Responsibility |
|---|---|
| `d_brain.cli` | `init`, `doctor`, and `run` entrypoints |
| `d_brain.doctor` | Installation and runtime diagnostics |
| `d_brain.bot` | Telegram authorization, handlers, menu, and delivery |
| `d_brain.control_plane` | Workflow registry, routing, context, and allowed writes |
| `d_brain.services` | Processing, storage, imports, retrieval, backups, and integrations |
| `d_brain.resources` | Anonymized project and vault templates |

`src/d_brain/control_plane/registry.py` is the canonical workflow catalog.
Runtime prompt builders live under `src/d_brain/services/`. Packaged phase and
policy files support those builders but do not override the registry.

## Capture flow

Telegram handlers authorize the sender using `OWNER_TELEGRAM_ID`, classify the
input, and delegate to a specialized service. Text and voice can enter the
daily capture area; files, pages, videos, and recordings enter import areas.
Searchable writes trigger derived indexing where configured.

## Processing flow

Interactive `/process` creates a preview. The scheduled write-heavy workflow
isolates capture, execute, and reflect phases. Each phase receives only its
declared context and allowed actions. Python validates and persists results at
the workflow boundary.

## Questions and retrieval

Question routing combines:

1. curated vault context;
2. compiled briefings;
3. QMD retrieval when available;
4. exact-path or exact-string lookup for identifiers.

Derived summaries and search indexes are aids, not ground truth. Durable
claims belong in curated vault Markdown.

Compiled pages are also enriched over time, not just read: a nightly pass
adds verified claims, tracks source trust, and classifies conflicts before
writing back to `compiled/` — temporal and contextual conflicts resolve
automatically (temporal: the newer source wins; contextual: both claims are
kept, each in its own scope); only a factual conflict waits for the owner in
the decisions queue. See `skills/compile-enrich/SKILL.md` for the full
pipeline.

## Storage and consistency

`vault-manifest.json` declares the memory root, authorized content roots,
infrastructure areas, context budget, QMD index, and frontmatter profiles.
Vault writers use guarded paths and a cooperative write lock. Migration tools
add backup, quiescence, proof, and write-ahead checks for high-risk changes.

## Security boundaries

- `.env` stores secrets and is mode `0600`.
- Telegram rejects users other than the configured owner.
- Control-plane hooks prevent runtime mutation of managed prompt assets.
- Systemd user units run without root and enable `NoNewPrivileges`.
- `PrivateTmp` is intentionally omitted, and `tests/test_systemd_templates.py`
  pins that. The original reason no longer applies: the writer used to publish
  an `O_TMPFILE` descriptor and could not do so from that mount namespace,
  while publication is now a `renameat2` between two directories inside the
  vault and never touches `/tmp`. Enabling it is a separate decision.
- Backups are encrypted to a public GPG recipient.
- The vault is not exposed as a generic MCP server.

For implementation-level ownership and workflow contracts, read the
[control-plane reference](../control-plane.md).
