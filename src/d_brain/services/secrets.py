"""Secret-scrubbing filter applied to content ingested from outside sources.

Ingest here means anything the vault absorbs from the outside world before it
is written to disk: raw PLAUD JSON payloads, archived web pages, extracted
documents, YouTube transcripts, and forwarded/typed Telegram messages. Any of
those can carry a credential the source system embedded (a presigned S3 URL,
a bearer token, an API key, a password in a URL, a PEM/PGP private key).
``scrub_secrets`` redacts the known shapes before that text reaches the vault.

Tradeoff, on purpose: the bare ``sig=``/``Signature=`` query parameter is
redacted unconditionally, even though plenty of non-secret query strings use
that name for something harmless (e.g. a content signature that isn't a
credential). A signed URL leaking is worse than a value being redacted that
did not need to be, so this stays broad rather than trying to guess intent.
"""

from __future__ import annotations

import re

# PEM/PGP private-key markers. The base64 body between BEGIN/END can run a
# few KB for a real key, so the window below is generous; anything larger is
# not a private key we need to catch. Bounding it -- and never searching past
# the next BEGIN marker -- keeps this from re-scanning to the end of the
# string for every unmatched BEGIN, which is what made a naive
# ``BEGIN.*?END`` regex quadratic on adversarial input (many BEGIN markers,
# no matching END).
_PRIVATE_KEY_HEADER_RE = re.compile(r"-----BEGIN ([A-Z ]*PRIVATE KEY(?: BLOCK)?)-----")
_PRIVATE_KEY_BODY_LIMIT = 20_000

# Any PEM/PGP private-key footer, independent of the type named in the BEGIN
# header that opened the block. When a real key's body contains a decoy
# BEGIN marker before its real END, the first header's search stops at that
# decoy (see ``next_begin`` below) and falls back to the line-by-line body
# scan; the decoy header then closes on the real END only because the footer
# match ignores the key type. With a type-bound footer both fragments leaked.
_PRIVATE_KEY_FOOTER_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")

# One line inside a key body that has no reachable END (a truncated paste):
# either base64, or one of the two cleartext fields an encrypted PEM key can
# carry right after its header.
_PRIVATE_KEY_BODY_LINE_RE = re.compile(r"[A-Za-z0-9+/=]+\s*")
_PRIVATE_KEY_FIELD_LINE_RE = re.compile(r"(Proc-Type|DEK-Info):.*")


def _scrub_private_keys(text: str) -> tuple[str, int]:
    """Redact PEM/PGP private-key blocks in one left-to-right pass.

    See the module-level comment on ``_PRIVATE_KEY_HEADER_RE`` for why this
    is a manual scan instead of a single ``BEGIN...*?...END`` regex.
    """
    matches = list(_PRIVATE_KEY_HEADER_RE.finditer(text))
    if not matches:
        return text, 0

    count = 0
    out: list[str] = []
    pos = 0
    for index, match in enumerate(matches):
        if match.start() < pos:
            continue  # inside a block already redacted below
        next_begin = (
            matches[index + 1].start() if index + 1 < len(matches) else len(text)
        )
        window_end = min(len(text), match.end() + _PRIVATE_KEY_BODY_LIMIT, next_begin)

        footer_match = _PRIVATE_KEY_FOOTER_RE.search(text, match.end(), window_end)
        if footer_match is not None:
            out.append(text[pos : match.start()])
            out.append("[redacted private key]")
            pos = footer_match.end()
            count += 1
            continue

        # No END reachable in the bounded window: either a truncated paste
        # (redact the header plus whatever body-looking lines follow it), or
        # the header text just being mentioned in running prose (redact only
        # the header itself). The two are told apart by whether the header
        # sits alone on its own line the way a real PEM/PGP header does --
        # prose keeps talking on the same line right after it.
        consumed_end = match.end()
        line_end = text.find("\n", match.end())
        if line_end != -1 and not text[match.end() : line_end].strip():
            cursor = line_end + 1
            # An encrypted PEM key separates its Proc-Type/DEK-Info fields
            # from the base64 body with one blank line; only that blank line
            # is part of the body, any other blank line ends it.
            after_field = False
            while cursor < window_end:
                next_line_end = text.find("\n", cursor)
                line_stop = next_line_end if next_line_end != -1 else len(text)
                line = text[cursor:line_stop]
                is_field = _PRIVATE_KEY_FIELD_LINE_RE.fullmatch(line) is not None
                is_blank_after_field = after_field and not line.strip()
                if not (
                    _PRIVATE_KEY_BODY_LINE_RE.fullmatch(line)
                    or is_field
                    or is_blank_after_field
                ):
                    break
                after_field = is_field
                consumed_end = line_stop
                if next_line_end == -1:
                    break
                cursor = next_line_end + 1

        out.append(text[pos : match.start()])
        out.append("[redacted private key]")
        pos = consumed_end
        count += 1

    if not count:
        return text, 0
    out.append(text[pos:])
    return "".join(out), count


_BEARER_TOKEN_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9\-_.~+/]{16,}=*", re.IGNORECASE
)

# Query-value tail: stop at the next separator, and at the markdown/URL
# delimiters ()[]<>` and a trailing comma that would otherwise get swallowed
# into "[redacted]" and break the surrounding link or sentence.
_QUERY_VALUE = r'[^&\s"\')\]>`,]*'

# AWS presigned-URL query parameters (S3 SigV4). Matched before the generic
# signature/password patterns below so those don't re-match inside them.
_AWS_SIGNED_PARAM_RE = re.compile(
    rf"(X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)={_QUERY_VALUE}",
    re.IGNORECASE,
)

_SIGNATURE_PARAM_RE = re.compile(
    rf"(?<!X-Amz-)\b(Signature|sig)={_QUERY_VALUE}",
    re.IGNORECASE,
)

_URL_PASSWORD_PARAM_RE = re.compile(
    rf"\b(password|passwd|pwd)={_QUERY_VALUE}",
    re.IGNORECASE,
)

# scheme://user:password@host -- only the password half is a secret.
_URL_USERINFO_RE = re.compile(r"(://[^\s/:@]+:)([^\s/:@]+)(@)")

_API_KEY_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def scrub_secrets(text: str) -> tuple[str, int]:
    """Redact known secret shapes from ``text``.

    Returns ``(scrubbed_text, replacement_count)``. When nothing matches,
    returns the input unchanged and ``0``.
    """
    if not text:
        return text, 0

    count = 0
    result = text

    result, n = _scrub_private_keys(result)
    count += n

    result, n = _BEARER_TOKEN_RE.subn("Bearer [redacted]", result)
    count += n

    result, n = _AWS_SIGNED_PARAM_RE.subn(lambda m: f"{m.group(1)}=[redacted]", result)
    count += n

    result, n = _SIGNATURE_PARAM_RE.subn(lambda m: f"{m.group(1)}=[redacted]", result)
    count += n

    result, n = _URL_PASSWORD_PARAM_RE.subn(
        lambda m: f"{m.group(1)}=[redacted]", result
    )
    count += n

    result, n = _URL_USERINFO_RE.subn(r"\1[redacted]\3", result)
    count += n

    for pattern in _API_KEY_PATTERNS:
        result, n = pattern.subn("[redacted]", result)
        count += n

    if count == 0:
        return text, 0
    return result, count
