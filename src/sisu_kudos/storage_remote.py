"""Bounded, read-only storage scans over the configured SSH collector."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import shlex
import signal
import time
from datetime import UTC, datetime
from typing import Any

from .config import Settings, validate_remote_settings

_MAX_LIMIT = 100
_MAX_SECONDS = 20.0
_MAX_RESPONSE_BYTES = 262_144
_MOUNTINFO_PATH = "/proc/self/mountinfo"

# This fixed, stdlib-only probe runs on the configured remote host. Values are
# inserted only as JSON string literals; user input never becomes shell syntax.
_REMOTE_PROBE = r"""import bisect, datetime, glob, hashlib, json, os, re, selectors, signal
import socket, stat, subprocess, time
MOUNT = __MOUNT_LITERAL__
EXPECTED_SOURCE = __SOURCE_LITERAL__
EXPECTED_HOST = __HOST_LITERAL__
LIMIT = __LIMIT_LITERAL__
MAX_SECONDS = __SECONDS_LITERAL__
VALIDATE_ONLY = __VALIDATE_LITERAL__
MOUNTINFO_PATH = __MOUNTINFO_LITERAL__

def unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)

def mount_rows():
    rows = []
    with open(MOUNTINFO_PATH, "rt", encoding="utf-8", errors="strict") as stream:
        for line in stream:
            left, separator, right = line.rstrip("\n").partition(" - ")
            before, after = left.split(), right.split()
            if not separator or len(before) < 6 or len(after) < 2:
                raise ValueError("mountinfo")
            rows.append({
                "id": before[0], "parent": before[1], "device": before[2],
                "root": unescape_mount(before[3]), "mount": unescape_mount(before[4]),
                "options": before[5], "optional": tuple(before[6:]),
                "fstype": after[0], "source": unescape_mount(after[1]),
                "super_options": tuple(after[2:]),
            })
    return rows

def canonical_mount(value):
    if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
            or (value != "/" and (value.endswith("/") or "//" in value))
            or any(part in (".", "..") for part in value.split("/"))):
        raise ValueError("mount")

def check_identity():
    if socket.gethostname() != EXPECTED_HOST:
        raise ValueError("identity")
    canonical_mount(MOUNT)
    # Reject symlink substitutions anywhere in the configured mount path.
    current = "/"
    for part in MOUNT.split("/")[1:]:
        if part:
            current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except OSError:
            raise ValueError("mount") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("mount")
    root_info = os.lstat(MOUNT)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("mount")
    table = mount_rows()
    matches = [row for row in table if row["mount"] == MOUNT]
    if len(matches) != 1:
        raise ValueError("mount")
    root = matches[0]
    if root["source"] != EXPECTED_SOURCE:
        raise ValueError("source")
    device = "%d:%d" % (os.major(root_info.st_dev), os.minor(root_info.st_dev))
    if root["device"] != device:
        raise ValueError("mount")
    prefix = "/" if MOUNT == "/" else MOUNT + "/"
    nested = [row for row in table if row["mount"] != MOUNT and row["mount"].startswith(prefix)]
    # Include all identifying mount fields so an overmount or bind change is
    # detected after du, even when the source string happens to stay the same.
    def signature(row):
        return (row["id"], row["parent"], row["device"], row["root"], row["mount"],
                row["options"], row["optional"], row["fstype"], row["source"],
                row["super_options"])
    identity = (socket.gethostname(), signature(root), root_info.st_dev, root_info.st_ino,
                tuple(sorted(signature(row) for row in nested)))
    return identity, nested

def display_name(raw):
    text = os.fsdecode(raw)
    safe = []
    for char in text:
        if ord(char) == 92:
            safe.append(chr(92) * 2)
        elif char.isprintable():
            safe.append(char)
        else:
            code = ord(char)
            safe.append(chr(92) + ("x%02x" % code if code <= 255 else "u%04x" % code))
    rendered = "".join(safe)
    units = lambda value: sum(2 if ord(char) > 65535 else 1 for char in value)
    if units(rendered) <= 240:
        return rendered
    suffix = "~" + hashlib.sha256(raw).hexdigest()[:12]
    prefix = []
    used = 0
    for char in rendered:
        width = 2 if ord(char) > 65535 else 1
        if used + width > 240 - units(suffix):
            break
        prefix.append(char)
        used += width
    return "".join(prefix) + suffix

