"""Real SQLite fixtures; no OpenClaw CLI, gateway or user sessions are modified."""

import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from sisu_kudos.adapters import safe_session
from sisu_kudos.crons import process_fingerprint
from sisu_kudos.openclaw_sessions import ReadOnlyOpenClawCollector

NOW = time.time() * 1000


@pytest.fixture
def store(tmp_path):
    home = tmp_path / "openclaw"
    home.mkdir()
    (home / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"entries": {"primary": {"model": "not-observed"}}},
                "acp": {"allowedAgents": ["codex"]},
            }
        )
    )
    path = home / "agents/primary/agent/openclaw-agent.sqlite"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE session_nodes (session_key TEXT PRIMARY KEY, "
        "current_session_id TEXT, entry_json TEXT, entry_valid INTEGER, "
        "updated_at INTEGER, archived_at INTEGER)"
    )
    entry = {
        "status": "running",
        "sessionStartedAt": NOW - 10000,
        "startedAt": NOW - 5000,
        "updatedAt": NOW,
        "model": "observed-model",
        "modelProvider": "observed-provider",
        "thinkingLevel": "max",
        "groupChannel": "#fixture",
        "inputTokens": 42,
        "outputTokens": 3,
        "totalTokens": 45,
        "contextTokens": 1000,
        "label": "Fixture task",
        "prompt": "PRIVATE PROMPT",
        "sessionFile": "/secret/old.jsonl",
        "systemPromptReport": {"text": "PRIVATE PROMPT"},
    }
    db.execute(
        "INSERT INTO session_nodes VALUES (?,?,?,1,?,NULL)",
        ("agent:primary:discord:channel:123456789012345678", "current", json.dumps(entry), NOW),
    )
    db.commit()
    db.close()
    state = home / "state/openclaw.sqlite"
    state.parent.mkdir()
    with sqlite3.connect(state) as db:
        db.execute(
            "CREATE TABLE agent_database_leases (agent_id TEXT, path TEXT, "
            "owner_pid INTEGER, owner_start_time INTEGER)"
        )
        db.execute(
            "INSERT INTO agent_database_leases VALUES (?,?,?,?)",
            ("primary", str(path), os.getpid(), process_fingerprint(os.getpid())),
        )
    return home, path


def reader(home):
    # SQLite parser and its view model are standalone; no private checkout is used.
    return ReadOnlyOpenClawCollector(home, None)


def update(path, **values):
    with sqlite3.connect(path) as db:
        entry = json.loads(db.execute("SELECT entry_json FROM session_nodes").fetchone()[0])
        entry.update(values)
        db.execute("UPDATE session_nodes SET entry_json=?", (json.dumps(entry),))


def test_readonly_collector_works_without_cli(store, monkeypatch):
    home, path = store
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("CLI must not run"))
    collector = reader(home)
    assert isinstance(collector, ReadOnlyOpenClawCollector)
    rows = collector.collect()
    assert len(rows) == 1
    row = rows[0]
    assert (row.agent_id, row.status, row.model) == ("primary", "running", "observed-model")
    assert row.discord_location == "#fixture"
    assert row.reasoning_effort == "max"
    assert row.total_tokens == 45
    assert row.task_started_at != row.started_at
    public = json.dumps(safe_session(row))
    for forbidden in ("PRIVATE", "old.jsonl", "123456789012345678", "session_key", "not-observed"):
        assert forbidden not in public
    assert collector.last_good == rows


@pytest.mark.parametrize(
    "state,expected",
    [
        ("done", "idle"),
        ("failed", "error"),
        ("killed", "error"),
        ("timeout", "error"),
        (None, "unknown"),
    ],
)
def test_terminal_and_unknown_states(store, state, expected):
    home, path = store
    update(path, status=state)
    assert reader(home).collect()[0].status == expected


@pytest.mark.parametrize("pid,start", [(999999999, 1), (os.getpid(), 1)])
def test_stale_or_reused_lease_cannot_make_running_card(store, pid, start):
    home, _ = store
    with sqlite3.connect(home / "state/openclaw.sqlite") as db:
        db.execute("UPDATE agent_database_leases SET owner_pid=?,owner_start_time=?", (pid, start))
    assert reader(home).collect()[0].status == "unknown"


def test_no_model_fallback_or_old_transcript(store):
    home, path = store
    update(path, model=None, modelProvider=None, modelOverride="not-observed")
    row = reader(home).collect()[0]
    assert row.model == "—"
    assert row.events == [] and row.current_tool == ""


def test_failure_retains_last_good_and_does_not_create_db(store):
    home, path = store
    collector = reader(home)
    initial = collector.collect()
    path.unlink()
    with pytest.raises(RuntimeError):
        collector.collect()
    assert collector.last_good == initial
    assert not path.exists()


def test_invalid_schema_is_visible_failure(store):
    home, path = store
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE session_nodes")
    with pytest.raises(RuntimeError):
        reader(home).collect()


def test_invalid_current_entry_is_not_silently_dropped(store):
    home, path = store
    with sqlite3.connect(path) as db:
        db.execute("UPDATE session_nodes SET entry_valid=-1")
    with pytest.raises(RuntimeError):
        reader(home).collect()


