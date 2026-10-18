"""Correct serialized Hermes turn attempts without modifying agent gateways.

The optional agent-monitor collector counts unmatched starts. A failed attempt may
have no matching end, so a later completed retry leaves a phantom running turn.
Within each session, the latest explicit turn boundary supersedes older attempts.
Keep the upstream collector's process fingerprint, gateway and freshness checks.
"""

from __future__ import annotations

from typing import Any

_TURN_STARTED = "Vuoro alkoi"
_TURN_ENDED = "Vuoro päättyi"


def current_turn_events(events: list[Any]) -> list[Any]:
    """Keep the latest serialized attempt per session; never mutate source events.

    Input order is the chronological file order supplied by Hermes.parse_log.
    Preserve orphan activity (e.g. compression children or a bounded log tail)
    and gateway boundaries. After an explicit end, late tool completions must not
    resurrect the session; only a new explicit start opens the next attempt.
    """
    boundaries: dict[str, tuple[int, bool]] = {}
    for index, event in enumerate(events):
        if event.session_id and event.kind == "STATE" and event.summary in {
            _TURN_STARTED,
            _TURN_ENDED,
        }:
            boundaries[event.session_id] = (index, event.summary == _TURN_STARTED)
    result = []
    for index, event in enumerate(events):
        boundary = boundaries.get(event.session_id)
        if boundary is None or index == boundary[0] or (boundary[1] and index > boundary[0]):
            result.append(event)
    return result


def serialized_hermes_collector(collector: Any) -> Any:
    """Wrap only candidate reconstruction; leave parsing/SQLite/health upstream."""
    base = type(collector)

    class SerializedHermesCollector(base):
        @staticmethod
        def _live_session_candidates(
            events: list[Any], gateway: dict[str, Any]
        ) -> dict[str, float]:
            return base._live_session_candidates(current_turn_events(events), gateway)

    return SerializedHermesCollector(collector.home, collector.resolver)
