"""Check best-effort SQL when payment amount metadata is missing."""

import sys
from pathlib import Path

_CHECKS_DIR = str(Path(__file__).resolve().parent)
if _CHECKS_DIR not in sys.path:
    sys.path.append(_CHECKS_DIR)

from _conversation import (
    ask_stable,
    clarifications,
    create_provider,
    sql_blocks,
)
from issue_20 import AMBIGUOUS


def check_missing_field_produces_sql_and_gap(ctx):
    provider_id, model = create_provider(
        ctx, name="Issue 90 E2E", secret="{{SECRET_issue90_e2e_key}}"
    )
    question = "请按支付日期统计 shop.payments 的支付金额，给出 Hive SQL。若字段知识不完整，请说明限制。"
    events = ask_stable(
        ctx,
        {
            "question": question,
            "agent": "pi",
            "provider_id": provider_id,
            "model": model,
        },
    )[0]
    done = next((data for name, data in reversed(events) if name == "done"), {})
    sql = done.get("sql") or []
    ctx.require(sql, f"missing best-effort SQL: {done}")
    ctx.require(
        any("shop.payments" in item.get("sql", "").lower() for item in sql),
        f"SQL does not use the relevant table: {sql}",
    )
    for item in sql:
        result = ctx.mcp_call(
            "validate_sql", {"sql": item["sql"], "dialect": item["dialect"]}
        )
        ctx.require(
            result.get("status") == "pass", f"SQL did not pass validation: {result}"
        )
    answer = done.get("text", "")
    ctx.require(
        any(
            marker in answer
            for marker in ("假设", "假定", "近似", "替代", "部分结果", "已知限制")
        ),
        f"missing assumption or limitation label: {answer}",
    )
    ctx.require(
        "金额" in answer, f"answer does not explain the missing amount: {answer}"
    )
    ctx.require(
        any(
            name == "tool_start"
            and str(data.get("name", "")).endswith("report_knowledge_gap")
            for name, data in events
        ),
        "agent did not report the knowledge gap",
    )
    gaps = ctx.rest.get("/api/v1/knowledge-gaps")
    ctx.require(
        gaps.status_code == 200
        and any("金额" in str(gap.get("missing", "")) for gap in gaps.json()),
        f"missing amount gap was not recorded: {gaps.text}",
    )
    return {"sql_blocks": len(sql)}


def check_conflicting_definition_still_clarifies(ctx):
    provider_id, model = create_provider(
        ctx, name="Issue 90 Clarification E2E", secret="{{SECRET_issue90_e2e_key}}"
    )
    events, retries = ask_stable(
        ctx,
        {
            "question": AMBIGUOUS,
            "agent": "pi",
            "provider_id": provider_id,
            "model": model,
        },
        attempts=3,
        expect_clarification=True,
    )
    ctx.require(
        events and events[0][0] == "conversation",
        f"ambiguous question did not open a conversation: {events[:1]}",
    )
    questions = clarifications(events)
    ctx.require(
        questions,
        "the ambiguous question did not raise a clarification event: "
        f"events={[name for name, _ in events]} "
        f"errors={[(name, data) for name, data in events if name == 'error']} "
        f"last={(events[-1:] if events else [])}",
    )
    ctx.require(
        not sql_blocks(events),
        "the agent wrote SQL for conflicting metric definitions instead of clarifying",
    )
    return {
        "conversation_id": events[0][1]["conversation_id"],
        "retries": retries,
        "options": sum(len(item.get("options") or []) for item in questions),
    }