def test_retired_directory_and_archived_or_old_entries_are_excluded(store):
    home, path = store
    retired = home / "agents/retired/agent/openclaw-agent.sqlite"
    retired.parent.mkdir(parents=True)
    retired.write_bytes(path.read_bytes())
    with sqlite3.connect(path) as db:
        db.execute("UPDATE session_nodes SET archived_at=?", (NOW,))
    assert reader(home).collect() == []
    with sqlite3.connect(path) as db:
        db.execute("UPDATE session_nodes SET archived_at=NULL, updated_at=?", (NOW - 90000000,))
    update(path, status="done")
    assert reader(home).collect() == []


def test_long_running_work_not_hidden_by_age_filter(store):
    home, path = store
    with sqlite3.connect(path) as db:
        db.execute("UPDATE session_nodes SET updated_at=?", (NOW - 90000000,))
    assert reader(home).collect()[0].status == "running"


def test_symlink_escape_is_refused(store, tmp_path):
    home, path = store
    other = tmp_path / "outside.sqlite"
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(RuntimeError):
        reader(home).collect()


def test_collector_uses_readonly_uri_and_closes_connections(store, monkeypatch):
    home, _ = store
    original = sqlite3.connect
    opened = []

    def connect(database, **kwargs):
        assert "mode=ro" in database and kwargs["uri"] is True
        db = original(database, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert reader(home).collect()
    assert opened
    for db in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")


def test_origin_composition_retains_cards_but_not_healthy_status(store, monkeypatch):
    from sisu_kudos.origins import collect_with_origins

    home, path = store
    service = SimpleNamespace(
        hermes_home=home, hermes_collector=SimpleNamespace(), openclaw_collector=reader(home)
    )

    prior = service.openclaw_collector.collect()
    path.unlink()
    service.collect = lambda: SimpleNamespace(
        generated_at="",
        sessions=[],
        health=[SimpleNamespace(runtime="openclaw", ok=False, message="fixture error")],
    )
    snapshot = collect_with_origins(service, resolver_factory=lambda _: None)
    assert snapshot.sessions == prior
    assert snapshot.health[0].ok is False
    from sisu_kudos.adapters import safe_agent_snapshot

    assert safe_agent_snapshot(snapshot)["sessions"][0]["stale"] is True


def test_candidate_limit_is_failure_not_silent_truncation(store):
    home, path = store
    with sqlite3.connect(path) as db:
        entry = db.execute("SELECT entry_json FROM session_nodes").fetchone()[0]
        db.executemany(
            "INSERT INTO session_nodes VALUES (?,?,?,1,?,NULL)",
            [(f"agent:primary:fixture:{n}", f"session-{n}", entry, NOW) for n in range(200)],
        )
    with pytest.raises(RuntimeError):
        reader(home).collect()


def test_missing_lease_database_fails_without_creating_it(store):
    home, _ = store
    state = home / "state/openclaw.sqlite"
    state.unlink()
    with pytest.raises(RuntimeError):
        reader(home).collect()
    assert not state.exists()


def test_malformed_json_fails_visibly(store):
    home, path = store
    with sqlite3.connect(path) as db:
        db.execute("UPDATE session_nodes SET entry_json='broken'")
    with pytest.raises(RuntimeError):
        reader(home).collect()


@pytest.fixture
def lifecycle(store, monkeypatch):
    home, path = store
    monkeypatch.setattr(
        "sisu_kudos.openclaw_sessions._owner_started_ms", lambda _: NOW - 60000, raising=False
    )
    update(
        path,
        status=None,
        lifecycleRunId="current-run",
        activeWriterRunId="current-run",
        startedAt=NOW - 10000,
        endedAt=None,
        abortedLastRun=False,
    )
    return home, path


def trace(path, kind, *, run="current-run", session="current", **data):
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS trajectory_runtime_events "
            "(session_id TEXT,seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,"
            "event_json TEXT,created_at INTEGER)"
        )
        db.execute(
            "INSERT INTO trajectory_runtime_events(session_id,run_id,event_json,created_at) "
            "VALUES (?,?,?,?)",
            (
                session,
                run,
                json.dumps(
                    {
                        "type": kind,
                        "modelId": "current-observed-model",
                        "provider": "observed",
                        "data": data,
                        "prompt": "PRIVATE TRAJECTORY PROMPT",
                    }
                ),
                NOW,
            ),
        )


def test_active_lifecycle_without_legacy_status_is_running(lifecycle):
    home, _ = lifecycle
    assert reader(home).collect()[0].status == "running"


def test_current_tool_metadata_without_content_or_config_model(lifecycle):
    home, path = lifecycle
    trace(path, "tool.call", name="bash", arguments="PRIVATE COMMAND", output="PRIVATE RESULT")
    row = reader(home).collect()[0]
    assert (row.status, row.current_tool, row.model) == ("tool", "bash", "current-observed-model")
    public = json.dumps(safe_session(row))
    assert "PRIVATE" not in public and "not-observed" not in public


