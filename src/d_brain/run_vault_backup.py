"""CLI entrypoint for encrypted pre-write vault snapshots."""

from __future__ import annotations

import os
from pathlib import Path

from d_brain.services.vault_backup import EncryptedVaultBackupService


def main() -> int:
    """Create one configured snapshot, or explicitly report disabled state."""

    recipient = os.environ.get("VAULT_BACKUP_GPG_RECIPIENT", "").strip()
    if not recipient:
        print("Vault backup skipped: VAULT_BACKUP_GPG_RECIPIENT is empty")
        return 0

    service = EncryptedVaultBackupService(
        vault_path=Path(os.environ.get("VAULT_PATH", "./vault")),
        backup_dir=Path(os.environ.get("VAULT_BACKUP_DIR", "./.vault-backups")),
        gpg_recipient=recipient,
        retention=int(os.environ.get("VAULT_BACKUP_RETENTION", "14")),
    )
    result = service.create_snapshot()
    print(f"Encrypted vault snapshot: {result.snapshot_path}")
    print(f"SHA-256: {result.sha256}")
    if result.pruned:
        print(f"Pruned old snapshots: {', '.join(result.pruned)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
