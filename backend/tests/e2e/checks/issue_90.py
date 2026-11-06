"""Check best-effort SQL when payment amount metadata is missing."""

import json
import os


def check_missing_field_produces_sql_and_gap(ctx):
    model = os.environ.get("TEST_PI_MODEL", "deepseek-v4.1-flash#claude")
    provider = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": "Issue 90 E2E",
            "type": "anthropic-messages",
            "base_url": os.environ.get("TEST_PI_BASE_URL", "http://127.0.0.1:3425"),
            "api_key": "{{SECRET_issue90_e2e_key}}",
            "models": [model],
        },
    )
    ctx.require(provider.status_code == 201, provider.text)
    question = "请按支付日期统计 shop.payments 的支付金额，给出 Hive SQL。若字段知识不完整，请说明限制。"
    with ctx.rest.stream(
        "POST",
        "/api/v1/conversations/ask",
        json={
            "question": question,
            "agent": "pi",
            "provider_id": provider.json()["id"],
            "model": model,
        },
        timeout=600,
    ) as response:
        ctx.require(response.status_code == 200, f"ask HTTP {response.status_code}")
        body = "".join(response.iter_text())
    events = []
    for block in body.split("\n\n"):
        name = next(
            (line[7:] for line in block.splitlines() if line.startswith("event: ")),
            None,
        )
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if name and data:
            events.append((name, json.loads("\n".join(data))))
    done = next((data for name, data in reversed(events) if name == "done"), {})
    sql = done.get("sql") or []
    ctx.require(sql, f"missing best-effort SQL: {done}")
    for item in sql:
        result = ctx.mcp_call(
            "validate_sql", {"sql": item["sql"], "dialect": item["dialect"]}
        )
        ctx.require(
            result.get("status") == "pass", f"SQL did not pass validation: {result}"
        )
    answer = done.get("text", "")
    ctx.require(
        any(word in answer for word in ("假设", "假定", "假设为")),
        f"missing assumption: {answer}",
    )
    ctx.require(
        any(
            name == "tool_start"
            and str(data.get("name", "")).endswith("report_knowledge_gap")
            for name, data in events
        ),
        "agent did not report the knowledge gap",
    )
    return {"sql_blocks": len(sql)}
