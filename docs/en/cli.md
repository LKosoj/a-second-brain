# CLI reference

[Русский](../ru/cli.md) | [Documentation index](../index.md)

The package installs one executable:

```text
a-second-brain
```

## `a-second-brain init`

Create a private instance:

```bash
a-second-brain init [PROJECT_DIR]
```

If `PROJECT_DIR` is omitted, the current directory is used. The command creates
the anonymized vault and project-local configuration. Existing project files
are preserved, and an existing `vault/` causes exit code `2` rather than an
overwrite.

Example:

```bash
a-second-brain init /srv/a-second-brain-instance
```

Created files include:

- `.env` with mode `0600`;
- `.env.example`;
- `.gitignore` protecting private runtime data;
- `mcp-config.json`;
- `vault-manifest.json`;
- `vault/` with generic prompts, policies, and starter notes.

## `a-second-brain doctor`

Inspect configuration and prerequisites:

```bash
a-second-brain doctor [PROJECT_DIR] [--smoke]
```

Checks include:

- `.env` presence and permissions;
- required variables without displaying their values;
- owner ID validity;
- vault, manifest, and MCP configuration;
- `uv`, `jq`, the selected AI CLI, and authentication;
- conditional Todoist and encrypted-backup requirements;
- optional QMD and PLAUD status.

`--smoke` sends `Reply with exactly OK` through the selected AI CLI. It does
not send vault content.

Output levels:

| Level | Meaning |
|---|---|
| `OK` | Check passed |
| `INFO` | Optional feature is not configured |
| `WARN` | Operation can continue, but review the finding |
| `ERR` | Required configuration or dependency failed |

Exit codes:

| Code | Meaning |
|---:|---|
| `0` | No errors; warnings may be present |
| `1` | One or more checks failed |
| `2` | Invalid CLI arguments, or `init` refused an existing vault |

The legacy `./scripts/doctor.sh` path remains as a thin wrapper around this
command for source checkouts.

## `a-second-brain qmd`

Run QMD against the private vault index:

```bash
a-second-brain qmd [QMD_ARGUMENTS...]
```

Examples:

```bash
a-second-brain qmd status
a-second-brain qmd update
a-second-brain qmd query "weekly focus"
a-second-brain qmd recall "weekly focus"
```

When `EMB_MODEL` is configured, `query` keeps the standard QMD ranking but
uses the project's remote embedding and rerank adapters. `recall` adds the
vault's memory relevance, recency, and lifecycle signals on top.

With no QMD arguments, the command shows index status.

## `a-second-brain run`

Start the Telegram bot in the foreground:

```bash
a-second-brain run
```

From a locked source checkout, prefer:

```bash
uv run --frozen --no-dev a-second-brain run
```

Stop the foreground process with `Ctrl+C`. Production operation should use the
provided systemd user unit.

## General help

```bash
a-second-brain --help
a-second-brain init --help
a-second-brain doctor --help
a-second-brain qmd --help
a-second-brain run --help
```
