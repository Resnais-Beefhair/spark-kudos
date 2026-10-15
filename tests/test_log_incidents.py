"""Regression fixtures: no private prose, no duplicate/outlived error alarms."""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sisu_kudos.logs import collect_hermes_logs, parse_log_line

FAIL = (
    "2026-10-13 07:19:05,267 ERROR plugins.platforms.discord.adapter: "
    "[Discord] Discord Gateway WebSocket transport closed (socket_closed, 1/2); forcing reconnect\n"
)
RUNNER = (
    "2026-10-13 07:19:05,269 ERROR gateway.run: "
    "Fatal discord adapter error (discord_websocket_health_stale)\n"
)
RECOVER = "2026-10-13 07:19:08,512 INFO gateway.run: ✓ discord reconnected successfully\n"
SCHEDULER_RUNNER = RUNNER.replace(
    "adapter error (", "adapter error for multiplexed profile scheduler ("
)


def fixture_logs(root, text):
    root.mkdir(parents=True, exist_ok=True)
    for file in ("agent.log", "errors.log", "gateway.log"):
        (root / file).write_text(text)
    return root


@pytest.mark.parametrize(
    "component,message",
    [
        (
            "gateway.run",
            "inbound message: platform=discord msg='PRIVATE_SENTINEL' "
            "reply_to_text='REPLY_SENTINEL'",
        ),
        ("agent.tool_executor", 'Tool terminal returned error: {"output":"PRIVATE_SENTINEL"}'),
        (
            "unknown.logger",
            "This arbitrary private prose PRIVATE_SENTINEL has no sensitive field key",
        ),
        ("agent.codex_runtime", 'API failed: {"messages":[{"content":"PRIVATE_SENTINEL"}]}'),
    ],
)
def test_arbitrary_log_prose_is_never_serialized(component, message):
    row = parse_log_line(f"2026-10-13 12:00:00,000 ERROR {component}: {message}", "errors")
    assert row is not None
    assert row["level"] == "ERROR"
    assert "PRIVATE_SENTINEL" not in json.dumps(row)
    assert "REPLY_SENTINEL" not in json.dumps(row)
    assert row["id"]


def test_six_rows_form_one_recovered_incident(tmp_path):
    root = fixture_logs(tmp_path / "logs", FAIL + RUNNER + RECOVER)
    result = collect_hermes_logs({"Scheduler": root})
    errors = [r for r in result["records"] if r["level"] == "ERROR"]
    assert len(errors) == 1
    event = errors[0]
    assert event["status"] == "recovered"
    assert event["resolved_at"].startswith("2026-10-13T07:19:08.512")
    assert set(event["sources"]) == {"agent", "errors", "gateway"}
    assert event["event_count"] == 2
    assert result["counts"]["error"] == 0
    assert result["counts"]["recovered"] == 1


def test_recurrence_is_new_and_previous_recovery_cannot_clear_it(tmp_path):
    root = fixture_logs(tmp_path / "logs", FAIL + RUNNER + RECOVER)
    first = collect_hermes_logs({"Scheduler": root})["records"]
    original = next(r for r in first if r["level"] == "ERROR")["id"]
    later = FAIL.replace("07:19:05", "08:19:05") + RUNNER.replace("07:19:05", "08:19:05")
    fixture_logs(root, FAIL + RUNNER + RECOVER + later)
    result = collect_hermes_logs({"Scheduler": root})
    errors = [r for r in result["records"] if r["level"] == "ERROR"]
    assert len(errors) == 2
    assert errors[0]["status"] == "unresolved"
    assert errors[0]["id"] != original
    assert errors[1]["id"] == original
    assert errors[1]["status"] == "recovered"
    assert result["counts"]["error"] == 1


def test_different_profiles_and_different_messages_never_merge(tmp_path):
    profiles = {
        name: fixture_logs(tmp_path / name, FAIL + RUNNER) for name in ("Scheduler", "Worker")
    }
    result = collect_hermes_logs(profiles)
    assert len(result["records"]) == 2
    assert result["counts"]["error"] == 2
    assert len({r["id"] for r in result["records"]}) == 2
    # Two private errors with identical public summaries remain two distinct events.
    root = fixture_logs(
        tmp_path / "other",
        "2026-10-13 07:19:05,000 ERROR x: private A\n2026-10-13 07:19:05,000 ERROR x: private B\n",
    )
    assert len(collect_hermes_logs(root)["records"]) == 2


def test_recovery_only_clears_discord_and_same_profile(tmp_path):
    profiles = {
        "Scheduler": fixture_logs(tmp_path / "scheduler", FAIL + RUNNER),
        "Worker": fixture_logs(tmp_path / "worker", RECOVER),
    }
    assert collect_hermes_logs(profiles)["counts"]["error"] == 1
    root = fixture_logs(
        tmp_path / "generic", "2026-10-13 07:19:05,000 ERROR gateway.run: real failure\n" + RECOVER
    )
    error = next(r for r in collect_hermes_logs(root)["records"] if r["level"] == "ERROR")
    assert error["status"] == "recorded"


