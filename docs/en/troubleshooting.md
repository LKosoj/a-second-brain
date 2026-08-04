# Troubleshooting

[Русский](../ru/troubleshooting.md) | [Documentation index](../index.md)

Start every investigation from the instance directory:

```bash
a-second-brain doctor
```

Use `--smoke` only after ordinary checks pass.

## Read doctor output

| Level | Action |
|---|---|
| `ERR` | Fix before starting or enabling services |
| `WARN` | Review; the command still returns success if no errors exist |
| `INFO` | Optional feature is disabled or unavailable |
| `OK` | Check passed |

The summary exit code is `1` when at least one `ERR` exists.

## `.env is missing`

Run initialization in the intended instance:

```bash
a-second-brain init /path/to/instance
```

If the vault already exists, do not rerun initialization. Restore or create
only `.env` from `.env.example`, then apply mode `0600`.

## Required variable is missing

Edit `.env`; do not export secrets in shell history. Required variables are
`TELEGRAM_BOT_TOKEN`, `DEEPGRAM_API_KEY`, and a positive
`OWNER_TELEGRAM_ID`.

## `.env permissions are 644`

Restrict the file:

```bash
chmod 600 .env
```

The finding is a warning because permissions alone do not prove exposure, but
it should be fixed immediately.

## `VAULT_PATH does not match ... memory_root`

The default manifest declares `vault`. Set:

```dotenv
VAULT_PATH=./vault
```

Custom layouts require a matching manifest and should remain inside the
instance root.

## AI CLI is missing or authentication is not confirmed

Install the command selected by `AI_CLI`, run its login command, and recheck:

```bash
a-second-brain doctor --smoke
```

Authentication detection is conservative. If login is valid but `doctor`
still warns, verify the CLI's own status command and environment for the same
service account that runs systemd.

## Todoist prerequisites are missing

When `TODOIST_API_KEY` is set, both `mcp-cli` and `npx` are required. Install
them for the service account or clear the key to disable Todoist.

## Bot exits immediately

Run in the foreground:

```bash
uv run --frozen --no-dev a-second-brain run
```

Then inspect:

```bash
journalctl --user -u a-second-brain.service -n 200 --no-pager
systemctl --user show a-second-brain.service \
  -p ExecMainStatus -p ExecMainStartTimestamp
```

Common causes are invalid tokens, missing AI CLI authentication, unreadable
vault paths, and a service environment different from the interactive shell.

## Timer did not run

```bash
systemctl --user list-timers 'a-second-brain-*'
journalctl --user -u a-second-brain-process.service --since yesterday
```

Check the host timezone, whether lingering is enabled, and whether the
pre-processing backup failed.

## Search results are stale

Run QMD maintenance and inspect its output:

```bash
uv run --frozen --no-dev a-second-brain qmd cleanup
```

Remember that QMD is derived. Do not edit its index as a substitute for
correcting vault Markdown.

## Before asking for help

Collect:

- sanitized `doctor` output;
- service and timer state;
- relevant journal lines with personal data removed;
- package version and operating system;
- exact command and exit code.

Never attach `.env`, a populated vault, raw transcripts, or unredacted logs.
