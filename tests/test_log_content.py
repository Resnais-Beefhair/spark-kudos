"""Owner-authorized content must not depend on an error-message allowlist."""

import json
from pathlib import Path

import pytest

from sisu_kudos.log_content import MAX_OPERATOR_MESSAGE, operator_message
from sisu_kudos.logs import _parse_log_blocks, collect_hermes_logs, parse_log_line


def test_operator_log_content_is_explicit_strict_opt_in(tmp_path):
    from sisu_kudos.config import ConfigurationError, Settings, load_settings

    path = tmp_path / "operator.toml"
    assert not Settings().log_details_enabled
    for value, expected in (("false", False), ("true", True)):
        path.write_text(f"[logs]\nshow_details={value}\n")
        assert load_settings(path).log_details_enabled is expected
    for value in ('"true"', '"false"', "1", "[]"):
        path.write_text(f"[logs]\nshow_details={value}\n")
        with pytest.raises(ConfigurationError, match="logs.show_details"):
            load_settings(path)


def test_owner_api_responses_are_not_cacheable():
    from fastapi.testclient import TestClient

    from sisu_kudos.app import create_app
    from sisu_kudos.config import Settings

    client = TestClient(create_app(Settings(log_details_enabled=True)))
    # Disabled resource route needs no lifespan/hardware polling.
    response = client.get("/api/agent-resources")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_openclaw_opt_in_and_cache_cannot_leak_details_back_to_safe_mode(monkeypatch):
    from types import SimpleNamespace

    from sisu_kudos import logs

    entry = {
        "_SYSTEMD_USER_UNIT": "openclaw-gateway.service",
        "PRIORITY": "3",
        "__REALTIME_TIMESTAMP": "1724061000000000",
        "_CURSOR": "example-cursor",
        "MESSAGE": "UnknownGatewayError: actual reason password=EXAMPLE_CREDENTIAL",
    }
    safe = logs._openclaw_record(entry)
    own = logs._openclaw_record(entry, show_details=True)
    assert own is not None and safe is not None
    assert "original_message" not in safe
    assert own["id"] == safe["id"]
    assert "actual reason" in own["original_message"]
    assert "EXAMPLE_CREDENTIAL" not in own["original_message"]
    old = logs._cached_openclaw_rows(show_details=True)
    try:
        logs._remember_openclaw_rows([own])
        monkeypatch.setattr(
            logs.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr=""),
        )
        assert all("original_message" not in row for row in logs.collect_openclaw_logs()["records"])
        assert any(
            "original_message" in row
            for row in logs.collect_openclaw_logs(show_details=True)["records"]
        )
    finally:
        logs._remember_openclaw_rows(old)


def test_preserves_unknown_prose_identifiers_paths_and_prompt_or_tool_text():
    value = (
        "SomeNewError: why it actually failed; /example/operator/config "
        "session=example-session channel=123456789012345678 "
        'person@example.test prompt="my own prompt" tool_result="my own result" '
        "https://example.test/api?model=demo&attempt=3 sha256=" + "f" * 64
    )
    assert operator_message(value) == value


@pytest.mark.parametrize(
    "value,secret",
    [
        ('API_KEY="EXAMPLE_CREDENTIAL"; reason=actual error', "EXAMPLE_CREDENTIAL"),
        ('{"api_key":"EXAMPLE_CREDENTIAL", "error":"actual error"}', "EXAMPLE_CREDENTIAL"),
        ("OPENAI_API_KEY=EXAMPLE_CREDENTIAL reason=actual error", "EXAMPLE_CREDENTIAL"),
        ("access_token=EXAMPLE_CREDENTIAL&error=actual", "EXAMPLE_CREDENTIAL"),
        (
            "Authorization: Bearer EXAMPLE_CREDENTIAL\nActual error on next line",
            "EXAMPLE_CREDENTIAL",
        ),
        (
            "failed Authorization: Basic EXAMPLE_CREDENTIAL because actual connection failed",
            "EXAMPLE_CREDENTIAL",
        ),
        ("Cookie: sessionid=EXAMPLE_CREDENTIAL; other=value\nActual error", "EXAMPLE_CREDENTIAL"),
        (
            "postgresql://owner:EXAMPLE_CREDENTIAL@example.test/db?error=actual",
            "EXAMPLE_CREDENTIAL",
        ),
        (
            "https://example.test/a?X-Amz-Signature=EXAMPLE_CREDENTIAL&error=actual",
            "EXAMPLE_CREDENTIAL",
        ),
        (
            'command --password "EXAMPLE_CREDENTIAL with spaces" --reason actual',
            "EXAMPLE_CREDENTIAL",
        ),
        (
            f"-----BEGIN {'PRIVATE KEY'}-----\nEXAMPLE_CREDENTIAL\n"
            f"-----END {'PRIVATE KEY'}-----\nActual error",
            "EXAMPLE_CREDENTIAL",
        ),
    ],
)
def test_masks_only_recognized_credentials(value, secret):
    output = operator_message(value)
    assert secret not in output
    assert "[REDACTED" in output
    assert "actual" in output.lower()


