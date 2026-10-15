"""Remote transport is opt-in, shell-safe, identity-bound and HTTP-optional."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from sisu_kudos.config import ConfigurationError, Settings
from sisu_kudos.remote import PROBE, RemoteCollector


def sample(host="node-b"):
    return {
        "host": host,
        "boot_id": "12345678-1234-1234-1234-123456789abc",
        "mono": 20,
        "wall": 1000,
        "cpu": 25,
        "cores": 8,
        "uptime": 100,
        "ram": {"total": 128 * 1024**3, "available": 16 * 1024**3},
        "disk": {"total": 1000, "free": 200, "used": 700, "percent": 77.8},
        "net": {},
        "guard": {},
        "processes": [],
        "process_count": 0,
        "processes_truncated": False,
        "model": {},
    }


def test_disabled_remote_never_invokes_ssh(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("SSH must not be called while remote collection is disabled")

    monkeypatch.setattr(subprocess, "run", unexpected)
    with pytest.raises(RuntimeError, match="not configured"):
        RemoteCollector(Settings())()


def test_programmatic_remote_settings_are_revalidated_before_ssh(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("SSH called"))
    collector = RemoteCollector(Settings(remote_host="node;touch bad", remote_hostname="node"))
    with pytest.raises(ConfigurationError, match="safe SSH destination"):
        collector()


def test_configured_remote_uses_validated_target_python_and_expected_hostname(monkeypatch):
    settings = Settings(
        remote_host="monitor@spark-b",
        remote_hostname="node-b",
        remote_host_id="gpu-b",
        remote_python="/usr/bin/python3",
        remote_gpu_state_path="/run/gpu-state.json",
        remote_model_url="http://127.0.0.1:9000",
    )
    captured = {}

    def run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps(sample()), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    result = RemoteCollector(settings)()
    assert result["identity"]["hostname"] == "node-b"
    assert captured["args"][-3:] == ["monitor@spark-b", "/usr/bin/python3", "-"]
    assert "StrictHostKeyChecking=yes" in captured["args"]
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"]["timeout"] == 6
    assert 'gpu_state_path="/run/gpu-state.json"' in captured["kwargs"]["input"]
    assert 'model_url="http://127.0.0.1:9000"' in captured["kwargs"]["input"]


def test_configured_expected_hostname_is_enforced():
    collector = RemoteCollector(Settings(remote_host="node", remote_hostname="expected"))
    with pytest.raises(ValueError, match="identity mismatch"):
        collector.parse(sample("other"))
    assert collector.parse(sample("expected"))["identity"]["hostname"] == "expected"


def test_default_probe_makes_no_model_http_requests_or_reads_gpu_state():
    probe = PROBE.replace("__GPU_STATE_PATH_LITERAL__", '""').replace("__MODEL_URL_LITERAL__", '""')
    assert "if not model_url" in probe
    assert "if gpu_state_path" in probe
    assert "http://127.0.0.1:8890" not in probe
    assert "/var/lib/kide-thermal-guard" not in probe


def test_remote_error_does_not_disclose_ssh_stderr_or_route(monkeypatch):
    settings = Settings(remote_host="user@private-host", remote_hostname="node")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=255, stdout="", stderr="private key and /home/demo/path"
        ),
    )
    with pytest.raises(RuntimeError, match="^Remote SSH unavailable$") as error:
        RemoteCollector(settings)()
    assert "private" not in str(error.value)
    assert "private-host" not in str(error.value)