def test_group_id_survives_added_provenance_and_recovery(tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    (root / "errors.log").write_text(FAIL + RUNNER)
    before = collect_hermes_logs(root)["records"][0]["id"]
    fixture_logs(root, FAIL + RUNNER + RECOVER)
    after = next(r for r in collect_hermes_logs(root)["records"] if r["level"] == "ERROR")
    assert after["id"] == before


def test_recovery_is_correlated_before_bounded_display_sample(tmp_path):
    busy = "".join(f"2026-10-13 08:00:{i % 60:02d},000 INFO x: event {i}\n" for i in range(400))
    root = fixture_logs(tmp_path / "logs", FAIL + RUNNER + RECOVER + busy)
    # A quiet errors file retains the failure after the gateway sample rolled on.
    (root / "errors.log").write_text(FAIL + RUNNER)
    result = collect_hermes_logs(root, limit=10)
    assert result["counts"]["error"] == 0


TRACE = """Traceback (most recent call last):
  File "/private/hermes_cli/proxy/server.py", line 190, in _stream_back
    await resp.write_eof()
  File "/private/aiohttp/http_writer.py", line 102, in _write
    raise ClientConnectionResetError("Cannot write to closing transport")
aiohttp.client_exceptions.ClientConnectionResetError: Cannot write to closing transport
"""
PROXY = "2026-10-13 12:17:44,056 ERROR aiohttp.server: Error handling request from 127.0.0.1\n"


@pytest.mark.parametrize(
    "trace,is_warning",
    [
        (TRACE, True),
        ("", False),
        (TRACE.replace("write_eof()", "write(chunk)"), False),
        (TRACE.replace("hermes_cli/proxy/server.py", "other/server.py"), False),
        (
            TRACE.replace(
                "ClientConnectionResetError: Cannot write to closing transport",
                "RuntimeError: real failure",
            ),
            False,
        ),
    ],
)
def test_proxy_disconnect_requires_exact_local_traceback(tmp_path, trace, is_warning):
    root = fixture_logs(tmp_path / "logs", PROXY + trace)
    result = collect_hermes_logs(root)
    row = result["records"][0]
    assert row["level"] == ("WARNING" if is_warning else "ERROR")
    assert (row["category"] == "proxy_client_closed") == is_warning
    assert "/private" not in repr(result)
    assert "Traceback" not in repr(result)
    if is_warning:
        assert row["original_level"] == "ERROR"


def test_client_disconnect_does_not_swallow_next_actual_error(tmp_path):
    root = fixture_logs(
        tmp_path / "logs", PROXY + TRACE + "2026-10-13 12:17:45,000 ERROR x: PRIVATE_SENTINEL\n"
    )
    data = collect_hermes_logs(root)
    assert data["counts"]["error"] == 1
    assert data["counts"]["warning"] == 1


@pytest.fixture(params=("UTC", "Europe/Helsinki", "America/New_York"))
def host_timezone(request, monkeypatch):
    """Naive log times and fixture state must share the host time zone."""
    try:
        with monkeypatch.context() as change:
            change.setenv("TZ", request.param)
            time.tzset()
            yield request.param
    finally:
        time.tzset()


def _state(home, recovery, *, wrong_pid=False, old=False):
    pid = os.getpid()
    ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    now = datetime.now().astimezone() - (timedelta(days=1) if old else timedelta())
    data = {
        "pid": pid,
        "start_time": ticks + int(wrong_pid),
        "gateway_state": "running",
        "updated_at": now.isoformat(),
        "platforms": {
            "scheduler:discord": {
                "state": "connected",
                "error_code": None,
                "error_message": None,
                "needs_attention": False,
                "writer_pid": pid,
                "writer_start_time": ticks,
                "updated_at": datetime.fromisoformat(recovery).astimezone().isoformat(),
            }
        },
    }
    (home / "gateway_state.json").write_text(json.dumps(data))


@pytest.mark.parametrize(
    "wrong_pid,old,recovered", [(False, False, True), (True, False, False), (False, True, False)]
)
def test_live_shared_gateway_recovery_is_process_fenced(
    tmp_path, wrong_pid, old, recovered, host_timezone
):
    root = fixture_logs(tmp_path / "default" / "logs", "")
    scheduler = fixture_logs(
        tmp_path / "default" / "profiles" / "scheduler" / "logs", FAIL + SCHEDULER_RUNNER
    )
    _state(root.parent, "2026-10-13T07:19:08.512", wrong_pid=wrong_pid, old=old)
    data = collect_hermes_logs({"Assistant": root, "Scheduler": scheduler})
    error = next(r for r in data["records"] if r["level"] == "ERROR")
    assert (error["status"] == "recovered") == recovered


def test_retained_history_does_not_use_current_connected_state_without_later_recovery(
    tmp_path, host_timezone
):
    root = fixture_logs(tmp_path / "default" / "logs", "")
    scheduler = fixture_logs(
        tmp_path / "default" / "profiles" / "scheduler" / "logs", FAIL + SCHEDULER_RUNNER
    )
    _state(root.parent, "2026-10-13T06:19:08.512")
    assert collect_hermes_logs({"Assistant": root, "Scheduler": scheduler})["counts"]["error"] == 1


def test_unknown_component_and_multiline_payload_are_not_forwarded(tmp_path):
    root = fixture_logs(
        tmp_path / "logs",
        "2026-10-13 12:00:00 ERROR PRIVATE_SENTINEL: message\nprivate continuation\n",
    )
    result = collect_hermes_logs(root)
    assert "PRIVATE_SENTINEL" not in repr(result)
    assert "private continuation" not in repr(result)
    assert result["counts"]["error"] == 1


def test_oversized_traceback_never_downgrades_from_partial_evidence(tmp_path):
    long_trace = TRACE + ("private continuation\n" * 130) + "RuntimeError: actual failure\n"
    root = fixture_logs(tmp_path / "logs", PROXY + long_trace)
    assert collect_hermes_logs(root)["counts"]["error"] == 1


@pytest.mark.parametrize("value", ["not-json", "{}", "[]", '{"pid":false}'])
def test_malformed_state_cannot_hide_error(tmp_path, value):
    root = fixture_logs(tmp_path / "logs", FAIL + RUNNER)
    (root.parent / "gateway_state.json").write_text(value)
    assert collect_hermes_logs(root)["counts"]["error"] == 1
