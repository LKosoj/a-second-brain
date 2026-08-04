# Operations

[Русский](../ru/operations.md) | [Documentation index](../index.md)

Run A Second Brain as a dedicated unprivileged Linux account. The supplied
deployment uses systemd user units and never creates a root-owned service.
Commands on this page assume a source checkout and a working systemd user
manager. A wheel-only instance does not contain the supplied unit installer.

## Foreground validation

Before enabling services:

```bash
cd /path/to/a-second-brain
uv run --frozen --no-dev a-second-brain doctor --smoke
uv run --frozen --no-dev a-second-brain run
```

Confirm that `/menu` works from the owner Telegram account, then stop the
foreground process.

## Install systemd user units

Render units without enabling them:

```bash
./scripts/install-systemd-user.sh
```

Review files under `~/.config/systemd/user/`. Enable the standard units only
after review:

```bash
./scripts/install-systemd-user.sh --enable
```

The enable step runs `doctor` first.

## Supplied units

| Unit | Purpose |
|---|---|
| `a-second-brain.service` | Telegram bot |
| `a-second-brain-process.timer` | Daily processing at 21:00 |
| `a-second-brain-plaud-sync.timer` | Hourly PLAUD sync when configured |
| `a-second-brain-qmd-maintenance.timer` | Weekly QMD cleanup when `qmd` exists |

The daily processing service runs an encrypted backup preflight before
write-heavy processing. With no GPG recipient configured, that preflight
reports a skip and exits successfully.

## Service control

```bash
systemctl --user status a-second-brain.service
systemctl --user restart a-second-brain.service
systemctl --user stop a-second-brain.service
systemctl --user start a-second-brain.service
```

Timer inspection:

```bash
systemctl --user list-timers 'a-second-brain-*'
systemctl --user status a-second-brain-process.timer
```

## Logs

The application writes to stdout and stderr. Under systemd, inspect the
journal:

```bash
journalctl --user -u a-second-brain.service -n 200 --no-pager
journalctl --user -u a-second-brain.service -f
journalctl --user -u a-second-brain-process.service --since today
```

Do not paste logs publicly before checking them for captured text, paths,
Telegram identifiers, and provider errors.

## Update a source checkout

Stop write-heavy work before a significant update:

```bash
for unit in \
  a-second-brain-process.timer \
  a-second-brain-plaud-sync.timer \
  a-second-brain-qmd-maintenance.timer \
  a-second-brain.service \
  a-second-brain-process.service \
  a-second-brain-plaud-sync.service \
  a-second-brain-qmd-maintenance.service
do
  systemctl --user stop "$unit" 2>/dev/null || true
done
git pull --ff-only
uv sync --frozen --no-dev
uv run --frozen --no-dev a-second-brain doctor
./scripts/install-systemd-user.sh --enable
```

If deployment templates changed, the installer performs `daemon-reload`.
Review release notes and create a verified backup before migrations.

## Run maintenance manually

```bash
uv run --frozen --no-dev python -m d_brain.run_daily_process --mode scheduled
uv run --frozen --no-dev python -m d_brain.run_plaud_sync
uv run --frozen --no-dev a-second-brain qmd cleanup
uv run --frozen --no-dev python -m d_brain.run_vault_backup
```

Manual commands use the same `.env` and project working directory as the
services.

## Run after logout

User services normally stop when the account has no session. An administrator
may enable lingering for the dedicated account:

```bash
sudo loginctl enable-linger <service-account>
```

This is a host-level policy decision and is intentionally outside the project
installer.
