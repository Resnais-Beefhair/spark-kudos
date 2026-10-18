"""Read-only optional telemetry adapters and browser-safe projections.

The browser contract is intentionally smaller than either source model. Raw command lines,
tool inputs, tool results, session keys, user messages and chain-of-thought never cross it.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def _add_path(path: Path) -> None:
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)


def load_sisu_collectors(repo: Path | None = None) -> Any:
    if repo is None:
        return importlib.import_module("sisu_kudos.builtin")
    _add_path(repo)
    return importlib.import_module("sisu_monitor.collectors")


def load_agent_service(repo: Path, openclaw_home: Path | None = None) -> Any:
    from .hermes_sessions import serialized_hermes_collector
    from .openclaw_sessions import ReadOnlyOpenClawCollector

    _add_path(repo / "src")
    module = importlib.import_module("agentmon.service")
    service = module.MonitorService()
    service.hermes_collector = serialized_hermes_collector(service.hermes_collector)
    service.openclaw_collector = ReadOnlyOpenClawCollector(
        openclaw_home or Path.home() / ".openclaw", service.resolver
    )
    return service


def primitive(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def clean_text(value: Any, limit: int = 180) -> str:
    """Return one bounded display line, stripping control characters."""
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[:limit]


def opaque_id(value: Any) -> str:
    """Create a stable browser-local join key without exposing source routing IDs."""
    raw = str(value or "unknown").encode("utf-8", errors="replace")
    return hashlib.blake2s(raw, digest_size=10, person=b"sisukudo").hexdigest()


_ROUTING_ID_RE = re.compile(r"(?<!\d)\d{16,20}(?!\d)")


def safe_location(value: Any) -> str:
    """Keep a human channel label but never expose Discord routing identifiers."""
    text = clean_text(value, 100)
    if not text:
        return "—"
    if _ROUTING_ID_RE.fullmatch(text):
        return "Discord"
    return _ROUTING_ID_RE.sub("[ID]", text)


def safe_process(row: Any) -> dict[str, Any]:
    return {
        "pid": int(getattr(row, "pid", 0) or 0),
        "name": clean_text(getattr(row, "name", "unknown"), 64),
        "cpu_percent": round(float(getattr(row, "cpu_percent", 0) or 0), 1),
        "memory_mb": round(float(getattr(row, "memory_mb", 0) or 0), 1),
    }


def safe_gpu_snapshot(gpu: Any, collector: dict[str, Any]) -> dict[str, Any]:
    data = asdict(gpu)
    data["memory_percent"] = gpu.memory_percent
    data["gpu_processes"] = (
        [safe_gpu_process(row) for row in gpu.gpu_processes]
        if gpu.gpu_processes is not None else None
    )
    health = data.get("health_status", data.get("health", "unavailable"))
    if health in {"ok", "healthy", "available"}:
        if collector.get("stale"):
            health = "stale"
        elif not collector.get("ok"):
            health = "unavailable"
    data["health_status"] = health
    if health not in {"ok", "healthy", "available"}:
        for key in (
            "memory_percent",
            "memory_used_mb",
            "memory_total_mb",
            "utilization_percent",
            "temperature",
            "power_watts",
            "power_limit_watts",
        ):
            data[key] = None
        data["gpu_processes"] = None
        for key in ("history", "mem_history", "temp_history", "power_history"):
            data[key] = []
    return data


def safe_gpu_process(row: Any) -> dict[str, Any]:
    util = getattr(row, "gpu_util", None)
    return {
        "pid": int(getattr(row, "pid", 0) or 0),
        "name": clean_text(getattr(row, "name", "unknown"), 64),
        "memory_mb": round(float(getattr(row, "memory_mb", 0) or 0), 1),
        "gpu_util": None if util is None else round(float(util), 1),
    }


def safe_event(event: Any, session: Any, snapshot_timestamp: str = "") -> dict[str, Any]:
    """Explicit allowlist. Notably excludes event.detail."""
    event_timestamp = clean_text(getattr(event, "timestamp", ""), 40)
    fallback_timestamp = clean_text(
        getattr(session, "updated_at", "")
        or getattr(session, "started_at", "")
        or snapshot_timestamp,
        40,
    )
    return {
        "timestamp": event_timestamp or fallback_timestamp,
        "timestamp_approximate": not bool(event_timestamp) and bool(fallback_timestamp),
        "kind": clean_text(getattr(event, "kind", "STATE"), 12),
        "summary": clean_text(getattr(event, "summary", ""), 180),
        "tool_name": clean_text(getattr(event, "tool_name", ""), 48),
        "duration_ms": getattr(event, "duration_ms", None),
        "is_error": bool(getattr(event, "is_error", False)),
        "total_tokens": getattr(event, "total_tokens", None),
        "session_uid": opaque_id(getattr(session, "uid", "")),
        "agent": clean_text(getattr(session, "display_name", "agent"), 64),
        "runtime": clean_text(getattr(session, "runtime", ""), 16),
        "model": clean_text(getattr(session, "model", ""), 80),
        "provider": clean_text(getattr(session, "provider", ""), 40),
    }


def safe_session(session: Any, snapshot_timestamp: str = "") -> dict[str, Any]:
    events = [
        safe_event(item, session, snapshot_timestamp)
        for item in list(getattr(session, "events", []))[-12:]
    ]
    return {
        "uid": opaque_id(getattr(session, "uid", "")),
        "runtime": clean_text(getattr(session, "runtime", ""), 16),
        "agent": clean_text(getattr(session, "display_name", "agent"), 64),
        "status": clean_text(getattr(session, "status", "unknown"), 16),
        "model": clean_text(getattr(session, "model", "—"), 80),
        "provider": clean_text(getattr(session, "provider", "—"), 40),
        "title": clean_text(getattr(session, "title", ""), 120),
        # Hermes titles name persistent conversations, not the current turn.
        # Never use task_preview: it may contain a raw user prompt.
        "task": clean_text(
            getattr(session, "task_title", "")
            if getattr(session, "runtime", "") == "hermes"
            else getattr(session, "title", ""),
            180,
        ),
        "location": safe_location(getattr(session, "discord_location", "—")),
        "started_at": clean_text(
            getattr(session, "task_started_at", "") or getattr(session, "started_at", ""), 40
        ),
        "updated_at": clean_text(getattr(session, "updated_at", ""), 40),
        "age_seconds": getattr(session, "age_seconds", None),
        "reasoning": clean_text(getattr(session, "reasoning_effort", ""), 24),
        "service_tier": clean_text(getattr(session, "service_tier", ""), 16),
        "current_tool": clean_text(getattr(session, "current_tool", ""), 48),
        "task_total_tokens": getattr(session, "task_total_tokens", None),
        "task_api_calls": getattr(session, "task_api_calls", None),
        "events": events,
    }


def safe_agent_snapshot(snapshot: Any, *, sessions: list[Any] | None = None) -> dict[str, Any]:
    active_states = {"running", "tool", "waiting"}
    snapshot_timestamp = clean_text(getattr(snapshot, "generated_at", ""), 40)
    sessions = list(getattr(snapshot, "sessions", [])) if sessions is None else sessions
    selected = [
        item
        for item in sessions
        if getattr(item, "status", "") in active_states
        or (getattr(item, "age_seconds", None) is not None and item.age_seconds <= 3600)
    ][:16]
    safe_sessions = [safe_session(item, snapshot_timestamp) for item in selected]
    failed_sources = {
        item.runtime for item in getattr(snapshot, "health", []) if not item.ok
    }
    for item in safe_sessions:
        if item["runtime"] in failed_sources:
            item["stale"] = True
    events = [event for item in safe_sessions for event in item["events"]]
    events.sort(key=lambda item: item["timestamp"])

    quota_obj = getattr(snapshot, "chatgpt_quota", None)
    chatgpt: dict[str, Any] = {
        "provider": "ChatGPT",
        "available": False,
        "status": "",
        "windows": [],
        "resets_available": None,
        "collected_at": "",
    }
    if quota_obj:
        windows = []
        for window in getattr(quota_obj, "windows", []):
            windows.append(
                {
                    "id": clean_text(getattr(window, "limit_id", ""), 40),
                    "label": clean_text(getattr(window, "label", "usage window"), 80),
                    "remaining_percent": int(getattr(window, "remaining_percent", 0) or 0),
                    "used_percent": int(getattr(window, "used_percent", 0) or 0),
                    "duration_minutes": getattr(window, "duration_minutes", None),
                    "resets_at": getattr(window, "resets_at", None),
                }
            )
        chatgpt.update(
            {
                "available": bool(windows) and not bool(getattr(quota_obj, "error", "")),
                "status": clean_text(
                    getattr(quota_obj, "plan_name", "ChatGPT")
                    or getattr(quota_obj, "error", ""),
                    100,
                ),
                "windows": windows,
                "resets_available": getattr(quota_obj, "reset_credits_available", None),
                "reset_credit_expires_at": getattr(quota_obj, "reset_credit_expires_at", None),
                "collected_at": clean_text(getattr(quota_obj, "collected_at", ""), 40),
            }
        )

    health = [
        {
            "source": clean_text(getattr(item, "runtime", ""), 20),
            "ok": bool(getattr(item, "ok", False)),
            "message": clean_text(getattr(item, "message", ""), 160),
            "count": int(getattr(item, "source_count", 0) or 0),
        }
        for item in getattr(snapshot, "health", [])
    ]
    return {
        "generated_at": snapshot_timestamp,
        "sessions": safe_sessions,
        "events": events[-80:],
        "health": health,
        "quotas": {
            "chatgpt": chatgpt,
        },
    }
