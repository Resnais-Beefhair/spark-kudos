"""Internal read-only session view, independent of optional agent frameworks."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Session:
    uid: str
    runtime: str
    agent_id: str
    display_name: str
    session_id: str
    session_key: str
    status: str
    model: str
    provider: str
    title: str
    task_preview: str
    source: str
    discord_location: str
    started_at: str
    task_started_at: str
    updated_at: str
    age_seconds: float
    reasoning_effort: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    context_tokens: int | None
    current_tool: str = ""
    service_tier: str = ""
    task_total_tokens: int | None = None
    task_api_calls: int | None = None
    events: list[Any] = field(default_factory=list)
