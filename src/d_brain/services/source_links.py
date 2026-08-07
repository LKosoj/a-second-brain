"""Helpers for source references and links across intake channels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from d_brain.services.localization import translate

PLAUD_APP_BASE_URL = "https://app.plaud.ai"


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Normalized source reference for one saved entry."""

    kind: str
    ref: str = ""
    url: str = ""
    label: str = ""


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def build_telegram_source_info(
    message: Any,
    *,
    language: str = "ru",
) -> SourceInfo:
    """Build a stable Telegram source ref and best-effort message URL.

    Telegram only guarantees direct message links for public/private groups and
    channels. Private chats with a bot may only get a deterministic source ref.
    """

    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return SourceInfo(kind="telegram")

    ref = f"telegram:{chat_id}:{message_id}"
    direct_link = _safe_str(getattr(message, "link", None))
    if direct_link:
        return SourceInfo(
            kind="telegram",
            ref=ref,
            url=direct_link,
            label=translate(language, "open_telegram_message"),
        )

    chat_type = _safe_str(getattr(chat, "type", "")).lower()
    username = _safe_str(getattr(chat, "username", ""))
    if username and chat_type != "private":
        return SourceInfo(
            kind="telegram",
            ref=ref,
            url=f"https://t.me/{username}/{message_id}",
            label=translate(language, "open_telegram_message"),
        )

    chat_id_str = str(chat_id)
    if chat_type in {"supergroup", "channel"} and chat_id_str.startswith("-100"):
        return SourceInfo(
            kind="telegram",
            ref=ref,
            url=f"https://t.me/c/{chat_id_str[4:]}/{message_id}",
            label=translate(language, "open_telegram_message"),
        )

    return SourceInfo(kind="telegram", ref=ref)


# Characters stripped from a resolved forward-sender name before it is
# embedded in a "[forward from: NAME]" daily-entry marker. Beyond the
# original "[]|#\n" (which could break out of the marker's own brackets or
# fabricate a second markdown heading), this also strips "\r": storage.py's
# _render_with_newline folds a bare "\r" (and "\r\n") into a real "\n" once
# the entry is written, so a name with a raw "\r" would otherwise split the
# "## HH:MM [forward from: ...]" header line into two physical lines (an
# orphaned "...]" fragment on its own line -- file corruption, though not a
# trust issue: FORWARD_MARK_RE only requires the "[forward from:" prefix, so
# it still matches the first, header-bearing line either way). Other control
# or Unicode line/paragraph-separator characters are not stripped here:
# _render_with_newline never folds them into a real "\n", so they cannot
# fracture the header line the way "\r" does, and FORWARD_MARK_RE's
# prefix-only match is unaffected by their presence.
_UNSAFE_FORWARD_NAME_RE = re.compile(r"[\[\]|#\n\r]")

# Mirrors the widest of the four "## HH:MM [...]" matchers in
# compiled_briefings.py (DAILY_ENTRY_SPLIT_RE, DAILY_ENTRY_MARK_RE,
# FORWARD_MARK_RE, OWN_ENTRY_MARK_RE): all four require a literal "##" at
# the true start of a line, so defeating that one shared shape defeats all
# four at once. Duplicated here instead of imported: this module loads on
# every message-intake path, while compiled_briefings.py pulls in the CLI
# runner and the rest of the compile-enrich pipeline.
_DAILY_ENTRY_HEADER_LOOKALIKE_RE = re.compile(r"^##\s+\d{1,2}:\d{2}\s+\[")

# Only "\r" and "\r\n" are treated as line boundaries here, not the wider
# set str.splitlines() recognizes ("\v", "\f", and Unicode line/paragraph
# separators): storage.py's _render_with_newline folds a bare "\r"/"\r\n"
# into a real "\n" when the entry is written, so a header lookalike hidden
# behind one of those would otherwise reach DAILY_ENTRY_SPLIT_RE's
# "\n"-anchored MULTILINE "^" as a genuine split point once on disk, even
# though it looked harmless here. The other separators never get folded to
# a real "\n", so DAILY_ENTRY_SPLIT_RE (the only reader whose match this
# leading space can actually defeat) never reacts to them regardless; a
# lookalike hidden behind one of those stays inside the same excerpt as its
# entry's real header either way, and CompiledBriefingService's per-excerpt
# minimum-trust rule (_source_trust_level / _min_trust) already rates that
# excerpt by its weakest header. "\r\n" is tried before the bare "\r" branch
# so a CRLF pair counts as the one boundary it is, not two.
_LINE_BOUNDARY_RE = re.compile(r"\r\n|\r|\n")