def excluded_pattern(path):
    # glob.escape yields GNU fnmatch-compatible bracket quoting for metacharacters.
    return "--exclude=" + glob.escape(path)

def consume_du(root, limit, seconds, nested):
    prefix = b"/" if root == "/" else os.fsencode(root.rstrip("/")) + b"/"
    excludes = [excluded_pattern(row["mount"]) for row in nested]
    du = ["du", "-x", "-B1", "--max-depth=1", "--all", "--null",
          "--no-dereference"] + excludes + ["--", root]
    command = ["timeout", "--signal=TERM", "--kill-after=0.5s",
               "%.3fs" % max(0.05, seconds), *du]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = bytearray()
    entries = []
    scanned = 0
    parse_ok = True
    timed_out = False
    ended = False
    deadline = time.monotonic() + max(0.05, seconds) + 0.9
    def add_record(record):
        nonlocal scanned, parse_ok
        count, separator, raw_path = record.partition(bytes((9,)))
        if not separator or not count.isdigit():
            parse_ok = False
            return
        if root == "/":
            if not raw_path.startswith(prefix) or raw_path == prefix:
                return
            relative = raw_path[1:]
        else:
            if not raw_path.startswith(prefix):
                return
            relative = raw_path[len(prefix):]
        if not relative or b"/" in relative or relative in (b".", b".."):
            return
        scanned += 1
        try:
            metadata = os.lstat(os.path.join(root, os.fsdecode(relative)))
        except OSError:
            parse_ok = False
            return
        if stat.S_ISLNK(metadata.st_mode):
            parse_ok = False
            return
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
        else:
            parse_ok = False
            return
        size = int(count)
        name = display_name(relative)
        if not name or name in (".", "..") or "/" in name:
            parse_ok = False
            return
        entry = {"path": name, "kind": kind, "size_bytes": size}
        key = (-size, name)
        bisect.insort(entries, (key, entry))
        if len(entries) > limit:
            entries.pop()
    try:
        while not ended:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready = selector.select(min(0.1, remaining))
            if ready:
                try:
                    chunk = os.read(process.stdout.fileno(), 65536)
                except OSError:
                    parse_ok = False
                    break
                if not chunk:
                    ended = True
                else:
                    pending.extend(chunk)
                    while True:
                        end = pending.find(bytes((0,)))
                        if end < 0:
                            break
                        record = bytes(pending[:end])
                        del pending[:end + 1]
                        add_record(record)
                    if len(pending) > 16384:
                        parse_ok = False
                        timed_out = True
                        break
            if process.poll() is not None and not ready:
                # The final pipe EOF will arrive on the next selector iteration.
                continue
        if pending:
            parse_ok = False
        if timed_out and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            returncode = process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                returncode = process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                returncode = 124
                timed_out = True
    finally:
        selector.close()
        if process.stdout:
            process.stdout.close()
    return [row for _, row in entries], scanned, (returncode == 0 and parse_ok and not timed_out)

def emit(payload, status=0):
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    raise SystemExit(status)

