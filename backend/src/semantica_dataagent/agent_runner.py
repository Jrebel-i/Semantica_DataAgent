"""Provider-neutral online agent plugin registry.

The REST layer only knows about this module. Each runner is registered as an
entry-point plugin and turns its own wire protocol into the same event stream.
"""

from importlib.metadata import entry_points

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
from .plugins import AGENT_RUNNER_GROUP

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


def discover_runners(registered=None) -> dict[str, AgentRunner]:
    """Load agent runners from the same entry-point registry as other plugins.

    ``registered`` is injectable so tests can register a throwaway runner
    without adding it to the package metadata.
    """

    if registered is None:
        registered = entry_points(group=AGENT_RUNNER_GROUP)
    found: dict[str, AgentRunner] = {}
    for entry_point in registered:
        runner = entry_point.load()()
        agent_id = getattr(runner, "agent_id", None)
        if not isinstance(agent_id, str) or not agent_id:
            raise TypeError(f"Agent runner {entry_point.name!r} has no agent_id")
        if agent_id != entry_point.name:
            raise ValueError(
                f"Agent runner entry point {entry_point.name!r} returned "
                f"agent_id {agent_id!r}"
            )
        if not callable(getattr(runner, "available", None)):
            raise TypeError(f"Agent runner {agent_id!r} has no available()")
        if not callable(getattr(runner, "run", None)):
            raise TypeError(f"Agent runner {agent_id!r} has no run()")
        if agent_id in found:
            raise ValueError(f"Duplicate agent runner: {agent_id}")
        found[agent_id] = runner
    return found


RUNNERS: dict[str, AgentRunner] = discover_runners()


def get_runner(agent_id: str) -> AgentRunner | None:
    return RUNNERS.get(agent_id)


def list_agents() -> list[dict]:
    return [
        {
            "id": agent_id,
            "name": agent_id,
            "available": runner.available(),
            "effort_levels": list(getattr(runner, "effort_levels", ())),
        }
        for agent_id, runner in sorted(RUNNERS.items())
    ]
