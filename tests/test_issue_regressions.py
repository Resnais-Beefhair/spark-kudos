"""Synthetic regressions for the first public audit (issues #1 through #5)."""

import re
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sisu_kudos import builtin, config, crons, diagnostics, scheduler
from sisu_kudos.adapters import safe_agent_snapshot
from sisu_kudos.app import create_app
from sisu_kudos.config import Settings
from sisu_kudos.state import LiveState


@pytest.mark.parametrize(
    "quota,expected",
    [
        (None, ""),
        (SimpleNamespace(plan_name="", windows=[]), ""),
        (SimpleNamespace(plan_name="", error="", windows=[]), ""),
        (SimpleNamespace(plan_name="Example plan", error="", windows=[]), "Example plan"),
        (
            SimpleNamespace(plan_name="", error="source temporarily offline", windows=[]),
            "source temporarily offline",
        ),
    ],
)
def test_quota_status_has_no_backend_translation_but_preserves_source_text(quota, expected):
    payload = safe_agent_snapshot(SimpleNamespace(chatgpt_quota=quota))
    assert payload["quotas"]["chatgpt"]["status"] == expected


def test_styles_do_not_generate_unlocalizable_prose():
    css = (Path(__file__).parents[1] / "src/sisu_kudos/web/styles.css").read_text()
    literals = re.findall(r"(?<![\w-])content\s*:\s*([\"'])(.*?)\1", css, re.S)
    assert not [text for _, text in literals if any(char.isalpha() for char in text)]


def test_cron_health_success_is_language_neutral_and_errors_remain_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(crons, "collect_hermes", lambda *args: [])
    monkeypatch.setattr(crons, "collect_openclaw", lambda *args: [])
    collector = crons.CronCollector((("Worker", tmp_path / "hermes"),), tmp_path / "openclaw")
    healthy = collector()
    assert all(row["ok"] and row["message"] == "" for row in healthy.health)

    def fail(*args):
        raise ValueError("private diagnostic path must not cross the API")

    monkeypatch.setattr(crons, "collect_hermes", fail)
    failed = next(row for row in collector().health if row["source"] == "hermes-cron")
    assert failed["ok"] is False
    assert failed["message"] == "Worker: ValueError"


@pytest.mark.parametrize("path,status", [("/", 200), ("/assets/app.js", 200), ("/missing", 404)])
def test_security_policy_preserves_baseline_and_hardens_read_only_routes(path, status):
    # No lifespan: checking headers must not start any real collectors.
    client = TestClient(create_app(Settings()))
    response = client.get(path)
    assert response.status_code == status
    policy = dict(
        item.strip().split(" ", 1)
        for item in response.headers["Content-Security-Policy"].split(";")
        if item.strip()
    )
    assert policy == {
        "default-src": "'self'",
        "script-src": "'self'",
        "style-src": "'self'",
        "img-src": "'self' data:",
        "connect-src": "'self'",
        "font-src": "'self'",
        "frame-ancestors": "'none'",
        "base-uri": "'none'",
        "form-action": "'none'",
    }
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize("path", ["/run/gpu/state.json", ""])
def test_remote_linux_paths_do_not_use_local_windows_semantics(monkeypatch, path):
    monkeypatch.setattr(config, "Path", PureWindowsPath)
    assert config._remote_state_path(path, "hosts.remote.gpu_state_path") == path


@pytest.mark.parametrize("path", ["relative.json", "/run/../state.json", r"C:\run\gpu.json"])
def test_remote_paths_still_reject_nonabsolute_or_parent_paths(path):
    with pytest.raises(config.ConfigurationError):
        config._remote_state_path(path, "hosts.remote.gpu_state_path")


async def test_process_rates_are_owned_by_each_live_state(monkeypatch):
    class CapturingScheduler:
        def __init__(self, specs, **kwargs):
            self.specs = {spec.name: spec for spec in specs}

        async def wait_for(self, *args, **kwargs):
            pass

        def names(self):
            return []

        def value(self, name):
            return None

        def pop_dirty(self):
            return set()

        def schedule_due(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(scheduler, "CollectorScheduler", CapturingScheduler)
    monkeypatch.setattr(
        builtin,
        "BuiltinCollectors",
        lambda settings: SimpleNamespace(
            **{
                f"collect_{name}": lambda: None
                for name in ("cpu", "ram", "gpu", "network", "system", "disks")
            }
        ),
    )
    info = {
        "pid": 676767,
        "name": "example-worker",
        "create_time": 900.0,
        "cpu_times": SimpleNamespace(user=4.0, system=1.0),
        "memory_info": SimpleNamespace(rss=1024**2),
        "io_counters": SimpleNamespace(read_bytes=1000, write_bytes=2000),
    }
    monkeypatch.setattr(
        diagnostics.psutil, "process_iter", lambda attrs: [SimpleNamespace(info=info)]
    )
    clock = iter((10.0, 20.0, 25.0, 30.0))
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    first, second = LiveState(Settings()), LiveState(Settings())
    try:
        await first.start()
        await second.start()
        sample_first = first.scheduler.specs["processes"].fn
        sample_second = second.scheduler.specs["processes"].fn
        assert sample_first()["cpu"][0]["cpu_percent"] is None
        info["cpu_times"] = SimpleNamespace(user=6.0, system=1.0)
        info["io_counters"] = SimpleNamespace(read_bytes=1100, write_bytes=2100)
        assert sample_first()["cpu"][0]["cpu_percent"] == 20.0
        fresh = sample_second()
        assert fresh["cpu"][0]["cpu_percent"] is None
        assert fresh["io"][0]["read_bytes_sec"] is None
        info["cpu_times"] = SimpleNamespace(user=8.0, system=1.0)
        assert sample_first()["cpu"][0]["cpu_percent"] == 20.0
    finally:
        await first.stop()
        await second.stop()
