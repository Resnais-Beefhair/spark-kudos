"""Synthetic filesystem tests; never traverse the maintainer's volumes."""

import asyncio
import os
from types import SimpleNamespace

import pytest

from sisu_kudos.storage import scan_disk_usage_process


async def test_usage_lists_direct_children_sorted_by_allocated_directory_totals(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    (models / "weights.bin").write_bytes(b"m" * 65536)
    (models / "nested").mkdir()
    (models / "nested" / "more.bin").write_bytes(b"n" * 32768)
    (tmp_path / "small.txt").write_bytes(b"x" * 4096)
    sparse = tmp_path / "sparse.bin"
    with sparse.open("wb") as f:
        f.truncate(1024**3)
    (tmp_path / "link").symlink_to(models, target_is_directory=True)
    result = await scan_disk_usage_process(tmp_path, max_seconds=2)
    assert result["complete"] is True
    assert result["size_basis"] == "allocated"
    assert result["scope"] == "mount_children"
    assert result["sampled_at"]
    rows = result["entries"]
    assert [x["path"] for x in rows] == ["models", "small.txt", "sparse.bin"]
    assert rows[0]["kind"] == "directory"
    assert rows[0]["size_bytes"] >= 98304
    assert rows[-1]["size_bytes"] == sparse.stat().st_blocks * 512
    assert str(tmp_path) not in repr(result)
    assert not any("nested" in x["path"] or x["path"] == "link" for x in rows)


async def test_usage_excludes_nested_mounts_even_on_same_device(monkeypatch, tmp_path):
    import sisu_kudos.storage as storage

    mounted = tmp_path / "other-volume"
    mounted.mkdir()
    (mounted / "private.bin").write_bytes(b"x" * 65536)
    (tmp_path / "own.bin").write_bytes(b"x" * 4096)
    monkeypatch.setattr(
        storage.psutil,
        "disk_partitions",
        lambda all=True: [
            SimpleNamespace(mountpoint=str(tmp_path), device="/dev/demo"),
            SimpleNamespace(mountpoint=str(mounted), device="/dev/demo"),
        ],
    )
    result = await scan_disk_usage_process(tmp_path, max_seconds=2)
    assert result["complete"] is True
    assert [x["path"] for x in result["entries"]] == ["own.bin"]


async def test_usage_limit_and_filename_controls(tmp_path):
    for i in range(5):
        (tmp_path / f"part-{i}").write_bytes(b"x" * (4096 * (i + 1)))
    (tmp_path / "unsafe\n<file>").write_bytes(b"x")
    result = await scan_disk_usage_process(tmp_path, limit=3, max_seconds=2)
    assert len(result["entries"]) == 3
    assert result["scanned_entries"] == 6
    sizes = [x["size_bytes"] for x in result["entries"]]
    assert sizes == sorted(sizes, reverse=True)
    assert all("\n" not in x["path"] for x in result["entries"])


async def test_usage_rejects_symlink_root(tmp_path):
    (tmp_path / "actual").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "actual", target_is_directory=True)
    with pytest.raises(ValueError):
        await scan_disk_usage_process(tmp_path / "alias")


async def test_empty_volume_is_complete_not_unavailable(tmp_path):
    result = await scan_disk_usage_process(tmp_path, max_seconds=2)
    assert result["complete"] is True and result["entries"] == []
    assert result["scanned_entries"] == 0


async def test_timeout_kills_owned_child_and_marks_partial(monkeypatch, tmp_path):
    import sisu_kudos.storage as storage

    class Output:
        async def read(self, _):
            await asyncio.Future()

    class Process:
        stdout = Output()
        returncode = None
        killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    child = Process()

    async def launch(*args, **kwargs):
        assert args[0] == "du" and "--one-file-system" in args and "--no-dereference" in args
        return child

    monkeypatch.setattr(storage.asyncio, "create_subprocess_exec", launch)
    result = await asyncio.wait_for(scan_disk_usage_process(tmp_path, max_seconds=0.01), 0.3)
    assert child.killed and result["complete"] is False


async def test_permission_failure_never_reports_complete(monkeypatch, tmp_path):
    import sisu_kudos.storage as storage

    class Process:
        stdout = asyncio.StreamReader()
        returncode = 1

        async def wait(self):
            return 1

    child = Process()
    child.stdout.feed_eof()

    async def launch(*args, **kwargs):
        return child

    monkeypatch.setattr(storage.asyncio, "create_subprocess_exec", launch)
    result = await scan_disk_usage_process(tmp_path, max_seconds=1)
    assert result["complete"] is False
    assert result["entries"] == []


@pytest.mark.parametrize(
    "name",
    [b"bad-\xff-name", ("a" * 239 + "🧩").encode(), b"control\t\n-name"],
)
async def test_usage_names_are_utf8_and_browser_length_safe(tmp_path, name):
    with open(os.fsencode(tmp_path) + b"/" + name, "wb") as stream:
        stream.write(b"allocated")
    data = await scan_disk_usage_process(tmp_path, max_seconds=2)
    assert data["complete"] is True
    path = data["entries"][0]["path"]
    path.encode("utf-8", errors="strict")
    assert len(path.encode("utf-16-le")) <= 480
    assert all(ord(char) >= 32 and ord(char) != 127 for char in path)


async def test_long_names_remain_distinguishable_after_display_truncation(tmp_path):
    for suffix in ("x", "y"):
        (tmp_path / ("a" * 250 + suffix)).write_bytes(b"allocated")
    data = await scan_disk_usage_process(tmp_path, max_seconds=2)
    assert len({entry["path"] for entry in data["entries"]}) == 2
