# Architecture

## Privacy model

The distributable application and the user's data have separate lifecycles:

```text
public Git repository                 private instance
├── src/d_brain/                      ├── .env
├── resources/vault_template/  init → ├── vault/
├── deploy/*.in (systemd + launchd)   ├── .vault-backups/
└── tests/                             └── runtime logs/state
```

The public repository never needs a real vault. Tests read the packaged,
anonymized template or create temporary vaults.

## Runtime components

| Component | Responsibility |
|---|---|
| `bot/` | Telegram input, authorization, commands, and delivery |
| `control_plane/` | Canonical workflow registry, routing, context, and write permissions |
| `services/` | Processing, imports, search, storage, backups, and integrations |
| `resources/vault_template/` | Generic private-vault seed copied by `a-second-brain init` |
| `cli.py` | Safe initialization and bot entrypoint |

`vault-manifest.json` defines allowed vault paths and frontmatter profiles.
The vault remains the source of truth for user knowledge; generated indexes
and compiled briefings are rebuildable.

## Main flows

1. Telegram capture writes authorized content into the daily or import area.
2. Interactive processing creates a preview; scheduled full processing runs
   capture, execute, and reflect as isolated phases.
3. Questions combine curated context, compiled briefings, and QMD retrieval.
4. Document, web, YouTube, and PLAUD integrations archive source material
   before indexing it.

The complete workflow catalog is
`src/d_brain/control_plane/registry.py`. Runtime prompt construction is in
`src/d_brain/services/`; packaged phase and policy files support it but do not
override the registry.

## Safety boundaries

- Telegram access is restricted to `OWNER_TELEGRAM_ID`.
- Secrets are loaded from `.env`, which is ignored by Git.
- Vault writes are constrained by the manifest and cooperative write lock.
- Initializers refuse to overwrite an existing vault.
- User systemd units run without root and with basic service hardening.
  LaunchAgent plists (macOS) deliver the same schedule without root and
  source `.env` via `scripts/lib/run_with_env.sh`.
