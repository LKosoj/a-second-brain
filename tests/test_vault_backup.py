from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from d_brain.run_vault_backup import main as backup_main
from d_brain.services import vault_backup
from d_brain.services.vault_backup import (
    EncryptedVaultBackupService,
    VaultBackupVerifyError,
)

_FAKE_GPG = """#!{python}
import os
import sys
import time

args = sys.argv[1:]
mode = os.environ.get("FAKE_GPG_MODE", "")
log_path = os.environ.get("FAKE_GPG_LOG")
watch_dir = os.environ.get("FAKE_GPG_WATCH_DIR")
keyid = os.environ.get("FAKE_GPG_KEYID", "FEEDFACE00000001")


def log(line):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")


def listing():
    if watch_dir:
        log("ls " + " ".join(sorted(os.listdir(watch_dir))))


if "--list-keys" in args:
    print("fpr:::::::::" + "0" * 24 + "FEEDFACE00000001:")
elif "--list-secret-keys" in args:
    sys.exit(0 if os.environ.get("FAKE_GPG_SECRET") == "1" else 2)
elif "--list-packets" in args:
    print(":pubkey enc packet: version 3, algo 18, keyid " + keyid)
elif "--encrypt" in args:
    log("pid %d" % os.getpid())
    listing()
    if mode == "stall":
        time.sleep(60)
    if mode == "die":
        sys.stdin.buffer.read(1024)
        sys.stderr.write("gpg: no public key\\n")
        sys.exit(2)
    while True:
        chunk = sys.stdin.buffer.read(65536)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
    listing()
    if mode == "linger":
        time.sleep(30)
elif "--decrypt" in args and os.environ.get("FAKE_GPG_DECRYPT") == "agent":
    sys.stderr.write("gpg: problem with the agent: Inappropriate ioctl for device\\n")
    sys.exit(2)
elif "--decrypt" in args and os.environ.get("FAKE_GPG_DECRYPT") == "broken":
    sys.stderr.write("gpg: decryption failed: Bad session key\\n")
    sys.exit(2)
elif "--decrypt" in args:
    with open(args[-1], "rb") as handle:
        sys.stdout.buffer.write(handle.read())
else:
    sys.exit(3)
"""


def _write_fake_gpg(directory: Path, name: str = "fake-gpg") -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(_FAKE_GPG.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _make_vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / ".session").mkdir()
    (vault / "daily" / "2026-04-04.md").write_text("# Daily\n", encoding="utf-8")
    (vault / ".session" / "runtime.json").write_text(
        '{"state":"ignored-by-git"}\n', encoding="utf-8"
    )
    private_skill = vault / "skills/private/local-private-skill"
    private_skill.mkdir(parents=True)
    (private_skill / "SKILL.md").write_text("# Project planning\n", encoding="utf-8")
    (vault / "blob.bin").write_bytes(os.urandom(20000))
    return vault


def _service(
    tmp_path: Path, *, retention: int = 14, vault: Path | None = None
) -> EncryptedVaultBackupService:
    return EncryptedVaultBackupService(
        vault_path=vault or _make_vault(tmp_path),
        backup_dir=tmp_path / "backups",
        gpg_recipient="backup@example.com",
        retention=retention,
        gpg_binary=_write_fake_gpg(tmp_path / "bin"),
    )


def _archive_names(snapshot: str | Path) -> list[str]:
    with tarfile.open(snapshot, "r:gz") as archive:
        return archive.getnames()


def _archive_file_count(snapshot: str | Path) -> int:
    with tarfile.open(snapshot, "r:gz") as archive:
        return sum(1 for member in archive if not member.isdir())


NOW = datetime(2026, 4, 4, 12, 30, 45, tzinfo=UTC)


def test_encrypted_vault_backup_captures_full_vault_and_prunes_retention(
    tmp_path: Path,
) -> None:
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    for index in range(2):
        old = backup_path / f"vault-2026040{index + 1}T000000Z.tar.gz.gpg"
        old.write_bytes(b"old")
        old.with_suffix(old.suffix + ".sha256").write_text("old\n", encoding="utf-8")
    service = _service(tmp_path, retention=2)

    result = service.create_snapshot(now=NOW)

    snapshot = Path(result.snapshot_path)
    assert snapshot.exists()
    assert Path(result.checksum_path).exists()
    assert len(list(backup_path.glob("vault-*.tar.gz.gpg"))) == 2
    assert result.pruned == ("vault-20260401T000000Z.tar.gz.gpg",)
    names = _archive_names(snapshot)
    assert "vault/daily/2026-04-04.md" in names
    assert "vault/.session/runtime.json" in names
    assert "vault/skills/private/local-private-skill/SKILL.md" in names
    assert "vault/skills/local-private-skill" not in names
    assert result.files == _archive_file_count(snapshot)
    assert result.skipped == 0


