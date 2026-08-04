"""Helpers for source references and links across intake channels."""

from __future__ import annotations

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
