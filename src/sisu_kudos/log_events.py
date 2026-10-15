"""Safe log projections and conservative Discord incident correlation.

Private prose/tracebacks stay local. Recovery is evidence, never inferred merely
from absence of errors. This module never initializes or writes upstream state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

_DISCORD_LOGGER = re.compile(
    r"(?:plugins\.platforms\.discord|hermes_plugins\.platforms__discord(?:__home_[a-f0-9]+)?)\.adapter"
)
_DISCONNECT = re.compile(
    r"\[Discord\] Discord Gateway WebSocket (?:transport closed|remained unhealthy) "
    r"\((socket_closed|client_closed|ack_stale|ack_unavailable|not_ready|latency_non_finite|"
    r"latency_exceeded|dispatch_stale|socket_state_unavailable|health_check_error), "
    r"\d+/\d+\); forcing reconnect"
)
_FATAL = re.compile(
    r"Fatal discord adapter error(?: for multiplexed profile (?P<profile>[\w-]+))? "
    r"\(discord_websocket_health_stale\)"
    r"(?:: Discord Gateway WebSocket health check failed: [a-z_]+)?"
)
_RECOVERED = re.compile(r"✓ discord reconnected(?: successfully| \(profile: (?P<profile>[\w-]+)\))")


def safe_component(component: str) -> str:
    if _DISCORD_LOGGER.fullmatch(component):
        return "discord.adapter"
    if component in {
        "gateway.run",
        "aiohttp.server",
        "agent.tool_executor",
        "agent",
        "agent.codex_runtime",
        "run_agent",
        "cron.scheduler",
        "gateway.session",
        "tools",
        "hermes_cli.proxy.server",
        "asyncio",
    }:
        return component
    return "hermes"


@lru_cache(maxsize=1)
def _identity_key() -> bytes:
    """Load an operator-provisioned private key without writing from the service.

    An absent/invalid key safely falls back to a process-random key: privacy is
    retained, but acknowledgements may reappear after restart. Normal deployment
    provisions the key once, outside ProtectHome's read-only runtime.
    """
    path = Path(
        os.getenv(
            "SISU_KUDOS_LOG_ID_KEY", str(Path.home() / ".local/state/sisu-kudos/log-identity.key")
        )
    )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and not info.st_mode & 0o077
                and info.st_nlink == 1
                and info.st_size == 32
            ):
                key = stream.read(33)
                if len(key) == 32:
                    return key
    except OSError:
        pass
    return secrets.token_bytes(32)


def identity(*parts: object) -> str:
    return hmac.new(
        _identity_key(), json.dumps(parts, ensure_ascii=False).encode(), hashlib.sha256
    ).hexdigest()[:24]


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def classify(component: str, message: str, level: str, profile: str | None) -> tuple[str, str, str]:
    """Return enums and fixed summaries, not redacted arbitrary prose."""
    match = _DISCONNECT.fullmatch(message) if _DISCORD_LOGGER.fullmatch(component) else None
    fatal = component == "gateway.run" and _FATAL.fullmatch(message)
    if fatal and (fatal["profile"] or "default") != profile:
        fatal = None
    if level == "ERROR" and (match or fatal):
        reason = f" ({match[1]})" if match else ""
        return (
            "discord_connection",
            "unresolved",
            f"Discord connection failure{reason}; reconnect started",
        )
    recovery = _RECOVERED.fullmatch(message)
    if (
        level == "INFO"
        and component == "gateway.run"
        and recovery
        and (recovery["profile"] or "default") == profile
    ):
        return "discord_recovered", "recorded", "Discord connection recovered"
    if component == "gateway.run" and message.startswith("inbound message:"):
        return "operational", "recorded", "Message received; content hidden"
    if component == "agent.tool_executor":
        text = (
            "Tool call reported an error"
            if level in {"WARNING", "ERROR", "CRITICAL"}
            else "Tool call log event"
        )
        return "operational", "recorded", text + "; content hidden"
    labels = {
        "ERROR": "Error",
        "CRITICAL": "Critical error",
        "WARNING": "Warning",
        "INFO": "Event",
        "DEBUG": "Diagnostic event",
    }
    return (
        "operational",
        "recorded",
        f"{labels[level]} recorded; details in local log [REDACTED]",
    )


def classify_proxy_eof(row: dict[str, Any], continuation: list[str]) -> None:
    """Only the verified proxy EOF reset is downgraded. Unknown trace stays ERROR."""
    if row["component"] != "aiohttp.server" or row["level"] != "ERROR":
        return
    lines = [line.strip() for line in continuation if line.strip()]
    if (
        not lines
        or lines[-1]
        != "aiohttp.client_exceptions.ClientConnectionResetError: Cannot write to closing transport"
    ):
        return
    frame = any(
        re.fullmatch(r'File "[^"\n]*/hermes_cli/proxy/server\.py", line \d+, in _stream_back', line)
        and i + 1 < len(lines)
        and lines[i + 1] == "await resp.write_eof()"
        for i, line in enumerate(lines)
    )
    if frame:
        row.update(
            level="WARNING",
            original_level="ERROR",
            category="proxy_client_closed",
            message=(
                "Model proxy client disconnected before response completion; "
                "task outcome unknown"
            ),
        )


def merge_copies(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in records:
        key = row.get("id") or identity(
            *(
                row.get(k)
                for k in ("timestamp", "runtime", "agent", "level", "component", "message")
            )
        )
        if key not in grouped:
            grouped[key] = dict(row, id=key, sources=list(row.get("sources", [row["source"]])))
        else:
            target = grouped[key]
            target["sources"] = sorted(
                set(target["sources"]) | set(row.get("sources", [row["source"]]))
            )
            # A full traceback in one copy is sufficient, without suppressing any
            # different event or carrying the raw traceback beyond the parser.
            if row.get("category") == "proxy_client_closed":
                for field in ("category", "message", "level", "original_level"):
                    target[field] = row[field]
        grouped[key]["source"] = primary_source(grouped[key]["sources"])
    return list(grouped.values())


def primary_source(sources: list[str]) -> str:
    return min(
        sources, key=lambda source: ({"errors": 0, "gateway": 1, "agent": 2}.get(source, 3), source)
    )


def live_recoveries(profiles: dict[str, Path]) -> dict[str, list[str]]:
    """Use live writer identity and fresh shared state, not stale profile mirrors."""
    output: dict[str, list[str]] = {}
    now = datetime.now().astimezone().timestamp()
    for owner, log_dir in profiles.items():
        home = log_dir.parent
        path = home / "gateway_state.json"
        try:
            if path.stat().st_size > 1_000_000:
                continue
            state = json.loads(path.read_text())
            pid, start = state["pid"], state["start_time"]
            if type(pid) is not int or type(start) is not int or pid <= 0:
                continue
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if int(stat[19]) != start or stat[0] == "Z":
                continue
            if (
                state.get("gateway_state") != "running"
                or not -5 <= now - epoch(state["updated_at"]) <= 60
            ):
                continue
            for agent, target in profiles.items():
                if agent == owner:
                    key = "discord"
                elif target.parent.parent == home / "profiles":
                    key = target.parent.name + ":discord"
                else:
                    continue
                platform = state.get("platforms", {}).get(key, {})
                if (
                    platform.get("state") != "connected"
                    or platform.get("needs_attention")
                    or platform.get("error_code")
                    or platform.get("error_message")
                    or platform.get("writer_pid") != pid
                    or platform.get("writer_start_time") != start
                ):
                    continue
                at = platform["updated_at"]
                if epoch(at) <= now + 5:
                    output.setdefault(agent, []).append(at)
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue  # Missing evidence cannot resolve an incident.
    return output


def correlate(
    records: list[dict[str, Any]], recoveries: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """Merge only complementary adapter/runner events within two seconds.

    Each later recurrence remains distinct, even if earlier recovery evidence has
    left the bounded logs. Other log errors are never marked active or recovered.
    """
    rows = sorted(merge_copies(records), key=lambda row: epoch(row["timestamp"]))
    output: list[dict[str, Any]] = []
    last: dict[str, dict[str, Any]] = {}
    recovered = {agent: list(times) for agent, times in recoveries.items()}
    for row in rows:
        agent, category = row["agent"], row.get("category")
        if category == "discord_recovered":
            recovered.setdefault(agent, []).append(row["timestamp"])
            last.pop(agent, None)
        if category != "discord_connection":
            output.append(row)
            continue
        prior = last.get(agent)
        role = "runner" if row["component"] == "gateway.run" else "adapter"
        if (
            prior
            and epoch(row["timestamp"]) - epoch(prior["timestamp"]) <= 2
            and role not in prior["_roles"]
        ):
            prior["_roles"].add(role)
            prior["_last_at"] = row["timestamp"]
            prior["event_count"] += 1
            prior["sources"] = sorted(set(prior["sources"]) | set(row["sources"]))
            prior["source"] = primary_source(prior["sources"])
            if role == "adapter":
                prior["message"] = row["message"]
        else:
            row.update(
                id=identity(
                    "discord_connection", row["runtime"], agent, row["timestamp"], row["id"]
                ),
                event_count=1,
                _roles={role},
                _last_at=row["timestamp"],
            )
            output.append(row)
            last[agent] = row
    for row in output:
        if row.get("category") != "discord_connection":
            continue
        after = epoch(row.pop("_last_at"))
        row.pop("_roles")
        candidates = [time for time in recovered.get(row["agent"], []) if epoch(time) > after]
        if candidates:
            row.update(status="recovered", resolved_at=min(candidates, key=epoch))
    return output
