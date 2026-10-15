import json
import sqlite3
from types import SimpleNamespace

from sisu_kudos.adapters import safe_session
from sisu_kudos.origins import openclaw_location


def test_openclaw_metadata_only_origin(tmp_path):
    db = tmp_path / "agents/primary/agent/openclaw-agent.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE session_nodes (current_session_id TEXT, entry_json TEXT)")
        conn.execute(
            "INSERT INTO session_nodes VALUES (?,?)",
            (
                "match",
                json.dumps(
                    {"groupChannel": "#Mallipalvelimen seuranta", "prompt": "PRIVATE SENTINEL"}
                ),
            ),
        )
        conn.execute("INSERT INTO session_nodes VALUES (?,?)", ("other", "{invalid"))
    assert openclaw_location(tmp_path, "primary", "match") == "#Mallipalvelimen seuranta"
    assert openclaw_location(tmp_path, "primary", "other") == ""
    assert openclaw_location(tmp_path, "../../escape", "match") == ""
    assert openclaw_location(tmp_path, "missing", "match") == ""


def test_routing_ids_never_leave_origin(tmp_path):
    db = tmp_path / "agents/primary/agent/openclaw-agent.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE session_nodes (current_session_id TEXT, entry_json TEXT)")
        conn.execute(
            "INSERT INTO session_nodes VALUES (?,?)",
            ("id", json.dumps({"displayName": "discord:123456789012345678#seuranta"})),
        )
    assert openclaw_location(tmp_path, "primary", "id") == "#seuranta"
    session = SimpleNamespace(
        discord_location="Server / #main / thread", task_started_at="task", started_at="session"
    )
    safe = safe_session(session)
    assert safe["location"] == "Server / #main / thread"
    assert safe["started_at"] == "task"
