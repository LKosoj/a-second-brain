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
- The archive is streamed straight into `gpg`; no unencrypted archive is ever
  written to disk. Leftover `.vault-snapshot-*` files from older versions are
  deleted automatically, as are `.partial` files older than one hour.
- Temporary write-staging directories (`.dbrain-write-*.stage`) and files that
  disappear while the archive is being written are skipped. Files and
  directories the service account cannot read (`PermissionError`) are skipped
  too: their number goes to the manifest as `unreadable`, the backup prints a
  warning and still exits with code 0.
- A neighboring `.manifest.json` holds only counts (files, skipped,
  unreadable), never file names.
- The checksum and the manifest are written before the snapshot is published,
  so a published snapshot always has both. After a hard stop, orphaned
  `.sha256` and `.manifest.json` files and their temporary write files
  (`.vault-*.tar.gz.gpg.*.tmp`) older than one hour are cleaned up on the
  next run. Other files in the backup directory are left alone.

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

## Weekly verification

`a-second-brain-backup-verify.timer` runs
`python -m d_brain.run_vault_backup --verify` every Sunday at 04:15. It checks
the newest snapshot (by modification time) in two levels:

- With the secret key on the host: the snapshot is decrypted as a stream
  (nothing is written to disk), the tar is read to the end, and the file count
  is compared with the manifest. The key must have no passphrase.
- Without the secret key (the intended setup): decryption is impossible, so
  the command checks the SHA-256, the OpenPGP structure, and that the archive
  is encrypted to the configured recipient. It exits successfully but prints
  `Warning: decryption not verified ...`. Snapshots made before the manifest
  existed produce a warning about the missing manifest.

A failed check exits non-zero, so the unit is marked failed. The verification
does not replace a real restoration drill on the machine that holds the
private key (see below). With an empty `VAULT_BACKUP_GPG_RECIPIENT` the
command does nothing.

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
