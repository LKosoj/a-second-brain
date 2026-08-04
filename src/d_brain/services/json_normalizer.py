"""Robust JSON extraction from mixed CLI and MCP output."""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_outermost_object(text: str) -> str:
    source = str(text or "")
    start = source.find("{")
    if start < 0:
        return ""

    depth = 0
    in_string = False
    escaped = False
    begin = -1

    for index, char in enumerate(source[start:], start=start):
        if escaped:
            escaped = False
            continue

        if in_string:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "{":
            if depth == 0:
                begin = index
            depth += 1
            continue

        if char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and begin >= 0:
                return source[begin : index + 1]

    return ""


def _iter_outermost_objects(text: str) -> list[str]:
    source = str(text or "")
    depth = 0
    in_string = False
    escaped = False
    begin = -1
    objects: list[str] = []

    for index, char in enumerate(source):
        if escaped:
            escaped = False
            continue

        if in_string:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "{":
            if depth == 0:
                begin = index
            depth += 1
            continue

        if char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and begin >= 0:
                objects.append(source[begin : index + 1])
                begin = -1

    return objects


def _sanitize_json_candidate(raw: str) -> str:
    source = str(raw or "")
    out: list[str] = []
    in_string = False
    escaped = False

    for char in source:
        code = ord(char)
        if in_string:
            if escaped:
                if char in {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}:
                    out.append(char)
                else:
                    out.extend(["\\", char])
                escaped = False
                continue
            if char == "\\":
                out.append(char)
                escaped = True
                continue
            if char == '"':
                out.append(char)
                in_string = False
                continue
            if code < 0x20:
                out.append(f"\\u{code:04x}")
                continue
            out.append(char)
            continue

        if char == '"':
            in_string = True
            out.append(char)
            continue
        if code < 0x20 and char not in {"\n", "\r", "\t"}:
            continue
        out.append(char)

    return "".join(out)


def _looks_like_json_object(candidate: str) -> bool:
    source = str(candidate or "").strip()
    if not source.startswith("{") or not source.endswith("}"):
        return False
    inner = source[1:-1].strip()
    if not inner:
        return True
    return ":" in inner and '"' in inner


def iter_json_object_candidates(raw: str) -> list[str]:
    source = str(raw or "").strip()
    if not source:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def append(value: str) -> None:
        normalized = str(value or "").strip()
        if (
            not normalized
            or normalized in seen
            or not _looks_like_json_object(normalized)
        ):
            return
        seen.add(normalized)
        candidates.append(normalized)

    for match in _JSON_FENCE_RE.finditer(source):
        inner = str(match.group(1) or "").strip()
        for item in _iter_outermost_objects(inner):
            append(item)

    for item in _iter_outermost_objects(source):
        append(item)

    return candidates


def extract_json_object(raw: str) -> str:
    source = str(raw or "").strip()
    if not source:
        return ""

    for match in _JSON_FENCE_RE.finditer(source):
        inner = str(match.group(1) or "").strip()
        if not inner:
            continue
        found = _extract_outermost_object(inner)
        if found and _looks_like_json_object(found):
            return found

    found = _extract_outermost_object(source)
    if found and _looks_like_json_object(found):
        return found
    return ""


def loads_safe(raw: str, *, strict_first: bool = True) -> Any:
    source = str(raw or "").strip()
    if not source:
        raise json.JSONDecodeError("empty json payload", source, 0)

    if strict_first:
        try:
            return json.loads(source)
        except json.JSONDecodeError:
            pass

    candidate = extract_json_object(source) or source
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        repaired = _sanitize_json_candidate(candidate)
        return json.loads(repaired)


def extract_first_json_dict(raw: str, *, error_context: str) -> dict[str, Any]:
    source = str(raw or "").strip()
    if not source:
        raise ValueError(f"No JSON object found in {error_context}")

    try:
        parsed = loads_safe(source)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    for candidate in iter_json_object_candidates(source):
        try:
            parsed = loads_safe(candidate, strict_first=False)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(f"No JSON object found in {error_context}")


__all__ = [
    "extract_first_json_dict",
    "extract_json_object",
    "iter_json_object_candidates",
    "loads_safe",
]
