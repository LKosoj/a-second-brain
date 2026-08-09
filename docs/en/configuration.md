# Configuration

[Русский](../ru/configuration.md) | [Documentation index](../index.md)

Runtime configuration is read from `.env` in the instance directory.
`a-second-brain init` creates it with mode `0600`. Never commit this file or
paste its values into issues, logs, or documentation.

## Required settings

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token from BotFather |
| `DEEPGRAM_API_KEY` | Voice transcription |
| `OWNER_TELEGRAM_ID` | Positive numeric Telegram user ID allowed to use the bot |

`AI_CLI` defaults to `claude`, but the selected command must be installed and
authenticated.

## Core behavior

| Variable | Default | Purpose |
|---|---:|---|
| `AI_CLI` | `claude` | `claude`, `claude-tmux`, `codex`, `qwen`, `gemini`, `kimi`, `grok`, or `opencode` |
| `CONTENT_LANGUAGE` | `ru` | Language used for saved and generated content |
| `OWNER_FULL_NAME` | empty | Owner matching in task-assignment prompts |
| `VAULT_PATH` | `./vault` | Private Obsidian-compatible vault |

Keep `VAULT_PATH` aligned with `memory_root` in `vault-manifest.json`. The
standard layout uses `./vault`.

## Todoist

| Variable | Default | Purpose |
|---|---:|---|
| `TODOIST_API_KEY` | empty | Enables Todoist task actions |

Todoist also requires `mcp-cli`, `npx`, and the project-local
`mcp-config.json`. When the key is empty, `doctor` treats Todoist as disabled.

## Web extraction

The application tries configured extraction providers when ordinary HTTP
retrieval is insufficient.

| Variable | Purpose |
|---|---|
| `TAVILY_API_KEY` | Tavily Extract |
| `JINA_API_KEY` | Jina Reader |
| `ZAI_API_KEY` | Z.AI Reader fallback |
| `PROXY_URL` | Optional compatible proxy reader |

All are optional. Configure only providers whose privacy and retention terms
you accept.

## PLAUD

| Variable | Default | Purpose |
|---|---:|---|
| `PLAUD_BEARER_TOKEN` | empty | Enables PLAUD recording import |
| `PLAUD_REGION` | `api` | `api` or `api-euc1` |

The PLAUD systemd timer is enabled only when the token is present during unit
installation.

## Search and compatible model gateway

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | API key for the compatible gateway |
| `BASE_URL` | OpenAI-compatible API base URL |
| `MODEL` | Recall-planner model |
| `EMB_MODEL` | Remote QMD embedding model; empty uses local `Qwen3-Embedding-0.6B` |
| `RERANK_MODEL` | QMD reranking model; empty uses local `Qwen3-Reranker-0.6B` |

Empty `EMB_MODEL` and `RERANK_MODEL` values keep the mode fully local and do
not require `OPENAI_API_KEY` or `BASE_URL`. The local models are downloaded
from Hugging Face on first use and run on the CPU by default. Do not assume
that a remote OpenAI-compatible endpoint has the same privacy properties as
OpenAI.

## Encrypted backups

| Variable | Default | Purpose |
|---|---:|---|
| `VAULT_BACKUP_DIR` | `./.vault-backups` | Encrypted snapshot directory |
| `VAULT_BACKUP_GPG_RECIPIENT` | empty | GPG recipient; empty disables snapshots |
| `VAULT_BACKUP_RETENTION` | `14` | Number of snapshots retained |

The backup directory must remain outside `VAULT_PATH`. Keep the private
decryption key off the application host. See
[Backup and restore](backup-and-restore.md).

## Gemini authentication variables

Gemini authentication may use `GOOGLE_API_KEY`, `GEMINI_API_KEY`,
`GOOGLE_APPLICATION_CREDENTIALS`, or `GOOGLE_GENAI_USE_VERTEXAI`. The other AI
CLIs use their own login commands.

## Validation

Run diagnostics after every configuration change:

```bash
a-second-brain doctor
```

The command reports whether a value is present, but never prints secret
values.
