"""Claude quota opt-in, freshness and independent snapshot composition."""

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sisu_kudos.config import ConfigurationError, Settings, load_settings
from sisu_kudos.state import LiveState


class Scheduler:
    def __init__(self):
        self.values = {}

    def names(self):
        return list(self.values)

    def value(self, name):
        return self.values.get(name)

    def state(self, name):
        return SimpleNamespace(
            updated_at=time.time(),
            error="",
            last_duration_s=0.01,
            failures=0,
            age_s=lambda: 0.1,
            is_stale=lambda: False,
        )


def sample(*, ago=0, used=4):
    return {
        "provider": "Claude",
        "available": True,
        "status": "Max 5×",
        "state": "available",
        "collected_at": (datetime.now(UTC) - timedelta(seconds=ago)).isoformat(),
        "windows": [
            {
                "id": "five_hour",
                "used_percent": used,
                "remaining_percent": 100 - used,
                "duration_minutes": 300,
                "resets_at": "2027-01-01T00:00:00+00:00",
            }
        ],
    }


def configured():
    live = LiveState(Settings(claude_quota_enabled=True))
    live.scheduler = Scheduler()
    return live


def test_claude_config_is_independent_and_paths_are_private(tmp_path):
    config = tmp_path / "kudos.toml"
    config.write_text(
        "[integrations]\nclaude_quota = true\n[paths]\n"
        'claude_credentials = "private/credentials.json"\n'
    )
    settings = load_settings(config)
    assert settings.claude_quota_enabled and not settings.quotas_enabled
    assert settings.claude_credentials_path == tmp_path / "private/credentials.json"
    assert not Settings().claude_quota_enabled
    assert Settings().claude_credentials_path is None
    payload = asyncio.run(LiveState(settings).snapshot())
    assert payload["ui"]["features"]["claude_quota"] is True
    assert "private/credentials" not in str(payload)
    config.write_text('[integrations]\nclaude_quota = "yes"\n')
    with pytest.raises(ConfigurationError, match="boolean"):
        load_settings(config)


def test_claude_can_publish_without_agentmon_or_chatgpt():
    live = configured()
    live.scheduler.values["claude_quota"] = sample()
    asyncio.run(live._publish({"claude_quota"}))
    quota = asyncio.run(live.snapshot())["agents"]["quotas"]["claude"]
    assert quota["available"] and not quota.get("stale")
    assert quota["windows"][0]["remaining_percent"] == 96
    assert live.agent_service is None


def test_agent_and_cron_refresh_cannot_erase_claude_quota():
    live = configured()
    live.scheduler.values["claude_quota"] = sample()
    asyncio.run(live._publish({"claude_quota"}))
    previous = live.data["agents"]["quotas"]["claude"]
    live.scheduler.values["agents"] = SimpleNamespace(sessions=[], generated_at="", health=[])
    asyncio.run(live._publish({"agents", "crons"}))
    assert live.data["agents"]["quotas"]["claude"]["windows"] == previous["windows"]


def test_claude_failure_retains_last_good_timestamp_but_marks_stale_and_recovers():
    live = configured()
    value = sample()
    live.scheduler.values["claude_quota"] = value
    asyncio.run(live._publish({"claude_quota"}))
    live.scheduler.values["claude_quota"] = {
        "provider": "Claude",
        "available": False,
        "status": "",
        "windows": [],
        "state": "rate_limited",
        "collected_at": datetime.now(UTC).isoformat(),
    }
    asyncio.run(live._publish({"claude_quota"}))
    quota = live.data["agents"]["quotas"]["claude"]
    assert quota["available"] and quota["stale"]
    assert quota["state"] == "rate_limited"
    assert quota["collected_at"] == value["collected_at"]
    assert quota["status"] == "Max 5×"
    recovered = sample(used=9)
    live.scheduler.values["claude_quota"] = recovered
    asyncio.run(live._publish({"claude_quota"}))
    quota = live.data["agents"]["quotas"]["claude"]
    assert not quota.get("stale") and quota["state"] == "available"
    assert quota["windows"][0]["remaining_percent"] == 91


def test_old_collector_payload_is_stale_even_after_successful_scheduler_call():
    live = configured()
    live.scheduler.values["claude_quota"] = sample(ago=600)
    asyncio.run(live._publish({"claude_quota"}))
    assert live.data["agents"]["quotas"]["claude"]["stale"]


async def test_disabled_claude_never_constructs_collector_or_reads_configured_credentials(
    monkeypatch, tmp_path
):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled Claude integration was accessed")

    monkeypatch.setitem(
        sys.modules, "sisu_kudos.claude_quota", SimpleNamespace(ClaudeQuotaCollector=forbidden)
    )
    live = LiveState(Settings(claude_credentials_path=tmp_path / "never-read.json"))
    try:
        await live.start()
        assert "claude_quota" not in live.scheduler.names()
        assert "claude" not in (await live.snapshot())["agents"]["quotas"]
    finally:
        await live.stop()


def test_missing_auth_does_not_become_measured_zero():
    live = configured()
    live.scheduler.values["claude_quota"] = {
        "provider": "Claude",
        "available": False,
        "status": "",
        "windows": [],
        "state": "login_required",
        "collected_at": "",
    }
    asyncio.run(live._publish({"claude_quota"}))
    quota = live.data["agents"]["quotas"]["claude"]
    assert not quota["available"] and quota["windows"] == []
