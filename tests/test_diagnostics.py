import asyncio
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from sisu_kudos.diagnostics import (
    _display_path,
    _terminate_process,
    collect_process_views,
    scan_largest_files,
    scan_largest_files_process,
    validate_mount_identity,
)


def test_process_views_are_bounded_and_include_self():
    data = collect_process_views(limit=4)
    assert len(data["cpu"]) <= 4
    assert len(data["memory"]) <= 4
    assert data["self"] is not None
    assert set(data["self"]) == {
        "pid",
        "name",
        "cpu_percent",
        "memory_mb",
        "read_bytes_sec",
        "write_bytes_sec",
    }
    assert "cmdline" not in repr(data)
    assert "env" not in repr(data)
    assert data["sampled_at"]
    assert "not per mount" in data["sample_note"]
    assert len(data["io"]) <= 4


def test_process_rates_use_interval_counters_and_reset_on_pid_reuse(monkeypatch):
    import sisu_kudos.diagnostics as diagnostics

    collector = diagnostics.ProcessViewCollector()
    snapshot = {
        "pid": 4242,
        "name": "worker",
        "create_time": 123.0,
        "cpu_times": SimpleNamespace(user=4.0, system=1.0),
        "memory_info": SimpleNamespace(rss=100 * 1024**2),
        "io_counters": SimpleNamespace(read_bytes=1000, write_bytes=2000),
    }
    monkeypatch.setattr(
        diagnostics.psutil, "process_iter", lambda attrs: [SimpleNamespace(info=snapshot)]
    )
    clock = iter((10.0, 20.0, 30.0))
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: next(clock))

    first = collector()
    assert first["cpu"][0]["cpu_percent"] is None
    assert first["io"][0]["read_bytes_sec"] is None

    snapshot["cpu_times"] = SimpleNamespace(user=6.0, system=1.0)
    snapshot["io_counters"] = SimpleNamespace(read_bytes=1100, write_bytes=2100)
    second = collector()
    assert second["cpu"][0]["cpu_percent"] == 20
    assert second["io"][0]["read_bytes_sec"] == 10
    assert second["io"][0]["write_bytes_sec"] == 10

    snapshot["create_time"] = 124.0  # PID was reused; do not bridge the old process.
    snapshot["cpu_times"] = SimpleNamespace(user=50.0, system=0.0)
    snapshot["io_counters"] = SimpleNamespace(read_bytes=9000, write_bytes=9000)
    reused = collector()
    assert reused["cpu"][0]["cpu_percent"] is None
    assert reused["io"][0]["read_bytes_sec"] is None
    assert reused["io"][0]["write_bytes_sec"] is None


def test_largest_file_scan_is_sorted_and_mount_relative(tmp_path: Path):
    (tmp_path / "small.bin").write_bytes(b"x" * 5)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "large.bin").write_bytes(b"x" * 50)
    (nested / "medium.bin").write_bytes(b"x" * 20)

    data = scan_largest_files(tmp_path, limit=2, max_seconds=2)

    assert data["complete"] is True
    assert [row["size_bytes"] for row in data["files"]] == [50, 20]
    assert data["files"][0]["path"] == "nested/large.bin"
    assert data["scanned_files"] == 3
    assert str(tmp_path) not in repr(data["files"])
    assert "mount" not in data


async def test_process_file_scan_is_sorted_and_mount_relative(tmp_path: Path):
    (tmp_path / "small.bin").write_bytes(b"x" * 5)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "large.bin").write_bytes(b"x" * 50)

    data = await scan_largest_files_process(tmp_path, limit=2, max_seconds=2)

    assert data["complete"] is True
    assert [row["size_bytes"] for row in data["files"]] == [50, 5]
    assert data["files"][0]["path"] == "nested/large.bin"
    assert str(tmp_path) not in repr(data["files"])
    assert "mount" not in data


def test_root_display_path_masks_home_directory():
    path = Path.home() / "private" / "file.bin"
    rendered = _display_path(Path("/"), path)
    assert rendered == "~/private/file.bin"
    assert str(Path.home()) not in rendered


def test_mount_identity_rejects_stale_source_and_accepts_stacked_mounts(
    monkeypatch, tmp_path: Path
):
    autofs = SimpleNamespace(device="systemd-1", mountpoint=str(tmp_path))
    partition = SimpleNamespace(device="/dev/current", mountpoint=str(tmp_path))
    monkeypatch.setattr(psutil, "disk_partitions", lambda all=True: [autofs, partition])

    assert validate_mount_identity(str(tmp_path), "/dev/current") == tmp_path
    with pytest.raises(ValueError, match="identity changed"):
        validate_mount_identity(str(tmp_path), "/dev/stale")


async def test_scan_child_cleanup_is_bounded_when_wait_never_returns():
    class StuckProcess:
        returncode = None
        killed = False

        def kill(self):
            self.killed = True

        async def wait(self):
            await asyncio.Future()

    process = StuckProcess()
    await asyncio.wait_for(_terminate_process(process, timeout=0.01), timeout=0.1)
    assert process.killed is True
