# Getting started

[Русский](../ru/getting-started.md) | [Documentation index](../index.md)

This guide installs A Second Brain without putting private vault data into the
public code repository.

## Requirements

- Linux and Python 3.12 or newer
- `uv` and `jq`
- a Telegram bot token from BotFather
- a Deepgram API key for voice transcription
- one supported AI CLI: Claude Code, Codex CLI, Qwen Code, or Gemini CLI
- an unprivileged Linux account

Optional integrations add their own requirements. See
[Integrations](integrations.md).

## Install from a source checkout

Clone the future public repository into a directory owned by the service
account:

```bash
git clone <PUBLIC_REPOSITORY_URL> a-second-brain
cd a-second-brain
./install.sh
```

The installer:

1. refuses to run as root;
2. installs locked runtime dependencies without the development group;
3. creates the private vault only when `vault/` does not exist;
4. creates `.env` with mode `0600`.

It does not start a service or publish any data.

## Install from a wheel

Download a wheel from a project release, or build one from a source checkout
with `uv build`. Install it into a Python environment, then create a private
instance:

```bash
python -m pip install a_second_brain-0.1.0-py3-none-any.whl
a-second-brain init /srv/a-second-brain-instance
cd /srv/a-second-brain-instance
```

The initializer creates `.env`, `.env.example`, `.gitignore`,
`mcp-config.json`, `vault-manifest.json`, and an anonymized starter `vault/`.
It refuses to overwrite an existing vault.

## Configure the minimum settings

Edit `.env` and set the three required values:

```dotenv
TELEGRAM_BOT_TOKEN=
DEEPGRAM_API_KEY=
OWNER_TELEGRAM_ID=
```

`OWNER_TELEGRAM_ID` is the only Telegram account allowed to use the bot. Use a
positive numeric Telegram user ID.

`AI_CLI` defaults to `claude`. Set it only to select another supported backend:

```dotenv
AI_CLI=codex
```

Authenticate the selected AI CLI separately. For example:

```bash
codex login
```

See [Configuration](configuration.md) for every setting.

## Validate the installation

From a source checkout, run the package-native diagnostics through the locked
environment:

```bash
uv run --frozen --no-dev a-second-brain doctor
```

When the package is installed in the active Python environment, use the
executable directly:

```bash
a-second-brain doctor
```

From another directory, pass the instance explicitly:

```bash
a-second-brain doctor /srv/a-second-brain-instance
```

Add `--smoke` to make one short request through the selected AI CLI:

```bash
a-second-brain doctor --smoke
```

Fix every `ERR` before starting the bot. `WARN` entries do not change the exit
code, but should be reviewed.

## First foreground run

From a source checkout:

```bash
uv run --frozen --no-dev a-second-brain run
```

From an active environment containing the installed wheel:

```bash
a-second-brain run
```

Send `/menu` to the bot from the owner account. Requests from other Telegram
accounts are rejected.

## Install user services

The supplied systemd templates and installer belong to a source checkout; they
are not included in an initialized wheel instance. After the source-checkout
foreground run succeeds:

```bash
./scripts/install-systemd-user.sh
./scripts/install-systemd-user.sh --enable
```

The first command only renders user units. The second runs `doctor`, then
enables the bot and scheduled processing. Continue with
[Operations](operations.md).

For a wheel-only deployment, keep using foreground mode or create a host-owned
service that invokes the absolute path to the environment's
`a-second-brain` executable with the instance as its working directory.