try:
    started = time.monotonic()
    before, nested = check_identity()
    if VALIDATE_ONLY:
        after, _ = check_identity()
        if before != after:
            raise ValueError("mount_changed")
        emit({"ok": True})
    remaining = MAX_SECONDS - (time.monotonic() - started)
    if remaining <= 0:
        entries, scanned, complete = [], 0, False
    else:
        entries, scanned, complete = consume_du(MOUNT, LIMIT, remaining, nested)
    after, _ = check_identity()
    if before != after:
        raise ValueError("mount_changed")
    emit({"ok": True, "entries": entries, "complete": complete,
          "scanned_entries": scanned,
          "sampled_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
except ValueError as error:
    code = str(error)
    if code not in {"identity", "mount", "source", "mount_changed"}:
        code = "mount"
    emit({"ok": False, "error": code}, 2)
except Exception:
    # No remote path, exception text, or stderr is returned to the caller.
    emit({"ok": False, "error": "unavailable"}, 2)
"""


def _validate_request(mount: str, expected_source: str, limit: int, max_seconds: float) -> None:
    if (
        not isinstance(mount, str)
        or not mount.startswith("/")
        or "\x00" in mount
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in mount)
        or (mount != "/" and (mount.endswith("/") or "//" in mount))
        or any(part in {".", ".."} for part in mount.split("/"))
    ):
        raise ValueError("invalid mount")
    try:
        mount_bytes = mount.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("invalid mount") from None
    if len(mount_bytes) > 4096:
        raise ValueError("invalid mount")
    if (
        not isinstance(expected_source, str)
        or not expected_source
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in expected_source)
    ):
        raise ValueError("invalid mount source")
    try:
        source_bytes = expected_source.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("invalid mount source") from None
    if len(source_bytes) > 4096:
        raise ValueError("invalid mount source")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise ValueError("limit must be between 1 and 100")
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or not 0.05 <= max_seconds <= _MAX_SECONDS
    ):
        raise ValueError("max_seconds must be between 0.05 and 20 seconds")


def _build_probe(
    mount: str,
    expected_source: str,
    expected_host: str,
    limit: int,
    max_seconds: float,
    validate_only: bool,
) -> str:
    replacements = {
        "__MOUNT_LITERAL__": json.dumps(mount),
        "__SOURCE_LITERAL__": json.dumps(expected_source),
        "__HOST_LITERAL__": json.dumps(expected_host),
        "__LIMIT_LITERAL__": str(limit),
        "__SECONDS_LITERAL__": repr(float(max_seconds)),
        "__VALIDATE_LITERAL__": "True" if validate_only else "False",
        "__MOUNTINFO_LITERAL__": json.dumps(_MOUNTINFO_PATH),
    }
    # One pass: a literal mount/source containing a marker must stay literal.
    return re.sub(
        "|".join(re.escape(marker) for marker in replacements),
        lambda match: replacements[match[0]],
        _REMOTE_PROBE,
    )


def _project_result(payload: Any, limit: int, duration_ms: int) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        code = payload.get("error") if isinstance(payload, dict) else None
        code = code if isinstance(code, str) else None
        messages = {
            "identity": "Remote host identity mismatch",
            "source": "Remote mount source changed",
            "mount_changed": "Remote mount changed during scan",
            "mount": "Remote mount is unavailable or unsafe",
        }
        message = (
            messages.get(code, "Remote storage scan unavailable")
            if code
            else ("Remote storage scan unavailable")
        )
        raise ValueError(message)
    raw_entries = payload.get("entries")
    complete = payload.get("complete")
    scanned = payload.get("scanned_entries")
    sampled_at = payload.get("sampled_at")
    if (
        not isinstance(raw_entries, list)
        or not isinstance(complete, bool)
        or isinstance(scanned, bool)
        or not isinstance(scanned, int)
        or scanned < 0
        or not isinstance(sampled_at, str)
    ):
        raise ValueError("Invalid remote storage response")
    try:
        sampled = datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("Invalid remote storage timestamp") from None
    if sampled.tzinfo is None or sampled.utcoffset() != UTC.utcoffset(sampled):
        raise ValueError("Invalid remote storage timestamp")
    safe_entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            complete = False
            continue
        path, kind, size = raw.get("path"), raw.get("kind"), raw.get("size_bytes")
        if (
            not isinstance(path, str)
            or not path
            or path in {".", ".."}
            or path.startswith("/")
            or "/" in path
            or "\x00" in path
            or any(not char.isprintable() for char in path)
            or sum(2 if ord(char) > 65535 else 1 for char in path) > 240
            or not isinstance(kind, str)
            or kind not in {"directory", "file"}
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > 2**53 - 1
        ):
            complete = False
            continue
        safe_entries.append({"path": path, "kind": kind, "size_bytes": size})
    safe_entries.sort(key=lambda entry: (-entry["size_bytes"], entry["path"]))
    return {
        "entries": safe_entries[:limit],
        "complete": complete,
        "scanned_entries": max(scanned, len(safe_entries)),
        "duration_ms": max(0, int(duration_ms)),
        "sampled_at": sampled.isoformat(),
        "scope": "mount_children",
        "size_basis": "allocated",
    }


async def _spawn_ssh(args: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks = bytearray()
    while True:
        block = await stream.read(min(16384, limit + 1 - len(chunks)))
        if not block:
            return bytes(chunks)
        chunks.extend(block)
        if len(chunks) > limit:
            raise ValueError("Remote response too large")


async def _stop_process(
    process: asyncio.subprocess.Process, exchange: asyncio.Task[tuple[int, bytes]] | None
) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=0.25)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=0.25)
    if exchange is not None and not exchange.done():
        exchange.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await exchange


async def scan_remote_storage(
    settings: Settings,
    mount: str,
    expected_source: str,
    *,
    limit: int = 40,
    max_seconds: float = 20.0,
    validate_only: bool = False,
) -> dict[str, Any]:
    """Scan direct children on the configured remote host, without following links.

    ``validate_only`` performs only live hostname/mount/source checks and returns
    ``{"valid": True}``; callers should use it before returning cached data.
    """
    _validate_request(mount, expected_source, limit, max_seconds)
    if not isinstance(validate_only, bool):
        raise ValueError("validate_only must be boolean")
    target, python, expected_host, _, _ = validate_remote_settings(settings)
    probe = _build_probe(mount, expected_source, expected_host, limit, max_seconds, validate_only)
    # This is an OpenSSH remote command string, so quote the two fixed command
    # tokens. The route and executable were revalidated from configured settings.
    remote_command = shlex.join([python, "-"])
    args = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=3",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ServerAliveInterval=2",
        "-o",
        "ServerAliveCountMax=1",
        target,
        remote_command,
    ]
    started = time.monotonic()
    try:
        process = await _spawn_ssh(args)
    except OSError:
        raise RuntimeError("Remote SSH unavailable") from None
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:
        await _stop_process(process, None)
        raise RuntimeError("Remote SSH unavailable")

    async def exchange() -> tuple[int, bytes]:
        stdin.write(probe.encode("utf-8"))
        await stdin.drain()
        stdin.close()
        output = await _read_bounded(stdout, _MAX_RESPONSE_BYTES)
        return await process.wait(), output

    task = asyncio.create_task(exchange())
    # Separate client-side cap also reaps SSH if the network/server vanishes;
    # the remote GNU timeout remains responsible for killing its owned du.
    ssh_cap = float(max_seconds) + 4.0
    try:
        done, _ = await asyncio.wait({task}, timeout=ssh_cap)
    except asyncio.CancelledError:
        await asyncio.shield(_stop_process(process, task))
        raise
    if not done:
        await _stop_process(process, task)
        if validate_only:
            raise RuntimeError("Remote SSH timed out") from None
        return _project_result(
            {
                "ok": True,
                "entries": [],
                "complete": False,
                "scanned_entries": 0,
                "sampled_at": datetime.now(UTC).isoformat(),
            },
            limit,
            int((time.monotonic() - started) * 1000),
        )
    try:
        returncode, output = task.result()
    except ValueError:
        await _stop_process(process, None)
        raise
    except OSError:
        await _stop_process(process, None)
        raise RuntimeError("Remote SSH unavailable") from None
    except Exception:
        await _stop_process(process, None)
        raise RuntimeError("Remote SSH unavailable") from None
    if not output:
        raise RuntimeError("Remote SSH unavailable")
    try:
        payload = json.loads(output)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("Remote SSH unavailable") from None
    if validate_only:
        if returncode or not isinstance(payload, dict) or payload.get("ok") is not True:
            _project_result(payload, limit, int((time.monotonic() - started) * 1000))
        return {"valid": True}
    if returncode and (not isinstance(payload, dict) or payload.get("ok") is not False):
        raise RuntimeError("Remote SSH unavailable")
    return _project_result(payload, limit, int((time.monotonic() - started) * 1000))
