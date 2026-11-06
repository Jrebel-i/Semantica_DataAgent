"""Shared helpers for conversation-level end-to-end checks."""

from __future__ import annotations

import json
import os


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        name: str | None = None
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if name is not None and data:
            events.append((name, json.loads("\n".join(data))))
    return events


def ask(ctx, body: dict) -> list[tuple[str, dict]]:
    with ctx.rest.stream(
        "POST", "/api/v1/conversations/ask", json=body, timeout=600
    ) as response:
        ctx.require(
            response.status_code == 200,
            f"ask HTTP {response.status_code}: {response.read()[:500]!r}",
        )
        ctx.require(
            response.headers.get("content-type", "").startswith("text/event-stream"),
            f"ask returned {response.headers.get('content-type')!r}",
        )
        text = "".join(response.iter_text())
    ctx.require(
        ctx.token not in text, "the personal token appeared in the SSE response"
    )
    return parse_sse(text)


def empty_done(events: list[tuple[str, dict]]) -> bool:
    if not events or events[-1][0] != "done":
        return False
    done = events[-1][1]
    return not (done.get("text") or done.get("sql") or done.get("citations"))


def ask_stable(
    ctx, body: dict, *, attempts: int = 3, expect_clarification: bool = False
) -> tuple[list[tuple[str, dict]], int]:
    """Retry an empty or undecided final turn, known model gateway flakes."""
    for attempt in range(attempts):
        events = ask(ctx, body)
        empty = empty_done(events)
        undecided = (
            expect_clarification
            and bool(events)
            and events[-1][0] == "done"
            and not clarifications(events)
            and not sql_blocks(events)
        )
        if not (empty or undecided) or attempt == attempts - 1:
            return events, attempt
    raise AssertionError("unreachable")


def clarifications(events: list[tuple[str, dict]]) -> list[dict]:
    questions: list[dict] = []
    for name, data in events:
        if name == "clarification":
            questions.extend(data.get("questions") or [])
    return questions


def sql_blocks(events: list[tuple[str, dict]]) -> list[str]:
    blocks: list[str] = []
    for name, data in events:
        if name == "done":
            blocks.extend(item.get("sql", "") for item in data.get("sql") or [])
    return [block for block in blocks if block]


def delegated_answer(ctx, question: str) -> tuple[dict, int]:
    """Retry a completed turn that stopped before doing any work."""
    retries = 0
    for attempt in range(3):
        answer = ctx.mcp_call("ask_dataagent", {"question": question}, timeout=600)
        if (
            answer.get("status") != "completed"
            or answer.get("sql")
            or answer.get("citations")
            or len(answer.get("answer") or "") >= 200
        ):
            return answer, retries
        if attempt < 2:
            retries += 1
    return answer, retries


def create_provider(
    ctx, *, name: str, secret: str, model: str | None = None
) -> tuple[str, str]:
    base_url = os.environ.get("TEST_PI_BASE_URL", "http://127.0.0.1:3425").rstrip("/")
    model = model or os.environ.get("TEST_PI_MODEL", "deepseek-v4.1-flash#claude")
    created = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": name,
            "type": "anthropic-messages",
            "base_url": base_url,
            "api_key": secret,
            "models": [model],
        },
    )
    ctx.require(
        created.status_code == 201,
        f"provider create HTTP {created.status_code}: {created.text[:500]}",
    )
    return created.json()["id"], model