def test_vault_backup_is_disabled_without_recipient(tmp_path: Path) -> None:
    service = EncryptedVaultBackupService(
        vault_path=tmp_path / "vault",
        backup_dir=tmp_path / "backups",
        gpg_recipient="",
    )

    assert service.enabled is False


def test_vault_backup_leaves_nothing_when_encryption_fails(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setenv("FAKE_GPG_MODE", "die")

    with pytest.raises(RuntimeError, match="no public key"):
        service.create_snapshot()

    assert list((tmp_path / "backups").iterdir()) == []


def test_backup_never_writes_plaintext_archive_to_disk(
    tmp_path: Path, monkeypatch
) -> None:
    log = tmp_path / "gpg.log"
    monkeypatch.setenv("FAKE_GPG_LOG", str(log))
    monkeypatch.setenv("FAKE_GPG_WATCH_DIR", str(tmp_path / "backups"))
    service = _service(tmp_path)

    service.create_snapshot(now=NOW)

    listings = [
        line.split()[1:]
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("ls")
    ]
    assert listings
    for names in listings:
        assert all(name.endswith(".partial") for name in names), names
    final = sorted(path.name for path in (tmp_path / "backups").iterdir())
    stem = "vault-20260404T123045Z.tar.gz.gpg"
    assert final == sorted([stem, f"{stem}.manifest.json", f"{stem}.sha256"])


def test_backup_skips_write_stage_directories(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    (vault / "notes").mkdir()
    (vault / "notes" / "a.md").write_text("a\n", encoding="utf-8")
    stage = vault / "notes" / ".dbrain-write-1-ab.stage"
    (stage / "candidate-x").mkdir(parents=True)
    (stage / "candidate-x" / "a.md").write_text("half\n", encoding="utf-8")
    service = _service(tmp_path, vault=vault)

    result = service.create_snapshot(now=NOW)

    names = _archive_names(result.snapshot_path)
    assert "vault/notes/a.md" in names
    assert not [name for name in names if ".dbrain-write-" in name]


def test_backup_survives_file_vanishing_during_archive(
    tmp_path: Path, monkeypatch
) -> None:
    vault = _make_vault(tmp_path)
    (vault / "gone.md").write_text("gone\n", encoding="utf-8")
    original = tarfile.TarFile.add

    def flaky(self, name, arcname=None, recursive=True, *, filter=None):
        if arcname == "vault/gone.md":
            raise FileNotFoundError(name)
        return original(self, name, arcname, recursive, filter=filter)

    monkeypatch.setattr(tarfile.TarFile, "add", flaky)
    service = _service(tmp_path, vault=vault)

    result = service.create_snapshot(now=NOW)

    names = _archive_names(result.snapshot_path)
    assert "vault/daily/2026-04-04.md" in names
    assert "vault/gone.md" not in names
    assert result.skipped == 1
    assert result.files == _archive_file_count(result.snapshot_path)


def test_backup_gpg_dies_midstream_leaves_no_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    vault = _make_vault(tmp_path)
    (vault / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024))
    monkeypatch.setenv("FAKE_GPG_MODE", "die")
    service = _service(tmp_path, vault=vault)

    with pytest.raises(RuntimeError, match="no public key"):
        service.create_snapshot()

    assert list((tmp_path / "backups").iterdir()) == []


def test_backup_tar_error_kills_gpg_and_leaves_no_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    log = tmp_path / "gpg.log"
    monkeypatch.setenv("FAKE_GPG_LOG", str(log))
    monkeypatch.setenv("FAKE_GPG_MODE", "linger")
    original = tarfile.TarFile.add

    def denied(self, name, arcname=None, recursive=True, *, filter=None):
        if arcname == "vault/daily/2026-04-04.md":
            # Let the fake gpg start and log its pid before tar fails.
            for _ in range(100):
                if log.exists():
                    break
                time.sleep(0.05)
            raise OSError(5, "I/O error", name)
        return original(self, name, arcname, recursive, filter=filter)

    monkeypatch.setattr(tarfile.TarFile, "add", denied)
    service = _service(tmp_path)

    with pytest.raises(OSError):
        service.create_snapshot()

    assert list((tmp_path / "backups").iterdir()) == []
    pid = int(
        next(
            line.split()[1]
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.startswith("pid")
        )
    )
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_backup_removes_legacy_plaintext_and_stale_partial(tmp_path: Path) -> None:
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    legacy = backup_path / ".vault-snapshot-old.tar.gz"
    legacy.write_bytes(b"plain vault")
    os.utime(legacy, (time.time() - 7200, time.time() - 7200))
    stale = backup_path / "vault-20260101T000000Z.tar.gz.gpg.partial"
    stale.write_bytes(b"x")
    old = datetime.now(UTC).timestamp() - 7200
    os.utime(stale, (old, old))
    fresh = backup_path / "vault-20260102T000000Z.tar.gz.gpg.partial"
    fresh.write_bytes(b"x")
    service = _service(tmp_path)

    service.create_snapshot(now=NOW)

    assert not legacy.exists()
    assert not stale.exists()
    assert fresh.exists()


def test_backup_removes_orphan_sidecars_and_stale_atomic_temp_files(
    tmp_path: Path,
) -> None:
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    old = time.time() - 7200
    snap = "vault-20260101T000000Z.tar.gz.gpg"
    stale = [
        backup_path / f"{snap}.sha256",
        backup_path / f"{snap}.manifest.json",
        backup_path / f".{snap}.sha256.123.abcdef012345.tmp",
    ]
    fresh_orphan = backup_path / "vault-20260102T000000Z.tar.gz.gpg.sha256"
    fresh_tmp = backup_path / f".{snap}.manifest.json.1.abcdef012345.tmp"
    foreign = backup_path / ".vault-notes.tmp"
    kept_snap = backup_path / "vault-20260103T000000Z.tar.gz.gpg"
    kept_sidecar = backup_path / f"{kept_snap.name}.sha256"
    for path in (*stale, fresh_orphan, fresh_tmp, foreign, kept_snap, kept_sidecar):
        path.write_bytes(b"x")
    for path in (*stale, foreign, kept_snap, kept_sidecar):
        os.utime(path, (old, old))
    service = _service(tmp_path)

    service.create_snapshot(now=NOW)

    assert not any(path.exists() for path in stale)
    assert fresh_orphan.exists()
    assert fresh_tmp.exists()
    assert foreign.exists()
    assert kept_snap.exists()
    assert kept_sidecar.exists()


def test_backup_keeps_recent_legacy_plaintext_of_parallel_run(
    tmp_path: Path,
) -> None:
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    recent = backup_path / ".vault-snapshot-live.tar.gz"
    recent.write_bytes(b"plain vault")
    old = backup_path / ".vault-snapshot-old.tar.gz"
    old.write_bytes(b"plain vault")
    past = datetime.now(UTC).timestamp() - 7200
    os.utime(old, (past, past))
    service = _service(tmp_path)

    service.create_snapshot(now=NOW)

    assert recent.exists()
    assert not old.exists()


def test_backup_does_not_delete_foreign_partial_on_exclusive_open_failure(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    foreign = tmp_path / "backups" / "vault-20260404T123045Z.tar.gz.gpg.partial"
    real_unique = service._unique_snapshot_path

    def racy(captured_at):
        # The partial appears after the free name was chosen.
        path = real_unique(captured_at)
        Path(f"{path}.partial").write_bytes(b"other run")
        return path

    monkeypatch.setattr(service, "_unique_snapshot_path", racy)

    with pytest.raises(FileExistsError):
        service.create_snapshot(now=NOW)

    assert foreign.read_bytes() == b"other run"


def test_backup_encryption_deadline_kills_stalled_gpg(
    tmp_path: Path, monkeypatch
) -> None:
    vault = _make_vault(tmp_path)
    (vault / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024))
    log = tmp_path / "gpg.log"
    monkeypatch.setenv("FAKE_GPG_LOG", str(log))
    monkeypatch.setenv("FAKE_GPG_MODE", "stall")
    monkeypatch.setattr(vault_backup, "GPG_ENCRYPT_DEADLINE_SECONDS", 1)
    service = _service(tmp_path, vault=vault)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        service.create_snapshot()

    assert time.monotonic() - started < 30
    assert list((tmp_path / "backups").iterdir()) == []
    pid = int(log.read_text(encoding="utf-8").split()[1])
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_backup_counts_unreadable_files_and_keeps_going(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    vault = _make_vault(tmp_path)
    (vault / "locked.md").write_text("secret\n", encoding="utf-8")
    original = tarfile.TarFile.add

    def locked(self, name, arcname=None, recursive=True, *, filter=None):
        if arcname == "vault/locked.md":
            raise PermissionError(13, "Permission denied", name)
        return original(self, name, arcname, recursive, filter=filter)

    monkeypatch.setattr(tarfile.TarFile, "add", locked)
    service = _service(tmp_path, vault=vault)

    with caplog.at_level("WARNING"):
        result = service.create_snapshot(now=NOW)

    assert "vault/locked.md" not in _archive_names(result.snapshot_path)
    assert result.unreadable == 1
    assert result.skipped == 0
    assert "locked.md" in caplog.text
    manifest = json.loads(
        Path(f"{result.snapshot_path}.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["unreadable"] == 1


def test_prune_keeps_newer_snapshot_taken_in_the_same_second(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, retention=1)
    first = service.create_snapshot(now=NOW)
    os.utime(first.snapshot_path, (1_000_000_000, 1_000_000_000))
    second = service.create_snapshot(now=NOW)

    assert second.snapshot_path.endswith("Z-1.tar.gz.gpg")
    assert second.pruned == (Path(first.snapshot_path).name,)
    assert Path(second.snapshot_path).exists()


def test_prune_removes_manifest_with_snapshot(tmp_path: Path) -> None:
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    old = backup_path / "vault-20260401T000000Z.tar.gz.gpg"
    old.write_bytes(b"old")
    Path(f"{old}.sha256").write_text("old\n", encoding="utf-8")
    Path(f"{old}.manifest.json").write_text("{}", encoding="utf-8")
    service = _service(tmp_path, retention=1)

    service.create_snapshot(now=NOW)

    assert not old.exists()
    assert not Path(f"{old}.manifest.json").exists()
    assert not Path(f"{old}.sha256").exists()


def test_manifest_has_counts_only(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.create_snapshot(now=NOW)

    manifest_path = Path(f"{result.snapshot_path}.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["files"] == result.files
    assert manifest["skipped"] == 0
    assert "vault/" not in manifest_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


def test_verify_detects_corrupted_snapshot(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = service.create_snapshot(now=NOW)
    snapshot = Path(result.snapshot_path)
    data = bytearray(snapshot.read_bytes())
    data[20:24] = b"\x00\x01\x02\x03"
    snapshot.write_bytes(bytes(data))

    with pytest.raises(VaultBackupVerifyError, match="SHA-256"):
        service.verify_latest_snapshot()


def test_verify_with_secret_key_decrypts_and_compares_file_count(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_GPG_SECRET", "1")
    service = _service(tmp_path)
    result = service.create_snapshot(now=NOW)
    snapshot = Path(result.snapshot_path)

    verified = service.verify_latest_snapshot()
    assert verified.decrypted is True
    assert verified.files == result.files
    assert verified.warnings == ()

    manifest_path = Path(f"{snapshot}.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] += 5
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(VaultBackupVerifyError, match="file count"):
        service.verify_latest_snapshot()

    manifest["files"] -= 5
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    data = snapshot.read_bytes()
    snapshot.write_bytes(data[: len(data) // 2])
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    Path(f"{snapshot}.sha256").write_text(
        f"{digest}  {snapshot.name}\n", encoding="utf-8"
    )
    with pytest.raises(VaultBackupVerifyError):
        service.verify_latest_snapshot()


def test_verify_without_secret_key_checks_checksum_and_recipient(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_GPG_SECRET", "0")
    service = _service(tmp_path)
    service.create_snapshot(now=NOW)

    verified = service.verify_latest_snapshot()
    assert verified.decrypted is False
    assert any("decryption not verified" in item for item in verified.warnings)

    monkeypatch.setenv("FAKE_GPG_KEYID", "DEADBEEFDEADBEEF")
    with pytest.raises(
        VaultBackupVerifyError, match="not encrypted to the configured recipient"
    ):
        service.verify_latest_snapshot()


def test_verify_ignores_hidden_recipient_key_id(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_GPG_SECRET", "0")
    service = _service(tmp_path)
    service.create_snapshot(now=NOW)

    monkeypatch.setenv("FAKE_GPG_KEYID", "0000000000000000")
    verified = service.verify_latest_snapshot()

    assert any("hidden" in item for item in verified.warnings)


def test_verify_unreadable_secret_key_is_warning_not_error(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_GPG_SECRET", "1")
    service = _service(tmp_path)
    service.create_snapshot(now=NOW)

    monkeypatch.setenv("FAKE_GPG_DECRYPT", "agent")
    verified = service.verify_latest_snapshot()

    assert verified.decrypted is False
    assert any("decryption not verified" in item for item in verified.warnings)
    assert not any("no secret key" in item for item in verified.warnings)


def test_verify_decrypt_error_includes_gpg_stderr(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_GPG_SECRET", "1")
    service = _service(tmp_path)
    service.create_snapshot(now=NOW)

    monkeypatch.setenv("FAKE_GPG_DECRYPT", "broken")
    with pytest.raises(VaultBackupVerifyError, match="Bad session key"):
        service.verify_latest_snapshot()


def test_verify_without_manifest_warns_and_picks_latest_by_mtime(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_GPG_SECRET", "1")
    service = _service(tmp_path)
    first = service.create_snapshot(now=NOW)
    second = service.create_snapshot(now=NOW)
    assert second.snapshot_path.endswith("Z-1.tar.gz.gpg")
    for result in (first, second):
        Path(f"{result.snapshot_path}.manifest.json").unlink()
    os.utime(first.snapshot_path, (2_000_000_000, 2_000_000_000))
    os.utime(second.snapshot_path, (1_900_000_000, 1_900_000_000))

    verified = service.verify_latest_snapshot()

    assert verified.snapshot_path == first.snapshot_path
    assert any("manifest" in item for item in verified.warnings)


def test_verify_fails_when_no_snapshots(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(VaultBackupVerifyError, match="no snapshots"):
        service.verify_latest_snapshot()


def test_run_vault_backup_verify_cli(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = _make_vault(tmp_path)
    _write_fake_gpg(tmp_path / "bin", name="gpg")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("VAULT_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("VAULT_BACKUP_GPG_RECIPIENT", "backup@example.com")
    monkeypatch.setenv("FAKE_GPG_SECRET", "1")

    assert backup_main([]) == 0
    assert backup_main(["--verify"]) == 0
    assert "Decrypted: yes" in capsys.readouterr().out

    snapshot = next((tmp_path / "backups").glob("vault-*.tar.gz.gpg"))
    snapshot.write_bytes(b"corrupted")
    assert backup_main(["--verify"]) == 1
    assert "SHA-256" in capsys.readouterr().err

    monkeypatch.setenv("VAULT_BACKUP_GPG_RECIPIENT", "")
    assert backup_main(["--verify"]) == 0
    assert "skipped" in capsys.readouterr().out


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_backup_counts_unreadable_directory_and_keeps_going(
    tmp_path: Path, caplog
) -> None:
    vault = _make_vault(tmp_path)
    locked = vault / "locked-dir"
    locked.mkdir()
    (locked / "inside.md").write_text("secret\n", encoding="utf-8")
    locked.chmod(0o000)
    service = _service(tmp_path, vault=vault)
    try:
        with caplog.at_level("WARNING"):
            result = service.create_snapshot(now=NOW)
    finally:
        locked.chmod(0o700)

    assert result.unreadable == 1
    assert "vault/locked-dir/inside.md" not in _archive_names(result.snapshot_path)
    assert "locked-dir" in caplog.text


def test_backup_sidecars_exist_before_snapshot_is_published(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    seen: dict[str, bool] = {}
    original = os.replace

    def spy(source, destination, **kwargs):
        if str(destination).endswith(".tar.gz.gpg"):
            seen["checksum"] = Path(f"{destination}.sha256").exists()
            seen["manifest"] = Path(f"{destination}.manifest.json").exists()
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    result = service.create_snapshot(now=NOW)

    assert seen == {"checksum": True, "manifest": True}
    assert service.verify_latest_snapshot().sha256 == result.sha256
    for suffix in (".sha256", ".manifest.json"):
        mode = stat.S_IMODE(Path(f"{result.snapshot_path}{suffix}").stat().st_mode)
        assert mode == 0o600


def test_backup_failed_publish_leaves_no_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    original = os.replace

    def broken(source, destination, **kwargs):
        if str(destination).endswith(".tar.gz.gpg"):
            raise OSError("disk gone")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        service.create_snapshot(now=NOW)

    assert list((tmp_path / "backups").iterdir()) == []
