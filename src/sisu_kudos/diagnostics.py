"""Bounded diagnostics used by drill-down views.

Collectors return explicit allowlists. Process command lines are never read, and file
scans expose display paths only for a mount already present in the system snapshot.
"""

from __future__ import annotations

import asyncio
import heapq
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .adapters import clean_text


def _counter(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value >= 0 and value < float("inf") else None


def _safe_process(proc: psutil.Process, info: dict[str, Any]) -> dict[str, Any] | None:
    memory = info.get("memory_info")
    if memory is None:
        return None
    cpu_times = info.get("cpu_times")
    cpu_time = None
    if cpu_times is not None:
        user = _counter(getattr(cpu_times, "user", None))
        system = _counter(getattr(cpu_times, "system", None))
        if user is not None and system is not None:
            cpu_time = user + system
    io = info.get("io_counters")
    created = _counter(info.get("create_time"))
    pid = int(info.get("pid") or 0)
    return {
        "pid": pid,
        "name": clean_text(info.get("name") or "unknown", 64),
        "cpu_percent": None,
        "memory_mb": round(float(memory.rss) / (1024 * 1024), 1),
        "read_bytes_sec": None,
        "write_bytes_sec": None,
        "_identity": (pid, created) if pid > 0 and created is not None else None,
        "_cpu_time": cpu_time,
        "_read_bytes": _counter(getattr(io, "read_bytes", None)),
        "_write_bytes": _counter(getattr(io, "write_bytes", None)),
    }


class _ProcessRateSampler:
    """Derive per-process CPU and I/O rates from identity-stable counters."""

    def __init__(self) -> None:
        self.previous: dict[tuple[int, float], dict[str, float | None]] = {}
        self.sampled_at: float | None = None

    def apply(self, rows: list[dict[str, Any]], sampled_at: float) -> None:
        elapsed = sampled_at - self.sampled_at if self.sampled_at is not None else None
        current: dict[tuple[int, float], dict[str, float | None]] = {}
        for row in rows:
            identity = row.pop("_identity")
            cpu_total = row.pop("_cpu_time")
            read_total = row.pop("_read_bytes")
            write_total = row.pop("_write_bytes")
            previous = self.previous.get(identity) if identity is not None else None
            if previous is not None and elapsed is not None and elapsed > 0:
                old_cpu, old_read, old_write = (
                    previous.get("cpu"),
                    previous.get("read"),
                    previous.get("write"),
                )
                if cpu_total is not None and old_cpu is not None and cpu_total >= old_cpu:
                    row["cpu_percent"] = round((cpu_total - old_cpu) / elapsed * 100, 1)
                if read_total is not None and old_read is not None and read_total >= old_read:
                    row["read_bytes_sec"] = round((read_total - old_read) / elapsed, 1)
                if write_total is not None and old_write is not None and write_total >= old_write:
                    row["write_bytes_sec"] = round((write_total - old_write) / elapsed, 1)
            if identity is not None:
                current[identity] = {
                    "cpu": cpu_total,
                    "read": read_total,
                    "write": write_total,
                }
        self.previous = current
        self.sampled_at = sampled_at


_process_rate_sampler = _ProcessRateSampler()


def collect_process_views(limit: int = 12) -> dict[str, Any]:
    """Collect CPU, RSS and process I/O leaders from one process-table pass."""
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(
        ["pid", "name", "create_time", "cpu_times", "memory_info", "io_counters"]
    ):
        try:
            row = _safe_process(proc, proc.info)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        if row is not None:
            rows.append(row)

    _process_rate_sampler.apply(rows, time.monotonic())
    by_cpu = sorted(
        rows,
        key=lambda row: (
            row["cpu_percent"] is not None,
            row["cpu_percent"] or 0,
            row["memory_mb"],
        ),
        reverse=True,
    )
    by_memory = sorted(
        rows, key=lambda row: (row["memory_mb"], row["cpu_percent"] or -1), reverse=True
    )
    by_io = sorted(
        rows,
        key=lambda row: (
            row["read_bytes_sec"] is not None or row["write_bytes_sec"] is not None,
            (row["read_bytes_sec"] or 0) + (row["write_bytes_sec"] or 0),
        ),
        reverse=True,
    )
    own = next((row for row in rows if row["pid"] == os.getpid()), None)
    return {
        "cpu": by_cpu[:limit],
        "memory": by_memory[:limit],
        "io": by_io[:limit],
        "self": own,
        "count": len(rows),
        "sampled_at": datetime.now(UTC).isoformat(),
        "sample_note": (
            "CPU and disk I/O rates are measured intervals tied to PID and start time; "
            "the first or reset counter is unknown. Disk I/O is per process, "
            "not per mount; per-process network I/O is unavailable."
        ),
    }


def _display_path(root: Path, path: Path) -> str:
    if root != Path("/"):
        try:
            return str(path.relative_to(root))
        except ValueError:
            return path.name
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def validate_mount_identity(mount: str, expected_source: str) -> Path:
    """Revalidate a snapshot mount before scanning its contents."""
    root = Path(mount)
    if root.is_symlink():
        raise ValueError("mount path changed")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("mount is unavailable") from exc
    if resolved != root.absolute() or not resolved.is_dir():
        raise ValueError("mount path changed")
    current = next(
        (
            item
            for item in psutil.disk_partitions(all=True)
            if Path(item.mountpoint).absolute() == resolved and item.device == expected_source
        ),
        None,
    )
    if current is None:
        raise ValueError("mount identity changed")
    return resolved


async def _terminate_process(process: Any, timeout: float = 1.0) -> None:
    """Kill a scan child without letting an uninterruptible mount block shutdown."""
    if process.returncode is None:
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        return


def scan_largest_files(
    root: Path, *, limit: int = 12, max_seconds: float = 10.0
) -> dict[str, Any]:
    """Find large regular files on one filesystem within a strict time budget.

    The result states whether the whole filesystem was scanned. A deadline can only
    be checked between filesystem calls, so a stalled remote mount may outlive the
    caller's await timeout; the scan still runs outside the event loop.
    """
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("mount is not a directory")
    device = root.stat().st_dev
    deadline = time.monotonic() + max(0.05, max_seconds)
    stack = [root]
    largest: list[tuple[int, str]] = []
    scanned_files = 0
    scanned_dirs = 0
    complete = True

    while stack:
        if time.monotonic() >= deadline:
            complete = False
            break
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        complete = False
                        break
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
                    if stat.st_dev != device:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        scanned_dirs += 1
                    elif entry.is_file(follow_symlinks=False):
                        scanned_files += 1
                        candidate = (int(stat.st_size), _display_path(root, Path(entry.path)))
                        if len(largest) < limit:
                            heapq.heappush(largest, candidate)
                        elif candidate[0] > largest[0][0]:
                            heapq.heapreplace(largest, candidate)
                if not complete:
                    break
        except (FileNotFoundError, PermissionError, OSError):
            continue

    rows = [
        {"size_bytes": size, "path": clean_text(path, 240)}
        for size, path in sorted(largest, reverse=True)
    ]
    return {
        "files": rows,
        "complete": complete,
        "scanned_files": scanned_files,
        "scanned_dirs": scanned_dirs,
        "duration_ms": round((time.monotonic() - (deadline - max(0.05, max_seconds))) * 1000),
    }


async def scan_largest_files_process(
    root: Path, *, limit: int = 12, max_seconds: float = 10.0
) -> dict[str, Any]:
    """Run a killable, streaming same-filesystem scan outside the server process."""
    root = Path(os.path.abspath(root))
    # systemd's filesystem sandbox permits direct traversal of allowlisted paths,
    # but denies opening the root directory itself once a mount namespace is in
    # use. Scan the root filesystem through explicit top-level starting points;
    # `-xdev` still prevents crossing into separately mounted filesystems.
    scan_roots = [root]
    if root == Path("/"):
        candidates = (
            "/boot",
            "/etc",
            "/home",
            "/opt",
            "/root",
            "/run",
            "/snap",
            "/srv",
            "/tmp",
            "/usr",
            "/var",
            "/swapfile",
        )
        scan_roots = [Path(path) for path in candidates if Path(path).exists()]
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        "find",
        *(str(path) for path in scan_roots),
        "-xdev",
        "-type",
        "f",
        "-printf",
        r"%s\t%p\0",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    largest: list[tuple[int, str]] = []
    scanned_files = 0
    pending = b""

    async def consume() -> None:
        nonlocal pending, scanned_files
        assert process.stdout is not None
        while chunk := await process.stdout.read(64 * 1024):
            parts = (pending + chunk).split(b"\0")
            pending = parts.pop()
            for raw in parts:
                size_raw, separator, path_raw = raw.partition(b"\t")
                if not separator:
                    continue
                try:
                    size = int(size_raw)
                except ValueError:
                    continue
                scanned_files += 1
                path = Path(path_raw.decode("utf-8", errors="replace"))
                candidate = (size, _display_path(root, path))
                if len(largest) < limit:
                    heapq.heappush(largest, candidate)
                elif size > largest[0][0]:
                    heapq.heapreplace(largest, candidate)

    complete = True
    try:
        await asyncio.wait_for(consume(), timeout=max_seconds)
        await process.wait()
        complete = process.returncode == 0
    except TimeoutError:
        complete = False
        await _terminate_process(process)
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise

    rows = [
        {"size_bytes": size, "path": clean_text(path, 240)}
        for size, path in sorted(largest, reverse=True)
    ]
    return {
        "files": rows,
        "complete": complete,
        "scanned_files": scanned_files,
        "scanned_dirs": None,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
