"""Provider-neutral online agent interface.

The REST layer only knows about this module. Each adapter (pi today, Claude and
Codex later) turns its own wire protocol into the same stream of events.
"""

from .agent_types import (
    CLARIFICATION,
    DONE,
    ERROR,
    EVENT_TYPES,
    TEXT_DELTA,
    TOOL_END,
    TOOL_START,
    AgentEvent,
    AgentRunner,
    AgentTurnRequest,
    ProviderConfig,
)

__all__ = [
    "CLARIFICATION",
    "DONE",
    "ERROR",
    "EVENT_TYPES",
    "TEXT_DELTA",
    "TOOL_END",
    "TOOL_START",
    "AgentEvent",
    "AgentRunner",
    "AgentTurnRequest",
    "ProviderConfig",
]


def _make_runners() -> dict[str, AgentRunner]:
    from .claude_runner import ClaudeRunner
    from .pi_runner import PiRunner

    return {"pi": PiRunner(), "claude": ClaudeRunner()}


RUNNERS: dict[str, AgentRunner] = _make_runners()


def get_runner(agent_id: str) -> AgentRunner | None:
    return RUNNERS.get(agent_id)


def list_agents() -> list[dict]:
    return [
        {"id": agent_id, "name": agent_id, "available": runner.available()}
        for agent_id, runner in sorted(RUNNERS.items())
    ]
