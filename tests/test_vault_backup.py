from __future__ import annotations

import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from d_brain.services.vault_backup import EncryptedVaultBackupService


def test_encrypted_vault_backup_captures_full_vault_and_prunes_retention(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    backup_path = tmp_path / "backups"
    (vault_path / "daily").mkdir(parents=True)
    (vault_path / ".session").mkdir()
    (vault_path / "daily" / "2026-04-04.md").write_text(
        "# Daily\n",
        encoding="utf-8",
    )
    (vault_path / ".session" / "runtime.json").write_text(
        '{"state":"ignored-by-git"}\n',
        encoding="utf-8",
    )
    private_skill = vault_path / "skills/private/local-private-skill"
    private_skill.mkdir(parents=True)
    (private_skill / "SKILL.md").write_text(
        "# Project planning\n",
        encoding="utf-8",
    )
    backup_path.mkdir()
    for index in range(2):
        old = backup_path / f"vault-2026040{index + 1}T000000Z.tar.gz.gpg"
        old.write_bytes(b"old")
        old.with_suffix(old.suffix + ".sha256").write_text(
            "old\n",
            encoding="utf-8",
        )

    service = EncryptedVaultBackupService(
        vault_path=vault_path,
        backup_dir=backup_path,
        gpg_recipient="backup@example.com",
        retention=2,
    )
    monkeypatch.setattr(service, "_verify_recipient", lambda: None)
    monkeypatch.setattr(
        service,
        "_encrypt_archive",
        lambda source, target: shutil.copyfile(source, target),
    )
    monkeypatch.setattr(service, "_verify_encrypted_archive", lambda path: None)

    result = service.create_snapshot(now=datetime(2026, 4, 4, 12, 30, 45, tzinfo=UTC))

    snapshot = Path(result.snapshot_path)
    assert snapshot.exists()
    assert Path(result.checksum_path).exists()
    assert len(list(backup_path.glob("vault-*.tar.gz.gpg"))) == 2
    assert result.pruned == ("vault-20260401T000000Z.tar.gz.gpg",)
    with tarfile.open(snapshot, "r:gz") as archive:
        names = archive.getnames()
    assert "vault/daily/2026-04-04.md" in names
    assert "vault/.session/runtime.json" in names
    assert "vault/skills/private/local-private-skill/SKILL.md" in names
    assert "vault/skills/local-private-skill" not in names


def test_vault_backup_is_disabled_without_recipient(tmp_path: Path) -> None:
    service = EncryptedVaultBackupService(
        vault_path=tmp_path / "vault",
        backup_dir=tmp_path / "backups",
        gpg_recipient="",
    )

    assert service.enabled is False


def test_vault_backup_removes_plaintext_when_encryption_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "private.md").write_text("secret\n", encoding="utf-8")
    backup_path = tmp_path / "backups"
    service = EncryptedVaultBackupService(
        vault_path=vault_path,
        backup_dir=backup_path,
        gpg_recipient="backup@example.com",
    )
    monkeypatch.setattr(service, "_verify_recipient", lambda: None)
    monkeypatch.setattr(
        service,
        "_encrypt_archive",
        lambda source, target: (_ for _ in ()).throw(RuntimeError("gpg failed")),
    )

    with pytest.raises(RuntimeError, match="gpg failed"):
        service.create_snapshot()

    assert list(backup_path.iterdir()) == []
