"""Synthetic lifecycle fixtures. No user profile, log, gateway or SQLite writes."""

from dataclasses import dataclass

from sisu_kudos.hermes_sessions import current_turn_events, serialized_hermes_collector


@dataclass(frozen=True)
class Event:
    session_id: str = "finished"
    kind: str = "STATE"
    summary: str = "Vuoro alkoi"


def test_failed_attempt_then_finished_retry_is_one_closed_turn():
    failed = Event()
    retry = Event()
    ended = Event(summary="Vuoro päättyi")
    source = [failed, retry, ended]
    assert current_turn_events(source) == [ended]
    assert source == [failed, retry, ended]  # The source is not edited.


def test_live_retry_supersedes_old_attempt_but_stays_live():
    retry = Event()
    tool = Event(kind="TOOL", summary="read_file")
    assert current_turn_events([Event(), retry, tool]) == [retry, tool]


def test_new_turn_after_end_and_late_completion_after_end():
    ended = Event(summary="Vuoro päättyi")
    late = Event(kind="DONE", summary="read_file valmis")
    start = Event()
    assert current_turn_events([Event(), ended, late]) == [ended]
    assert current_turn_events([Event(), ended, late, start]) == [start]


def test_concurrent_sessions_gateway_and_compression_children_are_independent():
    gateway = Event(session_id="", summary="Gateway käynnistyi")
    live = Event(session_id="live")
    child = Event(session_id="child", kind="PHASE", summary="API-kutsu #2")
    end = Event(summary="Vuoro päättyi")
    source = [gateway, Event(), live, Event(), child, end]
    assert current_turn_events(source) == [gateway, live, child, end]
    assert current_turn_events([]) == []


def test_wrapper_retains_upstream_gateway_checks_and_does_not_patch_base():
    # A contract spy, not a replacement collector. Real upstream replay is an
    # additional installed-integration gate; public CI needs no private checkout.
    class Source:
        def __init__(self, home, resolver):
            self.home, self.resolver = home, resolver

        @staticmethod
        def _live_session_candidates(events, gateway):
            return {"events": events, "gateway": gateway}

    source = Source("home", "resolver")
    wrapped = serialized_hermes_collector(source)
    ended = Event(summary="Vuoro päättyi")
    events = [Event(), Event(), ended]
    gateway = {"active_agents": 0, "gateway_state": "stopped"}
    result = wrapped._live_session_candidates(events, gateway)
    assert result == {"events": [ended], "gateway": gateway}
    assert (wrapped.home, wrapped.resolver) == (source.home, source.resolver)
    assert source._live_session_candidates(events, gateway)["events"] == events
