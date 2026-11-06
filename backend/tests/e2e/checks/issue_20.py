"""Issue #20 end-to-end checks: evidence-backed clarification and delegation."""

from __future__ import annotations

import json
import os

PROVIDER_SECRET = "{{SECRET_issue20_e2e_key}}"
AMBIGUOUS = (
    "上个月销售额是多少？我还没有决定采用支付销售额还是下单销售额，"
    "请先检索两种定义，向我提供有证据引用的口径选项，待我选择后再写 Hive SQL。"
)
PAYMENT_REF = "definitions/sales.md"
ORDER_REF = "sales_order.md"


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
    return _parse_sse(text)


def _provider(ctx) -> tuple[str, str]:
    base_url = os.environ.get("TEST_PI_BASE_URL", "http://127.0.0.1:3425").rstrip("/")
    model = os.environ.get("TEST_PI_MODEL", "deepseek-v4.1-flash#claude")
    created = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": "Issue 20 E2E",
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
    return created.json()["id"], model


def _clarifications(events: list[tuple[str, dict]]) -> list[dict]:
    questions: list[dict] = []
    for name, data in events:
        if name == "clarification":
            questions.extend(data.get("questions") or [])
    return questions


def _sql_blocks(events: list[tuple[str, dict]]) -> list[str]:
    blocks: list[str] = []
    for name, data in events:
        if name == "done":
            blocks.extend(item.get("sql", "") for item in data.get("sql") or [])
    return [block for block in blocks if block]


def _selected_definition(questions: list[dict]) -> tuple[dict, dict]:
    for question in questions:
        for option in question.get("options") or []:
            ref = str(option.get("ref") or "")
            if PAYMENT_REF in ref:
                return question, option
            if not ref:
                raise AssertionError(f"clarification option has no ref: {option!r}")
    for question in questions:
        for option in question.get("options") or []:
            if ORDER_REF in str(option.get("ref") or ""):
                return question, option
    raise AssertionError(
        "no clarification option referenced either sales definition: "
        f"{json.dumps(questions, ensure_ascii=False)[:1000]}"
    )


def check_online_clarification_selection_is_scoped_to_one_conversation(ctx):
    provider_id, model = _provider(ctx)
    first = _ask(
        ctx,
        {
            "question": AMBIGUOUS,
            "agent": "pi",
            "provider_id": provider_id,
            "model": model,
        },
    )
    ctx.require(first and first[0][0] == "conversation", f"first events: {first[:1]}")
    conversation_id = first[0][1]["conversation_id"]
    questions = _clarifications(first)
    ctx.require(
        questions,
        "the ambiguous question did not raise a clarification event: "
        f"{[(name, data) for name, data in first if name == 'error']}",
    )
    ctx.require(
        all(len(question.get("options") or []) >= 2 for question in questions),
        f"clarification questions need at least two options: {questions!r}",
    )
    ctx.require(
        all(
            option.get("ref")
            for question in questions
            for option in question.get("options") or []
        ),
        f"a clarification option has no evidence ref: {questions!r}",
    )
    ctx.require(
        not _sql_blocks(first),
        "the agent silently chose a sales definition instead of waiting for a choice",
    )

    question, option = _selected_definition(questions)
    ref = str(option["ref"])
    expected_table = "shop.payments" if PAYMENT_REF in ref else "shop.orders"
    prompt = "\n".join(
        [
            "澄清选择：请只针对当前问题按所选口径继续回答，不要把它作为全局规则。",
            f"问题：{question.get('question') or ''}",
            f"选项：{option.get('label') or ''}",
            f"证据：{ref}",
            (
                "请继续完成：用该口径对应的表写一个按 dt 统计记录数的 Hive SQL"
                "（单表查询，不要执行 SQL）。最终回答请包含完整的 SQL 代码块，"
                "不要仅摘要已验证的 SQL。"
            ),
        ]
    )
    second = _ask(ctx, {"question": prompt, "conversation_id": conversation_id})
    ctx.require(second[-1][0] == "done", f"clarification follow-up: {second[-1]}")
    done = second[-1][1]
    sql = [item.get("sql", "") for item in done.get("sql") or []]
    ctx.require(sql, f"the selected definition produced no SQL: {done!r}")
    ctx.require(
        any(expected_table in block for block in sql),
        f"the SQL did not use {expected_table} for {ref}: {sql}",
    )
    detail = ctx.rest.get(f"/api/v1/conversations/{conversation_id}")
    ctx.require(detail.status_code == 200, detail.text)
    stored = detail.json()
    ctx.require(len(stored["turns"]) == 2, f"turns: {len(stored['turns'])}")
    ctx.require(
        any(event["type"] == "clarification" for event in stored["turns"][0]["events"]),
        "the clarification event was not persisted",
    )

    other = _ask(
        ctx,
        {
            "question": AMBIGUOUS,
            "agent": "pi",
            "provider_id": provider_id,
            "model": model,
        },
    )
    other_questions = _clarifications(other)
    ctx.require(
        other_questions,
        "the previous selection leaked into a new conversation; no clarification was raised",
    )
    ctx.require(
        not _sql_blocks(other),
        "the new conversation silently reused the previous choice",
    )
    return {
        "conversation_id": conversation_id,
        "selected_ref": ref,
        "expected_table": expected_table,
        "options": sum(len(item.get("options") or []) for item in questions),
        "new_conversation_id": other[0][1]["conversation_id"],
    }


