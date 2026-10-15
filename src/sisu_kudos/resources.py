"""Read and safely project the optional local agent-resource registry snapshot."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_SNAPSHOT: Path | None = None
MAX_SNAPSHOT_BYTES = 1_000_000
MAX_RESOURCES = 200
STALE_AFTER_SECONDS = 60
_URL = re.compile(
    r"\b(?:https?|wss?|file)://[^\s<>\"']+"
    r"|\b(?:localhost|127\.0\.0\.1|(?:\d{1,3}\.){3}\d{1,3})"
    r"(?::\d+)?(?:/[^\s<>\"']*)?(?:\?[^\s<>\"']*)?"
    r"|\b[a-z0-9.-]+\.(?:local|lan|internal|home|test)(?::\d+)?(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)
_PRIVATE_PATH = re.compile(
    r"(?<!\S)(?:/(?:home|root|media|mnt|tmp|run|var|etc|opt|srv)/[^\s,;]+|~/[^\s,;]+)",
    re.IGNORECASE,
)
_SECRET = re.compile(
    r"\b(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|credential|session[_-]?key)"
    r"\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_ARGV = re.compile(r"(?:^|\s)--[A-Za-z][\w-]*(?:[=\s]+[^\s]+)?")
_COMMAND = re.compile(
    r"^(?:/|(?:python|node|bash|sh|zsh|curl|wget|uv|git|npm|systemctl|openclaw|hermes)\b)"
    r".+\s+.+$",
    re.IGNORECASE,
)


def _safe_text(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join("".join(ch for ch in value if ch.isprintable()).split())[:limit]
    if not text:
        return None
    if _ARGV.search(text) or _COMMAND.search(text):
        return "[argv redacted]"
    text = _URL.sub("[URL redacted]", text)
    text = _PRIVATE_PATH.sub("[path redacted]", text)
    text = _SECRET.sub("[secret redacted]", text)
    return text[:limit]


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _resource(row: Any, now: datetime) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    required = {
        "id": _safe_text(row.get("id"), 160),
        "profile": _safe_text(row.get("profile"), 100),
        "task_id": _safe_text(row.get("task_id"), 160),
        "label": _safe_text(row.get("label"), 180),
    }
    if not all(required.values()):
        return None
    if row.get("platform") not in {"hermes", "openclaw"}:
        return None
    if row.get("kind") not in {"browser", "process"}:
        return None
    if not isinstance(row.get("state"), str):
        return None
    if not isinstance(row.get("managed"), bool) or not isinstance(row.get("persistent"), bool):
        return None

    pid = row.get("pid")
    if isinstance(pid, bool) or (pid is not None and (not isinstance(pid, int) or pid <= 0)):
        return None
    started = _timestamp(row.get("started_at"))
    cpu = _finite_number(row.get("cpu_percent"))
    memory = row.get("memory_bytes")
    if isinstance(memory, bool) or (
        memory is not None and (not isinstance(memory, int) or memory < 0)
    ):
        memory = None
    return {
        "id": required["id"],
        "platform": row["platform"],
        "profile": required["profile"],
        "task_id": required["task_id"],
        "kind": row["kind"],
        "label": required["label"],
        "pid": pid,
        "started_at": row.get("started_at") if started else None,
        "age_seconds": (
            max(0, round((now - started).total_seconds())) if started is not None else None
        ),
        "cpu_percent": cpu,
        "memory_bytes": memory,
        "state": _safe_text(row["state"], 100) or "unknown",
        "managed": row["managed"],
        "persistent": row["persistent"],
        "persistence_reason": _safe_text(row.get("persistence_reason"), 240),
        "cleanup_reason": _safe_text(row.get("cleanup_reason"), 240),
    }


def _unavailable(warning: str) -> dict[str, Any]:
    return {
        "available": False,
        "status": "unavailable",
        "stale": False,
        "sampled_at": None,
        "age_seconds": None,
        "resources": [],
        "warnings": [warning],
    }


def read_resource_snapshot(
    path: Path | None = None,
    *,
    now: datetime | None = None,
    use_environment: bool = True,
    disabled: bool = False,
) -> dict[str, Any]:
    """Read the registry without exposing its path or unapproved source fields."""
    if disabled:
        return _unavailable("Resource integration is disabled")
    snapshot_path = path
    if snapshot_path is None and use_environment:
        configured = os.getenv("SISU_AGENT_RESOURCES_SNAPSHOT")
        snapshot_path = Path(configured).expanduser() if configured else DEFAULT_SNAPSHOT
    if snapshot_path is None:
        return _unavailable("Registry snapshot path is not configured")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    try:
        with snapshot_path.open("rb") as snapshot_file:
            raw = snapshot_file.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            return _unavailable("Registry snapshot exceeds the size limit")
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return _unavailable("Registry snapshot is not available")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _unavailable("Registry snapshot could not be read")

    if (
        not isinstance(payload, dict)
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != 1
    ):
        return _unavailable("Registry snapshot schema is unsupported")
    sampled = _timestamp(payload.get("sampled_at"))
    rows = payload.get("resources")
    if sampled is None or not isinstance(rows, list):
        return _unavailable("Registry snapshot is missing required fields")

    age = (current - sampled).total_seconds()
    stale = age > STALE_AFTER_SECONDS or age < -30
    warnings = (
        [text for item in payload.get("warnings", [])[:50] if (text := _safe_text(item, 240))]
        if isinstance(payload.get("warnings", []), list)
        else []
    )
    if age < -30:
        warnings.append("Registry timestamp is in the future")

    resources = []
    invalid = 0
    for row in rows[:MAX_RESOURCES]:
        clean = _resource(row, current)
        if clean is None:
            invalid += 1
        else:
            resources.append(clean)
    if len(rows) > MAX_RESOURCES:
        warnings.append("Additional registry resources omitted by the display limit")
    if invalid:
        warnings.append(f"{invalid} invalid registry row(s) omitted")
    return {
        "available": True,
        "status": "stale" if stale else "available",
        "stale": stale,
        "sampled_at": sampled.isoformat(timespec="seconds"),
        "age_seconds": max(0, round(age)),
        "resources": resources,
        "warnings": warnings[:50],
    }
