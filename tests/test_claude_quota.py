import json
import urllib.error
from datetime import UTC, datetime
from email.message import Message

import pytest

import sisu_kudos.claude_quota as claude_quota
from sisu_kudos.claude_quota import ClaudeQuotaCollector

_TOKEN = "synthetic-oauth-token-do-not-leak"
_EXPIRY_MS = 4_102_444_800_000
_SECRET_MARKERS = ("private@example.test", "account-secret-id", "scope-secret", _TOKEN)


class FakeResponse:
    def __init__(self, body=b"{}", *, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


def credentials_file(tmp_path, *, token=_TOKEN, expiry=_EXPIRY_MS, extra=None):
    oauth = {
        "accessToken": token,
        "expiresAt": expiry,
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_5x",
        "email": _SECRET_MARKERS[0],
        "accountId": _SECRET_MARKERS[1],
        "scopes": [_SECRET_MARKERS[2]],
    }
    if extra:
        oauth.update(extra)
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return path


def enable_fake_opener(monkeypatch, opener):
    captured = {}

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return opener

    monkeypatch.setattr(claude_quota.urllib.request, "build_opener", build_opener)
    return captured


def test_reads_usage_from_fixed_endpoint_and_returns_only_safe_fields(tmp_path, monkeypatch):
    path = credentials_file(tmp_path)
    original = path.read_bytes()
    payload = {
        "five_hour": {"utilization": 0, "resets_at": "2026-10-16T18:39:59.820787+00:00"},
        "seven_day": {"utilization": 27.5, "resets_at": "2026-10-19T07:59:59.820807Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "seven_day_oauth_apps": None,
        "extra_usage": {"is_enabled": False, "sensitive": "ignored"},
        "unknown_future_window": {"utilization": 99},
        "accountId": _SECRET_MARKERS[1],
    }
    opener = FakeOpener(FakeResponse(json.dumps(payload).encode()))
    captured = enable_fake_opener(monkeypatch, opener)
    collector = ClaudeQuotaCollector(path)

    result = collector()

    assert result["provider"] == "Claude"
    assert result["available"] is True
    assert result["state"] == "available"
    assert result["status"] == "Max 5×"
    assert [window["id"] for window in result["windows"]] == ["five_hour", "seven_day"]
    assert result["windows"][0] == {
        "id": "five_hour",
        "label": "5-hour window",
        "remaining_percent": 100.0,
        "used_percent": 0.0,
        "duration_minutes": 300,
        "resets_at": "2026-10-16T18:39:59.820787+00:00",
    }
    assert result["windows"][1]["remaining_percent"] == 72.5
    assert result["windows"][1]["duration_minutes"] == 10_080
    assert datetime.fromisoformat(result["collected_at"]).tzinfo == UTC
    request, timeout = opener.calls[0]
    assert request.full_url == "https://api.anthropic.com/api/oauth/usage"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {_TOKEN}"
    assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"
    assert timeout == 5
    assert any(isinstance(handler, claude_quota._NoRedirect) for handler in captured["handlers"])
    assert path.read_bytes() == original
    rendered = repr(result) + repr(collector)
    assert all(marker not in rendered for marker in _SECRET_MARKERS)
    assert str(path) not in rendered


@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        "{}",
        json.dumps({"claudeAiOauth": {"accessToken": _TOKEN}}),
        json.dumps(
            {"claudeAiOauth": {"accessToken": _TOKEN, "expiresAt": 1, "subscriptionType": "max"}}
        ),
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "bad\r\nheader",
                    "expiresAt": _EXPIRY_MS,
                    "subscriptionType": "max",
                }
            }
        ),
    ],
)
def test_missing_invalid_or_expired_credentials_require_login(tmp_path, monkeypatch, contents):
    path = tmp_path / ".credentials.json"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(
        claude_quota.urllib.request,
        "build_opener",
        lambda *_args: pytest.fail("invalid auth must not make a request"),
    )

    result = ClaudeQuotaCollector(path)()

    assert result["state"] == "login_required"
    assert result["available"] is False
    assert result["windows"] == []
    assert all(marker not in repr(result) for marker in _SECRET_MARKERS)


def test_missing_credentials_file_is_login_required_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_quota.urllib.request,
        "build_opener",
        lambda *_args: pytest.fail("missing credentials must not make a request"),
    )
    result = ClaudeQuotaCollector(tmp_path / "missing.json")()
    assert result["state"] == "login_required"


@pytest.mark.parametrize("utilization", [True, float("inf"), float("nan"), -0.1, 100.1, "0"])
def test_invalid_utilization_is_not_coerced_or_published(tmp_path, monkeypatch, utilization):
    credentials = credentials_file(tmp_path)
    payload = {"five_hour": {"utilization": utilization}}
    opener = FakeOpener(FakeResponse(json.dumps(payload).encode()))
    enable_fake_opener(monkeypatch, opener)

    result = ClaudeQuotaCollector(credentials)()

    assert result["state"] == "invalid_response"
    assert result["windows"] == []


