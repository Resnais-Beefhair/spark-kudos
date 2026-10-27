"""Owner details must also reach consumers of the standard message field."""

import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from sisu_kudos.app import create_app
from sisu_kudos.config import Settings
from sisu_kudos.log_content import operator_message
from sisu_kudos.state import LiveState


@pytest.mark.parametrize("details", (False, True))
def test_log_display_contract_matches_rest_snapshot_and_compact_revision(details):
    settings = Settings(logs_enabled=True, log_details_enabled=details)
    state = LiveState(settings)
    original = operator_message(
        "PreviouslyUnknownError: upstream did not respond password=EXAMPLE_CREDENTIAL"
    )
    record = {
        "id": "stable-event",
        "timestamp": "2026-08-19T12:30:00+00:00",
        "agent": "Example worker",
        "runtime": "hermes",
        "component": "future.logger",
        "level": "ERROR",
        "category": "operational",
        "status": "recorded",
        "message": "Error recorded; details in local log [REDACTED]",
        "diagnostic": None,
    }
    if details:
        record["original_message"] = original
    state.data["logs"] = {
        "records": [record],
        "revision": "source-revision",
        "redacted": True,
        "counts": {"warning": 0, "error": 1},
        "sources": [],
    }
    baseline = copy.deepcopy(state.data["logs"])
    app = create_app(settings)
    app.state.kudos_state = state
    client = TestClient(app)  # No lifespan/collectors or real host probes.
    direct = client.get("/api/logs").json()
    snapshot = client.get("/api/snapshot").json()["logs"]
    compact = asyncio.run(state.stream_snapshot())["logs"]
    expected = original if details else record["message"]
    for payload in (direct, snapshot):
        row = payload["records"][0]
        assert row["message"] == expected
        assert "EXAMPLE_CREDENTIAL" not in json.dumps(row)
        assert {k: row[k] for k in record if k != "message"} == {
            k: record[k] for k in record if k != "message"
        }
        if details:
            assert row["message"] == row["original_message"]
            assert row["message"].startswith("PreviouslyUnknownError: upstream did not respond")
    assert direct["revision"] == snapshot["revision"] == compact["revision"]
    assert (direct["revision"] != baseline["revision"]) is details
    assert "records" not in compact
    assert "original_message" not in json.dumps(compact)
    assert "PreviouslyUnknownError" not in json.dumps(compact)
    assert state.data["logs"] == baseline, (
        "display projection must not rewrite collector/correlation state"
    )


def test_owner_projection_does_not_invent_content_for_records_without_original():
    state = LiveState(Settings(log_details_enabled=True))
    record = {"id": "collector-status", "message": "Known source unavailable", "diagnostic": None}
    state.data["logs"] = {"records": [record], "revision": "source-revision"}
    one = asyncio.run(state.snapshot())["logs"]
    two = asyncio.run(state.snapshot())["logs"]
    assert one["records"] == [record]
    assert one["revision"] == two["revision"]
