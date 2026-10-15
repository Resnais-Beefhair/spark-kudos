"""Refresh human Discord labels from local metadata, never from chat messages."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .adapters import safe_location


def openclaw_location(home: Path, agent: str, session_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent) or not session_id:
        return ""
    db = home / "agents" / agent / "agent" / "openclaw-agent.sqlite"
    if not db.is_file() or not db.resolve().is_relative_to(home.resolve()):
        return ""
    try:
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=0.2) as connection:
            # Read only a saved human channel label, not transcript/message content.
            row = connection.execute(
                "SELECT json_extract(entry_json, '$.groupChannel'), "
                "json_extract(entry_json, '$.displayName') FROM session_nodes "
                "WHERE current_session_id=? AND json_valid(entry_json) LIMIT 1",
                (session_id,),
            ).fetchone()
        if row:
            channel, display = row
            if isinstance(channel, str) and channel.strip():
                return safe_location(channel)
            if isinstance(display, str) and display.startswith("discord:") and "#" in display:
                return safe_location("#" + display.split("#", 1)[1])
    except (OSError, sqlite3.Error, ValueError):
        pass
    return ""


def collect_with_origins(service: Any, *, resolver_factory: Any = None) -> Any:
    if resolver_factory is None:
        from agentmon.collectors.channels import ChannelResolver

        resolver_factory = ChannelResolver.discover
    resolver = resolver_factory(service.hermes_home)
    service.resolver = resolver
    service.hermes_collector.resolver = resolver
    service.openclaw_collector.resolver = resolver
    snapshot = service.collect()
    if any(row.runtime == "openclaw" and not row.ok for row in snapshot.health):
        # Source failure remains visible and freezes motion, but never erases last-good work.
        snapshot.sessions.extend(getattr(service.openclaw_collector, "last_good", []))
        snapshot.sessions.sort(
            key=lambda row: (
                row.status not in {"running", "tool", "error"},
                row.age_seconds or 10**12,
            )
        )
    for session in snapshot.sessions:
        if session.runtime != "openclaw":
            continue
        current = safe_location(session.discord_location)
        if current in {"—", "Discord", ""} or "[ID]" in current:
            location = openclaw_location(
                getattr(service.openclaw_collector, "home", Path.home() / ".openclaw"),
                session.agent_id,
                session.session_id,
            )
            if location:
                session.discord_location = location
    return snapshot
