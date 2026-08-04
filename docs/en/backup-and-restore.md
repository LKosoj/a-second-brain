# Backup and restore

[Русский](../ru/backup-and-restore.md) | [Documentation index](../index.md)

Scheduled write-heavy processing can create an encrypted full-vault snapshot
before changing data. Backups are optional but strongly recommended.

## Security model

- The application host stores only the recipient's public GPG key.
- The private decryption key stays on another trusted system.
- The backup directory is outside `VAULT_PATH`.
- Each encrypted archive has a neighboring SHA-256 checksum file.
- Rotation keeps the newest `VAULT_BACKUP_RETENTION` snapshots.

An encrypted archive still exposes metadata such as filename, timestamp, and
size. Protect the backup directory accordingly.

## Configure backups

Import the public recipient key into the service account's GPG keyring. Then
set:

```dotenv
VAULT_BACKUP_DIR=./.vault-backups
VAULT_BACKUP_GPG_RECIPIENT=backup@example.com
VAULT_BACKUP_RETENTION=14
```

Validate the path and command:

```bash
a-second-brain doctor
uv run --frozen --no-dev python -m d_brain.run_vault_backup
```

If `VAULT_BACKUP_GPG_RECIPIENT` is empty, the command reports that backup is
disabled and exits successfully. If a recipient is configured but encryption
fails, scheduled processing stops before changing the vault.

## Verify a snapshot

Work on a trusted machine with access to the encrypted file and checksum:

```bash
sha256sum --check vault-YYYYMMDDTHHMMSSZ.tar.gz.gpg.sha256
```

Do not continue if verification fails.

## Restore safely

Never extract directly over the live vault. Use a new temporary directory:

```bash
restore_dir="$(mktemp -d /tmp/a-second-brain-restore.XXXXXX)"
gpg --decrypt vault-YYYYMMDDTHHMMSSZ.tar.gz.gpg \
  | tar -xzf - -C "$restore_dir"
```

The restored vault is under `$restore_dir/vault`.

Before replacement:

1. stop the bot, processing, PLAUD, and QMD writer units;
2. compare the restored vault with the live vault;
3. preserve the current live vault as a separate rollback copy;
4. confirm ownership and restrictive permissions;
5. move the selected vault into place;
6. run `a-second-brain doctor`;
7. rebuild QMD and graph-derived state if required;
8. start the service and verify Telegram behavior.

The project intentionally provides no automatic destructive restore command.

## Test restoration

A backup is not proven until a restoration drill succeeds. Periodically
restore into an isolated directory, verify representative notes and
attachments, and confirm the private key is available to the authorized
operator.
