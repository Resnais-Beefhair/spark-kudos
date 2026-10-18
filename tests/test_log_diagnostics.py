"""Useful diagnostics from real logger shapes, without arbitrary private prose."""

import json

import pytest

from sisu_kudos.logs import parse_log_line


def parse(component, message, level="ERROR"):
    row = parse_log_line(
        f"2026-08-19 18:28:21,074 {level} {component}: {message}", "errors", "Worker"
    )
    assert row is not None
    return row


@pytest.mark.parametrize(
    "component,message,expected",
    [
        (
            "tools.vision_tools",
            "Error analyzing image: [Errno 32] Broken pipe",
            {"kind": "vision", "cause": "broken_pipe", "errno": 32},
        ),
        (
            "agent.chat_completion_helpers",
            "Streaming failed before delivery: Error code: 503 - "
            "{'error': {'message': 'local model unavailable', 'type': 'local_route_error'}}",
            {"kind": "api_stream", "cause": "service_unavailable", "http_status": 503},
        ),
        (
            "agent.conversation_loop",
            "API call failed (attempt 3/3) error_type=InternalServerError "
            "thread=PRIVATE_THREAD provider=PRIVATE_PROVIDER base_url=PRIVATE_URL "
            'model=PRIVATE_MODEL summary=HTTP 503 {"error":{"message":"PRIVATE_BODY"}}',
            {
                "kind": "api_request",
                "cause": "service_unavailable",
                "attempt": 3,
                "max_attempts": 3,
                "error_type": "InternalServerError",
                "http_status": 503,
            },
        ),
        (
            "agent.conversation_loop",
            "API call failed after 3 retries. HTTP 429: PRIVATE_UPSTREAM_TEXT "
            "Authorization: Basic PRIVATE_CREDENTIAL",
            {
                "kind": "api_request",
                "cause": "rate_limit",
                "retries_exhausted": 3,
                "http_status": 429,
            },
        ),
        (
            "agent.conversation_loop",
            "API call failed (attempt 1/3) error_type=APITimeoutError summary=PRIVATE_BODY",
            {
                "kind": "api_request",
                "cause": "timeout",
                "attempt": 1,
                "max_attempts": 3,
                "error_type": "APITimeoutError",
            },
        ),
    ],
)
def test_real_shapes_have_specific_safe_diagnostics(component, message, expected):
    row = parse(component, message)
    assert row is not None
    assert row.get("diagnostic") == expected
    assert row["component"] == component
    assert row["status"] == "recorded"  # A historical error is not current outage proof.
    assert "PRIVATE" not in json.dumps(row)
    assert "[REDACTED]" not in row["message"]
    assert "local log" not in row["message"]


@pytest.mark.parametrize(
    "status,cause",
    [
        (400, "invalid_request"),
        (401, "authentication"),
        (403, "permission_denied"),
        (408, "timeout"),
        (413, "request_too_large"),
        (429, "rate_limit"),
        (500, "server_error"),
        (502, "bad_gateway"),
        (503, "service_unavailable"),
        (504, "timeout"),
    ],
)
def test_http_status_is_the_trusted_outer_status_not_provider_body(status, cause):
    row = parse(
        "agent.conversation_loop",
        f"API call failed after 3 retries. HTTP {status}: PRIVATE_BODY HTTP 200",
    )
    assert row["diagnostic"]["http_status"] == status
    assert row["diagnostic"]["cause"] == cause
    assert str(status) in row["message"]
    assert "PRIVATE" not in json.dumps(row)


@pytest.mark.parametrize(
    "component,message",
    [
        ("unknown.logger", "API call failed after 3 retries. HTTP 429: PRIVATE_BODY"),
        ("gateway.run", "inbound message: msg='API call failed after 3 retries. HTTP 503'"),
        ("agent.tool_executor", 'Tool terminal returned error: {"error":"HTTP 503 PRIVATE_BODY"}'),
        ("agent.conversation_loop", "PRIVATE_BODY API call failed after 3 retries. HTTP 503"),
        ("agent.conversation_loop", "API call failed (attempt 4/3) error_type=PRIVATE_EXCEPTION"),
        ("agent.conversation_loop", "API call failed (attempt 0/3) error_type=PRIVATE_EXCEPTION"),
        (
            "agent.conversation_loop",
            "API call failed (attempt 9999/9999) error_type=PRIVATE_EXCEPTION",
        ),
        ("tools.vision_tools", "PRIVATE_BODY [Errno 32] Broken pipe"),
    ],
)
def test_non_diagnostic_messages_never_become_diagnostic_or_leak(component, message):
    row = parse(component, message)
    assert "diagnostic" not in row
    assert "PRIVATE" not in json.dumps(row)


def test_unknown_error_type_is_not_copied_but_safe_http_status_survives():
    row = parse(
        "agent.conversation_loop",
        "API call failed (attempt 1/3) error_type=PRIVATE_EXCEPTION summary=HTTP 503 PRIVATE_BODY",
    )
    assert row["diagnostic"]["http_status"] == 503
    assert "error_type" not in row["diagnostic"]
    assert "PRIVATE" not in json.dumps(row)


def test_enrichment_does_not_change_historical_event_identity():
    line = (
        "2026-08-19 18:28:21,074 ERROR tools.vision_tools: "
        "Error analyzing image: [Errno 32] Broken pipe"
    )
    first = parse_log_line(line, "agent", "Worker")
    second = parse_log_line(line, "errors", "Worker")
    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert first["level"] == second["level"] == "ERROR"
