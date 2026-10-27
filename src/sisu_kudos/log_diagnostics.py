"""Allowlisted operational diagnostics: enums/numbers, never provider/tool prose.

Match a known logger and its anchored emission format. Read the outer status and
bounded retry counters; do not search arbitrary messages for HTTP/error words.
"""

from __future__ import annotations

import re

Diagnostic = dict[str, str | int]

_EXECUTION_PREFIX = re.compile(r"^\[(?:subagent-[0-9]{1,4}|B[0-9]{1,6}:P[0-9]{1,6})\] ")

_ATTEMPT = re.compile(
    r"API call failed \(attempt (\d{1,4})/(\d{1,4})\) "
    r"error_type=([A-Za-z_][A-Za-z0-9_]{0,63})(?= |$)"
)
_EXHAUSTED = re.compile(r"API call failed after (\d{1,4}) retries[.:] ?")
_STREAM = re.compile(r"Streaming failed before delivery: Error code: ([1-5]\d{2})(?= -|:|$)")
_ERRNO = re.compile(r"Error analyzing image: \[Errno ([1-9]\d{0,3})\](?: |$)")
_HTTP = re.compile(r"HTTP\s+([1-5]\d{2})(?=[\s:.,]|$)")
_VISION_HTTP = re.compile(r"(?:Error code: |HTTP\s+)([1-5]\d{2})(?=[\s:.,-]|$)")
_ERROR_TYPES = {
    "APIError",
    "APIConnectionError",
    "APITimeoutError",
    "AuthenticationError",
    "PermissionDeniedError",
    "RateLimitError",
    "BadRequestError",
    "NotFoundError",
    "InternalServerError",
    "ClaudeAPIError",
    "TimeoutError",
    "ConnectionError",
}
_CAUSES = {
    400: "invalid_request",
    401: "authentication",
    403: "permission_denied",
    404: "not_found",
    408: "timeout",
    413: "request_too_large",
    422: "invalid_request",
    429: "rate_limit",
    500: "server_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "timeout",
}
_CAUSE_TEXT = {
    "unknown": "cause not classified",
    "invalid_request": "invalid request",
    "authentication": "authentication failed",
    "permission_denied": "permission denied",
    "not_found": "resource not found",
    "timeout": "timeout",
    "request_too_large": "request too large",
    "rate_limit": "usage or rate limit",
    "server_error": "server error",
    "bad_gateway": "upstream gateway error",
    "service_unavailable": "service unavailable",
    "connection": "connection failed",
    "broken_pipe": "broken pipe (EPIPE)",
    "cancelled": "request cancelled",
    "unclosed_client_session": "client session left unclosed",
    "unclosed_connector": "connection pool left unclosed",
}


def _vision_diagnostic(message: str) -> Diagnostic | None:
    prefix = "Error analyzing image: "
    if not message.startswith(prefix):
        return None
    detail = message[len(prefix) :]
    data: Diagnostic = {
        "kind": "vision",
        "cause": {
            "Claude request cancelled": "cancelled",
            "Request timed out.": "timeout",
            "Connection error.": "connection",
        }.get(detail, "unknown"),
    }
    if match := _ERRNO.match(message):
        number = int(match[1])
        if number <= 4095:
            data.update(
                errno=number,
                cause={
                    2: "not_found",
                    13: "permission_denied",
                    32: "broken_pipe",
                    104: "connection",
                    110: "timeout",
                    111: "connection",
                }.get(number, "unknown"),
            )
    elif match := _VISION_HTTP.match(detail):
        status = int(match[1])
        data.update(http_status=status, cause=_CAUSES.get(status, "unknown"))
    # An unknown exception still identifies the image operation. Never copy its
    # prose or search its private body for status codes/cancellation keywords.
    return data


def diagnose(component: str, message: str, level: str) -> Diagnostic | None:
    """Return a bounded structured projection, or None for unsupported evidence."""
    if level not in {"WARNING", "ERROR", "CRITICAL"}:
        return None
    message = message[:4096]
    if component in {"agent.conversation_loop", "agent.chat_completion_helpers"}:
        # Hermes delegate_tool / batch_runner add these bounded worker labels.
        # Normalize only the diagnostic view, never the raw event/identity input.
        message = _EXECUTION_PREFIX.sub("", message, count=1)
    if component == "asyncio":
        cause = {
            "Unclosed client session": "unclosed_client_session",
            "Unclosed connector": "unclosed_connector",
        }.get(message)
        return {"kind": "async_resource", "cause": cause} if cause else None
    data: Diagnostic
    status = None
    if component == "tools.vision_tools":
        return _vision_diagnostic(message)
    if component == "agent.chat_completion_helpers" and (match := _STREAM.match(message)):
        data = {"kind": "api_stream", "cause": "unknown"}
        status = int(match[1])
    elif component == "agent.conversation_loop":
        if match := _ATTEMPT.match(message):
            attempt, maximum = int(match[1]), int(match[2])
            if not 1 <= attempt <= maximum <= 1000:
                return None
            data = {
                "kind": "api_request",
                "cause": "unknown",
                "attempt": attempt,
                "max_attempts": maximum,
            }
            if match[3] in _ERROR_TYPES:
                data["error_type"] = match[3]
            summary = message[match.end() :].partition(" summary=")[2]
            if code := _HTTP.match(summary):
                status = int(code[1])
        elif match := _EXHAUSTED.match(message):
            retries = int(match[1])
            if not 1 <= retries <= 1000:
                return None
            data = {"kind": "api_request", "cause": "unknown", "retries_exhausted": retries}
            if code := _HTTP.match(message[match.end() :]):
                status = int(code[1])
        else:
            return None
    else:
        return None
    if status is not None:
        data.update(http_status=status, cause=_CAUSES.get(status, "unknown"))
    elif data.get("error_type") in {"APITimeoutError", "TimeoutError"}:
        data["cause"] = "timeout"
    elif data.get("error_type") in {"APIConnectionError", "ConnectionError"}:
        data["cause"] = "connection"
    return data


def diagnostic_message(data: Diagnostic) -> str:
    """Useful English REST fallback, built solely from the structured projection."""
    prefix = {
        "vision": "Image analysis failed",
        "api_stream": "Model response stream failed",
        "api_request": "Model API request failed",
        "async_resource": "HTTP client cleanup incomplete",
    }[str(data["kind"])]
    cause = _CAUSE_TEXT[str(data["cause"])]
    details = [f"HTTP {data['http_status']} ({cause})" if "http_status" in data else cause]
    if "attempt" in data:
        details.append(f"attempt {data['attempt']}/{data['max_attempts']}")
    if "retries_exhausted" in data:
        details.append(f"API retries exhausted ({data['retries_exhausted']})")
    if "error_type" in data:
        details.append(str(data["error_type"]))
    if "errno" in data:
        details.append(f"errno {data['errno']}")
    return prefix + ": " + "; ".join(details)
