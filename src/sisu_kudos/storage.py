"""Bounded, on-demand disk allocation summaries, never process I/O."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import hashlib
import heapq
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .diagnostics import _terminate_process


def _display_name(name: str) -> str:
    """Escape filesystem bytes/control characters; bound browser UTF-16 units."""
    safe = []
    for char in name:
        if char == "\\":
            safe.append("\\\\")
        elif char.isprintable():
            safe.append(char)
        else:
            code = ord(char)
            safe.append("\\" + (f"x{code:02x}" if code <= 255 else f"u{code:04x}"))
    rendered = "".join(safe)
    if len(rendered.encode("utf-16-le")) <= 480:
        return rendered
    suffix = "~" + hashlib.sha256(os.fsencode(name)).hexdigest()[:12]
    prefix = []
    used = 0
    for char in rendered:
        width = 2 if ord(char) > 65535 else 1
        if used + width > 240 - len(suffix):
            break
        prefix.append(char)
        used += width
    return "".join(prefix) + suffix


async def scan_disk_usage_process(
    root: Path, *, limit: int = 40, max_seconds: float = 60.0
) -> dict[str, Any]:
    """Measure direct children; directory sizes include their descendants.

    GNU du accounts allocated blocks, deduplicates hard links, does not follow
    symlinks and is fenced from every nested mount, including same-device binds.
    Only bounded display rows leave this function. Permission/deadline omissions
    remain explicitly partial; a partial list is not a global largest claim.
    """
    root = Path(os.path.abspath(root))
    if root.is_symlink() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("mount path changed")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("invalid display limit")
    device = root.stat().st_dev
    excluded = {
        Path(row.mountpoint)
        for row in psutil.disk_partitions(all=True)
        if Path(row.mountpoint) != root and root in Path(row.mountpoint).parents
    }
    scan_roots = [root]
    fallback = False
    # Some filesystem namespaces permit explicit roots but deny enumerating '/'.
    try:
        with os.scandir(root):
            pass
    except PermissionError:
        if root != Path("/"):
            raise RuntimeError("filesystem scan is unavailable") from None
        fallback = True
        scan_roots = []
        for name in (
            "boot",
            "etc",
            "home",
            "opt",
            "root",
            "run",
            "snap",
            "srv",
            "tmp",
            "usr",
            "var",
            "media",
            "mnt",
            "lost+found",
            "swapfile",
        ):
            path = root / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if info.st_dev == device and not stat.S_ISLNK(info.st_mode) and path not in excluded:
                scan_roots.append(path)
    if not scan_roots:
        raise RuntimeError("filesystem scan is unavailable")
    command = [
        "du",
        "--one-file-system",
        "--block-size=1",
        "--max-depth=" + ("0" if fallback else "1"),
        "--all",
        "--null",
        "--no-dereference",
        *("--exclude=" + glob.escape(str(path)) for path in sorted(excluded)),
        "--",
        *(str(path) for path in scan_roots),
    ]
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    largest: list[tuple[int, str, str]] = []
    pending = b""
    scanned = 0
    valid = True

    async def consume() -> None:
        nonlocal pending, scanned, valid
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            parts = (pending + chunk).split(b"\0")
            pending = parts.pop()
            if len(pending) > 65536:
                raise RuntimeError("filesystem output is invalid")
            for raw in parts:
                size_raw, separator, path_raw = raw.partition(b"\t")
                if not separator or not size_raw.isdigit():
                    valid = False
                    continue
                path = Path(os.fsdecode(path_raw))
                if path == root:
                    continue
                if path.parent != root or path in excluded:
                    valid = False
                    continue
                try:
                    info = path.lstat()
                except OSError:
                    valid = False
                    continue
                if info.st_dev != device or stat.S_ISLNK(info.st_mode):
                    continue
                kind = (
                    "directory"
                    if stat.S_ISDIR(info.st_mode)
                    else "file"
                    if stat.S_ISREG(info.st_mode)
                    else None
                )
                if kind is None:
                    continue
                scanned += 1
                candidate = (int(size_raw), _display_name(path.name), kind)
                if len(largest) < limit:
                    heapq.heappush(largest, candidate)
                elif candidate > largest[0]:
                    heapq.heapreplace(largest, candidate)
        if pending:
            valid = False
        await process.wait()

    completed = False
    try:
        await asyncio.wait_for(consume(), timeout=max_seconds)
        completed = process.returncode == 0 and valid and not fallback
    except TimeoutError:
        pass
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                await _terminate_process(process)
    return {
        "entries": [
            {"path": path, "kind": kind, "size_bytes": size}
            for size, path, kind in sorted(largest, key=lambda row: (-row[0], row[1], row[2]))
        ],
        "complete": completed,
        "scanned_entries": scanned,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "sampled_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scope": "mount_children",
        "size_basis": "allocated",
    }
