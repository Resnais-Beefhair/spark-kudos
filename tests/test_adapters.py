from dataclasses import dataclass, field

from sisu_kudos.adapters import clean_text, safe_agent_snapshot, safe_event, safe_location
from sisu_kudos.config import Settings
from sisu_kudos.state import LiveState


@dataclass
class Event:
    timestamp: str = "2026-08-15T20:00:00+03:00"
    kind: str = "TOOL"
    summary: str = "  safe   summary  "
    detail: str = "SECRET raw tool result"
    tool_name: str = "terminal"
    duration_ms: int = 12
    is_error: bool = False
    total_tokens: int = 42


@dataclass
class Session:
    uid: str = "hermes:private-routing-key"
    runtime: str = "hermes"
    display_name: str = "Assistant"
    status: str = "tool"
    model: str = "test-model"
    provider: str = "test"
    title: str = "Safe display title"
    task_preview: str = "SECRET raw user prompt"
    discord_location: str = "thread 1539664999956615168"
    age_seconds: float = 2
    updated_at: str = "2026-08-15T20:01:00+03:00"
    reasoning_effort: str = "high"
    service_tier: str = "fast"
    events: list = field(default_factory=lambda: [Event()])


@dataclass
class Snapshot:
    generated_at: str = "2026-08-15T20:00:00+03:00"
    sessions: list = field(default_factory=lambda: [Session()])
    health: list = field(default_factory=list)
    chatgpt_quota: object | None = None


def test_event_contract_excludes_raw_detail():
    data = safe_event(Event(), Session())
    assert "detail" not in data
    assert "SECRET" not in repr(data)
    assert data["summary"] == "safe summary"
    assert data["model"] == "test-model"
    assert data["provider"] == "test"


def test_event_without_own_timestamp_gets_honest_session_fallback():
    data = safe_event(Event(timestamp=""), Session())
    assert data["timestamp"] == "2026-08-15T20:01:00+03:00"
    assert data["timestamp_approximate"] is True


def test_event_without_any_source_time_gets_snapshot_fallback():
    data = safe_event(Event(timestamp=""), Session(updated_at=""), "2026-08-15T20:02:00+03:00")
    assert data["timestamp"] == "2026-08-15T20:02:00+03:00"
    assert data["timestamp_approximate"] is True


def test_snapshot_is_bounded_and_does_not_publish_unavailable_supergrok_quota():
    snap = Snapshot(sessions=[Session(uid=f"s:{i}") for i in range(30)])
    data = safe_agent_snapshot(snap)
    assert len(data["sessions"]) == 16
    assert "supergrok" not in data["quotas"]
    rendered = repr(data)
    assert "private-routing-key" not in rendered
    assert "SECRET raw user prompt" not in rendered
    assert data["sessions"][0]["uid"] == data["events"][0]["session_uid"]
    assert data["sessions"][0]["reasoning"] == "high"
    assert data["sessions"][0]["service_tier"] == "fast"


def test_old_error_sessions_do_not_remain_live_forever():
    recent = Session(uid="recent", status="error", age_seconds=120)
    old = Session(uid="old", status="error", age_seconds=7200)
    data = safe_agent_snapshot(Snapshot(sessions=[recent, old]))
    recent_uid = safe_agent_snapshot(Snapshot(sessions=[recent]))["sessions"][0]["uid"]
    assert [item["uid"] for item in data["sessions"]] == [recent_uid]


def test_clean_text_removes_controls_and_bounds():
    assert clean_text("a\x00  b", 3) == "a b"


def test_location_masks_discord_routing_identifiers():
    assert safe_location("1539664999956615168") == "Discord"
    assert safe_location("thread 1539664999956615168") == "thread [ID]"
    data = safe_agent_snapshot(Snapshot())
    assert "1539664999956615168" not in repr(data)


def test_transient_quota_failure_retains_last_good_window():
    state = LiveState(Settings())
    previous = {
        "quotas": {
            "chatgpt": {
                "available": True,
                "status": "ChatGPT Pro",
                "windows": [{"remaining_percent": 50}],
            }
        }
    }
    safe = safe_agent_snapshot(Snapshot())
    state._retain_last_good_quotas(safe, previous)
    quota = safe["quotas"]["chatgpt"]
    assert quota["available"] is True
    assert quota["stale"] is True
    assert quota["windows"] == [{"remaining_percent": 50}]
