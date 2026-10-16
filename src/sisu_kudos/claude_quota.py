"""Opt-in, read-only Claude subscription usage collector.

This module reads the existing Claude Code OAuth credential file but never refreshes,
rewrites, logs, or returns credentials. Network access is limited to Anthropic's fixed
usage endpoint and is intentionally performed only when the caller opts in by using it.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
_CREDENTIAL_LIMIT = 65_536
_RESPONSE_LIMIT = 131_072
_TIMEOUT_SECONDS = 5
_MAX_COOLDOWN_SECONDS = 3_600
_DEFAULT_COOLDOWN_SECONDS = 120
_WINDOWS: tuple[tuple[str, str, int], ...] = (
    ("five_hour", "5-hour window", 300),
    ("seven_day", "7-day window", 10_080),
    ("seven_day_opus", "7-day Opus window", 10_080),
    ("seven_day_sonnet", "7-day Sonnet window", 10_080),
    ("seven_day_oauth_apps", "7-day OAuth apps window", 10_080),
)
_MISSING = object()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the bearer token cannot leave the fixed origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ClaudeQuotaCollector:
    """Read Claude Code OAuth usage when explicitly called; never mutate credentials."""

    def __init__(self, credentials_path: Path | None = None) -> None:
        self._credentials_path = credentials_path or Path.home() / ".claude" / ".credentials.json"
        self._cooldown_until = 0.0

    def __call__(self) -> dict[str, Any]:
        """Return a browser-safe snapshot without exposing credentials or raw failures."""
        credentials = self._read_credentials()
        if credentials is None:
            return self._result("login_required")
        token, plan = credentials

        if time.monotonic() < self._cooldown_until:
            return self._result("rate_limited", status=plan)

        request = urllib.request.Request(
            _ENDPOINT,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
            method="GET",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status == 429:
                    headers = getattr(response, "headers", None)
                    retry_after = headers.get("Retry-After") if headers is not None else None
                    self._set_cooldown(retry_after)
                    return self._result("rate_limited", status=plan)
                if status in (401, 403):
                    return self._result("login_required")
                if status != 200:
                    return self._result("unavailable")
                body = response.read(_RESPONSE_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            try:
                if exc.code == 429:
                    retry_after = (
                        exc.headers.get("Retry-After") if exc.headers is not None else None
                    )
                    self._set_cooldown(retry_after)
                    return self._result("rate_limited", status=plan)
                if exc.code in (401, 403):
                    return self._result("login_required")
                # Refused redirects are not followed or reported verbatim.
                return self._result("unavailable")
            finally:
                exc.close()
        except Exception:
            # Transport/library exceptions may contain the URL or other internals.
            return self._result("unavailable")

        if len(body) > _RESPONSE_LIMIT:
            return self._result("invalid_response")
        try:
            payload = json.loads(body)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return self._result("invalid_response")
        windows = self._parse_windows(payload)
        if windows is None:
            return self._result("invalid_response")
        self._cooldown_until = 0.0
        return self._result("available", status=plan, windows=windows)

    def _read_credentials(self) -> tuple[str, str] | None:
        try:
            with self._credentials_path.open("rb") as handle:
                raw = handle.read(_CREDENTIAL_LIMIT + 1)
        except OSError:
            return None
        if len(raw) > _CREDENTIAL_LIMIT:
            return None
        try:
            data = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return None
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            return None
        token = oauth.get("accessToken")
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 8_192
            or not token.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            return None
        expires_at = oauth.get("expiresAt")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            return None
        try:
            expiry_ms = float(expires_at)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(expiry_ms) or expiry_ms <= time.time() * 1000:
            return None
        return token, _safe_plan(oauth)

    @staticmethod
    def _parse_windows(payload: Any) -> list[dict[str, Any]] | None:
        if not isinstance(payload, dict):
            return None
        windows: list[dict[str, Any]] = []
        for window_id, label, duration in _WINDOWS:
            value = payload.get(window_id, _MISSING)
            if value is _MISSING or value is None:
                continue
            if not isinstance(value, dict):
                return None
            utilization = value.get("utilization", _MISSING)
            if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
                return None
            try:
                used = float(utilization)
            except (OverflowError, ValueError):
                return None
            if not math.isfinite(used) or not 0 <= used <= 100:
                return None
            windows.append(
                {
                    "id": window_id,
                    "label": label,
                    "remaining_percent": 100.0 - used,
                    "used_percent": used,
                    "duration_minutes": duration,
                    "resets_at": _safe_reset(value.get("resets_at")),
                }
            )
        return windows if windows else None

    def _set_cooldown(self, retry_after: str | None) -> None:
        seconds = _retry_after_seconds(retry_after)
        self._cooldown_until = time.monotonic() + seconds

    @staticmethod
    def _result(
        state: str,
        *,
        status: str = "",
        windows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": "Claude",
            "available": state == "available",
            "status": status,
            "state": state,
            "windows": windows or [],
            "collected_at": datetime.now(UTC).isoformat(),
        }


def _safe_plan(oauth: dict[str, Any]) -> str:
    subscription = oauth.get("subscriptionType")
    tier = oauth.get("rateLimitTier")
    if subscription == "max":
        if isinstance(tier, str):
            return {
                "default_claude_max_5x": "Max 5×",
                "default_claude_max_20x": "Max 20×",
            }.get(tier, "Max")
        return "Max"
    if isinstance(subscription, str):
        return {
            "pro": "Pro",
            "free": "Free",
            "team": "Team",
            "enterprise": "Enterprise",
        }.get(subscription, "Claude")
    return "Claude"


def _safe_reset(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.isoformat()


def _retry_after_seconds(value: str | None) -> int:
    if isinstance(value, str) and re.fullmatch(r"\d{1,16}", value.strip()):
        seconds = int(value.strip())
    elif isinstance(value, str) and value:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = math.ceil((retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            seconds = _DEFAULT_COOLDOWN_SECONDS
    else:
        seconds = _DEFAULT_COOLDOWN_SECONDS
    return max(1, min(_MAX_COOLDOWN_SECONDS, seconds))
