"""Keyword search over the vault for /find (G6).

Uses ``qmd search`` only (BM25, no model, no embeddings API): ``qmd query``
loads models and can run for minutes, which is not what an explicit lookup
command should do. Read-only: nothing is written to the vault notes.
"""

import json
import logging
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from d_brain.services.file_browser import FileBrowserError, FileBrowserService
from d_brain.services.qmd import QmdService

logger = logging.getLogger(__name__)

FIND_LIMIT = 5
# Fetch more than we show: unsafe paths and duplicates are dropped afterwards.
FIND_FETCH = 15
FIND_TIMEOUT_SECONDS = 30
FIND_QUERY_MAX_CHARS = 200
SNIPPET_MAX_CHARS = 220
TITLE_MAX_CHARS = 80

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACES_RE = re.compile(r"\s+")


class FindUnavailable(Exception):
    """qmd is missing, timed out, failed, or returned something unreadable."""


@dataclass(frozen=True, slots=True)
class FindHit:
    rel_path: str
    title: str
    snippet: str


def normalize_query(raw: str) -> str:
    """Strip the query and turn control characters into spaces."""
    return _SPACES_RE.sub(" ", _CONTROL_RE.sub(" ", raw)).strip()


def is_findable_rel_path(rel_path: str) -> bool:
    """Pure check of a vault-relative path; never trusts what qmd reported.

    Hidden components cover ``.session``/``.qmd``/``.env``-like data at any
    depth (the file browser only checks the first one), and ``skills/`` holds
    the private skills (``skills/private``).
    """
    if not rel_path or "\\" in rel_path or not rel_path.endswith(".md"):
        return False
    path = PurePosixPath(rel_path)
    if path.is_absolute():
        return False
    parts = rel_path.split("/")
    if any(part in ("", "..") or part.startswith(".") for part in parts):
        return False
    return parts[0].casefold() != "skills"


def resolve_findable_file(vault_path: Path | str, rel_path: str) -> Path | None:
    """Real path of a findable file, or None if it is gone or not allowed.

    The *resolved* path is checked again: a symlink such as
    ``notes/a.md -> skills/private/x.md`` passes the string check.
    """
    if not is_findable_rel_path(rel_path):
        return None
    browser = FileBrowserService(vault_path)
    try:
        _, resolved = browser.resolve_file(root_id="vault", relative_path=rel_path)
        resolved_rel = resolved.relative_to(browser.vault_path).as_posix()
    except (FileBrowserError, ValueError, OSError):
        return None
    return resolved if is_findable_rel_path(resolved_rel) else None


def _emoji_to_hex(segment: str) -> str:
    """Port of qmd's ``emojiToHex``: runs of emoji become dash-joined hex codes."""
    out: list[str] = []
    run: list[str] = []
    prev = ""
    for char in segment:
        category = unicodedata.category(char)
        in_run = category in ("So", "Sk") or (
            category == "Mn" and unicodedata.category(prev or " ") == "So"
        )
        if in_run:
            if category in ("So", "Sk"):
                run.append(f"{ord(char):x}")
        else:
            if run:
                out.append("-".join(run))
                run = []
            out.append(char)
        prev = char
    if run:
        out.append("-".join(run))
    return "".join(out)


def _dash_separate(text: str) -> str:
    """Every run outside letters, digits and ``$`` becomes one ``-``; trim ends."""
    cleaned = "".join(
        char
        if unicodedata.category(char)[0] in ("L", "N") or char == "$"
        else "\0"
        for char in text
    )
    return re.sub(r"\0+", "-", cleaned).strip("-")


def qmd_handelize(path: str) -> str | None:
    """Port of qmd's ``handelize`` (qmd 2.5.x, ``dist/store.js``), or None.

    This is how qmd turns a file path inside a collection into the path in its
    ``qmd://`` URIs: ``___`` becomes ``/``, emoji become hex codes, and
    everything except letters/digits/``$`` collapses to ``-``. Case, Cyrillic
    and the extension are kept (only ``.[a-z0-9]+`` at the end survives as is).
    qmd raises for names with no usable content; None is the same here.
    """
    segments = [part for part in path.split("/") if part]
    if not segments:
        return None
    stem = re.sub(r"\.[^.]+\Z", "", segments[-1])
    if not any(
        unicodedata.category(char)[0] in ("L", "N")
        or unicodedata.category(char) in ("So", "Sk")
        or char == "$"
        for char in stem
    ):
        return None
    parts = path.replace("___", "/").split("/")
    result: list[str] = []
    for idx, part in enumerate(parts):
        part = _emoji_to_hex(part)
        if idx == len(parts) - 1:
            match = re.search(r"(\.[a-z0-9]+)\Z", part, re.ASCII | re.IGNORECASE)
            ext = match.group(1) if match else ""
            result.append(_dash_separate(part[: len(part) - len(ext)]) + ext)
        else:
            result.append(_dash_separate(part))
    return "/".join(filter(None, result)) or None


