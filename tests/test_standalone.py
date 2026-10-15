"""Standalone snapshots, configurable host IDs and disabled integration paths."""

import asyncio
import json
import subprocess
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from sisu_kudos.app import create_app
from sisu_kudos.config import Settings
from sisu_kudos.state import LiveState


class Status:
    def __init__(self, updated_at=None, error=""):
        self.updated_at = updated_at
        self.error = error
        self.last_duration_s = 0.01
        self.failures = int(bool(error))

    def age_s(self):
        return None if self.updated_at is None else max(0, time.time() - self.updated_at)

    def is_stale(self):
        return self.age_s() is None or self.age_s() > 30


class Scheduler:
    def __init__(self, names, values=None, statuses=None):
        self._names = list(names)
        self._values = values or {}
        self._statuses = statuses or {}

    def names(self):
        return self._names

    def value(self, name):
        return self._values.get(name)

    def state(self, name):
        return self._statuses.get(name, Status())


def test_default_snapshot_has_one_generic_host_and_safe_ui_metadata():
    live = LiveState(Settings())
    payload = asyncio.run(live.snapshot())
    assert list(payload["hosts"]) == ["local"]
    assert payload["hosts"]["local"]["label"] == "Spark 1"
    assert payload["hosts"]["local"]["role"] == "agents-services"
    assert payload["ui"] == {
        "language": "en",
        "local_host_id": "local",
        "features": {
            "agents": False,
            "logs": False,
            "crons": False,
            "resources": False,
            "quotas": False,
        },
    }
    assert "/home/" not in json.dumps(payload["ui"])
    assert "remote" not in payload["hosts"]


def test_arbitrary_configured_host_ids_remain_ordered_and_keep_independent_freshness():
    settings = Settings(
        local_host_id="spark-a",
        local_host_label="Compute A",
        local_host_role="workstation",
        remote_host="ops@spark-b",
        remote_hostname="node-b",
        remote_host_id="spark-b",
        remote_host_label="Compute B",
        remote_host_role="inference",
    )
    live = LiveState(settings)
    sample_time = time.time()
    remote_value = {
        "cpu": {"usage_percent": 22},
        "gpu": {"utilization_percent": 31},
        "ram": {"used_percent": 45},
        "network": {"rx_bytes_sec": 10, "tx_bytes_sec": 20},
    }
    live.scheduler = Scheduler(
        ["cpu", "ram", "network", "system", "remote:spark-b"],
        values={"remote:spark-b": remote_value},
        statuses={
            name: Status(sample_time)
            for name in ("cpu", "ram", "network", "system", "remote:spark-b")
        },
    )
    asyncio.run(live._publish({"remote:spark-b"}))
    payload = asyncio.run(live.snapshot())
    assert list(payload["hosts"]) == ["spark-a", "spark-b"]
    assert payload["hosts"]["spark-a"]["id"] == "spark-a"
    assert payload["hosts"]["spark-b"]["id"] == "spark-b"
    assert payload["hosts"]["spark-b"]["system"] == remote_value
    assert len(payload["hosts"]["spark-b"]["history"]) == 1
    assert payload["hosts"]["spark-b"]["health"]["ok"]


def test_remote_failure_retains_last_good_state_and_marks_host_stale():
    settings = Settings(remote_host="ssh-node", remote_hostname="node", remote_host_id="node")
    live = LiveState(settings)
    sample_time = time.time()
    old_value = {"cpu": {"usage_percent": 22}, "gpu": {}, "ram": {}, "network": {}}
    live.scheduler = Scheduler(
        ["cpu", "ram", "network", "system", "remote:node"],
        values={"remote:node": old_value},
        statuses={"remote:node": Status(sample_time)},
    )
    asyncio.run(live._publish({"remote:node"}))
    history_size = len(live._host_histories["node"])
    live.scheduler = Scheduler(
        ["cpu", "ram", "network", "system", "remote:node"],
        values={"remote:node": old_value},
        statuses={"remote:node": Status(sample_time, "private ssh output")},
    )
    asyncio.run(live._publish({"remote:node"}))
    host = asyncio.run(live.snapshot())["hosts"]["node"]
    assert host["system"] == old_value
    assert len(host["history"]) == history_size
    assert host["health"]["stale"] and not host["health"]["ok"]
    assert "private" not in json.dumps(host["health"])


def test_disabled_resource_api_does_not_read_environment_path(tmp_path, monkeypatch):
    private_path = tmp_path / "must-not-be-read.json"
    private_path.write_text('{"secret":"no"}', encoding="utf-8")
    monkeypatch.setenv("SISU_AGENT_RESOURCES_SNAPSHOT", str(private_path))

    async def no_start(self):
        return None

    monkeypatch.setattr(LiveState, "start", no_start)
    with TestClient(create_app(Settings())) as client:
        resource = client.get("/api/agent-resources").json()
        snapshot = client.get("/api/snapshot").json()
        event = asyncio.run(client.app.state.kudos_state.stream_snapshot())
    assert resource["status"] == "unavailable"
    assert resource["warnings"] == ["Resource integration is disabled"]
    assert "no" not in json.dumps(resource)
    assert snapshot["ui"]["features"]["resources"] is False
    assert event["ui"] == snapshot["ui"]


@pytest.mark.asyncio
async def test_default_live_state_runs_builtin_collectors_without_ssh_or_model_http(monkeypatch):
    from sisu_kudos import builtin

    def forbidden(*args, **kwargs):
        pytest.fail("an unconfigured optional network integration was invoked")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(builtin, "http_read", forbidden)
    live = LiveState(Settings())
    await live.start()
    try:
        names = set(live.scheduler.names())
        snapshot = await live.snapshot()
    finally:
        await live.stop()
    assert names == {"cpu", "ram", "gpu", "network", "system", "disks", "processes"}
    assert list(snapshot["hosts"]) == ["local"]
    assert snapshot["system"]["gpu"]["health_status"] == "unavailable"
    assert not snapshot["ui"]["features"]["agents"]


def test_enabled_resource_api_reads_only_configured_safe_snapshot(tmp_path, monkeypatch):
    sampled = datetime.now(UTC).isoformat()
    snapshot_path = tmp_path / "resources.json"
    snapshot_path.write_text(
        json.dumps({"schema_version": 1, "sampled_at": sampled, "resources": [], "warnings": []}),
        encoding="utf-8",
    )

    async def no_start(self):
        return None

    monkeypatch.setattr(LiveState, "start", no_start)
    settings = Settings(resources_enabled=True, resource_snapshot_path=snapshot_path)
    with TestClient(create_app(settings)) as client:
        payload = client.get("/api/agent-resources").json()
    assert payload["status"] == "available"
    assert payload["resources"] == []
    assert str(snapshot_path) not in json.dumps(payload)
