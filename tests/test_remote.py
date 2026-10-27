import asyncio
import copy
import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sisu_kudos.config import Settings
from sisu_kudos.remote import RemoteCollector, number
from sisu_kudos.state import LiveState

BOOT = "12345678-1234-1234-1234-123456789abc"


@pytest.mark.parametrize("missing", [None, "absent"])
def test_unknown_gpu_processes_are_not_reported_as_no_processes(row, missing):
    if missing == "absent":
        row["guard"].pop("gpu_processes")
    else:
        row["guard"]["gpu_processes"] = None
    value = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker")).parse(row)
    assert value["gpu"]["gpu_processes"] is None


@pytest.fixture
def row():
    return {
        "host": "worker",
        "boot_id": BOOT,
        "mono": 20,
        "wall": 1000,
        "cpu": 25,
        "cores": 20,
        "uptime": 100,
        "ram": {"total": 128 * 1024**3, "available": 16 * 1024**3},
        "disk": {
            "total": 1000,
            "free": 200,
            "used": 700,
            "percent": 77.8,
            "source": "/dev/nvme0n1p1",
        },
        "net": {"eth0": [1000, 2000]},
        "guard": {
            "boot_id": BOOT,
            "updated_at": datetime.fromtimestamp(990, UTC).isoformat(),
            "health": "ok",
            "gpu_util_percent": 30,
            "gpu_c": 42,
            "power_watts": 18,
            "acpi_max_c": 45,
            "cap_mhz": 2600,
            "gpu_processes": [
                {"pid": 5, "name": "other", "memory_mb": 1000, "gpu_util": 12},
                {"pid": 4, "name": "VLLM::Worker", "memory_mb": 90000, "gpu_util": 88},
            ],
        },
        "processes": [
            {
                "pid": 100,
                "name": "worker",
                "create_time": 10.0,
                "cpu_time": 20.0,
                "rss": 4096 * 1024**2,
                "read_bytes": 1000,
                "write_bytes": 2000,
            },
            {
                "pid": 101,
                "name": "daemon",
                "create_time": 11.0,
                "cpu_time": 5.0,
                "rss": 1024 * 1024**2,
                "read_bytes": 200,
                "write_bytes": 300,
            },
        ],
        "process_count": 2,
        "processes_truncated": False,
        "model": {
            "name": "qwen3.8-flash-next-ablit",
            "context_length": 65536,
            "requests_running": 1,
            "requests_waiting": 2,
            "kv_cache_percent": 25.0,
            "health": "ok",
            "sampled_at": datetime.fromtimestamp(1000, UTC).isoformat(),
            "_prompt_tokens_total": 600,
            "_generation_tokens_total": 300,
            "_server_started": 900.0,
        },
    }


def test_success_uma_and_network_rates(row):
    collector = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker"))
    first = collector.parse(row)
    assert first["network"]["rx_bytes_sec"] is None
    assert first["ram"]["used_percent"] == 87.5
    assert first["gpu"]["memory_percent"] == first["ram"]["used_percent"]
    assert first["disks"][0]["free_gb"] == 200 / 1024**3  # not total-used
    assert first["disks"][0]["source"] == "/dev/nvme0n1p1"
    assert first["processes"]["count"] == 2
    assert first["processes"]["cpu"][0]["cpu_percent"] is None
    assert first["processes"]["io"][0]["read_bytes_sec"] is None
    assert first["model"]["name"] == "qwen3.8-flash-next-ablit"
    assert first["model"]["context_length"] == 65536
    assert first["model"]["prompt_tokens_per_second"] is None
    second = copy.deepcopy(row)
    second["mono"] += 10
    second["net"]["eth0"] = [2000, 2200]
    second["processes"][0]["cpu_time"] += 1
    second["processes"][0]["read_bytes"] += 1000
    second["processes"][0]["write_bytes"] += 2000
    second["model"]["_prompt_tokens_total"] += 30
    second["model"]["_generation_tokens_total"] += 20
    data = collector.parse(second)
    assert data["network"]["rx_bytes_sec"] == 100
    assert data["processes"]["cpu"][0]["cpu_percent"] == 10
    assert data["processes"]["io"][0]["read_bytes_sec"] == 100
    assert data["processes"]["io"][0]["write_bytes_sec"] == 200
    assert data["model"]["prompt_tokens_per_second"] == 3
    assert data["model"]["generation_tokens_per_second"] == 2
    assert set(data["model"]) == {
        "name",
        "context_length",
        "requests_running",
        "requests_waiting",
        "kv_cache_percent",
        "prompt_tokens_per_second",
        "generation_tokens_per_second",
        "health",
        "sampled_at",
    }


@pytest.mark.parametrize("change", ["boot", "rollback", "interface", "mono"])
def test_network_reset_is_unknown(row, change):
    collector = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker"))
    collector.parse(row)
    row["mono"] += 10
    if change == "boot":
        row["boot_id"] = "22345678-1234-1234-1234-123456789abc"
    elif change == "rollback":
        row["net"]["eth0"][0] = 1
    elif change == "interface":
        row["net"] = {"eth1": [2000, 3000]}
    else:
        row["mono"] = 19
    assert collector.parse(row)["network"]["rx_bytes_sec"] is None


