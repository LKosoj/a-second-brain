"""Encrypted point-in-time snapshots for full-vault rollback."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

from d_brain.services.atomic_io import atomic_write_file_text

logger = logging.getLogger(__name__)

GPG_TIMEOUT_SECONDS = 300
# Whole-encryption limit, including the tar stream written into gpg's stdin:
# a hung gpg blocks that write forever, so the pipe needs its own deadline.
GPG_ENCRYPT_DEADLINE_SECONDS = 4 * 3600
# Atomic-write staging directories (frontmatter.py) are temporary and vanish
# on commit, so a snapshot taken next to a running bot must not include them.
_STAGE_PATTERN = ".dbrain-write-*.stage"
# A .partial this old belongs to a killed run, not to a parallel manual one.
_STALE_PARTIAL_SECONDS = 3600
_PUBKEY_KEYID = re.compile(r"pubkey enc packet:.*?keyid ([0-9A-Fa-f]{16})")
# gpg prints an all-zero keyid for a hidden recipient (--throw-keyids).
_HIDDEN_KEYID = "0000000000000000"
# gpg stderr when the secret key exists but cannot be unlocked in batch mode.
_UNLOCK_FAILURE = re.compile(
    r"no passphrase given|pinentry|problem with the agent|inappropriate ioctl"
    r"|operation cancelled|bad passphrase",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class VaultBackupResult:
    """One completed encrypted snapshot and its retention effects."""

    snapshot_path: str
    checksum_path: str
    sha256: str
    pruned: tuple[str, ...]
    files: int = 0
    skipped: int = 0
    unreadable: int = 0


@dataclass(frozen=True, slots=True)
class VaultBackupVerifyResult:
    """Outcome of checking the latest snapshot."""

    snapshot_path: str
    sha256: str
    decrypted: bool
    files: int | None
    warnings: tuple[str, ...]


class VaultBackupVerifyError(RuntimeError):
    """The latest snapshot is missing, corrupted, or unreadable."""


@dataclass(slots=True)
class _ArchiveStats:
    files: int = 0
    skipped: int = 0
    unreadable: int = 0


class _DecryptionNotVerified(Exception):
    """The secret key exists but gpg cannot unlock it without a terminal."""


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
        self._remove_stale_temp_files()
        self._verify_recipient()

        captured_at = (now or datetime.now(UTC)).astimezone(UTC)
        snapshot_path = self._unique_snapshot_path(captured_at)
        partial_path = Path(f"{snapshot_path}.partial")
        # Outside the try: if O_EXCL fails the partial belongs to someone
        # else and the cleanup below must not delete it.
        descriptor = os.open(
            partial_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            stats = self._encrypt_vault_stream(descriptor)
            self._verify_encrypted_archive(partial_path)
            if not partial_path.exists() or partial_path.stat().st_size == 0:
                raise RuntimeError("GPG produced an empty vault snapshot")
            os.chmod(partial_path, 0o600)
            # Sidecars go first: a published snapshot must always have its
            # checksum, and the pruning and --verify look only at *.gpg.
            digest = self._sha256(partial_path)
            checksum_path = Path(f"{snapshot_path}.sha256")
            manifest_path = Path(f"{snapshot_path}.manifest.json")
            try:
                atomic_write_file_text(
                    checksum_path,
                    f"{digest}  {snapshot_path.name}\n",
                    new_file_mode=0o600,
                )
                # Counts only: names next to the encrypted archive would leak
                # the vault structure. Verification compares against this.
                atomic_write_file_text(
                    manifest_path,
                    json.dumps(
                        {
                            "version": 1,
                            "files": stats.files,
                            "skipped": stats.skipped,
                            "unreadable": stats.unreadable,
                            "created": captured_at.isoformat(),
                        }
                    )
                    + "\n",
                    new_file_mode=0o600,
                )
                os.replace(partial_path, snapshot_path)
            except BaseException:
                checksum_path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
                raise
        finally:
            partial_path.unlink(missing_ok=True)

        pruned = self._prune_old_snapshots()
        return VaultBackupResult(
            snapshot_path=str(snapshot_path),
            checksum_path=str(checksum_path),
            sha256=digest,
            pruned=pruned,
            files=stats.files,
            skipped=stats.skipped,
            unreadable=stats.unreadable,
        )

    def _write_archive(self, tar: tarfile.TarFile, stats: _ArchiveStats) -> None:
        """Add the vault entry by entry, tolerating files that vanish mid-walk."""

        def walk_error(exc: OSError) -> None:
            # A directory that vanished between listing and scanning is not
            # an error; an unreadable one is counted like an unreadable file.
            if isinstance(exc, FileNotFoundError | NotADirectoryError):
                return
            if not isinstance(exc, PermissionError):
                raise exc
            stats.unreadable += 1
            logger.warning(
                "Vault backup skipped unreadable directory: %s", exc.filename
            )

        def add(path: Path, arcname: str) -> None:
            seen: list[tarfile.TarInfo] = []

            def keep(info: tarfile.TarInfo) -> tarfile.TarInfo:
                seen.append(info)
                return info

            # TarFile.add stats and opens the file before writing the header,
            # so a vanished file leaves the stream intact.
            try:
                tar.add(path, arcname=arcname, recursive=False, filter=keep)
            except (FileNotFoundError, NotADirectoryError):
                stats.skipped += 1
                return
            except PermissionError:
                # Counted apart from vanished files so the gap stays visible
                # without failing the nightly run that waits on this backup.
                stats.unreadable += 1
                logger.warning("Vault backup skipped unreadable file: %s", arcname)
                return
            if seen and not seen[0].isdir():
                stats.files += 1

        add(self.vault_path, "vault")
        for root, dirnames, filenames in os.walk(
            self.vault_path, topdown=True, followlinks=False, onerror=walk_error
        ):
            dirnames[:] = sorted(
                name for name in dirnames if not fnmatch(name, _STAGE_PATTERN)
            )
            for name in [*dirnames, *sorted(filenames)]:
                path = Path(root) / name
                relative = path.relative_to(self.vault_path).as_posix()
                add(path, f"vault/{relative}")

    def _encrypt_vault_stream(self, descriptor: int) -> _ArchiveStats:
        """Pipe the tar stream straight into gpg; no plaintext archive on disk.

        Takes ownership of the already opened output descriptor.
        """

        command = [
            self.gpg_binary,
            "--batch",
            "--yes",
            "--trust-model",
            "always",
            "--recipient",
            self.gpg_recipient,
            "--encrypt",
        ]
        stats = _ArchiveStats()
        process: subprocess.Popen[bytes] | None = None
        timed_out = threading.Event()
        timer: threading.Timer | None = None
        # A temp file instead of PIPE: a full stderr pipe would block gpg
        # while we are blocked writing to its stdin.
        with tempfile.TemporaryFile() as stderr_file:

            def gpg_error(reason: str) -> RuntimeError:
                stderr_file.seek(0)
                detail = stderr_file.read().decode(errors="replace").strip()
                return RuntimeError(
                    f"GPG vault encryption {reason}: {detail or 'unknown error'}"
                )

            try:
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=descriptor,
                        stderr=stderr_file,
                    )
                except FileNotFoundError as exc:
                    raise RuntimeError("gpg executable is not available") from exc
                finally:
                    os.close(descriptor)
                assert process.stdin is not None

                def expire() -> None:
                    timed_out.set()
                    with contextlib.suppress(OSError):
                        process.kill()

                timer = threading.Timer(GPG_ENCRYPT_DEADLINE_SECONDS, expire)
                timer.daemon = True
                timer.start()
                timeout_error = RuntimeError("GPG vault encryption timed out")
                try:
                    with tarfile.open(
                        fileobj=process.stdin, mode="w|gz", dereference=False
                    ) as tar:
                        self._write_archive(tar, stats)
                    process.stdin.close()
                except BrokenPipeError as exc:
                    # gpg died early (no key, disk full) or was killed by the
                    # deadline; report why.
                    try:
                        process.wait(timeout=GPG_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        raise timeout_error from exc
                    if timed_out.is_set():
                        raise timeout_error from exc
                    raise gpg_error(f"failed (exit {process.returncode})") from exc
                try:
                    process.wait(timeout=GPG_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired as exc:
                    raise timeout_error from exc
                if timed_out.is_set():
                    raise timeout_error
                if process.returncode != 0:
                    raise gpg_error("failed")
            finally:
                if timer is not None:
                    timer.cancel()
                # On a tar error gpg must not see a clean EOF: it would
                # finish a truncated but valid-looking archive.
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    with contextlib.suppress(OSError):
                        if process.stdin is not None:
                            process.stdin.close()
        return stats

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

    def _verify_encrypted_archive(self, path: Path) -> None:
        self._run_gpg(
            [self.gpg_binary, "--batch", "--list-packets", str(path)],
            operation="GPG snapshot validation",
        )

    @staticmethod
    def _run_gpg(command: list[str], *, operation: str) -> str:
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
        return f"{result.stdout}\n{result.stderr}"

    def _unique_snapshot_path(self, captured_at: datetime) -> Path:
        stem = captured_at.strftime("vault-%Y%m%dT%H%M%SZ")
        path = self.backup_dir / f"{stem}.tar.gz.gpg"
        counter = 1
        while path.exists() or Path(f"{path}.partial").exists():
            path = self.backup_dir / f"{stem}-{counter}.tar.gz.gpg"
            counter += 1
        return path

    def _remove_stale_temp_files(self) -> None:
        cutoff = datetime.now(UTC).timestamp() - _STALE_PARTIAL_SECONDS
        # .vault-snapshot-* are plaintext archives left by the old
        # implementation; same age rule as .partial in case an old-version
        # run is still going.
        # A concurrent run may delete its own temp file between the checks
        # below; a vanished file must not abort this run's ExecStartPre.
        for pattern in (".vault-snapshot-*", "vault-*.tar.gz.gpg.partial"):
            for path in self.backup_dir.glob(pattern):
                if path.is_symlink() or not path.is_file():
                    continue
                with contextlib.suppress(FileNotFoundError):
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
        # A SIGKILL between writing the sidecars and publishing the snapshot
        # leaves them without a .gpg; atomic_io temp files are
        # .<name>.<pid>.<hex>.tmp.
        orphan_patterns = (
            "vault-*.tar.gz.gpg.sha256",
            "vault-*.tar.gz.gpg.manifest.json",
        )
        for pattern in orphan_patterns:
            for path in self.backup_dir.glob(pattern):
                snapshot = path.name.rsplit(".tar.gz.gpg", 1)[0] + ".tar.gz.gpg"
                if (self.backup_dir / snapshot).exists():
                    continue
                self._unlink_if_stale(path, cutoff)
        for path in self.backup_dir.glob(".vault-*.tar.gz.gpg.*.tmp"):
            self._unlink_if_stale(path, cutoff)

    @staticmethod
    def _unlink_if_stale(path: Path, cutoff: float) -> None:
        if path.is_symlink() or not path.is_file():
            return
        with contextlib.suppress(FileNotFoundError):
            if path.stat().st_mtime < cutoff:
                path.unlink()

    def _prune_old_snapshots(self) -> tuple[str, ...]:
        # By mtime: "...Z-1" sorts before "...Z" by name, so a name sort
        # would drop the newer of two same-second snapshots.
        snapshots = sorted(
            self.backup_dir.glob("vault-*.tar.gz.gpg"),
            key=lambda path: (path.stat().st_mtime, path.name),
        )
        excess = snapshots[: max(0, len(snapshots) - self.retention)]
        pruned: list[str] = []
        for path in excess:
            path.unlink()
            Path(f"{path}.sha256").unlink(missing_ok=True)
            Path(f"{path}.manifest.json").unlink(missing_ok=True)
            pruned.append(path.name)
        return tuple(pruned)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def verify_latest_snapshot(self) -> VaultBackupVerifyResult:
        """Check the newest snapshot as far as this host's keys allow.

        Without a secret key (the intended setup) decryption is impossible, so
        only checksum, OpenPGP structure and the recipient key are checked.
        """

        # By mtime: "...Z-1" sorts before "...Z" by name.
        snapshots = list(self.backup_dir.glob("vault-*.tar.gz.gpg"))
        if not snapshots:
            raise VaultBackupVerifyError("no snapshots found")
        snapshot = max(snapshots, key=lambda path: path.stat().st_mtime)
        warnings: list[str] = []

        digest = self._sha256(snapshot)
        checksum_path = Path(f"{snapshot}.sha256")
        try:
            expected = checksum_path.read_text(encoding="utf-8").split()[0]
        except (OSError, IndexError) as exc:
            raise VaultBackupVerifyError(
                f"checksum file is missing or empty: {checksum_path.name}"
            ) from exc
        if digest != expected:
            raise VaultBackupVerifyError(
                f"SHA-256 mismatch for {snapshot.name}: snapshot is corrupted"
            )

        try:
            packets = self._run_gpg(
                [self.gpg_binary, "--batch", "--list-packets", str(snapshot)],
                operation="GPG snapshot validation",
            )
        except RuntimeError as exc:
            raise VaultBackupVerifyError(str(exc)) from exc

        unverified = "no secret key on this host"
        if self._has_secret_key():
            try:
                files = self._count_decrypted_files(snapshot)
            except _DecryptionNotVerified as exc:
                unverified = f"gpg cannot unlock the secret key in batch mode: {exc}"
            else:
                return self._verified_result(snapshot, digest, files, warnings)

        key_ids = {
            match.upper() for match in _PUBKEY_KEYID.findall(packets)
        } - {_HIDDEN_KEYID}
        if key_ids:
            if not key_ids & self._recipient_key_ids():
                raise VaultBackupVerifyError(
                    "snapshot is not encrypted to the configured recipient"
                )
        else:
            warnings.append("recipient key id is hidden in the snapshot: not compared")
        warnings.append(
            f"decryption not verified: {unverified}; "
            "run a restore drill on the key-holding machine"
        )
        return VaultBackupVerifyResult(
            snapshot_path=str(snapshot),
            sha256=digest,
            decrypted=False,
            files=None,
            warnings=tuple(warnings),
        )

    def _verified_result(
        self, snapshot: Path, digest: str, files: int, warnings: list[str]
    ) -> VaultBackupVerifyResult:
        manifest_files = self._manifest_files(snapshot)
        if manifest_files is None:
            warnings.append("manifest is missing: file count was not compared")
        elif manifest_files != files:
            raise VaultBackupVerifyError(
                f"file count mismatch: archive has {files}, "
                f"manifest says {manifest_files}"
            )
        return VaultBackupVerifyResult(
            snapshot_path=str(snapshot),
            sha256=digest,
            decrypted=True,
            files=files,
            warnings=tuple(warnings),
        )

    def _has_secret_key(self) -> bool:
        try:
            result = subprocess.run(
                [
                    self.gpg_binary,
                    "--batch",
                    "--list-secret-keys",
                    "--",
                    self.gpg_recipient,
                ],
                capture_output=True,
                check=False,
                timeout=GPG_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _recipient_key_ids(self) -> set[str]:
        output = self._run_gpg(
            [
                self.gpg_binary,
                "--batch",
                "--list-keys",
                "--with-colons",
                "--fingerprint",
                "--fingerprint",
                "--",
                self.gpg_recipient,
            ],
            operation="GPG recipient lookup",
        )
        ids: set[str] = set()
        for line in output.splitlines():
            fields = line.split(":")
            if fields[0] == "fpr" and len(fields) > 9 and fields[9]:
                ids.add(fields[9][-16:].upper())
        return ids

    @staticmethod
    def _manifest_files(snapshot: Path) -> int | None:
        try:
            manifest = json.loads(
                Path(f"{snapshot}.manifest.json").read_text(encoding="utf-8")
            )
            return int(manifest["files"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _count_decrypted_files(self, snapshot: Path) -> int:
        """Decrypt as a stream and walk the tar without writing it to disk."""

        files = 0
        with tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    [self.gpg_binary, "--batch", "--decrypt", str(snapshot)],
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                )
            except OSError as exc:
                raise VaultBackupVerifyError("gpg executable is not available") from exc
            assert process.stdout is not None
            problem: Exception | None = None
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|gz") as tar:
                    for member in tar:
                        if not member.isdir():
                            files += 1
                    # Drain to EOF: gpg reports corruption (integrity check)
                    # only through its exit code, checked below.
                    stream = tar.fileobj
                    while stream is not None and stream.read(1024 * 1024):
                        pass
            except (tarfile.TarError, EOFError, zlib.error, OSError) as exc:
                problem = exc
            finally:
                if process.poll() is None and problem is not None:
                    process.kill()
                process.stdout.close()
                try:
                    process.wait(timeout=GPG_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    problem = problem or RuntimeError("gpg decryption timed out")
            stderr_file.seek(0)
            detail = stderr_file.read().decode(errors="replace").strip()
            if _UNLOCK_FAILURE.search(detail):
                raise _DecryptionNotVerified(detail)
            if problem is None and process.returncode != 0:
                problem = RuntimeError(detail or f"gpg exit {process.returncode}")
            elif problem is not None and detail:
                problem = RuntimeError(f"{problem} (gpg: {detail})")
        if problem is not None:
            raise VaultBackupVerifyError(
                f"snapshot cannot be decrypted or read: {problem}"
            ) from problem
        return files