@pytest.mark.parametrize(
    "value",
    [
        "AWS_SECRET_ACCESS_KEY=EXAMPLE_CREDENTIAL reason=actual error",
        '{"SecretAccessKey":"EXAMPLE_CREDENTIAL", "error":"actual error"}',
        "--client-secret EXAMPLE_CREDENTIAL --access-token EXAMPLE_CREDENTIAL reason=actual error",
        "DEBUG request Cookie: session=EXAMPLE_CREDENTIAL; refresh=EXAMPLE_TWO reason=actual error",
    ],
)
def test_review_regressions_nested_secret_names_flags_and_inline_cookies(value):
    text = operator_message(value)
    assert "EXAMPLE_CREDENTIAL" not in text
    assert "EXAMPLE_TWO" not in text
    assert "actual error" in text


def test_bounds_content_explicitly_retaining_beginning_and_final_exception():
    value = "Original error header\n" + "a" * 40_000 + "\nValueError: actual reason"
    text = operator_message(value)
    assert len(text) == MAX_OPERATOR_MESSAGE
    assert text.startswith("Original error header")
    assert text.endswith("ValueError: actual reason")
    assert "shortened" in text


def test_sanitizes_terminal_controls_but_not_newlines_or_html_as_data():
    assert (
        operator_message("\x1b[31mError\x1b[0m\n<img src=x onerror=alert(1)>\x00")
        == "Error\n<img src=x onerror=alert(1)>"
    )


def test_default_remains_safe_and_operator_mode_retains_original_unknown_error():
    line = "2026-08-19 12:30:00,000 ERROR future.new_logger: PreviouslyUnknownError: actual reason"
    safe = parse_log_line(line, "errors", "Worker")
    own = parse_log_line(line, "errors", "Worker", show_details=True)
    assert safe is not None and own is not None
    assert "original_message" not in safe
    assert own["original_message"] == "PreviouslyUnknownError: actual reason"
    assert {k: v for k, v in own.items() if k != "original_message"} == safe
    assert "original_message" not in parse_log_line(line, "errors", "Worker", show_details="false")


def test_traceback_and_message_stay_together_and_credentials_do_not_leak():
    header = "2026-08-19 12:30:00,000 ERROR future.new_logger: Unknown error"
    lines = [
        header,
        "Traceback (most recent call last):",
        '  File "/example/config.py", line 4',
        '  password="EXAMPLE_CREDENTIAL"',
        "ValueError: actual final reason",
    ]
    safe = _parse_log_blocks(lines, "errors", "Worker", "worker")
    own = _parse_log_blocks(lines, "errors", "Worker", "worker", show_details=True)
    assert own[0]["id"] == safe[0]["id"]
    assert own[0]["original_message"].endswith("ValueError: actual final reason")
    assert "EXAMPLE_CREDENTIAL" not in json.dumps(own)
    assert "Traceback" in own[0]["original_message"]


def test_long_traceback_retains_final_exception_and_scrubs_before_shortening():
    header = "2026-08-19 12:30:00,000 ERROR future.logger: Deep traceback"
    marker = "PRIVATE KEY"
    lines = [
        header,
        f"-----BEGIN {marker}-----",
        *(["EXAMPLE_CREDENTIAL"] * 160),
        f"-----END {marker}-----",
        "ValueError: final actual cause",
    ]
    rows = _parse_log_blocks(lines, "errors", "Worker", "worker", show_details=True)
    text = rows[0]["original_message"]
    assert text.endswith("ValueError: final actual cause")
    assert "EXAMPLE_CREDENTIAL" not in text
    assert "[REDACTED PRIVATE KEY]" in text


def test_collector_requires_explicit_opt_in(tmp_path: Path):
    (tmp_path / "errors.log").write_text(
        "2026-08-19 12:30:00,000 ERROR future.logger: Actual operator error\n"
    )
    assert "original_message" not in collect_hermes_logs(tmp_path)["records"][0]
    assert (
        collect_hermes_logs(tmp_path, show_details=True)["records"][0]["original_message"]
        == "Actual operator error"
    )
