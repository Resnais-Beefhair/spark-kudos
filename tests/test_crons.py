"""Cron fixtures are local metadata only; never execute a user's scheduled job."""

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sisu_kudos.crons import CronCollector, collect_hermes, process_fingerprint

NOW = datetime.now(UTC)
START = (NOW - timedelta(minutes=40)).isoformat()


def hermes_store(tmp_path, *, status="running", pid=None, fingerprint=None, no_agent=False):
    home = tmp_path / "hermes"
    (home / "cron").mkdir(parents=True, exist_ok=True)
    jobs = {
        "jobs": [
            {
                "id": "job_1",
                "name": "Test maintenance",
                "no_agent": no_agent,
                "prompt": "PRIVATE PROMPT",
                "script": "PRIVATE COMMAND",
                "model": "configured-not-observed",
                "enabled": True,
            }
        ]
    }
    (home / "cron/jobs.json").write_text(json.dumps(jobs))
    with sqlite3.connect(home / "cron/executions.db") as db:
        db.execute(
            "CREATE TABLE executions (id TEXT, job_id TEXT, pid INTEGER, "
            "process_started_at INTEGER, status TEXT, claimed_at TEXT, "
            "started_at TEXT, finished_at TEXT)"
        )
        db.execute(
            "INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)",
            (
                "run-1",
                "job_1",
                os.getpid() if pid is None else pid,
                process_fingerprint(os.getpid()) if fingerprint is None else fingerprint,
                status,
                START,
                START if status != "claimed" else None,
                None,
            ),
        )
    return home


def test_hermes_running_script_has_real_owner_and_no_fictional_model(tmp_path):
    home = hermes_store(tmp_path, no_agent=True)
    rows = collect_hermes("Assistant", home, NOW.timestamp())
    assert len(rows) == 1
    row = rows[0].card
    assert row["work_kind"] == "cron"
    assert row["execution_kind"] == "script"
    assert row["model"] == "No language model"
    assert row["task"] == "Test maintenance"
    assert row["started_at"] == START
    assert "PRIVATE" not in json.dumps(row)
    assert "run-1" not in row["uid"]
    # A long quiet operation must not vanish merely because its start is old.
    assert row["status"] == "running"


@pytest.mark.parametrize("status", ["claimed", "completed", "failed", "unknown"])
def test_hermes_non_running_attempts_are_not_current_work(tmp_path, status):
    home = hermes_store(tmp_path, status=status)
    assert collect_hermes("Assistant", home, NOW.timestamp()) == []


@pytest.mark.parametrize("overrides", [{"pid": 999999999}, {"fingerprint": 1}])
def test_hermes_dead_or_reused_owner_not_displayed(tmp_path, overrides):
    home = hermes_store(tmp_path, **overrides)
    assert collect_hermes("Assistant", home, NOW.timestamp()) == []


def test_hermes_model_is_observed_not_configured(tmp_path):
    home = hermes_store(tmp_path)
    assert collect_hermes("Assistant", home, NOW.timestamp())[0].card["model"] == "—"
    with sqlite3.connect(home / "state.db") as db:
        db.execute(
            "CREATE TABLE sessions (id TEXT, parent_session_id TEXT, source TEXT, "
            "started_at REAL, ended_at REAL, model TEXT, billing_provider TEXT)"
        )
        db.execute(
            "INSERT INTO sessions VALUES (?,NULL,'cron',?,NULL,?,?)",
            (
                "cron_job_1_20261011_170000",
                datetime.fromisoformat(START).timestamp() + 2,
                "actual-model",
                "actual-provider",
            ),
        )
    run = collect_hermes("Assistant", home, NOW.timestamp())[0]
    assert run.card["model"] == "actual-model"
    assert run.card["provider"] == "actual-provider"
    assert run.session_ids == {"cron_job_1_20261011_170000"}


def test_hermes_completion_removes_row_next_read(tmp_path):
    home = hermes_store(tmp_path)
    assert collect_hermes("Assistant", home, NOW.timestamp())
    with sqlite3.connect(home / "cron/executions.db") as db:
        db.execute("UPDATE executions SET status='completed',finished_at=?", (NOW.isoformat(),))
    assert collect_hermes("Assistant", home, NOW.timestamp()) == []


def test_collector_retains_last_good_but_marks_failed_source(tmp_path):
    home = hermes_store(tmp_path)
    collector = CronCollector((("Assistant", home),), tmp_path / "openclaw")
    first = collector()
    assert len(first.runs) == 1
    (home / "cron/jobs.json").write_text("not json")
    failed = collector()
    assert len(failed.runs) == 1
    assert not next(h for h in failed.health if h["source"] == "hermes-cron")["ok"]
    assert failed.runs[0].card["stale"] is True


def test_process_fingerprint_uses_kernel_identity():
    fields = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
    assert process_fingerprint(os.getpid()) == int(fields[19])