@pytest.mark.parametrize(
    "kind,data,expected",
    [
        ("tool.result", {"name": "bash"}, "running"),
        ("model.completed", {}, "running"),
        ("session.ended", {"status": "success"}, "idle"),
        ("session.ended", {"status": "error"}, "error"),
    ],
)
def test_current_lifecycle_events_clear_tool_or_finish(lifecycle, kind, data, expected):
    home, path = lifecycle
    trace(path, "tool.call", name="bash")
    trace(path, kind, **data)
    row = reader(home).collect()[0]
    assert row.status == expected and row.current_tool == ""


@pytest.mark.parametrize(
    "values,expected",
    [
        ({"activeWriterRunId": "old-run"}, "unknown"),
        ({"activeWriterRunId": None}, "unknown"),
        ({"lifecycleRunId": None}, "unknown"),
        ({"abortedLastRun": True}, "unknown"),
        ({"startedAt": None}, "unknown"),
        ({"endedAt": NOW}, "idle"),
        ({"endedAt": NOW, "abortedLastRun": True}, "error"),
        ({"status": "failed"}, "error"),
        ({"status": "done"}, "idle"),
    ],
)
def test_lifecycle_cannot_revive_terminal_or_unowned_run(lifecycle, values, expected):
    home, path = lifecycle
    update(path, **values)
    trace(path, "tool.call", name="bash")
    row = reader(home).collect()[0]
    assert row.status == expected and row.current_tool == ""


def test_lifecycle_from_before_current_gateway_is_not_live(lifecycle, monkeypatch):
    home, _ = lifecycle
    monkeypatch.setattr("sisu_kudos.openclaw_sessions._owner_started_ms", lambda _: NOW)
    assert reader(home).collect()[0].status == "unknown"


def test_dead_lease_does_not_animate_current_tool(lifecycle):
    home, path = lifecycle
    with sqlite3.connect(home / "state/openclaw.sqlite") as db:
        db.execute("UPDATE agent_database_leases SET owner_start_time=1")
    trace(path, "tool.call", name="bash")
    assert reader(home).collect()[0].status == "unknown"


def test_old_run_and_old_session_activity_are_not_current(lifecycle):
    home, path = lifecycle
    trace(path, "tool.call", run="old-run", name="bash")
    trace(path, "tool.call", session="old-session", name="bash")
    row = reader(home).collect()[0]
    assert row.status == "running" and row.current_tool == ""


def test_retained_writer_after_completion_does_not_animate(lifecycle):
    home, path = lifecycle
    update(path, lifecycleRunId=None, endedAt=NOW)
    assert reader(home).collect()[0].status == "idle"


def test_recent_update_alone_is_not_activity(store):
    home, path = store
    update(path, status=None)
    assert reader(home).collect()[0].status == "unknown"


def test_long_lifecycle_not_filtered_by_age(lifecycle, monkeypatch):
    home, path = lifecycle
    monkeypatch.setattr("sisu_kudos.openclaw_sessions._owner_started_ms", lambda _: NOW - 100000000)
    update(path, startedAt=NOW - 90000000)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE session_nodes SET updated_at=?", (NOW - 90000000,))
    assert reader(home).collect()[0].status == "running"


@pytest.mark.parametrize("late", ["model.completed", "tool.call", "tool.result"])
def test_terminal_event_dominates_late_events(lifecycle, late):
    home, path = lifecycle
    trace(path, "session.ended", status="success")
    trace(path, late, name="bash")
    row = reader(home).collect()[0]
    assert row.status == "idle" and row.current_tool == ""


@pytest.mark.parametrize("terminal", ["done", "failed", "interrupted", None])
def test_old_terminal_lifecycles_do_not_exhaust_inventory(lifecycle, terminal):
    home, path = lifecycle
    old = NOW - 90000000
    entry = json.dumps(
        {
            "status": terminal,
            "startedAt": old - 10000,
            "endedAt": old,
            "activeWriterRunId": "retained-run",
            "lifecycleRunId": "retained-run",
        }
    )
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO session_nodes "
            "(session_key,current_session_id,updated_at,archived_at,entry_valid,entry_json) "
            "VALUES (?,?,?,?,?,?)",
            [
                (f"agent:primary:old:{i}", f"old-{i}", old, None, 1, entry)
                for i in range(200)
            ],
        )
    rows = reader(home).collect()
    assert len(rows) == 1 and rows[0].status == "running"


@pytest.mark.parametrize("field", ["modelId", "provider"])
@pytest.mark.parametrize(
    "value", [{"prompt": "PRIVATE_MODEL_PAYLOAD"}, ["PRIVATE_LIST"], True, 123]
)
def test_non_string_trajectory_identifiers_do_not_reach_public_fields(lifecycle, field, value):
    home, path = lifecycle
    trace(path, "tool.call", name="bash")
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE trajectory_runtime_events SET event_json=json_set(event_json,?,json(?))",
            (f"$.{field}", json.dumps(value)),
        )
    row = reader(home).collect()[0]
    assert row.status == "tool"
    assert "PRIVATE" not in json.dumps(safe_session(row))
    assert (row.model if field == "modelId" else row.provider) not in {"1", "123"}