# The runtime's own control markup, all of it "<!-- d-brain:... -->" or
# "<!-- plaud:<id>:... -->": the managed-block markers upsert_daily_block
# locates a block by (storage._managed_block_bounds) and the entry-status
# comments that tell the processor an entry is already handled
# (entry_status.ENTRY_STATUS_RE). Both are public strings in this
# repository, so a forwarded message -- or the text extracted from a
# forwarded document -- can carry them verbatim.
_RUNTIME_MARKUP_LOOKALIKE_RE = re.compile(r"^<!--\s*(?:d-brain|plaud):")


def _defuse_matching_lines(
    text: str,
    pattern: re.Pattern[str],
    spared: int = -1,
) -> str:
    """Indent every line matching ``pattern`` by one space, except ``spared``."""
    parts = _LINE_BOUNDARY_RE.split(text)
    boundaries = _LINE_BOUNDARY_RE.findall(text)
    pieces: list[str] = []
    for index, part in enumerate(parts):
        pieces.append(
            part if index == spared or not pattern.match(part) else f" {part}"
        )
        if index < len(boundaries):
            pieces.append(boundaries[index])
    return "".join(pieces)


def escape_embedded_runtime_markup(text: str) -> str:
    """Defuse runtime control markup embedded in someone else's text.

    Two things go wrong when a daily entry body carries these verbatim.
    A full marker pair lets the text host the reflect block: the next
    ``upsert_daily_block`` replaces only the range *between* the markers
    and leaves the surrounding heading alone, so the block's contents end
    up filed under the hostile entry's own heading, at that entry's trust.
    A single marker is worse in a different way -- ``_managed_block_bounds``
    then sees an unpaired marker and raises ``ManagedBlockError`` on every
    later write for that day. A forged entry-status comment simply makes
    the processor skip the entry as already handled.

    Same one-space treatment as ``escape_embedded_daily_headers``, and for
    the same reason: the readers all anchor at the start of a line, so the
    space defeats them while every visible character survives. Only entry
    *bodies* go through here -- the runtime writes its own markup as
    separate lines that never pass through this function.
    """
    if not text:
        return text
    return _defuse_matching_lines(text, _RUNTIME_MARKUP_LOOKALIKE_RE)


def collapse_to_single_line(value: Any) -> str:
    """Squeeze whitespace so a value cannot occupy a line of its own.

    For data interpolated into a one-line construct inside a managed block
    -- a "[[path|title]]" link, a '- "task"' bullet. ``upsert_daily_block``
    cannot defuse these the way ``append_to_daily`` defuses an entry body,
    because the block's own marker lines are indistinguishable from data
    once they are all one opaque string. Denying the data a line of its own
    is what makes that unnecessary: a value that cannot start a line can
    neither forge a marker nor duplicate the block's own one, which would
    otherwise raise ``ManagedBlockError`` on every later write for that day.

    Only ``None`` becomes "" (code review). ``str(value or "")`` also erased
    ``0`` and ``False``, which are values, not absences: a task the model
    returned with ``priority: 0`` reached the owner's daily report as a bare
    "priority: " with the number gone.
    """
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _leading_header_index(parts: list[str]) -> int:
    """Index of the block's own heading line, or -1 if it has none.

    Only the very first non-blank line qualifies, and only if it already
    looks like a daily-entry header. Callers of ``upsert_daily_block``
    build that line themselves (an f-string over their own clock), while
    everything below it is payload -- so sparing exactly one line spares
    the caller's heading without ever sparing data.
    """
    for index, part in enumerate(parts):
        if part.strip():
            return index if _DAILY_ENTRY_HEADER_LOOKALIKE_RE.match(part) else -1
    return -1


