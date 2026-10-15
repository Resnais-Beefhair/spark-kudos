"""Regression tests for the independent privacy / recovery review findings."""

import hashlib
import hmac
import json

import pytest

from sisu_kudos import log_events
from sisu_kudos.logs import collect_hermes_logs, parse_log_line

FAIL = (
    "2026-10-13 07:19:05,000 ERROR plugins.platforms.discord.adapter: "
    "[Discord] Discord Gateway WebSocket transport closed (socket_closed, 1/2); forcing reconnect\n"
)


@pytest.mark.parametrize(
    "source_profile,event_profile,resolved",
    [
        ("default", "scheduler", False),
        ("worker", "scheduler", False),
        ("scheduler", "scheduler", True),
        ("default", "default", True),
        ("scheduler", "default", False),
    ],
)
def test_explicit_recovery_profile_must_match_source(
    tmp_path, source_profile, event_profile, resolved
):
    home = tmp_path if source_profile == "default" else tmp_path / "profiles" / source_profile
    root = home / "logs"
    root.mkdir(parents=True)
    suffix = " successfully" if event_profile == "default" else f" (profile: {event_profile})"
    recover = f"2026-10-13 07:19:08,000 INFO gateway.run: ✓ discord reconnected{suffix}\n"
    (root / "gateway.log").write_text(FAIL + recover)
    data = collect_hermes_logs({"Source label": root})
    row = next(r for r in data["records"] if r["level"] == "ERROR")
    assert (row["status"] == "recovered") is resolved
    assert data["counts"]["error"] == int(not resolved)


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_bytes(b"k" * 32)
    path.chmod(0o600)
    monkeypatch.setenv("SISU_KUDOS_LOG_ID_KEY", str(path))
    log_events._identity_key.cache_clear()
    yield path
    log_events._identity_key.cache_clear()


def test_public_id_is_keyed_and_stable_across_key_reload(key_file):
    row = parse_log_line(
        "2026-10-13 12:00:00,000 ERROR gateway.run: candidate private message", "errors"
    )
    parts = (
        "hermes",
        "default",
        row["timestamp"],
        "ERROR",
        "gateway.run",
        "candidate private message",
    )
    encoded = json.dumps(parts, ensure_ascii=False).encode()
    unkeyed = hashlib.sha256(encoded).hexdigest()[:24]
    assert row["id"] != unkeyed
    expected = hmac.new(key_file.read_bytes(), encoded, hashlib.sha256).hexdigest()[:24]
    assert row["id"] == expected
    log_events._identity_key.cache_clear()
    assert log_events.identity(*parts) == expected
    key_file.write_bytes(b"z" * 32)
    log_events._identity_key.cache_clear()
    assert log_events.identity(*parts) != expected


@pytest.mark.parametrize("kind", ["missing", "public", "symlink", "hardlink", "short"])
def test_unsafe_key_uses_private_ephemeral_fallback(key_file, kind):
    if kind == "missing":
        key_file.unlink()
    elif kind == "public":
        key_file.chmod(0o644)
    elif kind == "symlink":
        target = key_file.with_name("target")
        key_file.rename(target)
        key_file.symlink_to(target)
    elif kind == "hardlink":
        key_file.with_name("linked").hardlink_to(key_file)
    else:
        key_file.write_bytes(b"short")
    first = log_events._identity_key()
    assert len(first) == 32 and first != b"k" * 32
    assert log_events._identity_key() == first
    log_events._identity_key.cache_clear()
    assert log_events._identity_key() != first


def test_distinct_same_timestamp_incidents_have_distinct_ids(tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    (root / "gateway.log").write_text(FAIL + FAIL.replace("socket_closed", "ack_stale"))
    records = collect_hermes_logs({"Assistant": root})["records"]
    assert len(records) == 2
    assert len({row["id"] for row in records}) == 2
