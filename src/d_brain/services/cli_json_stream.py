"""Recover final assistant text from machine-readable CLI streams."""

from __future__ import annotations

from typing import Any

from d_brain.services.json_normalizer import loads_safe


def _coerce_message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_coerce_message_text(item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "result"):
            text = _coerce_message_text(value.get(key))
            if text:
                return text
    return ""


def _extract_error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        if message:
            return message
    if isinstance(error, str) and error.strip():
        return error.strip()
    message = str(payload.get("message") or "").strip()
    if message:
        return message
    return ""


class BaseCliJsonStreamAdapter:
    """Minimal stateful adapter for one CLI's JSON event stream."""

    def __init__(self) -> None:
        self._final_output_text = ""
        self._assistant_parts: list[str] = []
        self._final_error_text = ""

    def feed_line(self, line: str) -> None:
        raise NotImplementedError

    def final_output_text(self) -> str:
        return self._final_output_text.strip() or "".join(self._assistant_parts).strip()

    def final_error_text(self) -> str:
        return self._final_error_text.strip()

    def _append_assistant_text(self, text: str) -> None:
        value = str(text or "")
        if not value:
            return
        self._assistant_parts.append(value)

    def _set_final_output_text(self, text: str) -> None:
        value = str(text or "")
        if not value:
            return
        self._final_output_text = value

    def _set_final_error_text(self, text: str) -> None:
        value = str(text or "").strip()
        if not value:
            return
        self._final_error_text = value


class CodexJsonStreamAdapter(BaseCliJsonStreamAdapter):
    def feed_line(self, line: str) -> None:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            return
        event_type = str(payload.get("type") or "").strip()
        if event_type == "item.completed":
            item = payload.get("item")
            if not isinstance(item, dict):
                return
            if str(item.get("type") or "").strip() != "agent_message":
                return
            text = str(item.get("text") or "")
            self._append_assistant_text(text)
            self._set_final_output_text(text)
            return
        if event_type in {"error", "turn.failed"}:
            self._set_final_error_text(_extract_error_message(payload))


class QwenJsonStreamAdapter(BaseCliJsonStreamAdapter):
    def feed_line(self, line: str) -> None:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            return

        event_type = str(payload.get("type") or "").strip().lower()
        if event_type == "assistant":
            message = payload.get("message")
            if not isinstance(message, dict):
                return
            content = message.get("content")
            if not isinstance(content, list):
                return
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip().lower() != "text":
                    continue
                text = str(item.get("text") or "")
                if text:
                    parts.append(text)
            if parts:
                self._append_assistant_text("".join(parts))
            return

        if event_type == "result":
            self._set_final_output_text(str(payload.get("result") or ""))


class ClaudeJsonStreamAdapter(QwenJsonStreamAdapter):
    pass


class GeminiJsonStreamAdapter(BaseCliJsonStreamAdapter):
    def feed_line(self, line: str) -> None:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            return

        event_type = str(payload.get("type") or "").strip().lower()
        role = str(payload.get("role") or "").strip().lower()
        if event_type == "message" and role == "assistant":
            self._append_assistant_text(_coerce_message_text(payload.get("content")))
            return
        if event_type == "result":
            for key in ("result", "response", "content"):
                text = _coerce_message_text(payload.get(key))
                if text:
                    self._set_final_output_text(text)
                    return


def build_cli_json_stream_adapter(cli_name: str) -> BaseCliJsonStreamAdapter | None:
    normalized = str(cli_name or "").strip().lower()
    if normalized == "codex":
        return CodexJsonStreamAdapter()
    if normalized == "qwen":
        return QwenJsonStreamAdapter()
    if normalized == "claude":
        return ClaudeJsonStreamAdapter()
    if normalized == "gemini":
        return GeminiJsonStreamAdapter()
    return None


def _drive_adapter(cli_name: str, raw_text: str) -> BaseCliJsonStreamAdapter | None:
    adapter = build_cli_json_stream_adapter(cli_name)
    if adapter is None:
        return None
    for line in str(raw_text or "").splitlines():
        payload_line = str(line or "").strip()
        if not payload_line:
            continue
        try:
            adapter.feed_line(payload_line)
        except Exception:
            continue
    return adapter


def recover_cli_text_from_raw_stream(cli_name: str, raw_text: str) -> str:
    adapter = _drive_adapter(cli_name, raw_text)
    if adapter is None:
        return ""
    return adapter.final_output_text()


def recover_cli_error_from_raw_stream(cli_name: str, raw_text: str) -> str:
    """Return a human-readable error message extracted from a CLI's JSON stream."""

    adapter = _drive_adapter(cli_name, raw_text)
    if adapter is None:
        return ""
    return adapter.final_error_text()


__all__ = [
    "build_cli_json_stream_adapter",
    "recover_cli_error_from_raw_stream",
    "recover_cli_text_from_raw_stream",
]
