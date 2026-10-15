"""Both scheduler lifecycle schemas, safety, and state integration."""

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sisu_kudos.adapters import safe_agent_snapshot
from sisu_kudos.config import Settings
from sisu_kudos.crons import (
    CronCollector,
    CronSnapshot,
    _connect,
    collect_openclaw,
    compose_crons,
    process_fingerprint,
)
from sisu_kudos.state import LiveState


def openclaw_store(tmp_path, *, payload="agentTurn", state="active", status="running"):
    home = tmp_path / "openclaw"
    path = home / "state/openclaw.sqlite"
    path.parent.mkdir(parents=True)
    started = int((time.time() - 120) * 1000)
    markers = {"runningAtMs": started, "runningReceiptId": "receipt-1"}
    if state == "queued":
        markers = {"queuedAtMs": started}
    if state == "wrong-receipt":
        markers["runningReceiptId"] = "old-receipt"
    if state == "wrong-time":
        markers["runningAtMs"] -= 1
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE cron_run_receipts(receipt_id TEXT,store_key TEXT,job_id TEXT,"
            "agent_id TEXT,status TEXT,owner_pid INTEGER,owner_start_time INTEGER,"
            "started_at_ms INTEGER,finished_at_ms INTEGER)"
        )
        db.execute(
            "CREATE TABLE cron_jobs(store_key TEXT,job_id TEXT,name TEXT,payload_kind TEXT,"
            "state_json TEXT,job_json TEXT,enabled INTEGER)"
        )
        db.execute(
            "INSERT INTO cron_run_receipts VALUES (?,?,?,?,?,?,?,?,NULL)",
            (
                "receipt-1",
                "/home/demo/cron/jobs.json",
                "job-1",
                "primary",
                status,
                os.getpid(),
                process_fingerprint(os.getpid()),
                started,
            ),
        )
        db.execute(
            "INSERT INTO cron_jobs VALUES (?,?,?,?,?,?,?)",
            (
                "/home/demo/cron/jobs.json",
                "job-1",
                "Scheduled test",
                payload,
                json.dumps(markers),
                json.dumps({"prompt": "PRIVATE PROMPT", "command": "SECRET"}),
                0,
            ),
        )
    return home, started


@pytest.mark.parametrize(
    "payload,kind",
    [("agentTurn", "agent"), ("command", "script"), ("script", "script"), ("heartbeat", "unknown")],
)
def test_openclaw_activation_and_disabled_job_still_running(tmp_path, payload, kind):
    home, started = openclaw_store(tmp_path, payload=payload)
    run = collect_openclaw(home, time.time())[0]
    assert run.card["agent"] == "primary"
    assert run.card["execution_kind"] == kind
    assert datetime.fromisoformat(run.card["started_at"]).timestamp() == started / 1000
    serialized = json.dumps(run.card)
    for forbidden in ("PRIVATE", "SECRET", "receipt-1", "job-1", "/home/"):
        assert forbidden not in serialized


@pytest.mark.parametrize("state", ["queued", "wrong-receipt", "wrong-time"])
def test_openclaw_reservation_not_running(tmp_path, state):
    home, _ = openclaw_store(tmp_path, state=state)
    assert collect_openclaw(home, time.time()) == []


@pytest.mark.parametrize("status", ["ok", "error", "skipped", "interrupted", "superseded"])
def test_openclaw_finished_not_running(tmp_path, status):
    home, _ = openclaw_store(tmp_path, status=status)
    assert collect_openclaw(home, time.time()) == []


@pytest.mark.parametrize("column,value", [("owner_pid", 999999999), ("owner_start_time", 1)])
def test_openclaw_dead_or_reused_owner(tmp_path, column, value):
    home, _ = openclaw_store(tmp_path)
    with sqlite3.connect(home / "state/openclaw.sqlite") as db:
        db.execute(f"UPDATE cron_run_receipts SET {column}=?", (value,))
    assert collect_openclaw(home, time.time()) == []


def test_openclaw_exact_run_enrichment_not_previous_or_other_job(tmp_path):
    home, started = openclaw_store(tmp_path)
    path = home / "agents/primary/agent/openclaw-agent.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE session_nodes(session_key TEXT,current_session_id TEXT,entry_json TEXT)"
        )
        for key, sid, when, model in [
            ("agent:primary:cron:job-1", "old", started - 2000, "old-model"),
            ("agent:primary:cron:job-11", "other", started + 1000, "other-model"),
            ("agent:primary:cron:job-1:run:exact", "exact", started + 1000, "real-model"),
        ]:
            db.execute(
                "INSERT INTO session_nodes VALUES (?,?,?)",
                (
                    key,
                    sid,
                    json.dumps(
                        {
                            "sessionStartedAt": when,
                            "model": model,
                            "modelProvider": "real-provider",
                            "prompt": "PRIVATE",
                        }
                    ),
                ),
            )
    run = collect_openclaw(home, time.time())[0]
    assert run.session_ids == {"exact"}
    assert run.card["model"] == "real-model"


