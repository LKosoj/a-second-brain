"""Read-only vault file browser utilities for Telegram UI."""

from dataclasses import dataclass
from pathlib import Path

TELEGRAM_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


class FileBrowserError(Exception):
    """Base error for file browser operations."""


class FileBrowserRootNotFoundError(FileBrowserError):
    """Requested browser root does not exist."""


class FileBrowserPathError(FileBrowserError):
    """Requested path is invalid for the selected root."""


class FileBrowserEntryNotFoundError(FileBrowserError):
    """Requested file or directory does not exist."""


@dataclass(frozen=True, slots=True)
class BrowserRoot:
    """One available starting point for vault navigation."""

    id: str
    label: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class BrowserEntry:
    """One browsable directory entry."""

    name: str
    relative_path: str
    is_directory: bool
    size: int


class FileBrowserService:
    """Safe read-only browser limited to selected vault roots."""

    ROOTS: tuple[BrowserRoot, ...] = (
        BrowserRoot("daily", "🗓 Дневные", "daily"),
        BrowserRoot("attachments", "📎 Вложения", "attachments"),
        BrowserRoot("imports", "📥 Импорт", "imports"),
        BrowserRoot("goals", "🎯 Цели", "goals"),
        BrowserRoot("thoughts", "🧠 Мысли", "thoughts"),
        BrowserRoot("business", "💼 Бизнес", "business"),
        BrowserRoot("projects", "🚀 Проекты", "projects"),
        BrowserRoot("vault", "🗂 Весь vault", "."),
    )

    def __init__(self, vault_path: Path | str) -> None:
        self.vault_path = Path(vault_path).resolve()

    def list_roots(self) -> list[BrowserRoot]:
        """Return existing curated roots in UI order."""
        roots: list[BrowserRoot] = []
        for root in self.ROOTS:
            path = self._root_path(root)
            if root.id == "vault" or path.exists():
                roots.append(root)
        return roots

    def get_root(self, root_id: str) -> BrowserRoot:
        """Resolve one configured root by id."""
        for root in self.ROOTS:
            if root.id == root_id:
                path = self._root_path(root)
                if root.id != "vault" and not path.exists():
                    raise FileBrowserRootNotFoundError(root_id)
                return root
        raise FileBrowserRootNotFoundError(root_id)

    def list_entries(
        self,
        *,
        root_id: str,
        current_dir: str = "",
    ) -> tuple[BrowserRoot, str, list[BrowserEntry]]:
        """List directory entries within one selected root."""
        root = self.get_root(root_id)
        resolved_path, relative_dir = self.resolve_path(
            root_id=root.id,
            relative_path=current_dir,
        )
        if not resolved_path.exists() or not resolved_path.is_dir():
            raise FileBrowserEntryNotFoundError(relative_dir or "/")

        entries: list[BrowserEntry] = []
        for path in resolved_path.iterdir():
            if relative_dir == "" and root.id == "vault" and path.name.startswith("."):
                continue

            try:
                stat = path.lstat()
            except OSError:
                continue

            if path.is_symlink():
                continue

            is_directory = path.is_dir()
            relative_path = path.relative_to(self._root_real_path(root)).as_posix()
            entries.append(
                BrowserEntry(
                    name=path.name,
                    relative_path=relative_path,
                    is_directory=is_directory,
                    size=stat.st_size,
                )
            )

        entries.sort(key=lambda entry: (not entry.is_directory, entry.name.lower()))
        return root, relative_dir, entries

    def resolve_file(
        self,
        *,
        root_id: str,
        relative_path: str,
    ) -> tuple[BrowserRoot, Path]:
        """Resolve one file path for Telegram sending."""
        root = self.get_root(root_id)
        resolved_path, _ = self.resolve_path(
            root_id=root.id,
            relative_path=relative_path,
        )
        if not resolved_path.exists() or not resolved_path.is_file():
            raise FileBrowserEntryNotFoundError(relative_path)
        return root, resolved_path

    def resolve_path(
        self,
        *,
        root_id: str,
        relative_path: str = "",
    ) -> tuple[Path, str]:
        """Resolve a path and guarantee that it stays within the selected root."""
        root = self.get_root(root_id)
        root_real_path = self._root_real_path(root)
        requested = root_real_path / relative_path

        try:
            resolved_path = requested.resolve(strict=True)
        except FileNotFoundError:
            resolved_path = requested.resolve(strict=False)

        if (
            resolved_path != root_real_path
            and root_real_path not in resolved_path.parents
        ):
            raise FileBrowserPathError("Requested path escapes the selected root")

        relative = resolved_path.relative_to(root_real_path)
        relative_str = "" if str(relative) == "." else relative.as_posix()

        if root.id == "vault" and relative.parts and relative.parts[0].startswith("."):
            raise FileBrowserPathError("Hidden internal paths are not available")

        return resolved_path, relative_str

    def _root_path(self, root: BrowserRoot) -> Path:
        if root.relative_path in {"", "."}:
            return self.vault_path
        return self.vault_path / root.relative_path

    def _root_real_path(self, root: BrowserRoot) -> Path:
        return self._root_path(root).resolve()
