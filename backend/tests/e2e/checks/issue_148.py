"""Issue #148 end-to-end checks: clarification options survive persistence."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime

_TIMESTAMP = "%Y-%m-%d %H:%M:%S.%f"


def _timestamp() -> str:
    return datetime.now(UTC).strftime(_TIMESTAMP)


def _owner_id(ctx) -> str:
    with sqlite3.connect(ctx.runtime.db_path) as db:
        row = db.execute(
            "SELECT id FROM tokens WHERE revoked = 0 ORDER BY created_at LIMIT 1"
        ).fetchone()
    ctx.require(row is not None, "no token owner in the test database")
    return row[0]


def _add_conversation_and_turn(ctx, owner_id: str, title: str) -> tuple[str, int]:
    conversation_id = uuid.uuid4().hex
    now = _timestamp()
    with sqlite3.connect(ctx.runtime.db_path) as db:
        db.execute(
            "INSERT INTO conversations "
            "(id, owner_id, title, agent, provider_id, model, created_at, "
            "updated_at, resumable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                conversation_id,
                owner_id,
                title,
                "pi",
                "provider-issue-148",
                "test-model",
                now,
                now,
            ),
        )
        cursor = db.execute(
            "INSERT INTO turns "
            "(conversation_id, sequence, question, status, answer, sql, "
            "citations, error, started_at, finished_at) "
            "VALUES (?, 1, ?, 'completed', '', '[]', '[]', NULL, ?, ?)",
            (conversation_id, title, now, now),
        )
        turn_id = cursor.lastrowid
    return conversation_id, int(turn_id)


def _record(ctx, turn_id: int, event) -> None:
    from semantica_dataagent.conversation_api import _record_event

    previous = os.environ.get("DATAAGENT_DB_PATH")
    os.environ["DATAAGENT_DB_PATH"] = str(ctx.runtime.db_path)
    try:
        _record_event(turn_id, 0, event)
    finally:
        if previous is None:
            os.environ.pop("DATAAGENT_DB_PATH", None)
        else:
            os.environ["DATAAGENT_DB_PATH"] = previous


def _detail(ctx, conversation_id: str) -> dict:
    response = ctx.rest.get(f"/api/v1/conversations/{conversation_id}")
    ctx.require(
        response.status_code == 200,
        f"conversation detail: {response.status_code} {response.text[:500]}",
    )
    return response.json()


def _mcp_questions(ctx) -> list[dict]:
    hits = ctx.mcp_call(
        "search_knowledge",
        {"query": ctx.config.search_query, "scope": "all", "top_k": 10},
    )
    refs = [str(hit["ref"]) for hit in hits if hit.get("ref")]
    ctx.require(refs, f"search_knowledge returned no evidence refs: {hits[:3]!r}")
    questions = [
        {
            "question": "销售额按支付口径还是下单口径？",
            "options": [
                {"label": "支付销售额（支付成功时间）", "ref": refs[0]},
                {
                    "label": "下单销售额（订单创建时间）",
                    "ref": refs[1] if len(refs) > 1 else refs[0],
                },
            ],
        }
    ]
    body = ctx.mcp_call("request_clarification", {"questions": questions})
    ctx.require(body.get("questions"), f"clarification returned no questions: {body!r}")
    return body["questions"]


def check_clarification_options_survive_reload(ctx):
    from semantica_dataagent.agent_runner import CLARIFICATION, AgentEvent

    owner_id = _owner_id(ctx)
    conversation_id, turn_id = _add_conversation_and_turn(
        ctx, owner_id, "issue 148 澄清持久化"
    )
    questions = _mcp_questions(ctx)
    _record(ctx, turn_id, AgentEvent(CLARIFICATION, {"questions": questions}))

    turn = _detail(ctx, conversation_id)["turns"][0]
    events = [event for event in turn["events"] if event["type"] == "clarification"]
    ctx.require(len(events) == 1, f"clarification events: {turn['events']!r}")
    stored = events[0]["data"]["questions"]
    ctx.require(
        stored == questions,
        "clarification options changed after persistence: "
        f"expected={json.dumps(questions, ensure_ascii=False)[:1000]} "
        f"stored={json.dumps(stored, ensure_ascii=False)[:1000]}",
    )
    for question in stored:
        ctx.require(question.get("id"), f"question id was lost: {question!r}")
        for option in question.get("options") or []:
            ctx.require(option.get("id"), f"option id was lost: {option!r}")
            ctx.require(option.get("label"), f"option label was lost: {option!r}")
            ctx.require(option.get("ref"), f"option ref was lost: {option!r}")
    return {
        "conversation_id": conversation_id,
        "questions": len(stored),
        "options": sum(len(item.get("options") or []) for item in stored),
    }


def check_other_event_summary_depth_unchanged(ctx):
    from semantica_dataagent.agent_runner import AgentEvent

    owner_id = _owner_id(ctx)
    conversation_id, turn_id = _add_conversation_and_turn(
        ctx, owner_id, "issue 148 摘要深度"
    )
    _record(
        ctx,
        turn_id,
        AgentEvent(
            "tool_end",
            {
                "name": "mcp__dataagent__search_knowledge",
                "payload": {"a": {"b": {"c": {"d": "deep-value"}}}},
            },
        ),
    )
    turn = _detail(ctx, conversation_id)["turns"][0]
    stored = turn["events"][0]["data"]
    ctx.require(
        stored.get("payload", {}).get("a", {}).get("b", {}).get("c") == {"d": "…"},
        f"non-clarification events must keep the depth limit: {stored!r}",
    )
    return {"event": stored}
