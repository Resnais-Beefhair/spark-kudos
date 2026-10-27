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
            "asyncio",
            "Unclosed client session",
            {"kind": "async_resource", "cause": "unclosed_client_session"},
        ),
        (
            "asyncio",
            "Unclosed connector",
            {"kind": "async_resource", "cause": "unclosed_connector"},
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
        ("unknown.logger", "Unclosed client session"),
        ("gateway.run", "inbound message: msg='Unclosed connector'"),
        ("asyncio", "PRIVATE_BODY Unclosed client session"),
        ("asyncio", "Unclosed client session PRIVATE_BODY"),
        ("asyncio", "Unclosed connector: PRIVATE_BODY"),
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


@pytest.mark.parametrize("prefix", ["[subagent-0] ", "[subagent-42] ", "[B12:P7] "])
@pytest.mark.parametrize(
    "component,message",
    [
        (
            "agent.conversation_loop",
            "API call failed after 3 retries. HTTP 429: PRIVATE_BODY HTTP 503",
        ),
        (
            "agent.conversation_loop",
            "API call failed (attempt 2/3) error_type=InternalServerError "
            "summary=HTTP 503: PRIVATE_BODY",
        ),
        (
            "agent.chat_completion_helpers",
            "Streaming failed before delivery: Error code: 503 - PRIVATE_BODY",
        ),
    ],
)
def test_worker_prefix_does_not_hide_supported_agent_diagnostics(prefix, component, message):
    plain = parse(component, message)
    child = parse(component, prefix + message)
    assert child.get("diagnostic") == plain["diagnostic"]
    assert child["message"] == plain["message"]
    assert "PRIVATE" not in json.dumps(child)
    assert prefix.strip() not in json.dumps(child)
    # Prefix belongs to event identity, not its mutable diagnostic projection.
    assert child["id"] != plain["id"]
    assert child["level"] == "ERROR" and child["status"] == "recorded"


@pytest.mark.parametrize(
    "component,message",
    [
        ("unknown.logger", "[subagent-0] API call failed after 3 retries. HTTP 429: PRIVATE_BODY"),
        ("gateway.run", "[subagent-0] API call failed after 3 retries. HTTP 429: PRIVATE_BODY"),
        ("tools.vision_tools", "[subagent-0] Error analyzing image: Claude request cancelled"),
        ("agent.conversation_loop", "[PRIVATE_LABEL] API call failed after 3 retries. HTTP 429"),
        ("agent.conversation_loop", "[subagent-99999] API call failed after 3 retries. HTTP 429"),
        ("agent.conversation_loop", "[subagent-0]API call failed after 3 retries. HTTP 429"),
        (
            "agent.conversation_loop",
            "[subagent-0] [subagent-1] API call failed after 3 retries. HTTP 429",
        ),
        (
            "agent.conversation_loop",
            "[subagent-0] PRIVATE_BODY API call failed after 3 retries. HTTP 429",
        ),
    ],
)
def test_worker_prefix_is_not_arbitrary_bracket_or_private_body_stripping(component, message):
    row = parse(component, message)
    assert "diagnostic" not in row
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


@pytest.mark.parametrize("header", ["Unclosed client session", "Unclosed connector"])
def test_resource_diagnostic_excludes_private_continuations_and_preserves_identity(header):
    from sisu_kudos.log_events import merge_copies
    from sisu_kudos.logs import _parse_log_blocks

    line = f"2026-08-19 18:28:21,074 ERROR asyncio: {header}"
    lines = [
        line,
        "client_session: <aiohttp.client.ClientSession object PRIVATE_ADDRESS>",
        "connections: ['https://PRIVATE_HOST/?token=PRIVATE_TOKEN']",
        "connector: PRIVATE_CONNECTOR Authorization: Bearer PRIVATE_CREDENTIAL",
    ]
    baseline = parse_log_line(line, "errors", "Worker")
    assert baseline is not None
    rows = _parse_log_blocks(lines, "errors", "Worker", "worker")
    assert len(rows) == 1
    row = rows[0]
    assert row["diagnostic"]["kind"] == "async_resource"
    assert "PRIVATE" not in json.dumps(row)
    for key in ("id", "timestamp", "level", "category", "status"):
        assert row[key] == baseline[key]
    merged = merge_copies([row, dict(row, source="agent", sources=["agent"])])
    assert len(merged) == 1
    assert merged[0]["diagnostic"] == row["diagnostic"]
    assert merged[0]["sources"] == ["agent", "errors"]


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("Claude request cancelled", {"kind": "vision", "cause": "cancelled"}),
        ("Request timed out.", {"kind": "vision", "cause": "timeout"}),
        ("Connection error.", {"kind": "vision", "cause": "connection"}),
        (
            "Error code: 503 - PRIVATE_BODY HTTP 401",
            {"kind": "vision", "cause": "service_unavailable", "http_status": 503},
        ),
        (
            "HTTP 429: PRIVATE_BODY",
            {"kind": "vision", "cause": "rate_limit", "http_status": 429},
        ),
        (
            "[Errno 13] Permission denied: /example/PRIVATE_FILE",
            {"kind": "vision", "cause": "permission_denied", "errno": 13},
        ),
        (
            "PRIVATE_BODY says HTTP 503 and Claude request cancelled",
            {"kind": "vision", "cause": "unknown"},
        ),
        (
            "Claude request cancelled PRIVATE_BODY",
            {"kind": "vision", "cause": "unknown"},
        ),
    ],
)
def test_vision_operation_and_cause_are_projected_without_exception_prose(detail, expected):
    row = parse("tools.vision_tools", f"Error analyzing image: {detail}")
    assert row.get("diagnostic") == expected
    assert "PRIVATE" not in json.dumps(row)
    assert "[REDACTED]" not in row["message"]
    assert "Image analysis failed" in row["message"]


