"""Exercise the online agent adapters through REST and the test knowledge store."""

import json
import os
import subprocess
import threading


def _ask(ctx, body):
    with ctx.rest.stream(
        "POST", "/api/v1/conversations/ask", json=body, timeout=600
    ) as response:
        ctx.require(
            response.status_code == 200,
            f"ask HTTP {response.status_code}: {response.read()[:500]!r}",
        )
        text = "".join(response.iter_text())
    ctx.require(ctx.token not in text, "token leaked into the event stream")
    events = []
    for block in text.split("\n\n"):
        name, data = None, []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if name and data:
            events.append((name, json.loads("\n".join(data))))
    return events


def _check_agent(ctx, agent, provider_type, base_url, model):
    response = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": f"Issue 19 {agent}",
            "type": provider_type,
            "base_url": base_url,
            "models": [model],
        },
    )
    ctx.require(response.status_code == 201, response.text)
    provider = response.json()["id"]
    observed = threading.Event()
    stop = threading.Event()

    def watch():
        while not stop.wait(0.2):
            p = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    "label=com.semantica-dataagent.agent=1",
                    "--filter",
                    f"label=com.semantica-dataagent.owner={ctx.runtime.server.pid}",
                    "--format",
                    "{{.Image}}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            if "semantica-dataagent-agent-runner:1.2.0" in p.stdout:
                observed.set()

    watcher = None
    if os.getenv("DATAAGENT_AGENT_MODE", "sandbox") == "local":
        observed.set()
    else:
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
    try:
        first = _ask(
            ctx,
            {
                "agent": agent,
                "provider_id": provider,
                "model": model,
                "question": "知识库里支付销售额的口径是什么？请先用 search_knowledge 查询，再用 shop.payments 写一个按 dt 统计记录数的 Hive SQL（单表查询，不需要金额字段，不要因为缺少金额字段而拒绝）。写出 SQL 后必须调用 validate_sql 验证，并在回答中附引用 id。",
            },
        )
    finally:
        stop.set()
        if watcher:
            watcher.join()
    ctx.require(observed.is_set(), f"{agent}: no sandbox observed")
    ctx.require(
        first and first[0][0] == "conversation",
        f"{agent}: no conversation event: {first}",
    )
    conversation = first[0][1]["conversation_id"]
    second = _ask(
        ctx,
        {
            "conversation_id": conversation,
            "question": "在上一轮 SQL 的基础上只统计 dt >= '2024-01-01'，其他口径不变；修改后的 SQL 必须调用 validate_sql 并附引用。",
        },
    )
    for index, events in enumerate((first, second), 1):
        ctx.require(
            events and events[-1][0] == "done", f"{agent} turn {index}: {events[-1:]}"
        )
        done = events[-1][1]
        ctx.require(
            done.get("sql") and done.get("citations"),
            f"{agent} turn {index}: missing SQL or citations: {done}",
        )
        ctx.require(
            any("shop.payments" in row.get("sql", "") for row in done["sql"]),
            f"{agent} turn {index}: wrong table",
        )
        ctx.require(
            any(
                name == "tool_start"
                and str(data.get("name", "")).endswith("validate_sql")
                for name, data in events
            ),
            f"{agent} turn {index}: agent did not call validate_sql",
        )
        for row in done["sql"]:
            validation = ctx.mcp_call(
                "validate_sql", {"sql": row["sql"], "dialect": row["dialect"]}
            )
            ctx.require(
                validation.get("status") == "pass",
                f"{agent} turn {index}: SQL validation {validation}",
            )
        ctx.require(
            any(name == "tool_start" for name, _ in events)
            and any(name == "tool_end" for name, _ in events),
            f"{agent} turn {index}: missing tool events",
        )
    ctx.require(
        "2024-01-01" in second[-1][1]["sql"][0]["sql"],
        f"{agent}: follow-up lost filter",
    )
    return {"conversation_id": conversation, "turns": 2}


def check_pi_conversation(ctx):
    return _check_agent(
        ctx,
        "pi",
        "anthropic-messages",
        os.getenv("TEST_PI_BASE_URL", "http://127.0.0.1:3425"),
        os.getenv("TEST_PI_MODEL", "deepseek-v4.1-flash#claude"),
    )


def check_claude_conversation(ctx):
    return _check_agent(
        ctx,
        "claude",
        "anthropic-messages",
        "http://127.0.0.1:3425",
        "deepseek-v4.1-flash#claude",
    )


def check_codex_conversation(ctx):
    return _check_agent(
        ctx,
        "codex",
        "openai-responses",
        "http://127.0.0.1:3425/v1",
        "deepseek-v4.1-flash#codex",
    )
