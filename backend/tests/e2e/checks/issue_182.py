"""Verify reasoning effort through the real REST API and a fake pi binary."""

from __future__ import annotations

import json
import os
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


def _install_fake_pi(ctx) -> tuple[Path, Path]:
    directory = os.environ.get("ISSUE182_FAKE_PI_DIR")
    capture = os.environ.get("ISSUE182_PI_ARGS")
    ctx.require(
        bool(directory and capture),
        "ISSUE182_FAKE_PI_DIR and ISSUE182_PI_ARGS must point at the fake pi setup",
    )
    assert directory is not None and capture is not None
    binary = Path(directory) / "pi"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

Path({capture!r}).write_text(json.dumps(sys.argv[1:]))
sys.stdin.readline()
print(json.dumps({{"type": "message_end", "message": {{
    "role": "assistant",
    "content": [{{"type": "text", "text": "fake pi answer"}}],
    "stopReason": "stop",
}}}}), flush=True)
print(json.dumps({{"type": "agent_settled"}}), flush=True)
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, Path(capture)


def _create_provider(ctx, name: str, models: list[str], capabilities: dict) -> str:
    response = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": name,
            "type": "anthropic-messages",
            "base_url": "http://127.0.0.1:9",
            "models": models,
            "model_capabilities": capabilities,
        },
    )
    ctx.require(
        response.status_code == 201,
        f"provider: {response.status_code} {response.text}",
    )
    return response.json()["id"]


def check_effort_validation_persistence_and_pi_thinking(ctx):
    _, capture = _install_fake_pi(ctx)
    agents = ctx.rest.get("/api/v1/agents")
    ctx.require(agents.status_code == 200, agents.text)
    levels = {row["id"]: row["effort_levels"] for row in agents.json()}
    ctx.require(levels.get("pi") == ALL_EFFORTS, f"pi effort levels: {levels}")
    ctx.require(levels.get("claude") == [], f"claude effort levels: {levels}")
    ctx.require(levels.get("codex") == [], f"codex effort levels: {levels}")

    provider_id = _create_provider(
        ctx,
        "issue182 effort provider",
        ["model-five", "model-low", "model-none"],
        {
            "model-five": {"effort_levels": ALL_EFFORTS},
            "model-low": {"effort_levels": ["low"]},
        },
    )
    base = {"provider_id": provider_id, "model": "model-five"}

    first = _ask(ctx, {"question": "first effort", **base, "effort": "extra"})
    conversation_id = first[0][1]["conversation_id"]
    arguments = json.loads(capture.read_text(encoding="utf-8"))
    ctx.require(
        arguments[arguments.index("--thinking") + 1] == "xhigh",
        f"extra did not map to xhigh: {arguments}",
    )

    _ask(
        ctx,
        {
            "question": "second effort",
            "conversation_id": conversation_id,
            **base,
            "effort": "medium",
        },
    )
    arguments = json.loads(capture.read_text(encoding="utf-8"))
    ctx.require(
        arguments[arguments.index("--thinking") + 1] == "medium",
        f"medium did not map to medium: {arguments}",
    )

    _ask(
        ctx,
        {
            "question": "no effort",
            "conversation_id": conversation_id,
            **base,
        },
    )
    arguments = json.loads(capture.read_text(encoding="utf-8"))
    ctx.require(
        "--thinking" not in arguments, f"effort appeared without a value: {arguments}"
    )

    detail = ctx.rest.get(f"/api/v1/conversations/{conversation_id}")
    ctx.require(detail.status_code == 200, detail.text)
    efforts = [turn["effort"] for turn in detail.json()["turns"]]
    ctx.require(efforts == ["extra", "medium", None], f"turn efforts: {efforts}")

    before = len(ctx.rest.get("/api/v1/conversations").json())
    invalid = ctx.rest.post(
        "/api/v1/conversations/ask",
        json={"question": "bad value", **base, "effort": "ultra"},
    )
    ctx.require(invalid.status_code == 422, f"invalid effort: {invalid.status_code}")
    unsupported_model = ctx.rest.post(
        "/api/v1/conversations/ask",
        json={
            "question": "model does not support it",
            **base,
            "model": "model-low",
            "effort": "high",
        },
    )
    ctx.require(
        unsupported_model.status_code == 422,
        f"unsupported model effort: {unsupported_model.status_code}",
    )
    no_model_levels = ctx.rest.post(
        "/api/v1/conversations/ask",
        json={
            "question": "model has no levels",
            **base,
            "model": "model-none",
            "effort": "medium",
        },
    )
    ctx.require(
        no_model_levels.status_code == 422,
        f"effort on a model without levels: {no_model_levels.status_code}",
    )
    no_agent_levels = ctx.rest.post(
        "/api/v1/conversations/ask",
        json={
            "question": "agent has no levels",
            **base,
            "agent": "claude",
            "effort": "low",
        },
    )
    ctx.require(
        no_agent_levels.status_code == 422,
        f"effort on an agent without levels: {no_agent_levels.status_code}",
    )
    after = len(ctx.rest.get("/api/v1/conversations").json())
    ctx.require(before == after, "rejected effort requests created conversations")

    return {
        "conversation_id": conversation_id,
        "turn_efforts": efforts,
        "pi_thinking": ["xhigh", "medium", None],
    }
