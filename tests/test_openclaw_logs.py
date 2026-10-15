import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import sisu_kudos.logs as logs
from sisu_kudos.logs import collect_all_logs, collect_openclaw_logs


@pytest.fixture(autouse=True)
def clear_openclaw_cache(monkeypatch):
    monkeypatch.setattr(logs, "_OPENCLAW_LAST_GOOD", [])
    monkeypatch.setattr(logs, "_OPENCLAW_STATUS_ROWS", {})


@pytest.mark.parametrize(
    "message,level",
    [
        ('{"_meta":{"logLevelName":"ERROR"},"message":"PRIVATE"}', "ERROR"),
        ('{"level":40,"message":"PRIVATE"}', "WARNING"),
        ("2026-10-01 [tools] exec failed: PRIVATE", "ERROR"),
        ("[ERROR] PRIVATE", "ERROR"),
        ("User discusses an error in an ordinary message", "INFO"),
    ],
)
def test_node_stdout_severity_is_not_lost(monkeypatch, message, level):
    _capture_journal(monkeypatch, _completed([_entry(message=message, priority="6")]))
    data = collect_openclaw_logs()
    assert data["records"][0]["level"] == level
    assert "PRIVATE" not in repr(data)


def test_source_failure_does_not_create_new_error_every_poll(monkeypatch):
    _capture_journal(monkeypatch, _completed(returncode=1))
    first = collect_openclaw_logs()
    second = collect_openclaw_logs()
    assert first["records"] == second["records"]


def _entry(
    message="2026-10-01T10:00:00.000Z [gateway] listening",
    *,
    priority="6",
    cursor="cursor-1",
    timestamp="1790848800000000",
    unit="openclaw-gateway.service",
):
    return {
        "MESSAGE": message,
        "PRIORITY": priority,
        "_CURSOR": cursor,
        "__REALTIME_TIMESTAMP": timestamp,
        "_SYSTEMD_USER_UNIT": unit,
    }


def _completed(entries=(), *, returncode=0, stdout=None):
    output = stdout if stdout is not None else "\n".join(json.dumps(row) for row in entries)
    return SimpleNamespace(returncode=returncode, stdout=output, stderr="private diagnostic")


def _capture_journal(monkeypatch, result):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return result

    monkeypatch.setattr(logs.subprocess, "run", fake_run)
    return seen


def test_openclaw_reader_uses_bounded_user_journal_and_structured_rows(monkeypatch):
    secret = "api_key=UNSAFE_PRIVATE_VALUE"
    seen = _capture_journal(monkeypatch, _completed([_entry(message=secret)]))

    data = collect_openclaw_logs(limit=20)

    command = seen["command"]
    assert "--user" in command
    assert "--unit=openclaw-gateway.service" in command
    assert "--output=json" in command
    assert "--since=30 minutes ago" in command
    assert "--no-pager" in command
    assert "--lines" in command
    assert int(command[command.index("--lines") + 1]) <= 120
    assert seen["kwargs"]["timeout"] <= 3
    assert data["sources"] == [
        {
            "name": "openclaw-gateway",
            "agent": "OpenClaw",
            "runtime": "openclaw",
            "available": True,
        }
    ]
    assert data["records"][0]["runtime"] == "openclaw"
    assert data["records"][0]["agent"] == "OpenClaw"
    assert "UNSAFE_PRIVATE_VALUE" not in repr(data)
    assert "MESSAGE" not in data["records"][0]


def test_journal_priority_errors_and_warnings_are_visible_in_counts_and_rows(monkeypatch):
    entries = [
        _entry("2026-10-01T10:00:00Z [gateway] request failed", priority="3", cursor="err"),
        _entry("2026-10-01T10:00:01Z [gateway] reconnecting", priority="4", cursor="warn"),
        _entry("2026-10-01T10:00:02Z [gateway] ready", priority="6", cursor="info"),
    ]
    _capture_journal(monkeypatch, _completed(entries))

    data = collect_openclaw_logs(limit=10)

    assert {row["level"] for row in data["records"]} == {"INFO", "WARNING", "ERROR"}
    assert data["counts"] == {"warning": 1, "error": 1, "recovered": 0}
    assert data["records"][-1]["message"] != entries[0]["MESSAGE"]


def test_private_content_injection_is_replaced_with_safe_classified_summary(monkeypatch):
    injected = (
        "2026-10-01T10:00:00Z [gateway] token=SECRET_VALUE prompt: PRIVATE_PROMPT "
        "tool_result=PRIVATE_RESULT session_abcdef123456 user@example.com"
    )
    _capture_journal(monkeypatch, _completed([_entry(message=injected)]))

    data = collect_openclaw_logs()
    rendered = repr(data)

    for value in (
        "SECRET_VALUE",
        "PRIVATE_PROMPT",
        "PRIVATE_RESULT",
        "abcdef123456",
        "user@example.com",
    ):
        assert value not in rendered
    assert data["records"][0]["message"] == "Gateway operational event"
    assert data["redacted"] is True


