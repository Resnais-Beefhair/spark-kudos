"""Opt-in owner log content: preserve prose; mask credentials, not private context."""

from __future__ import annotations

import re

MAX_OPERATOR_MESSAGE = 16_384
_VALUE = r"""(?:\[REDACTED(?: PRIVATE KEY)?\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)"""
_KEYS = (
    r"(?:[a-z0-9]{1,32}[_-]){0,4}(?:api[_-]?key|(?:access|refresh|id|auth|session)[_-]?token|"
    r"token|password|passwd|secret|client[_-]?secret|secret[_-]?access[_-]?key|"
    r"(?:secret|private|master|access)[_-]?key|"
    r"authorization|proxy-authorization|(?:set-)?cookie|signature|sig|"
    r"x-amz-signature|x-amz-security-token|x-goog-signature)"
)
_ASSIGNMENT = re.compile(r"""(?i)(["']?\b""" + _KEYS + r"""\b["']?\s*[:=]\s*)""" + _VALUE)
_FLAG = re.compile(r"(?i)(--" + _KEYS + r"\s+)" + _VALUE)
_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*")
_HEADERS = re.compile(r"(?im)^([ \t]*(?:authorization|proxy-authorization)\s*:[ \t]*)[^\r\n]*")
_COOKIE_NAME = r"[!#$%&'*+.^_`|~0-9a-z-]+"
_COOKIE_VALUE = r'(?:"(?:\\.|[^"\\])*"|[^;\s,]*)'
_COOKIE = re.compile(
    r"(?i)(\b(?:set-)?cookie\s*:[ \t]*)(?:"
    + _COOKIE_NAME
    + "="
    + _COOKIE_VALUE
    + r"(?:;[ \t]*"
    + _COOKIE_NAME
    + "(?:="
    + _COOKIE_VALUE
    + ")?)*"
    + r"|[^\s,]+)"
)
_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]{0,20}://)[^/\s@]+@", re.I)
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-(?:proj-|ant-)?[a-z0-9_-]{16,}|(?:gh[pousr]_|github_pat_)[a-z0-9_]{16,}|"
    r"xox[baprs]-[a-z0-9-]{12,}|eyJ[a-z0-9_-]+\.eyJ[a-z0-9_-]+\.[a-z0-9_-]+)\b",
    re.I,
)
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S
)
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def operator_message(value: str) -> str:
    """Keep unknown errors/tracebacks useful without exporting recognized credentials.

    This is owner-authorized content, not the safe public summary. Deliberately
    retain paths, identities, URLs, arbitrary error prose, prompt/tool text and
    hashes. Apply credential scrubbing before the explicit head/tail size limit.
    """
    text = str(value).encode("utf-8", "replace").decode("utf-8")
    text = _CONTROL.sub("", _ANSI.sub("", text))
    text = _PEM.sub("[REDACTED PRIVATE KEY]", text)
    text = _HEADERS.sub(lambda m: m[1] + "[REDACTED]", text)
    text = _COOKIE.sub(lambda m: m[1] + "[REDACTED]", text)
    text = _AUTH.sub(lambda m: m[1] + " [REDACTED]", text)
    text = _USERINFO.sub(r"\1[REDACTED]@", text)
    text = _KNOWN_TOKEN.sub("[REDACTED]", text)
    text = _ASSIGNMENT.sub(lambda m: m[1] + '"[REDACTED]"', text)
    text = _FLAG.sub(lambda m: m[1] + '"[REDACTED]"', text)
    if len(text) > MAX_OPERATOR_MESSAGE:
        marker = "\n… [log content shortened; beginning and end retained] …\n"
        head = 4096
        text = text[:head] + marker + text[-(MAX_OPERATOR_MESSAGE - head - len(marker)) :]
    return text