@pytest.mark.parametrize(
    "component,message,level",
    [
        ("unknown.logger", "Error analyzing image: Claude request cancelled", "ERROR"),
        (
            "tools.vision_tools",
            "User prompt: Error analyzing image: Claude request cancelled",
            "ERROR",
        ),
        ("tools.vision_tools", "Error analyzing image: Claude request cancelled", "INFO"),
        ("tools.vision_tools", "Error analyzing image EXTRA: Claude request cancelled", "ERROR"),
    ],
)
def test_vision_recognition_requires_its_logger_level_and_emission_prefix(
    component, message, level
):
    assert "diagnostic" not in parse(component, message, level)


def test_vision_cancellation_tracebacks_remain_private_and_events_stay_distinct():
    from sisu_kudos.log_events import merge_copies
    from sisu_kudos.logs import _parse_log_blocks

    headers = [
        "2026-08-19 18:28:21,862 ERROR tools.vision_tools: "
        "Error analyzing image: [Errno 32] Broken pipe",
        "2026-08-19 18:28:21,872 ERROR tools.vision_tools: "
        "Error analyzing image: Claude request cancelled",
        "2026-08-19 18:28:21,897 ERROR tools.vision_tools: "
        "Error analyzing image: Claude request cancelled",
    ]
    lines = []
    for header in headers:
        lines.extend(
            [
                header,
                "Traceback (most recent call last):",
                "  PRIVATE_PROMPT https://PRIVATE_HOST/?token=PRIVATE_TOKEN",
                "RuntimeError: Claude request cancelled PRIVATE_CONTINUATION",
            ]
        )
    rows = _parse_log_blocks(lines, "errors", "Worker", "worker")
    assert len(rows) == 3
    assert len({row["id"] for row in rows}) == 3
    assert [row["diagnostic"]["cause"] for row in rows] == ["broken_pipe", "cancelled", "cancelled"]
    assert "PRIVATE" not in json.dumps(rows)
    for row, header in zip(rows, headers, strict=True):
        baseline = parse_log_line(header, "agent", "Worker", profile="worker")
        assert baseline is not None
        for key in ("id", "timestamp", "level", "status", "category"):
            assert row[key] == baseline[key]
        assert len(merge_copies([row, baseline])) == 1