def escape_embedded_daily_headers(
    text: str,
    *,
    keep_leading_header: bool = False,
) -> str:
    """Defuse any line inside ``text`` shaped like a daily-entry header.

    ``storage.append_to_daily`` writes one entry as
    "## HH:MM [type]\\n<text>". The daily file is later cut back into
    per-entry excerpts by splitting on any line that looks like
    "## HH:MM [...]" (DAILY_ENTRY_SPLIT_RE, "\\n"-anchored only), and each
    excerpt's provenance trust (own / forwarded / integration / inferred,
    ТЗ 4.4) is read off its own header line. If ``text`` itself contains a
    line matching that shape, it is read back as a second, independent
    entry with its own trust rating -- letting forwarded (or otherwise not
    fully owner-authored) content pose as the owner's own words.

    A single leading space is enough to neutralize an offending line: the
    splitter requires "##" at the true start of the line, so the space
    breaks the match while leaving every visible character on the line
    intact -- nothing is removed or hidden from whoever reads the entry
    back.

    Applied to every entry body, own or forwarded alike: an "own"-marked
    entry routinely carries text the owner did not type themselves (a
    fetched link summary, extracted document text, image OCR), so an
    entry's own/forwarded marker is not actually where the trust boundary
    for this particular check sits.

    ``keep_leading_header`` is for ``upsert_daily_block``, whose callers
    hand over heading and payload as one opaque string. Defusing that
    heading too would erase the entry the block is supposed to be: the
    payload then merges into whatever entry precedes it in the file and
    inherits *its* trust, which is a promotion, not a demotion -- a PLAUD
    meeting summary written under an earlier "[voice]" entry reads back as
    the owner's own words. The caller must build that first line itself;
    passing data through it defeats the exemption.
    """
    if not text:
        return text
    spared = (
        _leading_header_index(_LINE_BOUNDARY_RE.split(text))
        if keep_leading_header
        else -1
    )
    return _defuse_matching_lines(text, _DAILY_ENTRY_HEADER_LOOKALIKE_RE, spared)


def forward_source_name(message: Any) -> str:
    """Best-effort display name for who forwarded ``message``.

    Shared by every handler that must tell an owner's own capture from
    someone else's forwarded content apart for daily-entry trust purposes
    (see TRUST_RANK / CONSEQUENTIAL_ACTION_TRUST_LEVELS in
    services/compiled_briefings.py): the handler's own daily-entry marker
    must read "[forward from: ...]", not its usual own-entry marker, or
    _source_trust_level will rate a stranger's forwarded content "own".
    Mirrors bot/handlers/forward.py's own sender resolution so a forwarded
    photo/voice/document is attributed the same way a forwarded text
    message is.
    """

    origin = getattr(message, "forward_origin", None)
    sender_user = getattr(origin, "sender_user", None)
    sender_user_name = getattr(origin, "sender_user_name", None)
    chat = getattr(origin, "chat", None)
    sender_name = getattr(origin, "sender_name", None)

    if sender_user:
        name = sender_user.full_name
    elif sender_user_name:
        name = sender_user_name
    elif chat:
        name = f"@{chat.username}" if chat.username else chat.title or "Channel"
    elif sender_name:
        name = sender_name
    else:
        name = "Unknown"

    return _UNSAFE_FORWARD_NAME_RE.sub("", name).strip() or "Unknown"


def build_plaud_source_info(file_id: str, *, language: str = "ru") -> SourceInfo:
    """Build source info for one PLAUD recording."""

    clean_file_id = _safe_str(file_id)
    if not clean_file_id:
        return SourceInfo(kind="plaud")
    return SourceInfo(
        kind="plaud",
        ref=f"plaud:{clean_file_id}",
        url=f"{PLAUD_APP_BASE_URL}/file/{clean_file_id}",
        label=translate(language, "open_plaud"),
    )


def format_source_markdown(source: SourceInfo | None, *, language: str = "ru") -> str:
    """Render source metadata as compact markdown lines."""

    if source is None:
        return ""

    lines: list[str] = []
    if source.url:
        label = source.label or "Open source"
        lines.append(f"> {translate(language, 'source')}: [{label}]({source.url})")
    if source.ref:
        lines.append(f"> {translate(language, 'source_ref')}: `{source.ref}`")
    return "\n".join(lines)
