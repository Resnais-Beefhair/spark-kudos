"""Safe projection and freshness tests for the optional agent resource registry."""

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from sisu_kudos.resources import read_resource_snapshot

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
ROW = {
    "id": "resource-1",
    "platform": "hermes",
    "profile": "assistant",
    "task_id": "task-42",
    "kind": "browser",
    "label": "Chrome browser",
    "pid": 1234,
    "started_at": "2026-10-14T08:55:00Z",
    "cpu_percent": 12.5,
    "memory_bytes": 4096,
    "state": "active",
    "managed": True,
    "persistent": True,
    "persistence_reason": "User session remains open",
    "cleanup_reason": None,
}


def write_snapshot(path: Path, *, sampled_at: str, resources=None, **extra):
    payload = {
        "schema_version": 1,
        "sampled_at": sampled_at,
        "resources": [ROW] if resources is None else resources,
        "warnings": [],
        **extra,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_snapshot_is_explicitly_unavailable(tmp_path):
    result = read_resource_snapshot(tmp_path / "missing.json", now=NOW)
    assert result["status"] == "unavailable"
    assert result["available"] is False
    assert result["resources"] == []
    assert result["age_seconds"] is None


def test_fresh_snapshot_keeps_unknown_load_null_and_adds_resource_age(tmp_path):
    row = {**ROW, "cpu_percent": None, "memory_bytes": None}
    path = tmp_path / "resources.json"
    write_snapshot(path, sampled_at="2026-10-14T08:59:50Z", resources=[row])
    result = read_resource_snapshot(path, now=NOW)
    assert result["status"] == "available"
    assert result["age_seconds"] == 10
    assert result["resources"][0]["age_seconds"] == 300
    assert result["resources"][0]["cpu_percent"] is None
    assert result["resources"][0]["memory_bytes"] is None


def test_stale_snapshot_remains_visible_as_stale(tmp_path):
    path = tmp_path / "resources.json"
    write_snapshot(path, sampled_at="2026-10-14T08:58:00Z")
    result = read_resource_snapshot(path, now=NOW)
    assert result["status"] == "stale"
    assert result["stale"] is True
    assert len(result["resources"]) == 1


def test_projection_drops_private_raw_url_argv_and_secret_fields(tmp_path):
    row = {
        **ROW,
        "label": "Chrome https://private.example/session?token=hidden",
        "persistence_reason": "python3 -c 'do private thing' --secret hidden",
        "cleanup_reason": 'api_key="hidden secret"',
        "private_argv": "do not serialize",
    }
    path = tmp_path / "resources.json"
    write_snapshot(
        path,
        sampled_at="2026-10-14T08:59:50Z",
        resources=[row],
        warnings=[
            "credential=hidden at https://private.example/path",
            "path /home/demo/.openclaw/session.json",
        ],
        private="not allowed",
    )
    result = read_resource_snapshot(path, now=NOW)
    encoded = json.dumps(result)
    assert "private.example" not in encoded
    assert "hidden" not in encoded
    assert "python3" not in encoded
    assert "private_argv" not in encoded
    assert "/home/demo" not in encoded
    assert "not allowed" not in encoded
    assert "[argv redacted]" in encoded


def test_invalid_rows_and_unsupported_schema_are_not_claimed(tmp_path):
    path = tmp_path / "resources.json"
    write_snapshot(
        path, sampled_at="2026-10-14T08:59:50Z", resources=[{**ROW, "platform": "other"}]
    )
    result = read_resource_snapshot(path, now=NOW)
    assert result["resources"] == []
    assert any("invalid registry row" in item for item in result["warnings"])
    write_snapshot(path, sampled_at="2026-10-14T08:59:50Z", schema_version=2)
    assert read_resource_snapshot(path, now=NOW)["status"] == "unavailable"


def test_api_reads_explicit_enabled_path_and_returns_only_safe_projection(tmp_path):
    path = tmp_path / "resources.json"
    write_snapshot(path, sampled_at="2026-10-14T08:59:50Z")
    from sisu_kudos.app import create_app
    from sisu_kudos.config import Settings

    configured = create_app(Settings(resources_enabled=True, resource_snapshot_path=path))
    response = TestClient(configured).get("/api/agent-resources")
    assert response.status_code == 200
    body = response.json()
    assert body["resources"][0]["task_id"] == "task-42"
    assert body["resources"][0]["memory_bytes"] == 4096
    assert set(body["resources"][0]) == {
        "id",
        "platform",
        "profile",
        "task_id",
        "kind",
        "label",
        "pid",
        "started_at",
        "age_seconds",
        "cpu_percent",
        "memory_bytes",
        "state",
        "managed",
        "persistent",
        "persistence_reason",
        "cleanup_reason",
    }