def test_huge_integer_utilization_is_rejected_without_overflow(tmp_path, monkeypatch):
    credentials = credentials_file(tmp_path)
    payload = {"five_hour": {"utilization": 10**1_000}}
    opener = FakeOpener(FakeResponse(json.dumps(payload).encode()))
    enable_fake_opener(monkeypatch, opener)

    result = ClaudeQuotaCollector(credentials)()

    assert result["state"] == "invalid_response"
    assert result["windows"] == []


def test_null_and_unknown_windows_do_not_become_fake_zeroes(tmp_path, monkeypatch):
    credentials = credentials_file(tmp_path)
    payload = {"five_hour": None, "seven_day_opus": None, "future": {"utilization": 0}}
    opener = FakeOpener(FakeResponse(json.dumps(payload).encode()))
    enable_fake_opener(monkeypatch, opener)

    result = ClaudeQuotaCollector(credentials)()

    assert result["state"] == "invalid_response"
    assert result["windows"] == []


def test_http_429_obeys_bounded_retry_after_without_eager_retry(tmp_path, monkeypatch):
    credentials = credentials_file(tmp_path)
    opener = FakeOpener(FakeResponse(status=429, headers={"Retry-After": "300"}))
    enable_fake_opener(monkeypatch, opener)
    monkeypatch.setattr(claude_quota.time, "monotonic", lambda: 100.0)
    collector = ClaudeQuotaCollector(credentials)

    first = collector()
    second = collector()

    assert first["state"] == second["state"] == "rate_limited"
    assert first["available"] is False
    assert first["windows"] == second["windows"] == []
    assert len(opener.calls) == 1
    assert all(marker not in repr(first) for marker in _SECRET_MARKERS)


def test_retry_after_is_capped_and_redirect_is_not_followed(tmp_path, monkeypatch):
    credentials = credentials_file(tmp_path)
    headers = Message()
    headers["Location"] = "https://attacker.invalid/collect"
    error = urllib.error.HTTPError(
        "https://api.anthropic.com/api/oauth/usage",
        302,
        "synthetic redirect",
        headers,
        None,
    )
    opener = FakeOpener(error=error)
    enable_fake_opener(monkeypatch, opener)
    monkeypatch.setattr(claude_quota.time, "monotonic", lambda: 0.0)
    collector = ClaudeQuotaCollector(credentials)

    result = collector()

    assert result["state"] == "unavailable"
    assert len(opener.calls) == 1
    assert opener.calls[0][0].full_url == "https://api.anthropic.com/api/oauth/usage"
    assert all(marker not in repr(result) for marker in _SECRET_MARKERS)
    assert claude_quota._retry_after_seconds("999999999999") == 3_600
    assert claude_quota._retry_after_seconds(None) == 120


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("as_exception", [False, True])
def test_http_auth_failures_are_login_required_and_close_error_body(
    tmp_path, monkeypatch, status, as_exception
):
    import io

    credentials = credentials_file(tmp_path)
    body = io.BytesIO(b"must-not-be-exposed")
    opener = FakeOpener(
        response=FakeResponse(status=status),
        error=urllib.error.HTTPError(
            "https://api.anthropic.com/api/oauth/usage", status, "private error", Message(), body
        )
        if as_exception
        else None,
    )
    enable_fake_opener(monkeypatch, opener)
    result = ClaudeQuotaCollector(credentials)()
    assert result["state"] == "login_required"
    assert not result["available"] and result["windows"] == []
    assert "must-not-be-exposed" not in repr(result)
    if as_exception:
        assert body.closed


def test_oversized_and_malformed_responses_are_safe_failures(tmp_path, monkeypatch):
    credentials = credentials_file(tmp_path)
    opener = FakeOpener(FakeResponse(b" " * (claude_quota._RESPONSE_LIMIT + 1)))
    enable_fake_opener(monkeypatch, opener)
    result = ClaudeQuotaCollector(credentials)()
    assert result["state"] == "invalid_response"
    assert result["windows"] == []

    opener.response = FakeResponse(b"not-json")
    result = ClaudeQuotaCollector(credentials)()
    assert result["state"] == "invalid_response"
    assert result["windows"] == []


def test_reset_timestamp_must_be_timezone_aware(tmp_path, monkeypatch):
    credentials = credentials_file(tmp_path)
    opener = FakeOpener(
        FakeResponse(
            json.dumps(
                {"five_hour": {"utilization": 50, "resets_at": "2026-10-16T18:39:59"}}
            ).encode()
        )
    )
    enable_fake_opener(monkeypatch, opener)

    result = ClaudeQuotaCollector(credentials)()

    assert result["state"] == "available"
    assert result["windows"][0]["resets_at"] is None
