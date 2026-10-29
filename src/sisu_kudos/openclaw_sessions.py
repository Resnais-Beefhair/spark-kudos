"""OpenClaw session metadata without CLI config/ownership side effects.

The CLI's writable state bootstrap fails inside ProtectSystem/ProtectHome on
OpenClaw 2026.10.2. KUDOS only needs committed metadata, not a Gateway lease.
No platform code is imported, and neither ownership checks nor sandbox policy
are changed. Unknown schemas/locators fail visibly instead of claiming no work.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .adapters import safe_location
from .crons import _connect, _read_json, process_fingerprint
from .logs import redact_message

MAX_AGENTS = 32
MAX_SESSIONS = 200
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# Never SELECT entry_json or transcript content. Only committed metadata leaves SQLite.
FIELDS = (
    "status",
    "sessionStartedAt",
    "startedAt",
    "endedAt",
    "abortedLastRun",
    "activeWriterRunId",
    "lifecycleRunId",
    "model",
    "modelProvider",
    "thinkingLevel",
    "reasoningLevel",
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "contextTokens",
    "groupChannel",
    "displayName",
    "label",
    "chatType",
)
PROJECTION = ", ".join(
    (
        f"CASE WHEN json_type(entry_json, '$.{field}')='text' "
        f"THEN json_extract(entry_json, '$.{field}') END AS {field}"
        if field in {"model", "modelProvider"}
        else f"json_extract(entry_json, '$.{field}') AS {field}"
    )
    for field in FIELDS
)


def _iso(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat(timespec="seconds")
    return ""


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value > 0 else None
    return None


def _owner_started_ms(ticks: int) -> float:
    return (psutil.boot_time() + ticks / os.sysconf("SC_CLK_TCK")) * 1000


def _status(row: sqlite3.Row, owner_started: float | None) -> str:
    status = {
        "queued": "waiting",
        "running": "running",
        "done": "idle",
        "failed": "error",
        "killed": "error",
        "timeout": "error",
        "interrupted": "error",
    }.get(row["status"], "unknown")
    if status in {"idle", "error"}:
        return status
    started, ended = _timestamp(row["startedAt"]), _timestamp(row["endedAt"])
    if started and ended and ended >= started:
        return "error" if row["abortedLastRun"] else "idle"
    # Current OpenClaw omits status while a writer owns the current lifecycle.
    # A recent update or a retained writer ID alone does NOT prove active work.
    run = row["lifecycleRunId"]
    if (
        row["status"] is None
        and isinstance(run, str)
        and 0 < len(run) <= 128
        and run == row["activeWriterRunId"]
        and started
        and owner_started is not None
        and started >= owner_started - 1000
        and not row["abortedLastRun"]
    ):
        status = "running"
    if status in {"running", "waiting"} and owner_started is None:
        return "unknown"
    return status


def _activity(db: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
    run = row["lifecycleRunId"]
    if not isinstance(run, str) or run != row["activeWriterRunId"]:
        return None
    # Never load arguments, results or messages, only structured current-run metadata.
    return db.execute(
        "SELECT json_extract(event_json,'$.type') AS type, "
        "json_extract(event_json,'$.data.name') AS tool, "
        "json_extract(event_json,'$.data.status') AS status, "
        "CASE WHEN json_type(event_json,'$.modelId')='text' "
        "THEN json_extract(event_json,'$.modelId') END AS model, "
        "CASE WHEN json_type(event_json,'$.provider')='text' "
        "THEN json_extract(event_json,'$.provider') END AS provider, created_at "
        "FROM trajectory_runtime_events WHERE session_id=? AND run_id=? "
        # Terminal evidence remains authoritative even if late events are appended.
        "ORDER BY (json_extract(event_json,'$.type')='session.ended') DESC, seq DESC LIMIT 1",
        (row["current_session_id"], run),
    ).fetchone()


class ReadOnlyOpenClawCollector:
    def __init__(self, home: Path, resolver: Any) -> None:
        self.home = home.resolve()
        self.resolver = resolver
        self.last_good: list[Any] = []

    def _targets(self) -> list[tuple[str, Path, bool]]:
        cfg = _read_json(self.home / "openclaw.json")
        # Do not silently implement only part of a custom/include-based config.
        if (
            not isinstance(cfg, dict)
            or "$include" in str(cfg)
            or cfg.get("session", {}).get("store")
        ):
            raise ValueError("Unsupported OpenClaw session-store configuration")
        agents = cfg.get("agents", {})
        entries = agents.get("entries")
        if entries is None:
            entries = {row["id"]: row for row in agents.get("list", [])}
        if not isinstance(entries, dict) or not entries:
            raise ValueError("Explicit OpenClaw agent roster required")
        ids = set(entries)
        acp = cfg.get("acp", {})
        ids.update(acp.get("allowedAgents", []))
        if acp.get("defaultAgent"):
            ids.add(acp["defaultAgent"])
        for entry in entries.values():
            runtime = entry.get("runtime", {})
            if runtime.get("type") == "acp" and runtime.get("acp", {}).get("agent"):
                ids.add(runtime["acp"]["agent"])
        ids.discard("*")
        if len(ids) > MAX_AGENTS or any(
            not isinstance(a, str) or not _ID.fullmatch(a) for a in ids
        ):
            raise ValueError("Unsupported OpenClaw agent roster")
        targets = []
        for agent in sorted(ids):
            # Canonical migrated store, NOT legacy agentDir or sessions.json.
            path = self.home / "agents" / agent / "agent" / "openclaw-agent.sqlite"
            if not path.resolve().is_relative_to(self.home):
                raise ValueError("OpenClaw store escapes state directory")
            targets.append((agent, path, agent in entries))
        return targets

    def _live_owners(self) -> dict[tuple[str, Path], float]:
        path = self.home / "state/openclaw.sqlite"
        if not path.resolve().is_relative_to(self.home):
            raise ValueError("OpenClaw lease store escapes state directory")
        with _connect(path) as db:
            rows = db.execute(
                "SELECT agent_id,path,owner_pid,owner_start_time "
                "FROM agent_database_leases LIMIT ?",
                (MAX_AGENTS * 8 + 1,),
            ).fetchall()
        if len(rows) > MAX_AGENTS * 8:
            raise ValueError("OpenClaw lease inventory limit exceeded")
        live = {}
        for row in rows:
            expected = row["owner_start_time"]
            if expected is not None and expected == process_fingerprint(row["owner_pid"]):
                path = Path(row["path"])
                if not path.is_absolute():
                    path = self.home / path
                live[(row["agent_id"], path.resolve())] = _owner_started_ms(expected)
        return live

    def collect(self) -> list[Any]:
        try:
            rows = self._collect()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as error:
            # Do not surface SQL/path/config content through MonitorService's error field.
            raise RuntimeError(
                f"Read-only OpenClaw session source is unavailable ({type(error).__name__})"
            ) from error
        self.last_good = rows
        return rows

    def _collect(self) -> list[Any]:
        from .models import Session as AgentSession

        now = time.time() * 1000
        targets = self._targets()
        live = self._live_owners()
        sessions = []
        for agent, path, required in targets:
            # An ACP backend need not have created a store yet; configured agents must.
            if not required and not path.exists():
                continue
            with _connect(path) as db:
                # Invalid current rows must cause a visible source error, not disappear.
                rows = db.execute(
                    # The CTE contains only the whitelist, never raw entry_json.
                    "WITH current AS (SELECT session_key,current_session_id,updated_at,"
                    f"entry_valid,{PROJECTION} FROM session_nodes WHERE archived_at IS NULL) "
                    "SELECT * FROM current WHERE updated_at>=? OR (("
                    "status IN ('running','queued') OR (status IS NULL "
                    "AND typeof(lifecycleRunId)='text' "
                    "AND length(lifecycleRunId) BETWEEN 1 AND 128 "
                    "AND lifecycleRunId=activeWriterRunId AND startedAt>0 "
                    "AND typeof(startedAt) IN ('integer','real') "
                    "AND COALESCE(abortedLastRun,0)=0)) "
                    "AND NOT COALESCE(typeof(endedAt) IN ('integer','real') "
                    "AND typeof(startedAt) IN ('integer','real') AND endedAt>=startedAt,0)) "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (now - 86_400_000, MAX_SESSIONS + 1),
                ).fetchall()
                if len(sessions) + len(rows) > MAX_SESSIONS:
                    raise ValueError("OpenClaw session inventory limit exceeded")
                has_activity = db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='trajectory_runtime_events'"
                ).fetchone()
                activities = {
                    row["current_session_id"]: _activity(db, row)
                    for row in rows
                    if has_activity and _status(row, live.get((agent, path.resolve()))) == "running"
                }
            for row in rows:
                if row["entry_valid"] != 1 or not row["current_session_id"]:
                    raise ValueError("Invalid OpenClaw session metadata")
                key = row["session_key"]
                if not key.startswith(f"agent:{agent}:"):
                    raise ValueError("Unsupported OpenClaw session owner")
                status = _status(row, live.get((agent, path.resolve())))
                activity = activities.get(row["current_session_id"])
                tool = ""
                if activity:
                    if activity["type"] == "session.ended":
                        status = "idle" if activity["status"] == "success" else "error"
                    elif (
                        activity["type"] == "tool.call"
                        and isinstance(activity["tool"], str)
                        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}", activity["tool"])
                    ):
                        status, tool = "tool", activity["tool"]
                location = row["groupChannel"]
                if not location:
                    display = row["displayName"] or ""
                    location = "#" + display.split("#", 1)[1] if "#" in display else ""
                title = redact_message(row["label"] or "", 120)
                sessions.append(
                    AgentSession(
                        uid=f"openclaw:{agent}:{row['current_session_id']}",
                        runtime="openclaw",
                        agent_id=agent,
                        display_name=agent.title(),
                        session_id=row["current_session_id"],
                        session_key=key,
                        status=status,
                        model=str((activity["model"] if activity else None) or row["model"] or "—"),
                        provider=str(
                            (activity["provider"] if activity else None)
                            or row["modelProvider"]
                            or "—"
                        ),
                        title=title,
                        task_preview=title,
                        source=str(row["chatType"] or ""),
                        discord_location=safe_location(location),
                        started_at=_iso(row["sessionStartedAt"]),
                        task_started_at=_iso(row["startedAt"]) or _iso(row["sessionStartedAt"]),
                        updated_at=_iso(row["updated_at"]),
                        age_seconds=max(0, now - row["updated_at"]) / 1000,
                        current_tool=tool,
                        reasoning_effort=str(row["thinkingLevel"] or row["reasoningLevel"] or ""),
                        input_tokens=_number(row["inputTokens"]),
                        output_tokens=_number(row["outputTokens"]),
                        total_tokens=_number(row["totalTokens"]),
                        context_tokens=_number(row["contextTokens"]),
                    )
                )
        return sessions
