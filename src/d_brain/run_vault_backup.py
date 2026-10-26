"""CLI entrypoint for encrypted pre-write vault snapshots."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from d_brain.services.vault_backup import (
    EncryptedVaultBackupService,
    VaultBackupVerifyError,
)


def _service_from_env(recipient: str) -> EncryptedVaultBackupService:
    return EncryptedVaultBackupService(
        vault_path=Path(os.environ.get("VAULT_PATH", "./vault")),
        backup_dir=Path(os.environ.get("VAULT_BACKUP_DIR", "./.vault-backups")),
        gpg_recipient=recipient,
        retention=int(os.environ.get("VAULT_BACKUP_RETENTION", "14")),
    )


def main(argv: list[str] | None = None) -> int:
    """Create one configured snapshot (or verify the latest with --verify)."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check the latest snapshot instead of creating a new one",
    )
    args = parser.parse_args(argv)

    recipient = os.environ.get("VAULT_BACKUP_GPG_RECIPIENT", "").strip()
    if not recipient:
        if args.verify:
            print(
                "Vault backup verification skipped: "
                "VAULT_BACKUP_GPG_RECIPIENT is empty"
            )
        else:
            print("Vault backup skipped: VAULT_BACKUP_GPG_RECIPIENT is empty")
        return 0

    service = _service_from_env(recipient)
    if args.verify:
        try:
            verified = service.verify_latest_snapshot()
        except VaultBackupVerifyError as exc:
            print(f"Vault backup verification failed: {exc}", file=sys.stderr)
            return 1
        print(f"Verified snapshot: {verified.snapshot_path}")
        print(f"Decrypted: {'yes' if verified.decrypted else 'no'}")
        if verified.files is not None:
            print(f"Files: {verified.files}")
        for warning in verified.warnings:
            print(f"Warning: {warning}")
        return 0

    result = service.create_snapshot()
    print(f"Encrypted vault snapshot: {result.snapshot_path}")
    print(f"SHA-256: {result.sha256}")
    if result.skipped:
        print(f"Files: {result.files} (skipped {result.skipped} vanished)")
    if result.unreadable:
        print(f"Warning: skipped {result.unreadable} unreadable files (permissions)")
    if result.pruned:
        print(f"Pruned old snapshots: {', '.join(result.pruned)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
