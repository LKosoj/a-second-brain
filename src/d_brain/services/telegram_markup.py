"""Shared Telegram markup conversion helpers."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

TELEGRAM_TEXT_LIMIT = 4000
EDIT_TRUNCATION_SUFFIX = " ✂️"
LEGACY_TELEGRAM_HTML_RE = re.compile(
    r"</?(?:b|i|code|pre|s|u|a)(?:\s[^>]*)?>",
    re.IGNORECASE,
)


def contains_legacy_telegram_html(text: str) -> bool:
    """Whether the payload looks like legacy Telegram HTML."""

    return bool(LEGACY_TELEGRAM_HTML_RE.search(str(text or "")))


def html_to_markdown(text: str) -> str:
    """Convert the supported Telegram HTML subset to markdown."""

    value = str(text or "")
    value = re.sub(r"<pre>(.*?)</pre>", r"```\n\1\n```", value, flags=re.DOTALL)
    value = re.sub(r"<b>(.*?)</b>", r"**\1**", value, flags=re.DOTALL)
    value = re.sub(r"<i>(.*?)</i>", r"*\1*", value, flags=re.DOTALL)
    value = re.sub(r"<code>(.*?)</code>", r"`\1`", value, flags=re.DOTALL)
    value = re.sub(r"<s>(.*?)</s>", r"~~\1~~", value, flags=re.DOTALL)
    value = re.sub(r"<u>(.*?)</u>", r"\1", value, flags=re.DOTALL)
    value = re.sub(
        r'<a href="([^"]+)">([^<]+)</a>',
        r"[\2](\1)",
        value,
        flags=re.DOTALL,
    )
    return html.unescape(value)


def normalize_markdown_input(text: str, *, parse_mode: str | None = None) -> str:
    """Normalize one incoming payload to markdown."""

    value = str(text or "").strip()
    if not value:
        return ""
    if parse_mode == "HTML" or contains_legacy_telegram_html(value):
        return html_to_markdown(value).strip()
    return value


def _extract_markdown_links(text: str) -> list[tuple[int, int, str, str]]:
    value = str(text or "")
    links: list[tuple[int, int, str, str]] = []
    index = 0
    while index < len(value):
        start = value.find("[", index)
        if start < 0:
            break
        close = value.find("](", start + 1)
        if close < 0:
            break
        href_start = close + 2
        position = href_start
        depth = 0
        while position < len(value):
            char = value[position]
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            position += 1
        if position >= len(value):
            index = start + 1
            continue
        label = value[start + 1 : close]
        href = value[href_start:position]
        if label and href:
            links.append((start, position + 1, label, href))
        index = position + 1
    return links


def _markdown_links_to_html_placeholders(text: str) -> tuple[str, list[str]]:
    value = str(text or "")
    href_placeholders: list[str] = []
    rendered: list[str] = []
    cursor = 0

    for start, end, label, href in _extract_markdown_links(value):
        rendered.append(html.escape(value[cursor:start], quote=False))
        anchor = (
            f'<a href="{html.escape(href, quote=True)}">'
            f"{html.escape(label, quote=False)}</a>"
        )
        href_placeholders.append(anchor)
        rendered.append(f"\x00LINK{len(href_placeholders) - 1}\x00")
        cursor = end

    rendered.append(html.escape(value[cursor:], quote=False))
    return "".join(rendered), href_placeholders


def _markdown_inline_to_html(text: str) -> str:
    value, href_placeholders = _markdown_links_to_html_placeholders(text)

    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"~~(.+?)~~", r"<s>\1</s>", value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", value)
    if href_placeholders:
        value = re.sub(
            r"\x00LINK(\d+)\x00",
            lambda m: href_placeholders[int(m.group(1))],
            value,
        )
    return value


def markdown_to_html(markdown_text: str) -> str:
    """Convert a small markdown subset into Telegram-safe HTML."""

    value = str(markdown_text or "").strip()
    if not value:
        return ""

    rendered_lines: list[str] = []
    in_code_block = False
    code_lines: list[str] = []
    for raw_line in value.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                rendered_lines.append(
                    f"<pre>{html.escape('\n'.join(code_lines), quote=False)}</pre>"
                )
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(raw_line)
            continue

        if not stripped:
            rendered_lines.append("")
            continue

        heading_match = re.match(r"^#{1,6}\s+(.+)$", stripped)
        bullet_match = re.match(r"^[-*]\s+(.+)$", stripped)
        ordered_match = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        quote_match = re.match(r"^>\s*(.+)$", stripped)

        if heading_match:
            rendered_lines.append(
                f"<b>{_markdown_inline_to_html(heading_match.group(1))}</b>"
            )
            continue
        if bullet_match:
            rendered_lines.append(
                f"• {_markdown_inline_to_html(bullet_match.group(1))}"
            )
            continue
        if ordered_match:
            rendered_lines.append(
                f"{ordered_match.group(1)}. "
                f"{_markdown_inline_to_html(ordered_match.group(2))}"
            )
            continue
        if quote_match:
            rendered_lines.append(f"• {_markdown_inline_to_html(quote_match.group(1))}")
            continue
        rendered_lines.append(_markdown_inline_to_html(stripped))

    if in_code_block:
        rendered_lines.append(
            f"<pre>{html.escape('\n'.join(code_lines), quote=False)}</pre>"
        )
    return "\n".join(rendered_lines)


def markdown_to_plain_text(markdown_text: str) -> str:
    """Drop markup while keeping the textual content."""

    html_text = markdown_to_html(markdown_text)
    text = re.sub(r"<[^>]+>", "", html_text)
    return html.unescape(text)


def _escape_markdown_v2_text(text: str) -> str:
    value = str(text or "").replace("\\", "\\\\")
    for char in r"_*[]()~`>#+-=|{}.!":
        value = value.replace(char, f"\\{char}")
    return value


def _escape_markdown_v2_code(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace("`", "\\`")


def _escape_markdown_v2_url(url: str) -> str:
    return str(url or "").replace("\\", "\\\\").replace(")", "\\)")


class _HtmlToMarkdownV2Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.tag_stack: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        attrs_map = {key: value or "" for key, value in attrs}
        if tag_name == "b":
            self.parts.append("*")
            self.tag_stack.append(("b", ""))
        elif tag_name == "i":
            self.parts.append("_")
            self.tag_stack.append(("i", ""))
        elif tag_name == "s":
            self.parts.append("~")
            self.tag_stack.append(("s", ""))
        elif tag_name == "u":
            self.parts.append("__")
            self.tag_stack.append(("u", ""))
        elif tag_name == "code":
            self.parts.append("`")
            self.tag_stack.append(("code", ""))
        elif tag_name == "pre":
            self.parts.append("```\n")
            self.tag_stack.append(("pre", ""))
        elif tag_name == "a":
            self.parts.append("[")
            self.tag_stack.append(("a", attrs_map.get("href", "")))
        else:
            self.tag_stack.append((tag_name, ""))

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if not self.tag_stack:
            return

        while self.tag_stack:
            current_tag, value = self.tag_stack.pop()
            if current_tag == "b":
                self.parts.append("*")
            elif current_tag == "i":
                self.parts.append("_")
            elif current_tag == "s":
                self.parts.append("~")
            elif current_tag == "u":
                self.parts.append("__")
            elif current_tag == "code":
                self.parts.append("`")
            elif current_tag == "pre":
                self.parts.append("\n```")
            elif current_tag == "a":
                self.parts.append(f"]({_escape_markdown_v2_url(value)})")
            if current_tag == tag_name:
                break

    def handle_data(self, data: str) -> None:
        if not data:
            return
        in_code = any(tag in {"code", "pre"} for tag, _value in self.tag_stack)
        if in_code:
            self.parts.append(_escape_markdown_v2_code(data))
        else:
            self.parts.append(_escape_markdown_v2_text(html.unescape(data)))

    def handle_entityref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&#{name};"))

    def render(self) -> str:
        return "".join(self.parts)


def html_to_markdown_v2(html_text: str) -> str:
    """Convert Telegram-safe HTML to Telegram MarkdownV2."""

    parser = _HtmlToMarkdownV2Parser()
    parser.feed(str(html_text or ""))
    parser.close()
    return parser.render()


def markdown_to_markdown_v2(markdown_text: str) -> str:
    """Convert markdown input to Telegram MarkdownV2."""

    return html_to_markdown_v2(markdown_to_html(markdown_text))


def truncate_plain_text_for_edit(
    text: str,
    *,
    max_length: int = TELEGRAM_TEXT_LIMIT,
    suffix: str = EDIT_TRUNCATION_SUFFIX,
) -> str:
    """Trim one plain Telegram edit payload and mark it as shortened."""

    value = str(text or "")
    if len(value) <= max_length:
        return value
    if max_length <= len(suffix):
        return suffix[:max_length]
    body_limit = max_length - len(suffix)
    return value[:body_limit].rstrip() + suffix
