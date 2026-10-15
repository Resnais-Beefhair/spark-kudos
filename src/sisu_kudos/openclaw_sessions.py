"""OpenClaw session metadata without CLI config/ownership side effects.

The CLI's writable state bootstrap fails inside ProtectSystem/ProtectHome on
OpenClaw 2026.10.2. KUDOS only needs committed metadata, not a Gateway lease.
No platform code is imported, and neither ownership checks nor sandbox policy
are changed. Unknown schemas/locators fail visibly instead of claiming no work.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
PROJECTION = ", ".join(f"json_extract(entry_json, '$.{field}') AS {field}" for field in FIELDS)


def _iso(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat(timespec="seconds")
    return ""


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


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

    def _live_owners(self) -> set[tuple[str, Path]]:
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
        live = set()
        for row in rows:
            expected = row["owner_start_time"]
            if expected is not None and expected == process_fingerprint(row["owner_pid"]):
                path = Path(row["path"])
                if not path.is_absolute():
                    path = self.home / path
                live.add((row["agent_id"], path.resolve()))
        return live

    def collect(self) -> list[Any]:
        try:
            rows = self._collect()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as error:
            # Do not surface SQL/path/config content through MonitorService's error field.
            raise RuntimeError(
                "Read-only OpenClaw session source is unavailable "
                f"({type(error).__name__})"
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
                    f"SELECT session_key,current_session_id,updated_at,entry_valid,{PROJECTION} "
                    "FROM session_nodes WHERE archived_at IS NULL AND "
                    "(updated_at>=? OR json_extract(entry_json,'$.status')='running') "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (now - 86_400_000, MAX_SESSIONS + 1),
                ).fetchall()
            if len(sessions) + len(rows) > MAX_SESSIONS:
                raise ValueError("OpenClaw session inventory limit exceeded")
            for row in rows:
                if row["entry_valid"] != 1 or not row["current_session_id"]:
                    raise ValueError("Invalid OpenClaw session metadata")
                key = row["session_key"]
                if not key.startswith(f"agent:{agent}:"):
                    raise ValueError("Unsupported OpenClaw session owner")
                state = row["status"]
                status = {
                    "running": "running",
                    "done": "idle",
                    "failed": "error",
                    "killed": "error",
                    "timeout": "error",
                    "interrupted": "error",
                }.get(state, "unknown")
                if status == "running" and (agent, path.resolve()) not in live:
                    status = "unknown"  # Dead/reused owner must not animate as live work.
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
                        model=str(row["model"] or "—"),
                        provider=str(row["modelProvider"] or "—"),
                        title=title,
                        task_preview=title,
                        source=str(row["chatType"] or ""),
                        discord_location=safe_location(location),
                        started_at=_iso(row["sessionStartedAt"]),
                        task_started_at=_iso(row["startedAt"]) or _iso(row["sessionStartedAt"]),
                        updated_at=_iso(row["updated_at"]),
                        age_seconds=max(0, now - row["updated_at"]) / 1000,
                        reasoning_effort=str(row["thinkingLevel"] or row["reasoningLevel"] or ""),
                        input_tokens=_number(row["inputTokens"]),
                        output_tokens=_number(row["outputTokens"]),
                        total_tokens=_number(row["totalTokens"]),
                        context_tokens=_number(row["contextTokens"]),
                    )
                )
        return sessions