def check_local_ask_dataagent_answers_and_continues(ctx):
    _provider(ctx)
    question = (
        "请统计 shop.orders 表每个 dt 分区的行数（COUNT(*)），只数记录，不使用其他字段，"
        "不计算金额，不要 JOIN 其他表。这是一个明确的记录数请求，不涉及销售额或订单数指标，"
        "不需要业务口径定义；即使知识库缺少正式指标定义，也请直接用 Hive SQL 回答，不要只"
        "登记缺口或提交纠错。必须先用 describe_tables 确认表字段，写出 SQL 后必须调用 "
        "validate_sql；回答中原样附上工具返回的表引用，例如 table:shop.orders。"
    )
    answer = ctx.mcp_call("ask_dataagent", {"question": question}, timeout=600)
    ctx.require(
        answer.get("status") == "completed",
        f"ask_dataagent status: {answer.get('status')!r} {answer.get('message')!r}",
    )
    conversation_id = answer.get("conversation_id")
    first_turn = answer.get("turn_id")
    ctx.require(
        conversation_id, f"ask_dataagent returned no conversation_id: {answer!r}"
    )
    ctx.require(first_turn, f"ask_dataagent returned no turn_id: {answer!r}")
    ctx.require(answer.get("answer"), "ask_dataagent returned an empty answer")
    ctx.require(
        answer.get("citations"), f"ask_dataagent returned no citations: {answer!r}"
    )
    sql = [item.get("sql", "") for item in answer.get("sql") or []]
    ctx.require(sql, f"ask_dataagent returned no SQL: {answer!r}")

    follow_up = ctx.mcp_call(
        "ask_dataagent",
        {
            "question": (
                "在上一轮 SQL 的基础上只统计 dt >= '2024-01-01'，"
                "其他口径不变；修改后的 SQL 仍必须调用 validate_sql 并附引用。"
            ),
            "conversation_id": conversation_id,
        },
        timeout=600,
    )
    ctx.require(
        follow_up.get("status") == "completed",
        f"follow-up status: {follow_up.get('status')!r} {follow_up.get('message')!r}",
    )
    ctx.require(
        follow_up.get("conversation_id") == conversation_id,
        f"follow-up used another conversation: {follow_up.get('conversation_id')!r}",
    )
    second_turn = follow_up.get("turn_id")
    ctx.require(
        second_turn and second_turn != first_turn,
        f"follow-up did not create a new turn: {answer.get('turn_id')!r} -> {second_turn!r}",
    )

    detail = ctx.rest.get(f"/api/v1/conversations/{conversation_id}")
    ctx.require(detail.status_code == 200, detail.text)
    stored = detail.json()
    ctx.require(
        [turn["sequence"] for turn in stored["turns"]] == [1, 2],
        f"stored turn sequences: {[turn['sequence'] for turn in stored['turns']]}",
    )
    ctx.require(
        all(turn["status"] == "completed" for turn in stored["turns"]),
        "a delegated turn did not complete",
    )
    return {
        "conversation_id": conversation_id,
        "turns": len(stored["turns"]),
        "sql_blocks": len(sql),
        "citations": len(answer["citations"]),
    }
