"""Issue #17 end-to-end checks: provider secrets, SSE, SQL and session reuse."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

PROVIDER_SECRET = "issue17-e2e-provider-key-not-a-secret"


def _parse_sse(text: str) -> list[tuple[str, dict]]:
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


def _ask(ctx, body: dict) -> list[tuple[str, dict]]:
    with ctx.rest.stream(
        "POST", "/api/v1/conversations/ask", json=body, timeout=300
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
        ctx.token not in text,
        "the personal token appeared in the SSE response",
    )
    return _parse_sse(text)


def check_online_agent_streams_validated_sql_and_reuses_session(ctx):
    base_url = os.environ.get("TEST_PI_BASE_URL", "http://127.0.0.1:3425").rstrip("/")
    model = os.environ.get("TEST_PI_MODEL", "deepseek-v4.1-flash#claude")

    created = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": "Issue 17 E2E",
            "type": "anthropic-messages",
            "base_url": base_url,
            "api_key": PROVIDER_SECRET,
            "models": [model],
        },
    )
    ctx.require(
        created.status_code == 201,
        f"provider create HTTP {created.status_code}: {created.text[:500]}",
    )
    provider = created.json()
    provider_id = provider["id"]
    ctx.require(provider.get("has_api_key") is True, "has_api_key should be true")
    ctx.require("api_key" not in provider, "provider response echoed api_key")
    ctx.require(PROVIDER_SECRET not in created.text, "provider response echoed the key")

    listing = ctx.rest.get("/api/v1/providers")
    ctx.require(listing.status_code == 200, listing.text)
    ctx.require(PROVIDER_SECRET not in listing.text, "provider list echoed the key")

    database = Path(ctx.work_dir) / "app.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT api_key FROM providers WHERE id = ?", (provider_id,)
        ).fetchone()
    ctx.require(row and row[0], "provider key was not stored")
    ctx.require(PROVIDER_SECRET not in row[0], "provider key was stored in plaintext")

    cleared = ctx.rest.put(f"/api/v1/providers/{provider_id}", json={"api_key": ""})
    ctx.require(cleared.status_code == 200, cleared.text)
    ctx.require(cleared.json().get("has_api_key") is False, "the key was not cleared")

    agents = ctx.rest.get("/api/v1/agents")
    ctx.require(agents.status_code == 200, agents.text)
    by_id = {item["id"]: item for item in agents.json()}
    ctx.require(
        by_id.get("pi", {}).get("available") is True,
        f"pi is not available to the backend: {by_id}",
    )

    question = (
        "知识库里支付销售额的口径是什么？请用 shop.payments 表写一个按 dt 统计"
        "记录数的 Hive SQL（单表查询，不要 JOIN 其他表）。必须先用 "
        "search_knowledge 查口径和相关表，写出 SQL 后必须调用 validate_sql，"
        "并在回答里附引用 id。"
    )
    first = _ask(
        ctx,
        {
            "question": question,
            "agent": "pi",
            "provider_id": provider_id,
            "model": model,
        },
    )
    names = [name for name, _ in first]
    ctx.require(names and names[0] == "conversation", f"first events: {names}")
    ctx.require(names[-1] == "done", f"stream ended with: {names[-1:]}")
    conversation_id = first[0][1]["conversation_id"]

    starts = [data for name, data in first if name == "tool_start"]
    ends = [data for name, data in first if name == "tool_end"]
    ctx.require(
        any(str(item.get("name", "")).endswith("search_knowledge") for item in starts),
        "the agent did not call search_knowledge",
    )
    validations = [
        item for item in ends if str(item.get("name", "")).endswith("validate_sql")
    ]
    ctx.require(validations, "the agent did not call validate_sql")
    ctx.require(
        any("status=pass" in str(item.get("summary", "")) for item in validations),
        f"validate_sql did not pass: {validations}",
    )

    done = [data for name, data in first if name == "done"][-1]
    ctx.require(done.get("text"), "the final answer was empty")
    ctx.require(done.get("sql"), "the final answer had no SQL block")
    ctx.require(
        any("shop.payments" in item.get("sql", "") for item in done["sql"]),
        f"the SQL did not use shop.payments: {done['sql']}",
    )
    ctx.require(done.get("citations"), "the final answer had no citations")

    detail = ctx.rest.get(f"/api/v1/conversations/{conversation_id}")
    ctx.require(detail.status_code == 200, detail.text)
    stored = detail.json()
    ctx.require(len(stored["turns"]) == 1, f"turns: {len(stored['turns'])}")
    turn = stored["turns"][0]
    ctx.require(turn["status"] == "completed", turn["status"])
    ctx.require(turn["sql"] == done["sql"], "stored SQL differs from the stream")
    ctx.require(
        turn["citations"] == done["citations"],
        "stored citations differ from the stream",
    )
    ctx.require(
        {"tool_start", "tool_end"} <= {event["type"] for event in turn["events"]},
        "tool events were not persisted",
    )

    follow_up = _ask(
        ctx,
        {
            "question": (
                "在上一轮 SQL 的基础上只统计 dt >= '2024-01-01'，其他口径不变；"
                "修改后的 SQL 仍必须调用 validate_sql 并附引用。"
            ),
            "conversation_id": conversation_id,
        },
    )
    ctx.require(follow_up[-1][0] == "done", f"follow-up: {follow_up[-1][0]}")
    history = ctx.rest.get(f"/api/v1/conversations/{conversation_id}").json()
    ctx.require(
        [item["sequence"] for item in history["turns"]] == [1, 2],
        f"turn sequences: {[item['sequence'] for item in history['turns']]}",
    )
    ctx.require(
        all(item["status"] == "completed" for item in history["turns"]),
        "a turn did not complete",
    )

    configured_root = os.environ.get("DATAAGENT_AGENT_ROOT")
    agent_root = (
        Path(configured_root)
        if configured_root
        else Path(ctx.work_dir) / "agent-workspaces"
    )
    session_dir = agent_root / conversation_id / "sessions"
    session_files = sorted(session_dir.rglob("*.jsonl"))
    ctx.require(session_files, f"no pi session file under {session_dir}")
    matching = []
    for path in session_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        ctx.require(
            ctx.token not in text,
            f"the personal token leaked into {path}",
        )
        if "支付销售额" in text and "dt >= '2024-01-01'" in text:
            matching.append(path)
    ctx.require(
        matching,
        "the follow-up did not reuse the first turn's pi session file",
    )

    return {
        "provider_id": provider_id,
        "conversation_id": conversation_id,
        "tool_calls": len(starts),
        "sql_blocks": len(done["sql"]),
        "citations": len(done["citations"]),
        "session_files": len(session_files),
    }
