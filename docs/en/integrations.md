# Integrations

[Русский](../ru/integrations.md) | [Documentation index](../index.md)

Integrations are enabled by operator configuration. Keep credentials in
`.env`, review each provider's retention terms, and expose only the minimum
permissions required.

## AI CLI backends

Set one backend:

```dotenv
AI_CLI=claude
```

Supported values and common authentication commands:

| Backend | Authentication |
|---|---|
| Claude Code | `claude auth login` |
| Codex CLI | `codex login` |
| Qwen Code | `qwen auth qwen-oauth` |
| Gemini CLI | configure a supported Google/Gemini credential |

Validate installation and authentication:

```bash
a-second-brain doctor --smoke
```

Agent CLIs receive selected vault context for the requested workflow. Treat the
CLI vendor and its configured model gateway as processors of private data.

## Deepgram

Deepgram transcribes Telegram voice messages. Configure:

```dotenv
DEEPGRAM_API_KEY=
```

The key is required by the current runtime. Audio is sent to the configured
Deepgram service for transcription.

## Todoist

Todoist actions require:

- `TODOIST_API_KEY`;
- `mcp-cli`;
- `npx`;
- project-local `mcp-config.json`.

The configuration pins the Todoist MCP package version. Task creation is
allowed only by workflows that explicitly grant the action and establish owner
responsibility.

Leave `TODOIST_API_KEY` empty to disable Todoist. `doctor` then reports an
informational result rather than an error.

## QMD

[QMD](https://github.com/tobi/qmd) supplies local vault indexing and retrieval.
This project is tested with `@tobilu/qmd` 2.5.3. Install Node.js, then have an
administrator install the pinned CLI in the system prefix:

```bash
sudo npm install -g @tobilu/qmd@2.5.3
qmd --version
```

The default mode is fully local and does not require the OpenAI-compatible
variables below. When `EMB_MODEL` is empty, the project uses
`Qwen3-Embedding-0.6B`; when `RERANK_MODEL` is empty, QMD uses
`Qwen3-Reranker-0.6B`. The models are downloaded from Hugging Face during the
first embedding or query run and use the CPU by default. To use a remote
embedding provider, set `OPENAI_API_KEY` and `EMB_MODEL`; set `BASE_URL` for a
non-default compatible endpoint. `MODEL` belongs to the recall planner rather
than QMD indexing.

```dotenv
OPENAI_API_KEY=
BASE_URL=
MODEL=
EMB_MODEL=
RERANK_MODEL=
```

Initialize or rebuild the project-local index from a source checkout:

```bash
uv run --frozen --no-dev a-second-brain qmd update
uv run --frozen --no-dev a-second-brain qmd embed
uv run --frozen --no-dev a-second-brain qmd status
```

From an active environment containing the installed wheel:

```bash
a-second-brain qmd update
a-second-brain qmd embed
a-second-brain qmd status
```

The QMD index is derived data, not the source of truth. Vault Markdown remains
canonical. Weekly cleanup is enabled only when `qmd` is discoverable during
systemd unit installation. If QMD is removed later, disable its timer:

```bash
systemctl --user disable --now a-second-brain-qmd-maintenance.timer
```

## PLAUD

Configure recording import with:

```dotenv
PLAUD_REGION=api
PLAUD_BEARER_TOKEN=
```

Supported regions are `api` and `api-euc1`. The optional hourly timer is
enabled only when the token is present.

Removing the token does not disable an already enabled timer:

```bash
systemctl --user disable --now a-second-brain-plaud-sync.timer
```

Imported transcripts may contain personal and business-sensitive material.
They are stored under the private vault and must not enter the public code
repository.

## Web extraction

Optional extraction providers are configured through `TAVILY_API_KEY`,
`JINA_API_KEY`, `ZAI_API_KEY`, and `PROXY_URL`. The runtime can archive the
source and extracted text under the vault's import area.

## Encrypted backups

GPG is an external integration. Only the public recipient key belongs on the
application host. See [Backup and restore](backup-and-restore.md).
