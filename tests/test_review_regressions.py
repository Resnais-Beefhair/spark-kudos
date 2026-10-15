"""Review regressions: unknown GPU never becomes measured zero or empty list."""

from types import SimpleNamespace

import pytest

from sisu_kudos.adapters import load_sisu_collectors, safe_gpu_snapshot
from sisu_kudos.config import Settings
from sisu_kudos.state import LiveState


@pytest.mark.parametrize("health", ["unavailable", "stale", "unsafe", "reset-required"])
def test_unhealthy_gpu_nulls_all_current_measurements(health):
    collectors = load_sisu_collectors(Settings().sisu_monitor_repo)
    value = collectors.GpuInfo(health=health, utilization_percent=0, power_watts=12)
    out = safe_gpu_snapshot(value, {"ok": True, "stale": False})
    assert out["utilization_percent"] is None and out["power_watts"] is None
    assert out["gpu_processes"] is None
    assert out["history"] == []
    assert out["health_status"] == health


def test_real_idle_and_empty_process_list_remain_known():
    collectors = load_sisu_collectors(Settings().sisu_monitor_repo)
    value = collectors.GpuInfo(health="ok", utilization_percent=0)
    assert safe_gpu_snapshot(value, {"ok": True, "stale": False})["utilization_percent"] == 0
    assert safe_gpu_snapshot(value, {"ok": True, "stale": False})["gpu_processes"] == []
    assert safe_gpu_snapshot(value, {"ok": True, "stale": True})["utilization_percent"] is None


async def test_failed_gpu_history_is_gap_and_log_revision_tracks_content():
    state = LiveState(Settings(logs_enabled=True))
    collectors = load_sisu_collectors(Settings().sisu_monitor_repo)
    rows = {
        "gpu": collectors.GpuInfo(health="unavailable"),
        "logs": {"records": [{"message": "first"}], "counts": {"error": 1}},
    }
    state.collectors = SimpleNamespace(SERVICE_ORDER=[])
    state.scheduler = SimpleNamespace(
        names=lambda: ["gpu", "logs"], value=lambda name: rows.get(name)
    )
    state._health = lambda name: {"ok": True, "stale": False}
    await state._publish({"gpu", "logs"})
    assert state.data["history"][-1]["gpu"] is None
    revision = state.data["logs"]["revision"]
    rows["logs"]["records"][0]["message"] = "new error same count"
    await state._publish({"logs"})
    assert state.data["logs"]["revision"] != revision
