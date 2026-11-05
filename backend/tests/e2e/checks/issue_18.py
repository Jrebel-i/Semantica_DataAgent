"""Verify restoring and expiring a real pi conversation."""

import json
import os
import sqlite3
from pathlib import Path


def _ask(ctx, body):
    with ctx.rest.stream(
        "POST", "/api/v1/conversations/ask", json=body, timeout=600
    ) as response:
        ctx.require(
            response.status_code == 200,
            f"ask failed: {response.status_code} {response.read()[:500]!r}",
        )
        raw = "".join(response.iter_text())
    events = []
    for block in raw.split("\n\n"):
        name = next(
            (x[7:] for x in block.splitlines() if x.startswith("event: ")), None
        )
        data = next((x[6:] for x in block.splitlines() if x.startswith("data: ")), None)
        if name and data:
            events.append((name, json.loads(data)))
    ctx.require(
        events and events[-1][0] == "done", f"agent did not finish: {events[-1:]}"
    )
    return events


def check_session_retention(ctx):
    model = os.getenv("TEST_PI_MODEL", "deepseek-v4.1-flash#claude")
    provider = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": "issue 18",
            "type": "anthropic-messages",
            "base_url": os.getenv("TEST_PI_BASE_URL", "http://127.0.0.1:3425"),
            "models": [model],
        },
    )
    ctx.require(provider.status_code == 201, provider.text)
    first = _ask(
        ctx,
        {
            "agent": "pi",
            "provider_id": provider.json()["id"],
            "model": model,
            "question": "请记住我的项目代号是紫色海豚。只需确认记住。",
        },
    )
    conversation = first[0][1]["conversation_id"]
    workspace = Path(ctx.work_dir) / "agent-workspaces" / conversation
    ctx.require(any((workspace / "sessions").rglob("*")), "session files missing")
    second = _ask(
        ctx,
        {"conversation_id": conversation, "question": "我刚才告诉你的项目代号是什么？"},
    )
    answer = second[-1][1].get("text", "")
    ctx.require("紫色海豚" in answer, f"restored session lost fact: {answer}")
    with sqlite3.connect(ctx.runtime.db_path) as db:
        db.execute(
            "UPDATE conversations SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (conversation,),
        )
    response = ctx.rest.get(f"/api/v1/conversations/{conversation}")
    ctx.require(response.status_code == 200, response.text)
    detail = response.json()
    ctx.require(detail["resumable"] is False, "expired conversation is resumable")
    ctx.require(len(detail["turns"]) == 2, "history was lost")
    ctx.require(not workspace.exists(), "session files were not deleted")
    follow = ctx.rest.post(
        "/api/v1/conversations/ask",
        json={"conversation_id": conversation, "question": "继续"},
    )
    ctx.require(
        follow.status_code == 409 and "expired" in follow.text,
        f"follow-up accepted: {follow.status_code} {follow.text}",
    )
