"""Verify every agent runner is registered and still answers two turns."""

import importlib.util
from pathlib import Path


def _load_issue_19():
    path = Path(__file__).with_name("issue_19.py")
    spec = importlib.util.spec_from_file_location("issue_78_issue_19_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_agent_runners_are_registered(ctx):
    response = ctx.rest.get("/api/v1/agents")
    ctx.require(response.status_code == 200, response.text)
    agents = {item["id"]: item for item in response.json()}
    for agent_id in ("pi", "claude", "codex"):
        ctx.require(agent_id in agents, f"{agent_id} is not registered: {agents}")
        ctx.require(
            agents[agent_id]["name"] == agent_id,
            f"{agent_id} has wrong display name: {agents[agent_id]}",
        )
        ctx.require(
            agents[agent_id]["available"] is True,
            f"{agent_id} runtime is not available: {agents[agent_id]}",
        )
    return {"agents": sorted(agents)}


def check_all_agent_runners_answer_two_turns(ctx):
    helpers = _load_issue_19()
    results = {}
    for name in (
        "check_pi_conversation",
        "check_claude_conversation",
        "check_codex_conversation",
    ):
        results[name] = helpers.__dict__[name](ctx)
    return results
