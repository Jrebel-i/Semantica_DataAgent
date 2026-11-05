"""Shared online agent protocol and event types."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

TEXT_DELTA = "text_delta"
TOOL_START = "tool_start"
TOOL_END = "tool_end"
CLARIFICATION = "clarification"
DONE = "done"
ERROR = "error"
EVENT_TYPES = (TEXT_DELTA, TOOL_START, TOOL_END, CLARIFICATION, DONE, ERROR)


@dataclass(slots=True)
class AgentEvent:
    type: str
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.type not in EVENT_TYPES:
            raise ValueError(f"Unknown agent event type: {self.type}")

    def to_dict(self) -> dict:
        return {"type": self.type, **self.data}


@dataclass(slots=True)
class ProviderConfig:
    id: str
    name: str
    type: str
    base_url: str
    models: list[str]
    api_key: str | None = None


@dataclass(slots=True)
class AgentTurnRequest:
    conversation_id: str
    prompt: str
    session_id: str
    workspace: Path
    provider: ProviderConfig
    model: str
    mcp_url: str
    mcp_token: str


class AgentRunner(Protocol):
    agent_id: str

    def available(self) -> bool: ...

    def run(self, request: AgentTurnRequest) -> AsyncIterator[AgentEvent]: ...
