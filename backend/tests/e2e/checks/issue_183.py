"""Verify reasoning effort reaches pi, Claude, and Codex through the real REST API."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

ALL_EFFORTS = ["low", "medium", "high", "extra", "max"]


def _events(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        name = None
        data = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name is not None:
            events.append((name, data))
    return events


def _ask(ctx, payload: dict) -> list[tuple[str, dict]]:
    with ctx.rest.stream(
        "POST", "/api/v1/conversations/ask", json=payload, timeout=180
    ) as response:
        ctx.require(
            response.status_code == 200,
            f"ask: HTTP {response.status_code} {response.read()[:400]!r}",
        )
        body = "".join(response.iter_text())
    events = _events(body)
    ctx.require(
        any(name == "done" for name, _ in events),
        f"answer did not finish: {events[-3:]}",
    )
    return events


def _script_header(capture: Path) -> str:
    return f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

path = Path({str(capture)!r})
runs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
runs.append(sys.argv[1:])
path.write_text(json.dumps(runs), encoding="utf-8")
"""


def _install_fake_clis(ctx) -> dict[str, Path]:
    raw = os.environ.get("ISSUE183_FAKE_BIN_DIR")
    ctx.require(
        bool(raw),
        "ISSUE183_FAKE_BIN_DIR must point at a directory on the service PATH",
    )
    assert raw is not None
    directory = Path(raw)
    directory.mkdir(parents=True, exist_ok=True)
    captures = {
        "pi": directory / "capture-pi.json",
        "claude": directory / "capture-claude.json",
        "codex": directory / "capture-codex.json",
    }
    for capture in captures.values():
        capture.unlink(missing_ok=True)

    scripts = {
        "pi": _script_header(captures["pi"])
        + """sys.stdin.readline()
print(json.dumps({"type": "message_end", "message": {
    "role": "assistant",
    "content": [{"type": "text", "text": "fake pi answer"}],
    "stopReason": "stop",
}}), flush=True)
print(json.dumps({"type": "agent_settled"}), flush=True)
""",
        "claude": _script_header(captures["claude"])
        + """print(json.dumps({"type": "result", "result": "fake claude answer"}), flush=True)
""",
        "codex": _script_header(captures["codex"])
        + """print(json.dumps({"type": "thread.started", "thread_id": "thread-183"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {
    "type": "agent_message",
    "text": "fake codex answer",
}}), flush=True)
""",
    }
    for name, text in scripts.items():
        binary = directory / name
        binary.write_text(text, encoding="utf-8")
        binary.chmod(0o755)
    return captures


def _last_arguments(capture: Path) -> list[str]:
    runs = json.loads(capture.read_text(encoding="utf-8"))
    return runs[-1]


def _mapped_effort(agent: str, arguments: list[str]) -> str:
    if agent == "pi":
        return arguments[arguments.index("--thinking") + 1]
    if agent == "claude":
        return arguments[arguments.index("--effort") + 1]
    value = arguments[arguments.index("-c") + 1]
    prefix = 'model_reasoning_effort="'
    if not value.startswith(prefix) or not value.endswith('"'):
        raise AssertionError(f"codex effort config has the wrong shape: {value!r}")
    return value[len(prefix) : -1]


def _create_provider(ctx, name: str, provider_type: str) -> str:
    response = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": name,
            "type": provider_type,
            "base_url": "http://127.0.0.1:9",
            "models": ["effort-model", "limited-model"],
            "model_capabilities": {
                "effort-model": {"effort_levels": ALL_EFFORTS},
                "limited-model": {"effort_levels": ["low"]},
            },
        },
    )
    ctx.require(
        response.status_code == 201,
        f"provider: {response.status_code} {response.text}",
    )
    return response.json()["id"]


def check_claude_and_codex_effort_mapping(ctx):
    captures = _install_fake_clis(ctx)
    agents = ctx.rest.get("/api/v1/agents")
    ctx.require(agents.status_code == 200, agents.text)
    levels = {row["id"]: row["effort_levels"] for row in agents.json()}
    for agent in ("pi", "claude", "codex"):
        ctx.require(
            levels.get(agent) == ALL_EFFORTS,
            f"{agent} effort levels: {levels}",
        )

    providers = {
        "pi": _create_provider(ctx, "issue183 pi", "anthropic-messages"),
        "claude": _create_provider(ctx, "issue183 claude", "anthropic-messages"),
        "codex": _create_provider(ctx, "issue183 codex", "openai-responses"),
    }
    observed = {}
    for agent in ("pi", "claude", "codex"):
        provider_id = providers[agent]
        first = _ask(
            ctx,
            {
                "question": f"{agent} extra effort",
                "agent": agent,
                "provider_id": provider_id,
                "model": "effort-model",
                "effort": "extra",
            },
        )
        conversation_id = first[0][1]["conversation_id"]
        arguments = _last_arguments(captures[agent])
        ctx.require(
            _mapped_effort(agent, arguments) == "xhigh",
            f"{agent} did not map extra to xhigh: {arguments}",
        )
        if agent == "claude":
            session = str(uuid.UUID(conversation_id))
            session_file = (
                ctx.runtime.db_path.parent
                / "agent-workspaces"
                / conversation_id
                / "sessions"
                / "claude"
                / "projects"
                / "project"
                / f"{session}.jsonl"
            )
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text("{}\n", encoding="utf-8")

        _ask(
            ctx,
            {
                "question": f"{agent} max effort",
                "conversation_id": conversation_id,
                "effort": "max",
            },
        )
        arguments = _last_arguments(captures[agent])
        ctx.require(
            _mapped_effort(agent, arguments) == "max",
            f"{agent} did not map max to max: {arguments}",
        )
        if agent == "claude":
            ctx.require("--resume" in arguments, f"claude resume: {arguments}")
        if agent == "codex":
            ctx.require(
                "resume" in arguments and "thread-183" in arguments,
                f"codex resume: {arguments}",
            )
        observed[agent] = {
            "extra": "xhigh",
            "max": "max",
            "resume": "--resume" in arguments
            or ("resume" in arguments and "thread-183" in arguments),
        }

        for payload, label in (
            (
                {
                    "question": "unsupported model effort",
                    "agent": agent,
                    "provider_id": provider_id,
                    "model": "limited-model",
                    "effort": "high",
                },
                "limited model",
            ),
            (
                {
                    "question": "unknown effort",
                    "agent": agent,
                    "provider_id": provider_id,
                    "model": "effort-model",
                    "effort": "ultra",
                },
                "unknown level",
            ),
        ):
            rejected = ctx.rest.post("/api/v1/conversations/ask", json=payload)
            ctx.require(
                rejected.status_code == 422,
                f"{agent} {label}: {rejected.status_code} {rejected.text}",
            )

    before = len(ctx.rest.get("/api/v1/conversations").json())
    ctx.require(before == 3, f"unexpected conversation count: {before}")
    return {"levels": levels, "mapped": observed}
