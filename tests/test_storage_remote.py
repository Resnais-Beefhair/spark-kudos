"""Synthetic tests for bounded remote storage scans; no SSH host is contacted."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import time
from pathlib import Path

import pytest

from sisu_kudos import storage_remote
from sisu_kudos.config import ConfigurationError, Settings


def _settings(*, hostname: str | None = None, target: str = "worker") -> Settings:
    return Settings(remote_host=target, remote_hostname=hostname or socket.gethostname())


def test_probe_preserves_literal_marker_names():
    import ast

    expected = {
        "MOUNT": "/__SOURCE_LITERAL__",
        "EXPECTED_SOURCE": "__HOST_LITERAL__",
        "EXPECTED_HOST": "worker",
    }
    probe = storage_remote._build_probe(
        expected["MOUNT"], expected["EXPECTED_SOURCE"], expected["EXPECTED_HOST"], 40, 20.0, True
    )
    tree = ast.parse(probe)
    actual = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in expected
    }
    assert actual == expected


def _mountinfo_line(mount: Path, *, mount_id: int = 900, source: str = "/dev/test") -> str:
    device = mount.stat().st_dev
    major, minor = os.major(device), os.minor(device)
    return f"{mount_id} 1 {major}:{minor} / {mount} rw - ext4 {source} rw\n"


def _local_probe_runner(monkeypatch, *, env: dict[str, str] | None = None) -> None:
    async def spawn(args: list[str]) -> asyncio.subprocess.Process:
        assert args[0] == "ssh"
        assert "StrictHostKeyChecking=yes" in args
        assert args[-1] == "python3 - -" or args[-1] == "python3 -"
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )

    monkeypatch.setattr(storage_remote, "_spawn_ssh", spawn)


def _fake_du(fake_bin: Path, content: str) -> None:
    fake_bin.mkdir()
    executable = fake_bin / "du"
    executable.write_text("#!/bin/sh\n" + content, encoding="utf-8")
    executable.chmod(0o755)


def _probe_env(*, fake_bin: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if fake_bin:
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    return env


@pytest.mark.parametrize(
    "mount,source,limit,seconds",
    [
        ("/tmp/../etc", "/dev/root", 40, 2.0),
        ("/tmp//data", "/dev/root", 40, 2.0),
        ("/tmp/data/", "/dev/root", 40, 2.0),
        ("/tmp/data", "/dev/root\n;touch /tmp/pwn", 40, 2.0),
        ("/tmp/data", "/dev/root", True, 2.0),
        ("/tmp/data", "/dev/root", 101, 2.0),
        ("/tmp/data", "/dev/root", 201, 2.0),
        ("/tmp/data", "/dev/root", 40, 21.0),
    ],
)
def test_rejects_hostile_or_unbounded_request_tokens(mount, source, limit, seconds):
    with pytest.raises(ValueError):
        storage_remote._validate_request(mount, source, limit, seconds)


def test_unconfigured_or_option_like_ssh_destination_is_rejected(monkeypatch):
    called = False

    async def forbidden(_args):
        nonlocal called
        called = True
        raise AssertionError("SSH must not start")

    monkeypatch.setattr(storage_remote, "_spawn_ssh", forbidden)
    with pytest.raises(ConfigurationError):
        asyncio.run(storage_remote.scan_remote_storage(Settings(), "/", "/dev/root"))
    with pytest.raises(ConfigurationError):
        asyncio.run(
            storage_remote.scan_remote_storage(
                _settings(target="worker;touch /tmp/pwn"), "/", "/dev/root"
            )
        )
    assert not called


def test_result_projection_sorts_caps_and_rejects_unsafe_paths():
    payload = {
        "ok": True,
        "entries": [
            {"path": "zeta", "kind": "file", "size_bytes": 7},
            {"path": "alpha", "kind": "directory", "size_bytes": 7},
            {"path": "small", "kind": "file", "size_bytes": 2},
            {"path": "/etc/passwd", "kind": "file", "size_bytes": 900},
            {"path": "sub/file", "kind": "file", "size_bytes": 800},
            {"path": "bad\nname", "kind": "file", "size_bytes": 800},
            {"path": "odd", "kind": "symlink", "size_bytes": 800},
            {"path": "negative", "kind": "file", "size_bytes": -1},
            {"path": "x" * 241, "kind": "file", "size_bytes": 1000},
            {"path": "unsafe-number", "kind": "file", "size_bytes": 2**53},
        ],
        "complete": True,
        "scanned_entries": 10,
        "sampled_at": "2026-10-18T12:00:00+00:00",
    }
    result = storage_remote._project_result(payload, 2, 15)
    assert result["entries"] == [
        {"path": "alpha", "kind": "directory", "size_bytes": 7},
        {"path": "zeta", "kind": "file", "size_bytes": 7},
    ]
    assert result["complete"] is False
    assert result["scanned_entries"] == 10
    assert result["duration_ms"] == 15
    assert result["scope"] == "mount_children"
    assert result["size_basis"] == "allocated"
    assert all("/" not in item["path"] for item in result["entries"])
    assert "mount" not in result and "source" not in result


@pytest.mark.asyncio
async def test_validate_only_checks_live_source_without_running_du(monkeypatch, tmp_path):
    mount = tmp_path / "mounted"
    mount.mkdir()
    info = tmp_path / "mountinfo"
    info.write_text(_mountinfo_line(mount), encoding="utf-8")
    monkeypatch.setattr(storage_remote, "_MOUNTINFO_PATH", str(info))
    fake_bin = tmp_path / "bin"
    marker = tmp_path / "du-called"
    _fake_du(fake_bin, f'touch {marker}\nexec {shutil.which("du")} "$@"\n')
    _local_probe_runner(monkeypatch, env=_probe_env(fake_bin=fake_bin))

    result = await storage_remote.scan_remote_storage(
        _settings(), str(mount), "/dev/test", validate_only=True
    )
    assert result == {"valid": True}
    assert not marker.exists()


@pytest.mark.asyncio
async def test_full_scan_returns_sorted_relative_allocated_children_and_skips_symlinks(
    monkeypatch, tmp_path
):
    mount = tmp_path / "mounted"
    mount.mkdir()
    (mount / "small.bin").write_bytes(b"s" * 1024)
    (mount / "large.bin").write_bytes(b"l" * 8192)
    (mount / "line\nbreak").write_bytes(b"n" * 1024)
    (mount / ("long" * 63 + "xxx")).write_bytes(b"z")
    (mount / "folder").mkdir()
    (mount / "folder" / "inside.bin").write_bytes(b"i" * 1024)
    (mount / "link").symlink_to(mount / "large.bin")
    info = tmp_path / "mountinfo"
    info.write_text(_mountinfo_line(mount), encoding="utf-8")
    monkeypatch.setattr(storage_remote, "_MOUNTINFO_PATH", str(info))
    _local_probe_runner(monkeypatch)

    result = await storage_remote.scan_remote_storage(_settings(), str(mount), "/dev/test")
    rows = result["entries"]
    assert rows == sorted(rows, key=lambda row: (-row["size_bytes"], row["path"]))
    assert {row["path"] for row in rows} == {
        "small.bin",
        "large.bin",
        "folder",
        "line\\x0abreak",
        next(path for path in (row["path"] for row in rows) if path.startswith("long")),
    }
    long_path = next(row["path"] for row in rows if row["path"].startswith("long"))
    assert len(long_path) == 240 and "~" in long_path
    assert next(row for row in rows if row["path"] == "folder")["kind"] == "directory"
    assert next(row for row in rows if row["path"] == "large.bin")["size_bytes"] == (
        (mount / "large.bin").stat().st_blocks * 512
    )
    assert result["complete"] is False  # direct symlink was omitted, not followed
    assert result["scanned_entries"] == 6
    assert result["scope"] == "mount_children"
    assert result["size_basis"] == "allocated"
    assert all("/" not in row["path"] for row in rows)
    assert all(set(row) == {"path", "kind", "size_bytes"} for row in rows)


@pytest.mark.asyncio
async def test_nested_bind_mount_path_is_excluded(monkeypatch, tmp_path):
    mount = tmp_path / "mounted"
    nested = mount / "nested[1]"
    nested.mkdir(parents=True)
    (nested / "large.bin").write_bytes(b"x" * 8192)
    (mount / "ordinary.bin").write_bytes(b"y" * 1024)
    root = _mountinfo_line(mount)
    device = mount.stat().st_dev
    nested_line = (
        f"901 900 {os.major(device)}:{os.minor(device)} /sub {nested} rw - ext4 /dev/bind rw\n"
    )
    info = tmp_path / "mountinfo"
    info.write_text(root + nested_line, encoding="utf-8")
    monkeypatch.setattr(storage_remote, "_MOUNTINFO_PATH", str(info))
    _local_probe_runner(monkeypatch)

    result = await storage_remote.scan_remote_storage(_settings(), str(mount), "/dev/test")
    assert [row["path"] for row in result["entries"]] == ["ordinary.bin"]
    assert result["complete"] is True


@pytest.mark.asyncio
async def test_du_permission_failure_keeps_rows_but_marks_scan_partial(monkeypatch, tmp_path):
    mount = tmp_path / "mounted"
    mount.mkdir()
    visible = mount / "visible.bin"
    visible.write_bytes(b"x")
    info = tmp_path / "mountinfo"
    info.write_text(_mountinfo_line(mount), encoding="utf-8")
    monkeypatch.setattr(storage_remote, "_MOUNTINFO_PATH", str(info))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_du = fake_bin / "du"
    fake_du.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        'os.write(1, b"4096\\t" + os.fsencode(os.environ["DU_ENTRY"]) + b"\\0")\n'
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake_du.chmod(0o755)
    env = _probe_env(fake_bin=fake_bin)
    env["DU_ENTRY"] = str(visible)
    _local_probe_runner(monkeypatch, env=env)

    result = await storage_remote.scan_remote_storage(_settings(), str(mount), "/dev/test")
    assert result["entries"] == [{"path": "visible.bin", "kind": "file", "size_bytes": 4096}]
    assert result["scanned_entries"] == 1
    assert result["complete"] is False


@pytest.mark.parametrize(
    ("replacement_source", "replacement_id", "message"),
    [
        ("/dev/changed", 900, "Remote mount source changed"),
        ("/dev/test", 901, "Remote mount changed during scan"),
    ],
)
@pytest.mark.asyncio
async def test_source_or_mount_identity_change_after_du_is_rejected(
    monkeypatch, tmp_path, replacement_source, replacement_id, message
):
    mount = tmp_path / "mounted"
    mount.mkdir()
    info = tmp_path / "mountinfo"
    original = _mountinfo_line(mount)
    replacement = _mountinfo_line(mount, mount_id=replacement_id, source=replacement_source)
    info.write_text(original, encoding="utf-8")
    monkeypatch.setattr(storage_remote, "_MOUNTINFO_PATH", str(info))
    fake_bin = tmp_path / "bin"
    _fake_du(
        fake_bin,
        f'printf "%s\\n" "$MOUNT_RECORD" > "$MOUNTINFO_TEST"\nexec {shutil.which("du")} "$@"\n',
    )
    env = _probe_env(fake_bin=fake_bin)
    env["MOUNTINFO_TEST"] = str(info)
    env["MOUNT_RECORD"] = replacement.rstrip("\n")
    _local_probe_runner(monkeypatch, env=env)

    with pytest.raises(ValueError, match=message):
        await storage_remote.scan_remote_storage(_settings(), str(mount), "/dev/test")


@pytest.mark.asyncio
async def test_root_symlink_is_rejected(monkeypatch, tmp_path):
    real_mount = tmp_path / "real"
    real_mount.mkdir()
    link = tmp_path / "mounted"
    link.symlink_to(real_mount, target_is_directory=True)
    info = tmp_path / "mountinfo"
    info.write_text(
        _mountinfo_line(real_mount).replace(str(real_mount), str(link)), encoding="utf-8"
    )
    monkeypatch.setattr(storage_remote, "_MOUNTINFO_PATH", str(info))
    _local_probe_runner(monkeypatch)

    with pytest.raises(ValueError, match="Remote mount is unavailable or unsafe"):
        await storage_remote.scan_remote_storage(_settings(), str(link), "/dev/test")


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_returns_incomplete(monkeypatch, tmp_path):
    mount = tmp_path / "mounted"
    mount.mkdir()
    info = tmp_path / "mountinfo"
    info.write_text(_mountinfo_line(mount), encoding="utf-8")
    monkeypatch.setattr(storage_remote, "_MOUNTINFO_PATH", str(info))
    fake_bin = tmp_path / "bin"
    _fake_du(fake_bin, "sleep 5\n")
    _local_probe_runner(monkeypatch, env=_probe_env(fake_bin=fake_bin))

    started = time.monotonic()
    result = await storage_remote.scan_remote_storage(
        _settings(), str(mount), "/dev/test", max_seconds=0.1
    )
    assert time.monotonic() - started < 3
    assert result["complete"] is False
    assert result["entries"] == []
    assert result["scanned_entries"] == 0


@pytest.mark.asyncio
async def test_host_identity_mismatch_is_rejected(monkeypatch, tmp_path):
    mount = tmp_path / "mounted"
    mount.mkdir()
    info = tmp_path / "mountinfo"
    info.write_text(_mountinfo_line(mount), encoding="utf-8")
    monkeypatch.setattr(storage_remote, "_MOUNTINFO_PATH", str(info))
    _local_probe_runner(monkeypatch)

    with pytest.raises(ValueError, match="Remote host identity mismatch"):
        await storage_remote.scan_remote_storage(
            _settings(hostname="wrong-host"), str(mount), "/dev/test", validate_only=True
        )


@pytest.mark.asyncio
async def test_expected_source_is_json_quoted_not_shell_executed(monkeypatch, tmp_path):
    mount = tmp_path / "mounted"
    mount.mkdir()
    info = tmp_path / "mountinfo"
    info.write_text(_mountinfo_line(mount), encoding="utf-8")
    monkeypatch.setattr(storage_remote, "_MOUNTINFO_PATH", str(info))
    _local_probe_runner(monkeypatch)
    marker = tmp_path / "source-injection-ran"
    hostile_source = f"wrong;touch {marker}"

    with pytest.raises(ValueError, match="Remote mount source changed"):
        await storage_remote.scan_remote_storage(_settings(), str(mount), hostile_source)
    assert not marker.exists()