@pytest.mark.parametrize("change", ["missing", "old", "future", "boot", "unsafe", "reset"])
def test_guard_failure_does_not_discard_os_metrics(row, change):
    g = row["guard"]
    if change == "missing":
        row["guard"] = {}
    elif change in ("old", "future"):
        g["updated_at"] = datetime.fromtimestamp(900 if change == "old" else 1100, UTC).isoformat()
    elif change == "boot":
        g["boot_id"] = "old"
    else:
        g["health"] = "unsafe" if change == "unsafe" else "reset_required"
    s = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker")).parse(row)
    assert s["cpu"]["usage_percent"] == 25
    assert s["ram"]["available_gb"] == 16
    assert s["gpu"]["utilization_percent"] is None
    assert s["gpu"]["gpu_processes"] is None
    assert s["gpu"]["health"] != "ok"


def test_remote_output_allowlist_and_bad_numbers(row):
    row["prompt"] = "secret"
    row["guard"]["health_detail"] = "secret"
    row["guard"]["gpu_c"] = float("nan")
    row["guard"]["gpu_processes"][1].update(name="/home/demo/model --secret", cmdline="secret")
    row["processes"][0]["cmdline"] = "private --secret"
    row["model"]["name"] = "/home/demo/model --secret"
    data = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker")).parse(row)
    assert data["gpu"]["health"] == "partial"
    assert data["gpu"]["temperature"] is None
    assert "secret" not in json.dumps(data)
    assert data["gpu"]["gpu_processes"][0]["name"] == "process"
    assert data["gpu"]["gpu_processes"][0]["pid"] == 4
    assert data["gpu"]["gpu_processes"][0]["gpu_util"] is None
    assert data["gpu"]["gpu_processes"][0]["memory_mb"] == 90000
    assert data["gpu"]["gpu_processes"][1]["memory_mb"] == 1000
    assert data["model"]["name"] is None
    assert data["model"]["health"] == "partial"
    assert all(number(v) is None for v in (True, "3", float("inf"), float("nan"), -1))


def test_remote_disk_source_is_sanitized(row):
    row["disk"]["source"] = "/dev/root\nnot-a-source"
    data = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker")).parse(row)
    assert data["disks"][0]["source"] is None


def test_model_endpoint_missing_or_malformed_is_unknown_and_counter_reset_is_unknown(row):
    collector = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker"))
    first = collector.parse(row)
    assert first["model"]["health"] == "ok"

    reset = copy.deepcopy(row)
    reset["mono"] += 10
    reset["model"]["_prompt_tokens_total"] = 2
    reset["model"]["_generation_tokens_total"] = 1
    parsed = collector.parse(reset)
    assert parsed["model"]["prompt_tokens_per_second"] is None
    assert parsed["model"]["generation_tokens_per_second"] is None

    missing = copy.deepcopy(row)
    missing["mono"] += 20
    missing.pop("model")
    parsed = collector.parse(missing)["model"]
    assert parsed["name"] is None
    assert parsed["health"] == "unknown"
    assert parsed["requests_running"] is None
    assert parsed["kv_cache_percent"] is None
    assert parsed["prompt_tokens_per_second"] is None


def test_model_malformed_telemetry_fields_remain_unknown(row):
    row["model"].update(
        context_length=True,
        requests_running=float("nan"),
        requests_waiting="2",
        kv_cache_percent=101,
        sampled_at="not-a-timestamp",
        _prompt_tokens_total="600",
        _generation_tokens_total=float("inf"),
        _server_started=float("nan"),
    )
    model = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker")).parse(row)[
        "model"
    ]
    assert model["name"] == "qwen3.8-flash-next-ablit"
    assert model["context_length"] is None
    assert model["requests_running"] is None
    assert model["requests_waiting"] is None
    assert model["kv_cache_percent"] is None
    assert model["prompt_tokens_per_second"] is None
    assert model["generation_tokens_per_second"] is None
    assert model["sampled_at"] is None


def test_model_restart_requires_new_counter_baseline(row):
    collector = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker"))
    collector.parse(row)
    restarted = copy.deepcopy(row)
    restarted["mono"] += 10
    restarted["model"]["_server_started"] += 1
    restarted["model"]["_prompt_tokens_total"] += 10
    restarted["model"]["_generation_tokens_total"] += 10
    assert collector.parse(restarted)["model"]["prompt_tokens_per_second"] is None
    after_restart = copy.deepcopy(restarted)
    after_restart["mono"] += 10
    after_restart["model"]["_prompt_tokens_total"] += 20
    after_restart["model"]["_generation_tokens_total"] += 10
    rates = collector.parse(after_restart)["model"]
    assert rates["prompt_tokens_per_second"] == 2
    assert rates["generation_tokens_per_second"] == 1