def test_malformed_and_unsupported_schema_are_reported_without_raw_content(monkeypatch):
    _capture_journal(
        monkeypatch,
        _completed(stdout='not-json-private\n{"MESSAGE":"private schema mismatch"}'),
    )

    data = collect_openclaw_logs()

    assert data["sources"][0]["available"] is False
    assert data["counts"]["error"] == 1
    assert len(data["records"]) == 1
    assert data["records"][0]["level"] == "ERROR"
    assert "private" not in repr(data).lower()


def test_partial_malformed_input_keeps_valid_rows_and_exposes_warning(monkeypatch):
    good = _entry(cursor="good")
    _capture_journal(monkeypatch, _completed([good], stdout=json.dumps(good) + "\nnot-json"))

    data = collect_openclaw_logs()

    assert data["sources"][0]["available"] is True
    assert data["counts"]["warning"] == 1
    assert any(
        row["component"] == "collector" and row["level"] == "WARNING" for row in data["records"]
    )


def test_collection_failure_preserves_last_good_and_adds_visible_error(monkeypatch):
    _capture_journal(monkeypatch, _completed([_entry(cursor="last-good")]))
    first = collect_openclaw_logs()
    assert any(
        row["source"] == "openclaw-gateway" and row["level"] == "INFO" for row in first["records"]
    )

    def unavailable(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("journalctl", timeout=1.5)

    monkeypatch.setattr(logs.subprocess, "run", unavailable)
    data = collect_openclaw_logs(limit=10)

    assert data["sources"][0]["available"] is False
    assert data["counts"]["error"] == 1
    assert any(row["message"] == "Gateway operational event" for row in data["records"])
    assert any(
        row["component"] == "collector" and row["level"] == "ERROR" for row in data["records"]
    )


def test_all_collector_error_is_visible_even_when_hermes_is_empty(monkeypatch):
    _capture_journal(monkeypatch, _completed(returncode=1, stdout=""))

    data = collect_openclaw_logs()

    assert data["sources"][0]["available"] is False
    assert data["counts"]["error"] == 1
    assert len(data["records"]) == 1
    assert data["records"][0]["component"] == "collector"


def test_duplicate_journal_cursors_are_deduplicated(monkeypatch):
    entry = _entry(cursor="same-cursor")
    _capture_journal(monkeypatch, _completed([entry, entry]))

    data = collect_openclaw_logs()

    assert len(data["records"]) == 1


def test_collect_all_logs_balances_sources_and_marks_runtime(monkeypatch):
    hermes_records = []
    for source in ("agent", "gateway"):
        for index in range(8):
            hermes_records.append(
                {
                    "timestamp": f"2026-10-01T10:00:{index:02d}+03:00",
                    "runtime": "hermes",
                    "agent": "Assistant",
                    "source": source,
                    "level": "INFO",
                    "component": "test",
                    "message": f"safe {source} {index}",
                }
            )
    hermes = {
        "records": hermes_records,
        "counts": {"warning": 0, "error": 0},
        "sources": [
            {"name": "agent", "agent": "Assistant", "available": True},
            {"name": "gateway", "agent": "Assistant", "available": True},
            {"name": "gateway", "agent": "Assistant", "available": True},
        ],
        "redacted": True,
    }
    oc_record = {
        "timestamp": datetime.fromtimestamp(1790848800, UTC)
        .astimezone()
        .isoformat(timespec="milliseconds"),
        "runtime": "openclaw",
        "agent": "OpenClaw",
        "source": "openclaw-gateway",
        "level": "ERROR",
        "component": "gateway",
        "message": "Gateway reported an operational error",
    }
    oc_records = []
    for index in range(8):
        row = dict(oc_record)
        row["timestamp"] = f"2026-10-01T10:00:{index:02d}+03:00"
        oc_records.append(row)
    monkeypatch.setattr(logs, "collect_hermes_logs", lambda *_args, **_kwargs: hermes)
    monkeypatch.setattr(
        logs,
        "collect_openclaw_logs",
        lambda *_args, **_kwargs: {
            "records": [oc_records[0], dict(oc_records[0]), *oc_records[1:]],
            "counts": {"warning": 0, "error": 8},
            "sources": [
                {
                    "name": "openclaw-gateway",
                    "agent": "OpenClaw",
                    "runtime": "openclaw",
                    "available": True,
                },
                {
                    "name": "openclaw-gateway",
                    "agent": "OpenClaw",
                    "runtime": "openclaw",
                    "available": True,
                },
            ],
            "redacted": True,
        },
    )

    data = collect_all_logs({"Assistant": "/unused"}, limit=8)

    assert len(data["records"]) == 8
    assert {row["runtime"] for row in data["records"]} == {"hermes", "openclaw"}
    by_source = {
        source: sum(row["source"] == source for row in data["records"])
        for source in ("agent", "gateway", "openclaw-gateway")
    }
    assert sorted(by_source.values()) == [2, 3, 3]
    assert data["counts"] == {"warning": 0, "error": 2, "recovered": 0}
    assert len(data["sources"]) == 3
    assert all("runtime" in source for source in data["sources"])
    assert (
        len(
            {
                (row["timestamp"], row["runtime"], row["agent"], row["source"])
                for row in data["records"]
            }
        )
        == 8
    )
