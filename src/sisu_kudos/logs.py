"""Bounded, redacted read-only view of canonical Hermes logs.

The browser never receives raw log lines. Only structured lines from an explicit
allowlist are parsed, aggressively redacted and bounded before serialization.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .log_events import (
    classify,
    classify_proxy_eof,
    correlate,
    identity,
    live_recoveries,
    merge_copies,
    safe_component,
)

LOG_FILES = {
    "agent": "agent.log",
    "errors": "errors.log",
    "gateway": "gateway.log",
}

_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:,\d+)?)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)"
    r"(?:\s+\[[^]]+\])?\s+(?P<component>[^:\s]+):\s*(?P<message>.*)$"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|passwd|secret|authorization|cookie|session[_-]?key|"
    r"[a-z0-9_-]*(?:cmdline|command[_-]?line)|prompt|tool[_-]?(?:input|result)|"
    r"message[_-]?(?:content|text))\s*[:=].*$"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_URL_USERINFO_RE = re.compile(r"(?i)\b((?:https?|wss?)://)[^/\s@]+@")
_URL_QUERY_RE = re.compile(r"((?:https?|wss?)://[^\s?#]+)(?:\?[^\s#]*)?(?:#[^\s]*)?", re.I)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_SESSION_RE = re.compile(r"\b(?:sess(?:ion)?|thread|channel)[_:=/-]?[A-Za-z0-9-]{6,}\b", re.I)
_SNOWFLAKE_RE = re.compile(r"(?<!\d)\d{16,20}(?!\d)")
_LONG_SECRET_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9])")
_HOME_RE = re.compile(r"/home/[^/\s]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LEVEL_WEIGHT = {
    "DEBUG": 0,
    "INFO": 1,
    "WARNING": 2,
    "WARN": 2,
    "ERROR": 3,
    "CRITICAL": 4,
    "FATAL": 4,
}


def _tail_lines(path: Path, count: int = 500, max_bytes: int = 512_000) -> list[str]:
    """Read a bounded tail without following symlinks outside the configured directory."""
    if not path.exists() or not path.is_file():
        return []
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - max_bytes))
        data = handle.read(max_bytes)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return lines[-count:]


def redact_message(value: str, limit: int = 320) -> str:
    """Remove common secret, routing and private-content shapes from a message."""
    text = _CONTROL_RE.sub("", str(value))
    text = _SENSITIVE_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    text = _URL_QUERY_RE.sub(r"\1", text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _SESSION_RE.sub("[SESSION]", text)
    text = _SNOWFLAKE_RE.sub("[ID]", text)
    text = _LONG_SECRET_RE.sub("[REDACTED]", text)
    text = _HOME_RE.sub("~", text)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def parse_log_line(
    line: str, source: str, agent: str = "default", *, profile: str | None = None
) -> dict[str, Any] | None:
    match = _LINE_RE.match(line)
    if not match:
        return None
    level = match.group("level")
    if level == "WARN":
        level = "WARNING"
    if level == "FATAL":
        level = "CRITICAL"
    raw_message = match.group("message")
    if not raw_message:
        return None
    raw_component = match.group("component")
    component = safe_component(raw_component)
    category, status, message = classify(raw_component, raw_message, level, profile)
    timestamp = match.group("timestamp").replace(",", ".")
    try:
        parsed = datetime.fromisoformat(timestamp.replace(" ", "T"))
        timestamp = parsed.astimezone().isoformat(timespec="milliseconds")
    except ValueError:
        return None
    return {
        "id": identity("hermes", agent, timestamp, level, raw_component, raw_message),
        "timestamp": timestamp,
        "runtime": "hermes",
        "agent": redact_message(agent, 40),
        "source": source,
        "sources": [source],
        "level": level,
        "component": component,
        "message": message,
        "category": category,
        "status": status,
    }


def _parse_log_blocks(
    lines: list[str], source: str, agent: str, profile: str
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current = None
    continuation: list[str] = []
    for line in lines:
        # Any structured start terminates the traceback, even if malformed.
        if re.match(r"^\d{4}-\d{2}-\d{2}[ T]", line):
            if current is not None:
                classify_proxy_eof(current, continuation)
                records.append(current)
            current = parse_log_line(line, source, agent, profile=profile)
            continuation = []
        elif current is not None:
            if len(continuation) < 120:
                continuation.append(line)
            elif len(continuation) == 120:
                continuation.append("TRACE_TRUNCATED")
    if current is not None:
        classify_proxy_eof(current, continuation)
        records.append(current)
    return records


def collect_hermes_logs(log_dirs: Path | Mapping[str, Path], limit: int = 160) -> dict[str, Any]:
    """Collect balanced recent records from explicitly allowed Hermes profile logs."""
    profiles = {"default": log_dirs} if isinstance(log_dirs, Path) else dict(log_dirs)
    per_stream_limit = max(1, min(limit, 100, 300 // max(1, len(profiles) * len(LOG_FILES))))
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for agent, log_dir in profiles.items():
        root = log_dir.resolve(strict=False)
        profile = log_dir.parent.name if log_dir.parent.parent.name == "profiles" else "default"
        for source, filename in LOG_FILES.items():
            path = log_dir / filename
            resolved = path.resolve(strict=False)
            if resolved.parent != root:
                continue
            exists = path.exists() and path.is_file()
            sources.append({"name": source, "agent": agent, "available": exists})
            if not exists:
                continue
            records.extend(_parse_log_blocks(_tail_lines(path), source, agent, profile))
    # Correlate BEFORE display sampling, so busy INFO streams cannot hide recovery.
    records = correlate(records, live_recoveries(profiles))
    selected: list[dict[str, Any]] = []
    stream_counts: dict[tuple[str, str], int] = {}
    for row in sorted(records, key=lambda row: row["timestamp"], reverse=True):
        key = (row["agent"], row["source"])
        count = stream_counts.get(key, 0)
        if count < per_stream_limit:
            selected.append(row)
            stream_counts[key] = count + 1
    records = selected[:300]
    return {
        "records": records,
        "counts": _log_counts(records),
        "sources": sources,
        "redacted": True,
    }


_OPENCLAW_UNIT = "openclaw-gateway.service"
_OPENCLAW_SOURCE = "openclaw-gateway"
_OPENCLAW_MAX_LINES = 120
_OPENCLAW_MAX_BYTES = 512_000
_OPENCLAW_TIMEOUT_S = 1.5
_OPENCLAW_PRIORITY = {
    "0": "CRITICAL",
    "1": "CRITICAL",
    "2": "CRITICAL",
    "3": "ERROR",
    "4": "WARNING",
    "5": "INFO",
    "6": "INFO",
    "7": "DEBUG",
}
_OPENCLAW_FIELDS = (
    "_CURSOR",
    "_SYSTEMD_UNIT",
    "_SYSTEMD_USER_UNIT",
    "__REALTIME_TIMESTAMP",
    "PRIORITY",
    "MESSAGE",
)
_OPENCLAW_COMPONENTS = {
    "agent",
    "auth",
    "browser",
    "channel",
    "config",
    "cron",
    "gateway",
    "http",
    "plugin",
    "process",
    "runtime",
    "system",
    "tool",
    "update",
    "websocket",
    "ws",
}
_OPENCLAW_CACHE_LOCK = threading.Lock()
_OPENCLAW_LAST_GOOD: list[dict[str, Any]] = []
_OPENCLAW_STATUS_ROWS: dict[str, dict[str, Any]] = {}


def _journalctl_command(limit: int) -> list[str]:
    fields = ",".join(_OPENCLAW_FIELDS)
    return [
        "journalctl",
        "--user",
        f"--unit={_OPENCLAW_UNIT}",
        "--since=30 minutes ago",
        "--lines",
        str(min(limit, _OPENCLAW_MAX_LINES)),
        "--reverse",
        "--output=json",
        f"--output-fields={fields}",
        "--no-pager",
    ]


def _journal_timestamp(value: Any) -> str | None:
    if isinstance(value, bool) or not str(value).isdigit():
        return None
    try:
        return (
            datetime.fromtimestamp(int(value) / 1_000_000)
            .astimezone()
            .isoformat(timespec="milliseconds")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _openclaw_component(message: str) -> str:
    match = re.search(r"\[([A-Za-z0-9_.-]{1,32})\]", message)
    if match:
        candidate = match.group(1).lower()
        if candidate in _OPENCLAW_COMPONENTS:
            return candidate
    return "gateway"


def _openclaw_level(message: str, journal_level: str) -> str:
    """Node stdout uses journal INFO even for structured application errors."""
    explicit = None
    try:
        item = json.loads(message)
        if isinstance(item, dict):
            raw = item.get("level")
            meta = item.get("_meta")
            if isinstance(meta, dict):
                raw = meta.get("logLevelName", raw)
            explicit = {
                "10": "DEBUG",
                "20": "DEBUG",
                "30": "INFO",
                "40": "WARNING",
                "50": "ERROR",
                "60": "CRITICAL",
                "WARN": "WARNING",
            }.get(str(raw).upper(), str(raw).upper())
    except (ValueError, TypeError):
        pass
    if explicit not in _LEVEL_WEIGHT:
        # Only explicit severity tags or the known tool failure envelope, not arbitrary prose.
        match = re.search(r"\[(ERROR|WARN(?:ING)?|CRITICAL|FATAL)\]", message)
        explicit = match.group(1) if match else None
        if re.search(r"\[tools?\]\s+[A-Za-z_.-]+ failed:", message):
            explicit = "ERROR"
        explicit = {"WARN": "WARNING", "FATAL": "CRITICAL"}.get(str(explicit), explicit)
    if explicit in _LEVEL_WEIGHT and _LEVEL_WEIGHT[explicit] > _LEVEL_WEIGHT[journal_level]:
        return explicit
    return journal_level


def _openclaw_summary(message: str, level: str) -> str:
    """Classify a gateway message without forwarding its free-form contents."""
    if level in {"ERROR", "CRITICAL"}:
        return "Gateway reported an operational error"
    if level == "WARNING":
        return "Gateway reported a warning"
    folded = message.casefold()
    if "timeout" in folded or "timed out" in folded:
        return "Gateway operation timed out"
    if "reconnect" in folded or "retry" in folded:
        return "Gateway connection retry"
    if "shutdown" in folded or "stopping" in folded:
        return "Gateway shutdown event"
    if "connect" in folded or "disconnect" in folded:
        return "Gateway connection event"
    if "listen" in folded or "ready" in folded or "started" in folded:
        return "Gateway operational event"
    if "health" in folded:
        return "Gateway health event"
    if level == "DEBUG":
        return "Gateway diagnostic event"
    return "Gateway operational event"


def _openclaw_record(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    unit = entry.get("_SYSTEMD_USER_UNIT") or entry.get("_SYSTEMD_UNIT")
    if unit != _OPENCLAW_UNIT:
        return None
    message = entry.get("MESSAGE")
    timestamp = _journal_timestamp(entry.get("__REALTIME_TIMESTAMP"))
    priority = entry.get("PRIORITY")
    level = _OPENCLAW_PRIORITY.get(str(priority))
    if not isinstance(message, str) or not message or timestamp is None or level is None:
        return None
    level = _openclaw_level(message, level)
    return {
        "id": identity("openclaw", timestamp, entry.get("_CURSOR"), level, message),
        "timestamp": timestamp,
        "runtime": "openclaw",
        "agent": "OpenClaw",
        "source": _OPENCLAW_SOURCE,
        "sources": [_OPENCLAW_SOURCE],
        "level": level,
        "component": _openclaw_component(message),
        "message": _openclaw_summary(message, level),
        "category": "operational",
        "status": "recorded",
    }


def _openclaw_status_row(message: str, level: str = "ERROR") -> dict[str, Any]:
    with _OPENCLAW_CACHE_LOCK:
        if message in _OPENCLAW_STATUS_ROWS:
            return dict(_OPENCLAW_STATUS_ROWS[message])
    row = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "runtime": "openclaw",
        "agent": "OpenClaw",
        "source": _OPENCLAW_SOURCE,
        "level": level,
        "component": "collector",
        "message": message,
        "sources": [_OPENCLAW_SOURCE],
        "category": "operational",
        "status": "recorded",
    }
    row["id"] = identity("openclaw-collector", row["timestamp"], message)
    with _OPENCLAW_CACHE_LOCK:
        _OPENCLAW_STATUS_ROWS[message] = row
    return dict(row)


def _log_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "warning": sum(_LEVEL_WEIGHT.get(row.get("level", ""), 0) == 2 for row in records),
        "error": sum(
            _LEVEL_WEIGHT.get(row.get("level", ""), 0) >= 3 and row.get("status") != "recovered"
            for row in records
        ),
        "recovered": sum(row.get("status") == "recovered" for row in records),
    }


def _cached_openclaw_rows() -> list[dict[str, Any]]:
    with _OPENCLAW_CACHE_LOCK:
        return [dict(row) for row in _OPENCLAW_LAST_GOOD]


def _remember_openclaw_rows(records: list[dict[str, Any]]) -> None:
    with _OPENCLAW_CACHE_LOCK:
        _OPENCLAW_LAST_GOOD[:] = [dict(row) for row in records[:300]]


def collect_openclaw_logs(limit: int = 160) -> dict[str, Any]:
    """Collect bounded OpenClaw gateway records from the user journal.

    Journal messages are used only to derive a safe fixed summary and an
    allowlisted component. Collection failures retain the previous successful
    OpenClaw rows and add an explicit error record rather than erasing them.
    """
    limit = max(1, min(int(limit), 300))
    source = {
        "name": _OPENCLAW_SOURCE,
        "agent": "OpenClaw",
        "runtime": "openclaw",
        "available": False,
    }
    try:
        result = subprocess.run(
            _journalctl_command(limit),
            capture_output=True,
            text=True,
            timeout=_OPENCLAW_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        records = _cached_openclaw_rows()[: max(0, limit - 1)]
        records.append(
            _openclaw_status_row("OpenClaw journal unavailable; last known records retained")
        )
        records.sort(key=lambda row: row["timestamp"], reverse=True)
        return {
            "records": records,
            "counts": _log_counts(records),
            "sources": [source],
            "redacted": True,
        }

    if result.returncode != 0:
        records = _cached_openclaw_rows()[: max(0, limit - 1)]
        records.append(
            _openclaw_status_row("OpenClaw journal unavailable; last known records retained")
        )
        records.sort(key=lambda row: row["timestamp"], reverse=True)
        return {
            "records": records,
            "counts": _log_counts(records),
            "sources": [source],
            "redacted": True,
        }

    stdout = result.stdout if isinstance(result.stdout, str) else ""
    encoded = stdout.encode("utf-8", errors="replace")
    truncated = len(encoded) > _OPENCLAW_MAX_BYTES
    if truncated:
        encoded = encoded[:_OPENCLAW_MAX_BYTES]
        encoded = encoded[: encoded.rfind(b"\n") + 1]
        stdout = encoded.decode("utf-8", errors="replace")

    parsed_entries: list[Mapping[str, Any]] = []
    malformed = int(truncated)
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            malformed += 1
            continue
        if not isinstance(entry, Mapping):
            malformed += 1
            continue
        parsed_entries.append(entry)

    records: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    unsupported = 0
    for entry in parsed_entries:
        cursor = entry.get("_CURSOR")
        key = (
            ("cursor", cursor)
            if isinstance(cursor, str) and cursor
            else (
                "fallback",
                str(entry.get("__REALTIME_TIMESTAMP", "")),
                str(entry.get("PRIORITY", "")),
                str(entry.get("MESSAGE", "")),
            )
        )
        if key in seen:
            continue
        seen.add(key)
        record = _openclaw_record(entry)
        if record is None:
            unsupported += 1
            continue
        records.append(record)

    records.sort(key=lambda row: row["timestamp"], reverse=True)
    failed_schema = not records and bool(parsed_entries or malformed or unsupported)
    if failed_schema:
        source["available"] = False
        records = _cached_openclaw_rows()[: max(0, limit - 1)]
        records.append(
            _openclaw_status_row("OpenClaw journal schema invalid; last known records retained")
        )
    else:
        source["available"] = True
        _remember_openclaw_rows(records)
        if not malformed and not unsupported:
            with _OPENCLAW_CACHE_LOCK:
                _OPENCLAW_STATUS_ROWS.clear()

    if (malformed or unsupported) and not failed_schema:
        records.append(
            _openclaw_status_row(
                "Skipped malformed or unsupported OpenClaw journal records",
                level="WARNING",
            )
        )
    records.sort(key=lambda row: row["timestamp"], reverse=True)
    records = records[:limit]
    return {
        "records": records,
        "counts": _log_counts(records),
        "sources": [source],
        "redacted": True,
    }


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return merge_copies(records)


def _balanced_recent_records(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    streams: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in records:
        key = (
            str(row.get("runtime", "")),
            str(row.get("agent", "")),
            str(row.get("source", "")),
        )
        streams.setdefault(key, []).append(row)
    for rows in streams.values():
        rows.sort(key=lambda row: row.get("timestamp", ""), reverse=True)

    selected: list[dict[str, Any]] = []
    positions = {key: 0 for key in streams}
    while len(selected) < limit:
        progressed = False
        for key in sorted(streams):
            position = positions[key]
            rows = streams[key]
            if position < len(rows):
                selected.append(rows[position])
                positions[key] += 1
                progressed = True
                if len(selected) == limit:
                    break
        if not progressed:
            break
    selected.sort(key=lambda row: row.get("timestamp", ""), reverse=True)
    return selected


def collect_all_logs(log_dirs: Path | Mapping[str, Path], limit: int = 160) -> dict[str, Any]:
    """Combine balanced Hermes and OpenClaw logs under one bounded contract."""
    limit = max(1, min(int(limit), 300))
    hermes = collect_hermes_logs(log_dirs, limit=limit)
    openclaw = collect_openclaw_logs(limit=limit)

    records: list[dict[str, Any]] = []
    for row in hermes.get("records", []):
        safe_row = dict(row)
        safe_row.setdefault("runtime", "hermes")
        records.append(safe_row)
    for row in openclaw.get("records", []):
        safe_row = dict(row)
        safe_row.setdefault("runtime", "openclaw")
        records.append(safe_row)
    records = _balanced_recent_records(_dedupe_records(records), limit)

    sources: list[dict[str, Any]] = []
    source_positions: dict[tuple[str, str, str], int] = {}
    for runtime, result in (("hermes", hermes), ("openclaw", openclaw)):
        for source in result.get("sources", []):
            safe_source = dict(source)
            safe_source.setdefault("runtime", runtime)
            key = (
                str(safe_source.get("runtime", runtime)),
                str(safe_source.get("agent", "")),
                str(safe_source.get("name", "")),
            )
            if key in source_positions:
                index = source_positions[key]
                sources[index]["available"] = bool(
                    sources[index].get("available") or safe_source.get("available")
                )
                continue
            source_positions[key] = len(sources)
            sources.append(safe_source)

    return {
        "records": records,
        "counts": _log_counts(records),
        "sources": sources,
        "redacted": True,
    }
