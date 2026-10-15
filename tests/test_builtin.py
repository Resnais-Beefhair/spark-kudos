"""Standalone collectors use synthetic sources; no GPU, service or remote is needed."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sisu_kudos.builtin import BuiltinCollectors, shared_gpu


def config(**changes):
    return SimpleNamespace(
        **dict(
            dict(services=(), network_interfaces=(), disk_mounts=("/",), gpu_state_path=None),
            **changes,
        )
    )


def test_no_implicit_services_or_gpu_producer():
    collectors = BuiltinCollectors(config())
    assert collectors.SERVICE_ORDER == ()
    value = collectors.collect_gpu()
    assert value.health == "unavailable"
    assert value.utilization_percent is None and value.power_watts is None


def gpu_source(tmp_path, **changes):
    now = datetime.now(UTC)
    row = dict(
        boot_id="test-boot",
        updated_at=now.isoformat(),
        health="ok",
        gpu_util_percent=0,
        gpu_c=40,
        power_watts=12,
    )
    row.update(changes)
    path = tmp_path / "shared.json"
    path.write_text(json.dumps(row))
    return shared_gpu(path, now=now, boot_id="test-boot")


def test_gpu_real_idle_is_known_but_missing_processes_arent_invented(tmp_path):
    value = gpu_source(tmp_path)
    assert value.health == "ok" and value.utilization_percent == 0
    assert value.power_watts == 12 and value.is_unified_memory
    assert value.gpu_processes is None


@pytest.mark.parametrize(
    "changes,health",
    [
        ({"boot_id": "other-boot"}, "unavailable"),
        ({"updated_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()}, "stale"),
        ({"updated_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat()}, "stale"),
        ({"gpu_util_percent": True}, "partial"),
        ({"gpu_c": float("nan")}, "partial"),
        ({"power_watts": -1}, "partial"),
        ({"health": "unsafe"}, "unsafe"),
        ({"health": "raw private error"}, "unavailable"),
    ],
)
def test_gpu_fails_closed(tmp_path, changes, health):
    value = gpu_source(tmp_path, **changes)
    assert value.health == health
    assert value.utilization_percent is None and value.power_watts is None
    assert "raw private error" not in value.health_detail


def test_gpu_rejects_oversized_or_malformed_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("x" * 131073)
    assert shared_gpu(path).health == "unavailable"
    path.write_text("not JSON")
    assert shared_gpu(path).utilization_percent is None


def test_gpu_process_projection_omits_arbitrary_text(tmp_path):
    value = gpu_source(
        tmp_path,
        gpu_processes=[
            {"pid": 12, "name": "/home/demo/private prompt", "gpu_util": 20, "memory_mb": 100},
            {"pid": True, "name": "ignored"},
        ],
    )
    assert len(value.gpu_processes) == 1
    assert value.gpu_processes[0].name == "process"


def test_network_rates_have_first_reset_and_missing_gaps(monkeypatch):
    c = BuiltinCollectors(config(network_interfaces=("eth0",)))
    now = iter([1.0, 3.0, 4.0])
    monkeypatch.setattr("sisu_kudos.builtin.time.monotonic", lambda: next(now))
    source = {"eth0": SimpleNamespace(bytes_recv=100, bytes_sent=50)}
    monkeypatch.setattr("sisu_kudos.builtin.psutil.net_io_counters", lambda **_: source)
    assert c.collect_network().rx_bytes_sec is None
    source["eth0"] = SimpleNamespace(bytes_recv=300, bytes_sent=150)
    assert c.collect_network().rx_bytes_sec == 100
    source["eth0"] = SimpleNamespace(bytes_recv=1, bytes_sent=1)
    assert c.collect_network().rx_bytes_sec is None
    source.clear()
    with pytest.raises(RuntimeError, match="interface is unavailable"):
        c.collect_network()


def test_cpu_counters_are_instance_local_across_scheduler_threads(monkeypatch):
    c = BuiltinCollectors(config())
    times = iter([(100, 20), (200, 70), (10, 2)])
    monkeypatch.setattr(c, "_cpu_counters", lambda: next(times))
    assert c.collect_cpu().usage_percent is None
    assert c.collect_cpu().usage_percent == 50
    assert c.collect_cpu().usage_percent is None


def test_missing_mount_remains_visible_as_unknown(monkeypatch):
    c = BuiltinCollectors(config(disk_mounts=("/not-mounted",)))
    monkeypatch.setattr("sisu_kudos.builtin.psutil.disk_partitions", lambda **_: [])
    value = c.collect_disks()[0]
    assert value.health == "unavailable"
    assert value.use_percent is None and value.source == ""


def test_configured_service_is_queried_without_shell(monkeypatch):
    service = SimpleNamespace(
        name="model",
        label="Model server",
        kind="systemd",
        scope="user",
        unit="model-server.service",
    )
    c = BuiltinCollectors(config(services=(service,)))

    def run(command, **kwargs):
        assert command == [
            "systemctl",
            "--user",
            "show",
            "model-server.service",
            "--property=ActiveState,MemoryCurrent",
            "--no-pager",
        ]
        assert "shell" not in kwargs
        return SimpleNamespace(returncode=0, stdout="ActiveState=active\nMemoryCurrent=1048576\n")

    monkeypatch.setattr("sisu_kudos.builtin.subprocess.run", run)
    value = c.collect_service("model")
    assert value.name == "Model server" and value.status == "running"
    assert value.memory_mb == 1
