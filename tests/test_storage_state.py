"""Disk usage endpoint/state fences and lifecycle; all scans synthetic."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sisu_kudos.app import create_app
from sisu_kudos.config import Settings
from sisu_kudos.state import LiveState


def result():
    return {
        "entries": [{"path": "models", "kind": "directory", "size_bytes": 8192}],
        "complete": True,
        "scanned_entries": 1,
        "duration_ms": 1,
        "sampled_at": "2026-01-01T12:00:00+00:00",
        "scope": "mount_children",
        "size_basis": "allocated",
    }


def state(monkeypatch, tmp_path):
    import sisu_kudos.state as module

    live = LiveState(Settings())
    live.data["system"] = {"disks": [{"mount": str(tmp_path), "source": "/dev/demo"}]}
    monkeypatch.setattr(module, "validate_mount_identity", lambda mount, source: Path(mount))
    return live


async def test_singleflight_cache_and_cancelled_waiter(monkeypatch, tmp_path):
    import sisu_kudos.state as module

    live = state(monkeypatch, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def scan(root):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return result()

    monkeypatch.setattr(module, "scan_disk_usage_process", scan)
    first = asyncio.create_task(live.disk_usage(str(tmp_path)))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(live.disk_usage(str(tmp_path)))
    await asyncio.sleep(0)
    assert calls == 1
    # Holding a scan never holds the snapshot lock.
    assert await asyncio.wait_for(live.snapshot(), 0.2)
    release.set()
    assert (await second)["cached"] is False
    cached = await live.disk_usage(str(tmp_path))
    assert cached["cached"] is True and calls == 1
    await live.stop()


async def test_cache_hit_revalidates_device(monkeypatch, tmp_path):
    import sisu_kudos.state as module

    live = state(monkeypatch, tmp_path)

    async def scan(root):
        return result()

    monkeypatch.setattr(module, "scan_disk_usage_process", scan)
    await live.disk_usage(str(tmp_path))

    def disappeared(*args):
        raise ValueError("mount changed")

    monkeypatch.setattr(module, "validate_mount_identity", disappeared)
    with pytest.raises(ValueError):
        await live.disk_usage(str(tmp_path))


async def test_source_change_never_reuses_old_cache(monkeypatch, tmp_path):
    import sisu_kudos.state as module

    live = state(monkeypatch, tmp_path)
    calls = 0

    async def scan(root):
        nonlocal calls
        calls += 1
        return result()

    monkeypatch.setattr(module, "scan_disk_usage_process", scan)
    await live.disk_usage(str(tmp_path))
    live.data["system"]["disks"][0]["source"] = "/dev/replacement"
    assert (await live.disk_usage(str(tmp_path)))["cached"] is False
    assert calls == 2


async def test_device_change_after_scan_discards_result(monkeypatch, tmp_path):
    import sisu_kudos.state as module

    live = state(monkeypatch, tmp_path)
    changed = False

    def validate(mount, source):
        if changed:
            raise ValueError("mount changed")
        return tmp_path

    async def scan(root):
        nonlocal changed
        changed = True
        return result()

    monkeypatch.setattr(module, "scan_disk_usage_process", scan)
    monkeypatch.setattr(module, "validate_mount_identity", validate)
    with pytest.raises(ValueError):
        await live.disk_usage(str(tmp_path))
    assert not live._usage_cache


@pytest.mark.parametrize("host,mount", [("other", "/"), ("local", "/unconfigured"), ("", "/")])
async def test_unknown_host_or_mount_rejected(monkeypatch, tmp_path, host, mount):
    live = state(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        await live.disk_usage(mount, host)


async def test_network_mount_is_not_a_local_volume(monkeypatch, tmp_path):
    live = state(monkeypatch, tmp_path)
    live.data["system"]["disks"][0]["is_remote"] = True
    with pytest.raises(ValueError):
        await live.disk_usage(str(tmp_path))


async def test_global_serialization_and_stop_owns_tasks(monkeypatch, tmp_path):
    import sisu_kudos.state as module

    live = state(monkeypatch, tmp_path)
    second_mount = str(tmp_path / "second")
    live.data["system"]["disks"].append({"mount": second_mount, "source": "/dev/second"})
    entered, cancelled = asyncio.Event(), asyncio.Event()
    active = 0

    async def scan(root):
        nonlocal active
        active += 1
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(module, "scan_disk_usage_process", scan)
    one = asyncio.create_task(live.disk_usage(str(tmp_path)))
    await entered.wait()
    two = asyncio.create_task(live.disk_usage(second_mount))
    await asyncio.sleep(0)
    assert active == 1
    await asyncio.wait_for(live.stop(), 0.3)
    assert cancelled.is_set()
    assert not live._usage_tasks
    await asyncio.gather(one, two, return_exceptions=True)


@pytest.mark.parametrize(
    "error,code",
    [
        (ValueError("private /outside/private-sentinel/value"), 404),
        (RuntimeError("Bearer PRIVATE"), 503),
        (OSError("secret filesystem output"), 503),
        (TimeoutError("private host"), 503),
        (None, 200),
    ],
)
def test_api_contract_and_no_exception_details(error, code):
    app = create_app(Settings())

    async def usage(mount, host):
        assert mount == "/volume" and host == "local"
        if error:
            raise error
        return result() | {"cached": False, "cache_age_seconds": 0}

    app.state.kudos_state = SimpleNamespace(disk_usage=usage)
    client = TestClient(app)
    try:
        response = client.get("/api/disks/usage", params={"host": "local", "mount": "/volume"})
        assert response.status_code == code
        assert all(
            token not in response.text
            for token in ("PRIVATE", "/outside/private-sentinel", "secret", "Bearer")
        )
        if error is None:
            assert response.json()["entries"][0]["path"] == "models"
            assert response.headers["Cache-Control"] == "no-store"
    finally:
        client.close()


@pytest.mark.parametrize("complete,ttl", [(True, 600), (False, 600)])
async def test_usage_cache_expires_at_the_reported_ttl(monkeypatch, tmp_path, complete, ttl):
    import sisu_kudos.state as module

    live = state(monkeypatch, tmp_path)
    clock = [100.0]
    calls = 0
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def scan(root):
        nonlocal calls
        calls += 1
        return result() | {"complete": complete}

    monkeypatch.setattr(module, "scan_disk_usage_process", scan)
    assert (await live.disk_usage(str(tmp_path)))["cached"] is False
    clock[0] += ttl - 1
    assert (await live.disk_usage(str(tmp_path)))["cached"] is True
    clock[0] += 1
    assert (await live.disk_usage(str(tmp_path)))["cached"] is False
    assert calls == 2


@pytest.mark.parametrize("complete,expected_scans", [(True, 1), (False, 2)])
async def test_empty_partial_is_not_cached_but_empty_complete_is(
    monkeypatch, tmp_path, complete, expected_scans
):
    import sisu_kudos.state as module

    live = state(monkeypatch, tmp_path)
    calls = 0

    async def scan(root):
        nonlocal calls
        calls += 1
        return result() | {"complete": complete, "entries": [], "scanned_entries": 0}

    monkeypatch.setattr(module, "scan_disk_usage_process", scan)
    await live.disk_usage(str(tmp_path))
    await live.disk_usage(str(tmp_path))
    assert calls == expected_scans


async def test_remote_singleflight_cache_revalidation_and_replaced_source(monkeypatch):
    import sisu_kudos.storage_remote as module

    live = LiveState(replace(Settings(), remote_host="worker", remote_hostname="worker"))
    remote = {"disks": [{"mount": "/", "source": "/dev/demo", "is_remote": False}]}
    live.scheduler = SimpleNamespace(value=lambda name: remote)
    calls = []
    reject_cached = False

    async def scan(settings, mount, source, *, validate_only=False):
        calls.append((mount, source, validate_only))
        if validate_only and reject_cached:
            raise ValueError("remote volume changed")
        await asyncio.sleep(0)
        return {"valid": True} if validate_only else result()

    monkeypatch.setattr(module, "scan_remote_storage", scan)
    host = live.settings.remote_host_id
    answers = await asyncio.gather(*(live.disk_usage("/", host) for _ in range(3)))
    assert all(answer["cached"] is False for answer in answers)
    assert calls == [("/", "/dev/demo", False)]
    assert (await live.disk_usage("/", host))["cached"] is True
    assert calls[-1] == ("/", "/dev/demo", True)
    reject_cached = True
    with pytest.raises(ValueError, match="remote volume changed"):
        await live.disk_usage("/", host)
    remote["disks"][0]["source"] = "/dev/replaced"
    assert (await live.disk_usage("/", host))["cached"] is False
    assert calls[-1] == ("/", "/dev/replaced", False)


def test_non_utf8_filename_survives_actual_scanner_api_and_cache(monkeypatch, tmp_path):
    import os

    with open(os.fsencode(tmp_path) + b"/bad-\xff-name", "wb") as stream:
        stream.write(b"allocated")
    live = state(monkeypatch, tmp_path)
    app = create_app()
    app.state.kudos_state = live
    client = TestClient(app)
    try:
        for cached in (False, True):
            response = client.get("/api/disks/usage", params={"mount": str(tmp_path)})
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["entries"][0]["path"] == r"bad-\udcff-name"
            assert response.json()["cached"] is cached
    finally:
        client.close()