def test_process_rates_reset_on_counter_rollback_and_pid_reuse(row):
    collector = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker"))
    collector.parse(row)

    second = copy.deepcopy(row)
    second["mono"] += 10
    second["processes"][0]["cpu_time"] += 2
    second["processes"][0]["read_bytes"] += 2000
    second["processes"][0]["write_bytes"] += 1000
    rates = collector.parse(second)["processes"]
    worker = next(item for item in rates["cpu"] if item["pid"] == 100)
    assert worker["cpu_percent"] == 20

    reused = copy.deepcopy(second)
    reused["mono"] += 10
    reused["processes"][0]["create_time"] += 1
    reused["processes"][0]["cpu_time"] += 100
    reused["processes"][0]["read_bytes"] += 10000
    rates = collector.parse(reused)["processes"]
    worker = next(item for item in rates["cpu"] if item["pid"] == 100)
    assert worker["cpu_percent"] is None
    assert worker["read_bytes_sec"] is None

    rollback = copy.deepcopy(reused)
    rollback["mono"] += 10
    rollback["processes"][0]["cpu_time"] = 1
    rollback["processes"][0]["read_bytes"] = 1
    rates = collector.parse(rollback)["processes"]
    worker = next(item for item in rates["cpu"] if item["pid"] == 100)
    assert worker["cpu_percent"] is None
    assert worker["read_bytes_sec"] is None


@pytest.mark.parametrize("change", ["identity", "time", "memory", "cpu"])
def test_bad_core_data_rejected(row, change):
    if change == "identity":
        row["host"] = "other"
    elif change == "time":
        row["mono"] = "NaN"
    elif change == "memory":
        row["ram"]["available"] = row["ram"]["total"] + 1
    else:
        row["cpu"] = 101
    with pytest.raises(ValueError):
        RemoteCollector(Settings(remote_host="worker", remote_hostname="worker")).parse(row)


def test_ssh_timeout_and_no_shell(monkeypatch):
    def run(args, **kwargs):
        assert args[-3:] == ["worker", "python3", "-"]
        assert "StrictHostKeyChecking=yes" in args
        assert kwargs["timeout"] == 6
        assert "shell" not in kwargs
        raise subprocess.TimeoutExpired(args, 6)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Remote SSH timed out"):
        RemoteCollector(Settings(remote_host="worker", remote_hostname="worker"))()


def test_ssh_errors_are_redacted(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=255, stderr="private home key information", stdout=""
        ),
    )
    with pytest.raises(RuntimeError, match="^Remote SSH unavailable$"):
        RemoteCollector(Settings(remote_host="worker", remote_hostname="worker"))()


async def test_failed_host_retains_sample_and_stream_is_compact(row):
    live = LiveState(
        Settings(remote_host="worker", remote_hostname="worker", remote_host_id="worker")
    )
    data = RemoteCollector(Settings(remote_host="worker", remote_hostname="worker")).parse(row)
    status = SimpleNamespace(
        updated_at=100,
        error="",
        last_duration_s=0.1,
        failures=0,
        age_s=lambda: 1,
        is_stale=lambda: False,
    )
    live.scheduler = SimpleNamespace(
        state=lambda n: (
            status
            if n == "remote:worker"
            else SimpleNamespace(
                updated_at=101,
                error="",
                last_duration_s=0.01,
                failures=0,
                age_s=lambda: 0,
                is_stale=lambda: False,
            )
        ),
        value=lambda n: data if n == "remote:worker" else None,
        names=lambda: ["remote:worker", "cpu", "ram", "network", "system"],
    )
    live.collectors = SimpleNamespace(SERVICE_ORDER=[])
    await live._publish({"remote:worker"})
    first = await live.snapshot()
    status.error = "Kide SSH unavailable"
    status.failures = 1
    await live._publish({"remote:worker"})
    failed = await live.snapshot()
    assert failed["hosts"]["worker"]["system"] == first["hosts"]["worker"]["system"]
    assert failed["hosts"]["worker"]["generated_at"] == first["hosts"]["worker"]["generated_at"]
    assert len(failed["hosts"]["worker"]["history"]) == 1
    assert failed["hosts"]["worker"]["health"]["stale"]
    assert failed["hosts"]["local"]["health"]["ok"]
    status.error = ""
    status.updated_at = 110
    await live._publish({"remote:worker"})
    assert len((await live.snapshot())["hosts"]["worker"]["history"]) == 2
    assert len((await live.stream_snapshot())["hosts"]["worker"]["history"]) == 1


async def test_scheduler_remote_failure_does_not_block_other_collectors():
    from sisu_kudos.scheduler import CollectorScheduler, CollectorSpec

    def fail():
        raise RuntimeError("offline")

    scheduler = CollectorScheduler(
        [CollectorSpec("worker", fail, 10, 8), CollectorSpec("cpu", lambda: 25, 2, 4)]
    )
    try:
        await scheduler.wait_for(["worker", "cpu"], timeout=2)
        assert scheduler.value("cpu") == 25
        assert scheduler.state("worker").error
    finally:
        scheduler.close()
        await asyncio.sleep(0)