def raw_session(sid="exact", *, cron=True):
    return SimpleNamespace(
        uid=f"openclaw:primary:{sid}",
        runtime="openclaw",
        agent_id="primary",
        display_name="Primary",
        session_id=sid,
        source="",
        status="tool",
        model="measured",
        session_key=f"agent:primary:cron:job-1:run:{sid}" if cron else "agent:primary:discord:abc",
        current_tool="web_search",
        events=[],
        age_seconds=0,
    )


def raw_snapshot(*sessions):
    return SimpleNamespace(
        generated_at=datetime.now(UTC).isoformat(),
        sessions=list(sessions),
        health=[SimpleNamespace(runtime="openclaw", ok=True, message="ok", source_count=1)],
        chatgpt_quota=None,
    )


def test_compose_dedup_and_completed_suppression(tmp_path):
    home, _ = openclaw_store(tmp_path)
    run = collect_openclaw(home, time.time())[0]
    run.session_ids = {"exact"}
    raw = raw_snapshot(raw_session(), raw_session("chat", cron=False))
    base = safe_agent_snapshot(raw)
    snapshot = CronSnapshot(raw.generated_at, [run], [{"source": "openclaw-cron", "ok": True}])
    output = compose_crons(base, raw, snapshot)
    assert len(output["sessions"]) == 2
    cron = next(s for s in output["sessions"] if s.get("work_kind") == "cron")
    assert cron["current_tool"] == "web_search" and cron["model"] == "measured"
    assert cron["task"] == "Scheduled test"
    assert cron["uid"] == run.card["uid"]
    done = compose_crons(base, raw, CronSnapshot(raw.generated_at, [], snapshot.health))
    assert len(done["sessions"]) == 1
    assert "cron" not in json.dumps(done["sessions"])
    assert len(base["sessions"]) == 2  # cached normal snapshot is never mutated


def test_missing_optional_session_db_does_not_hide_proven_run(tmp_path):
    home, _ = openclaw_store(tmp_path)
    path = home / "agents/primary/agent/openclaw-agent.sqlite"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrupt")
    assert len(collect_openclaw(home, time.time())) == 1


def test_sources_isolated_and_recover_from_failure(tmp_path):
    home, _ = openclaw_store(tmp_path)
    collector = CronCollector((), home)
    assert len(collector().runs) == 1
    with sqlite3.connect(home / "state/openclaw.sqlite") as db:
        db.execute("ALTER TABLE cron_jobs RENAME TO broken_jobs")
    failed = collector()
    assert failed.runs[0].card["stale"]
    assert not next(h for h in failed.health if h["source"] == "openclaw-cron")["ok"]
    assert next(h for h in failed.health if h["source"] == "hermes-cron")["ok"]
    with sqlite3.connect(home / "state/openclaw.sqlite") as db:
        db.execute("ALTER TABLE broken_jobs RENAME TO cron_jobs")
        db.execute("UPDATE cron_run_receipts SET status='ok',finished_at_ms=1")
    recovered = collector()
    assert not recovered.runs and all(h["ok"] for h in recovered.health)


def test_read_only_connection_is_closed(tmp_path):
    path = tmp_path / "sample.db"
    sqlite3.connect(path).close()
    with _connect(path) as db, pytest.raises(sqlite3.OperationalError):
        db.execute("CREATE TABLE should_fail(x)")
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


async def test_cron_refresh_is_independent_of_slow_agent_collection(tmp_path):
    home, _ = openclaw_store(tmp_path)
    collector = CronCollector((), home)
    raw = raw_snapshot(
        *[raw_session(f"old-{i}") for i in range(20)], raw_session("chat", cron=False)
    )
    values = {"agents": raw, "crons": collector()}
    state = LiveState(Settings(agents_enabled=True, crons_enabled=True))
    state.collectors = SimpleNamespace(SERVICE_ORDER=[])
    state.scheduler = SimpleNamespace(
        names=lambda: ["agents", "crons"], value=lambda n: values.get(n)
    )
    state._health = lambda name: {"ok": True, "stale": False}
    await state._publish({"agents", "crons"})
    assert len(state.data["agents"]["sessions"]) == 2
    with sqlite3.connect(home / "state/openclaw.sqlite") as db:
        db.execute("UPDATE cron_run_receipts SET status='ok',finished_at_ms=1")
    values["crons"] = collector()
    await state._publish({"crons"})
    assert len(state.data["agents"]["sessions"]) == 1
    assert "cron_generated_at" in state.data["agents"]


async def test_crons_work_when_agent_service_failed_to_start(tmp_path):
    home, _ = openclaw_store(tmp_path)
    values = {"crons": CronCollector((), home)()}
    state = LiveState(Settings(agents_enabled=True, crons_enabled=True))
    state.collectors = SimpleNamespace(SERVICE_ORDER=[])
    state.scheduler = SimpleNamespace(
        names=lambda: ["crons"],
        value=lambda n: values[n] if n in {"agents", "crons"} else None,
    )
    state._health = lambda name: {"ok": True, "stale": False}
    await state._publish({"crons"})
    assert len(state.data["agents"]["sessions"]) == 1
