"""Encrypted point-in-time snapshots for full-vault rollback."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

GPG_TIMEOUT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class VaultBackupResult:
    """One completed encrypted snapshot and its retention effects."""

    snapshot_path: str
    checksum_path: str
    sha256: str
    pruned: tuple[str, ...]


class EncryptedVaultBackupService:
    """Create an encrypted tar snapshot before write-heavy vault processing."""

    def __init__(
        self,
        *,
        vault_path: Path | str,
        backup_dir: Path | str,
        gpg_recipient: str,
        retention: int = 14,
        gpg_binary: str = "gpg",
    ) -> None:
        self.vault_path = Path(vault_path).resolve()
        self.backup_dir = Path(backup_dir).resolve()
        self.gpg_recipient = str(gpg_recipient or "").strip()
        self.retention = max(1, int(retention))
        self.gpg_binary = gpg_binary

    @property
    def enabled(self) -> bool:
        """Whether a public-key recipient was explicitly configured."""
        return bool(self.gpg_recipient)

    def create_snapshot(self, *, now: datetime | None = None) -> VaultBackupResult:
        """Create, validate, checksum, and rotate one encrypted vault snapshot."""

        if not self.enabled:
            raise ValueError("VAULT_BACKUP_GPG_RECIPIENT is empty")
        if not self.vault_path.is_dir():
            raise FileNotFoundError(f"Vault directory not found: {self.vault_path}")
        if self.backup_dir.is_relative_to(self.vault_path):
            raise ValueError("VAULT_BACKUP_DIR must be outside VAULT_PATH")

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.backup_dir, 0o700)
        self._verify_recipient()

        captured_at = (now or datetime.now(UTC)).astimezone(UTC)
        snapshot_path = self._unique_snapshot_path(captured_at)
        partial_path = Path(f"{snapshot_path}.partial")
        plaintext_path = self._create_plaintext_archive()
        try:
            self._encrypt_archive(plaintext_path, partial_path)
            self._verify_encrypted_archive(partial_path)
            if not partial_path.exists() or partial_path.stat().st_size == 0:
                raise RuntimeError("GPG produced an empty vault snapshot")
            os.chmod(partial_path, 0o600)
            os.replace(partial_path, snapshot_path)
        finally:
            plaintext_path.unlink(missing_ok=True)
            partial_path.unlink(missing_ok=True)

        digest = self._sha256(snapshot_path)
        checksum_path = Path(f"{snapshot_path}.sha256")
        checksum_path.write_text(
            f"{digest}  {snapshot_path.name}\n",
            encoding="utf-8",
        )
        os.chmod(checksum_path, 0o600)
        pruned = self._prune_old_snapshots()
        return VaultBackupResult(
            snapshot_path=str(snapshot_path),
            checksum_path=str(checksum_path),
            sha256=digest,
            pruned=pruned,
        )

    def _create_plaintext_archive(self) -> Path:
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=".vault-snapshot-",
            suffix=".tar.gz",
            dir=self.backup_dir,
        )
        os.close(file_descriptor)
        archive_path = Path(temp_name)
        os.chmod(archive_path, 0o600)
        try:
            with tarfile.open(archive_path, "w:gz", dereference=False) as archive:
                archive.add(self.vault_path, arcname="vault", recursive=True)
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        return archive_path

    def _verify_recipient(self) -> None:
        self._run_gpg(
            [
                self.gpg_binary,
                "--batch",
                "--list-keys",
                "--",
                self.gpg_recipient,
            ],
            operation="GPG recipient lookup",
        )

    def _encrypt_archive(self, source: Path, target: Path) -> None:
        self._run_gpg(
            [
                self.gpg_binary,
                "--batch",
                "--yes",
                "--trust-model",
                "always",
                "--recipient",
                self.gpg_recipient,
                "--output",
                str(target),
                "--encrypt",
                str(source),
            ],
            operation="GPG vault encryption",
        )

    def _verify_encrypted_archive(self, path: Path) -> None:
        self._run_gpg(
            [self.gpg_binary, "--batch", "--list-packets", str(path)],
            operation="GPG snapshot validation",
        )

    @staticmethod
    def _run_gpg(command: list[str], *, operation: str) -> None:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=GPG_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("gpg executable is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{operation} timed out") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"{operation} failed: {detail}")

    def _unique_snapshot_path(self, captured_at: datetime) -> Path:
        stem = captured_at.strftime("vault-%Y%m%dT%H%M%SZ")
        path = self.backup_dir / f"{stem}.tar.gz.gpg"
        counter = 1
        while path.exists():
            path = self.backup_dir / f"{stem}-{counter}.tar.gz.gpg"
            counter += 1
        return path

    def _prune_old_snapshots(self) -> tuple[str, ...]:
        snapshots = sorted(self.backup_dir.glob("vault-*.tar.gz.gpg"))
        excess = snapshots[: max(0, len(snapshots) - self.retention)]
        pruned: list[str] = []
        for path in excess:
            path.unlink()
            Path(f"{path}.sha256").unlink(missing_ok=True)
            pruned.append(path.name)
        return tuple(pruned)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