class _NormalizedNames:
    """Lazy map from qmd-normalized names to real findable vault files.

    Built at most once per collection root per query: one walk instead of one
    per hit. Only files that pass ``is_findable_rel_path`` are indexed, so
    hidden directories and ``skills/`` never come back through this route.
    """

    def __init__(self, vault_path: Path | str) -> None:
        self._vault = Path(vault_path)
        self._by_root: dict[Path, dict[str, list[str]]] = {}

    def lookup(self, root: Path, remainder: str) -> str | None:
        """Vault-relative path of the one file qmd named ``remainder``, if any."""
        index = self._by_root.get(root)
        if index is None:
            index = self._by_root[root] = self._build(root)
        matches = index.get(remainder, [])
        if len(matches) > 1:
            # Two files collapse to the same qmd name; guessing could show the
            # wrong note, so the hit is skipped.
            logger.debug("Ambiguous /find hit %s: %s", remainder, matches)
            return None
        return matches[0] if matches else None

    def _build(self, root: Path) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        start = self._vault / root
        for dirpath, dirnames, filenames in os.walk(start):
            # Same pruning as qmd's own indexing (hidden parts are skipped).
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    rel_path = full.relative_to(self._vault).as_posix()
                    in_root = full.relative_to(start).as_posix()
                except ValueError:
                    continue
                if not is_findable_rel_path(rel_path):
                    continue
                key = qmd_handelize(in_root)
                if key is not None:
                    index.setdefault(key, []).append(rel_path)
        return index


def _clean_snippet(raw: object) -> str:
    lines = [
        line
        for line in str(raw or "").splitlines()
        # qmd sometimes starts a snippet with a diff-style "@@ -1,2 @@" line.
        if not line.lstrip().startswith("@@")
    ]
    text = _SPACES_RE.sub(" ", " ".join(lines)).strip()
    if len(text) > SNIPPET_MAX_CHARS:
        text = text[: SNIPPET_MAX_CHARS - 1].rstrip() + "…"
    return text


def _clean_title(raw: object, rel_path: str) -> str:
    title = _SPACES_RE.sub(" ", str(raw or "")).strip()
    if not title:
        title = PurePosixPath(rel_path).stem
    if len(title) > TITLE_MAX_CHARS:
        title = title[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return title


def find_in_vault(
    vault_path: Path | str, query: str, *, limit: int = FIND_LIMIT
) -> list[FindHit]:
    """Run ``qmd search`` and return up to ``limit`` safe, existing hits."""
    service = QmdService(Path(vault_path))
    try:
        result = service.run(
            "search",
            "--json",
            "-n",
            str(FIND_FETCH),
            # Without "--" a query like "--rm" would be read as an option.
            "--",
            query,
            timeout=FIND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FindUnavailable(f"qmd search did not run: {exc!r}") from exc
    if result.returncode != 0:
        logger.warning("qmd search failed: %s", (result.stderr or "")[-300:])
        raise FindUnavailable(f"qmd search exited with {result.returncode}")

    stdout = (result.stdout or "").strip()
    if not stdout:
        return []
    try:
        items = json.loads(stdout)
    except ValueError as exc:
        raise FindUnavailable("qmd search returned invalid JSON") from exc
    if not isinstance(items, list):
        raise FindUnavailable("qmd search returned unexpected JSON")

    hits: list[FindHit] = []
    seen: set[str] = set()
    names = _NormalizedNames(vault_path)
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = str(item.get("file", ""))
        rel_path = service.uri_to_rel_path(uri)
        if rel_path is None:
            continue
        if resolve_findable_file(vault_path, rel_path) is None:
            # qmd normalizes names in its URIs (``Заметка #1.md`` ->
            # ``Заметка-1.md``): look for the real file behind the name.
            parts = service.split_uri(uri)
            real = names.lookup(*parts) if parts is not None else None
            if real is None or resolve_findable_file(vault_path, real) is None:
                logger.debug("Skipping unusable /find hit: %s", rel_path)
                continue
            rel_path = real
        if rel_path in seen:
            continue
        seen.add(rel_path)
        hits.append(
            FindHit(
                rel_path=rel_path,
                title=_clean_title(item.get("title"), rel_path),
                snippet=_clean_snippet(item.get("snippet")),
            )
        )
        if len(hits) >= limit:
            break
    return hits
