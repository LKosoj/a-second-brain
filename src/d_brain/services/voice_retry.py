"""Retry transcription of voice entries saved as unrecognized audio.

The bot keeps the recording and a daily entry marked ``voice_unrecognized``
when Deepgram fails or is not configured (handlers/voice.py). The nightly run
calls this before classification, so a recording that is recognized on the
second try is processed like any other entry of the day.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path

from d_brain.manifest import load_manifest_for_vault
from d_brain.services.entry_status import (
    ENTRY_STATUS_RE,
    entry_spans,
    extract_entry_statuses,
)
from d_brain.services.frontmatter import (
    UnsafeVaultPathError,
    write_validated_vault_markdown,
)
from d_brain.services.source_links import (
    escape_embedded_daily_headers,
    escape_embedded_runtime_markup,
)
from d_brain.services.vault_lock import vault_write_lock

logger = logging.getLogger(__name__)

VOICE_UNRECOGNIZED_STATUS = "voice_unrecognized"
UNRECOGNIZED_NOTE = "Не распознано"
_ATTACHMENT_RE = re.compile(
    r"!\[\[(attachments/\d{4}-\d{2}-\d{2}/[A-Za-z0-9._-]+)\]\]"
)
_CAS_MISMATCH = "atomic write source does not match expected_full_sha256"


def build_unrecognized_body(relative_path: str) -> str:
    """Body of the daily entry for audio that could not be transcribed."""
    return f"![[{relative_path}]]\n\n{UNRECOGNIZED_NOTE}"


@dataclass(frozen=True)
class VoiceRetryResult:
    recognized: int = 0
    failed: int = 0  # transcriber raised or returned nothing; marker stays
    skipped: int = 0  # no file, unsafe path or no attachment in the entry


@dataclass(frozen=True)
class _Candidate:
    """Entry identity. Offsets are not kept: the bot may append meanwhile."""

    time: str
    entry_type: str
    attachment: str


def _split_blocks(text: str) -> list[tuple[str, str, int, int]]:
    return [
        (match.group("time"), match.group("type"), match.start(), end)
        for match, end in entry_spans(text)
    ]


def _find_candidates(text: str) -> tuple[list[_Candidate], int]:
    """Unrecognized entries with a usable attachment link, plus the unusable count."""
    found: list[_Candidate] = []
    unusable = 0
    for time, entry_type, start, end in _split_blocks(text):
        block = text[start:end]
        if VOICE_UNRECOGNIZED_STATUS not in extract_entry_statuses(block):
            continue
        link = _ATTACHMENT_RE.search(block)
        if link is None:
            logger.warning("Unrecognized voice entry at %s has no attachment", time)
            unusable += 1
            continue
        found.append(_Candidate(time, entry_type, link.group(1)))
    return found, unusable


def _safe_attachment(vault_path: Path, relative: str) -> Path | None:
    path = vault_path / relative
    if path.is_symlink() or not path.is_file():
        return None
    root = (vault_path / "attachments").resolve()
    if not path.resolve().is_relative_to(root):
        return None
    return path


def _rewrite_block(block: str, transcript: str) -> str:
    """Drop the unrecognized marker and put the transcript in place of the note."""
    # Same defusing as append_to_daily: the words of a forwarded recording
    # must not become a daily header or a runtime status marker.
    body = escape_embedded_runtime_markup(
        escape_embedded_daily_headers(transcript.strip())
    )
    out: list[str] = []
    drop_blank = False
    for line in block.splitlines(keepends=True):
        stripped = line.strip()
        marker = ENTRY_STATUS_RE.match(stripped)
        if marker is not None and marker.group(1) == VOICE_UNRECOGNIZED_STATUS:
            drop_blank = True
            continue
        if drop_blank and not stripped:
            drop_blank = False
            continue
        drop_blank = False
        if stripped == UNRECOGNIZED_NOTE:
            out.append(body + line[len(line.rstrip("\r\n")) :])
        else:
            out.append(line)
    return "".join(out)


def _replace_entry(
    vault_path: Path, daily_file: Path, key: _Candidate, text: str
) -> bool:
    """Swap the marker for the transcript; False if the entry changed or vanished."""
    for _attempt in range(2):
        with vault_write_lock(vault_path) as lock:
            original = daily_file.read_bytes()
            content = original.decode("utf-8")
            target = None
            for time, entry_type, start, end in _split_blocks(content):
                block = content[start:end]
                link = _ATTACHMENT_RE.search(block)
                if (
                    (time, entry_type) == (key.time, key.entry_type)
                    and link is not None
                    and link.group(1) == key.attachment
                    and VOICE_UNRECOGNIZED_STATUS in extract_entry_statuses(block)
                ):
                    target = (start, end)
                    break
            if target is None:
                return False
            start, end = target
            rewritten = _rewrite_block(content[start:end], text)
            updated = content[:start] + rewritten + content[end:]
            try:
                write_validated_vault_markdown(
                    vault_path,
                    daily_file,
                    updated.encode("utf-8"),
                    manifest=load_manifest_for_vault(vault_path),
                    existing_lock=lock,
                    expected_full_sha256=sha256(original).hexdigest(),
                )
            except UnsafeVaultPathError as exc:
                if str(exc) != _CAS_MISMATCH:
                    raise
                continue
            return True
    return False


def retry_unrecognized_voice(
    vault_path: Path,
    days: Iterable[date],
    transcribe: Callable[[bytes], str],
) -> VoiceRetryResult:
    """Transcribe saved audio of unrecognized entries and fill in the text."""
    recognized = failed = skipped = 0
    for day in days:
        daily_file = vault_path / "daily" / f"{day.isoformat()}.md"
        try:
            text = daily_file.read_bytes().decode("utf-8")
        except FileNotFoundError:
            continue
        candidates, unusable = _find_candidates(text)
        skipped += unusable
        for key in candidates:
            audio_path = _safe_attachment(vault_path, key.attachment)
            if audio_path is None:
                logger.warning("Skipping voice retry for %s", key.attachment)
                skipped += 1
                continue
            # Network call stays outside the vault lock: the bot writes the
            # same daily file and would stall for the whole request.
            try:
                transcript = transcribe(audio_path.read_bytes()).strip()
            except Exception:
                logger.exception("Voice retry transcription failed: %s", key.attachment)
                failed += 1
                continue
            if not transcript:
                failed += 1
                continue
            try:
                done = _replace_entry(vault_path, daily_file, key, transcript)
            except Exception:
                logger.exception("Voice retry could not update %s", daily_file)
                done = False
            if done:
                recognized += 1
            else:
                logger.warning("Voice entry %s changed, left as is", key.attachment)
                failed += 1
    return VoiceRetryResult(recognized, failed, skipped)
