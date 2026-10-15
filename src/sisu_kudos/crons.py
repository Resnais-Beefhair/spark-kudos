"""Read-only scheduler evidence. A recent schedule/session is NOT a running cron.

Never import platform scheduler APIs: their nominal reads can recover/write stores.
Only bounded metadata queries are used; prompts, command bodies and results are ignored.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters import opaque_id, safe_session
from .logs import redact_message

MAX_RUNS = 64
MAX_JOBS_BYTES = 4_194_304


@dataclass
class CronRun:
    runtime: str
    owner: str
    job_id: str
    started: float
    card: dict[str, Any]
    session_ids: set[str] = field(default_factory=set)


@dataclass
class CronSnapshot:
    generated_at: str
    runs: list[CronRun]
    health: list[dict[str, Any]]


def process_fingerprint(pid: int) -> int | None:
    """Linux start ticks, without reading argv or process environments."""
    try:
        if isinstance(pid, bool) or int(pid) <= 0:
            return None
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] in {"Z", "X"} else int(fields[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _epoch(value: Any) -> float | None:
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            value = parsed.timestamp()
        if isinstance(value, bool):
            return None
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _read_json(path: Path, limit: int = MAX_JOBS_BYTES) -> Any:
    with path.open("rb") as handle:
        blob = handle.read(limit + 1)
    if len(blob) > limit:
        raise ValueError("Metadata size limit exceeded")
    return json.loads(blob)


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + 0.4
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        yield db
    finally:
        db.close()


def _card(
    runtime: str, owner: str, identity: str, title: Any, started: str, now: float, kind: str
) -> dict[str, Any]:
    return {
        "uid": opaque_id(identity),
        "runtime": runtime,
        "agent": redact_message(owner, 64),
        "work_kind": "cron",
        "execution_kind": kind,
        "status": "running",
        "stale": False,
        "model": "No language model" if kind == "script" else "—",
        "provider": "—",
        "title": redact_message(title or "Unnamed scheduled task", 120),
        "task": redact_message(title or "Unnamed scheduled task", 120),
        "location": "Cron · script run" if kind == "script" else "Cron · scheduled task",
        "started_at": started,
        "updated_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "age_seconds": max(0, now - (_epoch(started) or now)),
        "reasoning": "",
        "service_tier": "",
        "current_tool": "",
        "task_total_tokens": None,
        "task_api_calls": None,
        "events": [],
    }


def _hermes_sessions(home: Path, job_id: str, started: float, now: float) -> list[sqlite3.Row]:
    path = home / "state.db"
    if not path.is_file():
        return []
    # Exact prefix comparison (LIKE would treat job IDs containing '_' as wildcards).
    prefix = f"cron_{job_id}_"
    with _connect(path) as db:
        return db.execute(
            "WITH RECURSIVE lineage(id, parent_session_id, model, billing_provider, "
            "started_at, ended_at, depth) AS ("
            "SELECT id,parent_session_id,model,billing_provider,started_at,ended_at,0 "
            "FROM sessions WHERE source='cron' AND substr(id,1,?)=? "
            "AND started_at>=? AND started_at<=? UNION ALL "
            "SELECT s.id,s.parent_session_id,s.model,s.billing_provider,s.started_at,s.ended_at,"
            "l.depth+1 FROM sessions s JOIN lineage l ON s.parent_session_id=l.id "
            "WHERE l.depth<32 AND s.started_at<=?) "
            "SELECT * FROM lineage ORDER BY started_at DESC LIMIT 64",
            (len(prefix), prefix, started - 1, now + 5, now + 5),
        ).fetchall()


def collect_hermes(owner: str, home: Path, now: float) -> list[CronRun]:
    ledger, store = home / "cron/executions.db", home / "cron/jobs.json"
    if not ledger.exists():
        # No store is a legitimately empty profile. An existing job store without
        # execution telemetry is unavailable, not evidence that all jobs are idle.
        if store.exists():
            raise ValueError("Execution ledger unavailable")
        return []
    payload = _read_json(store)
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs):
        raise ValueError("Unsupported job metadata")
    by_id = {str(j.get("id")): j for j in jobs}
    with _connect(ledger) as db:
        rows = db.execute(
            "SELECT id,job_id,pid,process_started_at,started_at,claimed_at "
            "FROM executions WHERE status='running' AND finished_at IS NULL "
            "ORDER BY started_at DESC LIMIT ?",
            (MAX_RUNS + 1,),
        ).fetchall()
    if len(rows) > MAX_RUNS:
        raise ValueError("Execution limit exceeded")
    runs = []
    for row in rows:
        started = _epoch(row["started_at"])
        current = process_fingerprint(row["pid"])
        recorded = row["process_started_at"]
        # Strict on this Linux host; absence or a reused PID is not live evidence.
        if current is None or recorded != current or started is None or started > now + 5:
            continue
        job_id = str(row["job_id"])
        job = by_id.get(job_id, {})
        kind = "script" if job.get("no_agent") is True else "agent" if job else "unknown"
        card = _card(
            "hermes",
            owner,
            f"cron:hermes:{home.name}:{row['id']}",
            job.get("name"),
            row["started_at"],
            now,
            kind,
        )
        try:
            sessions = [] if kind == "script" else _hermes_sessions(home, job_id, started, now)
        except (OSError, sqlite3.Error):
            sessions = []  # Optional enrichment must never hide a proven live run.
        if sessions:
            latest = sessions[0]
            card["model"] = redact_message(latest["model"] or "—", 80)
            card["provider"] = redact_message(latest["billing_provider"] or "—", 40)
        profile = "default" if home.name == ".hermes" else home.name
        runs.append(CronRun("hermes", profile, job_id, started, card, {s["id"] for s in sessions}))
    return runs


def _openclaw_sessions(
    home: Path, owner: str, job_id: str, started: float, now: float
) -> list[sqlite3.Row]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", owner):
        raise ValueError("Invalid execution agent")
    path = home / "agents" / owner / "agent/openclaw-agent.sqlite"
    if not path.is_file():
        return []
    root = f"agent:{owner}:cron:{job_id}"
    prefix = root + ":run:"
    with _connect(path) as db:
        return db.execute(
            "SELECT current_session_id, json_extract(entry_json,'$.model') AS model, "
            "json_extract(entry_json,'$.modelProvider') AS provider "
            "FROM session_nodes WHERE json_valid(entry_json) AND "
            "(session_key=? OR substr(session_key,1,?)=?) AND "
            "json_extract(entry_json,'$.sessionStartedAt') BETWEEN ? AND ? "
            "ORDER BY json_extract(entry_json,'$.sessionStartedAt') DESC LIMIT 64",
            (root, len(prefix), prefix, (started - 1) * 1000, (now + 5) * 1000),
        ).fetchall()


def collect_openclaw(home: Path, now: float) -> list[CronRun]:
    path = home / "state/openclaw.sqlite"
    if not path.exists() and not home.exists():
        return []
    with _connect(path) as db:
        rows = db.execute(
            "SELECT r.store_key,r.job_id,r.agent_id,r.receipt_id,r.started_at_ms,"
            "r.owner_pid,r.owner_start_time,j.name,j.payload_kind,"
            "json_extract(j.state_json,'$.runningAtMs') AS running_at,"
            "json_extract(j.state_json,'$.runningReceiptId') AS running_receipt "
            "FROM cron_run_receipts r LEFT JOIN cron_jobs j USING(store_key,job_id) "
            "WHERE r.status='running' AND r.finished_at_ms IS NULL "
            "ORDER BY r.started_at_ms DESC LIMIT ?",
            (MAX_RUNS + 1,),
        ).fetchall()
    if len(rows) > MAX_RUNS:
        raise ValueError("Execution limit exceeded")
    runs = []
    for row in rows:
        # A receipt exists already while queued. Activation matches BOTH the
        # receipt identity and timestamp in the same durable transaction.
        if row["running_receipt"] != row["receipt_id"] or row["running_at"] != row["started_at_ms"]:
            continue
        current = process_fingerprint(row["owner_pid"])
        started = _epoch(row["started_at_ms"] / 1000)
        if (
            current is None
            or current != row["owner_start_time"]
            or started is None
            or started > now + 5
        ):
            continue
        owner = str(row["agent_id"] or "")
        kind = (
            "script"
            if row["payload_kind"] in {"command", "script"}
            else ("agent" if row["payload_kind"] == "agentTurn" else "unknown")
        )
        name = owner
        card = _card(
            "openclaw",
            name,
            f"cron:openclaw:{row['store_key']}:{row['receipt_id']}",
            row["name"],
            datetime.fromtimestamp(started, UTC).isoformat(),
            now,
            kind,
        )
        try:
            sessions = (
                []
                if kind != "agent"
                else _openclaw_sessions(home, owner, row["job_id"], started, now)
            )
        except (OSError, sqlite3.Error):
            sessions = []
        if sessions:
            card["model"] = redact_message(sessions[0]["model"] or "—", 80)
            card["provider"] = redact_message(sessions[0]["provider"] or "—", 40)
        runs.append(
            CronRun(
                "openclaw",
                owner,
                row["job_id"],
                started,
                card,
                {s["current_session_id"] for s in sessions},
            )
        )
    return runs


class CronCollector:
    """Independent sources retain last-good evidence, with explicit stale flags."""

    def __init__(self, hermes_homes: tuple[tuple[str, Path], ...], openclaw_home: Path):
        self.hermes_homes = hermes_homes
        self.openclaw_home = openclaw_home
        self._last_good: dict[str, list[CronRun]] = {}

    def __call__(self) -> CronSnapshot:
        now = time.time()
        runs, health = [], []
        sources: list[tuple[str, str, Callable[[], list[CronRun]]]] = [
            ("hermes", owner, lambda o=owner, h=home: collect_hermes(o, h, now))
            for owner, home in self.hermes_homes
        ]
        sources.append(("openclaw", "OpenClaw", lambda: collect_openclaw(self.openclaw_home, now)))
        for runtime in ("hermes", "openclaw"):
            errors, count = [], 0
            for rt, owner, collect in sources:
                if rt != runtime:
                    continue
                key = f"{rt}:{owner}"
                try:
                    items = collect()
                    self._last_good[key] = items
                except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                    # Never send exception text: it may contain a database/home path.
                    errors.append(f"{owner}: {type(exc).__name__}")
                    items = [
                        CronRun(
                            r.runtime,
                            r.owner,
                            r.job_id,
                            r.started,
                            r.card | {"stale": True},
                            r.session_ids,
                        )
                        for r in self._last_good.get(key, [])
                    ]
                runs.extend(items)
                count += len(items)
            health.append(
                {
                    "source": f"{runtime}-cron",
                    "ok": not errors,
                    "message": "; ".join(errors) if errors else "Cron-ajorekisteri luettu",
                    "count": count,
                }
            )
        return CronSnapshot(datetime.fromtimestamp(now, UTC).isoformat(), runs, health)


def is_cron_session(session: Any) -> bool:
    return (
        getattr(session, "source", "") == "cron"
        or str(getattr(session, "session_id", "")).startswith("cron_")
        or ":cron:" in str(getattr(session, "session_key", ""))
    )


def compose_crons(
    safe: dict[str, Any],
    agent_snapshot: Any,
    cron_snapshot: CronSnapshot | None,
    *,
    stale: bool = False,
) -> dict[str, Any]:
    """The ledger owns lifecycle. Session metadata only enriches an exact run match."""
    raw_sessions = getattr(agent_snapshot, "sessions", [])
    # Suppress cron session history even when its upstream age heuristic says running.
    cron_uids = {opaque_id(s.uid) for s in raw_sessions if is_cron_session(s)}
    result = safe | {"sessions": [s for s in safe.get("sessions", []) if s["uid"] not in cron_uids]}
    if cron_snapshot is None:
        return result
    cards = []
    observed = _epoch(safe.get("generated_at"))
    fresh = observed is not None and -5 <= time.time() - observed <= 45
    for run in cron_snapshot.runs:
        card = run.card.copy()
        card["stale"] = bool(card["stale"] or stale)
        native = next(
            (
                s
                for s in raw_sessions
                if s.runtime == run.runtime
                and s.agent_id == run.owner
                and s.session_id in run.session_ids
            ),
            None,
        )
        source_ok = any(
            h.get("source") == run.runtime and h.get("ok") for h in safe.get("health", [])
        )
        if (
            native is not None
            and fresh
            and source_ok
            and not card["stale"]
            and run.card["execution_kind"] == "agent"
        ):
            details = safe_session(native)
            # Title, lifecycle, start time and identity always come from the receipt.
            for key in (
                "model",
                "provider",
                "reasoning",
                "service_tier",
                "task_total_tokens",
                "task_api_calls",
            ):
                if details[key] not in (None, "", "—"):
                    card[key] = details[key]
            if native.status in {"running", "tool", "waiting"}:
                card["status"] = native.status
                card["current_tool"] = details["current_tool"] if native.status == "tool" else ""
        cards.append(card)
    result["sessions"] += cards
    result["health"] = safe.get("health", []) + cron_snapshot.health
    result["cron_generated_at"] = cron_snapshot.generated_at
    return result
