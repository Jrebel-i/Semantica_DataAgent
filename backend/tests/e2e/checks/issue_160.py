"""Issue #160 end-to-end checks: SQL quality diagnostics and event detail."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime

_TIMESTAMP = "%Y-%m-%d %H:%M:%S.%f"
MISSING_PARTITION = "SELECT id FROM shop.orders"
VALID_PARTITION = (
    "SELECT id FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)"
)


def _owner_id(ctx) -> str:
    with sqlite3.connect(ctx.runtime.db_path) as db:
        row = db.execute(
            "SELECT id FROM tokens WHERE revoked = 0 ORDER BY created_at LIMIT 1"
        ).fetchone()
    ctx.require(row is not None, "no token owner in the test database")
    return row[0]


def _add_turn(ctx, owner_id: str) -> tuple[str, int]:
    conversation_id = uuid.uuid4().hex
    now = datetime.now(UTC).strftime(_TIMESTAMP)
    with sqlite3.connect(ctx.runtime.db_path) as db:
        db.execute(
            "INSERT INTO conversations "
            "(id, owner_id, title, agent, provider_id, model, created_at, "
            "updated_at, resumable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                conversation_id,
                owner_id,
                "issue 160 工具事件",
                "pi",
                "provider-issue-160",
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
            (conversation_id, "issue 160", now, now),
        )
        turn_id = cursor.lastrowid
    return conversation_id, int(turn_id)


def _record(ctx, turn_id: int, seq: int, event) -> None:
    from semantica_dataagent.conversation_api import _record_event

    previous = os.environ.get("DATAAGENT_DB_PATH")
    os.environ["DATAAGENT_DB_PATH"] = str(ctx.runtime.db_path)
    try:
        _record_event(turn_id, seq, event)
    finally:
        if previous is None:
            os.environ.pop("DATAAGENT_DB_PATH", None)
        else:
            os.environ["DATAAGENT_DB_PATH"] = previous


def check_validate_sql_reports_partition_details(ctx):
    missing = ctx.mcp_call(
        "validate_sql", {"sql": MISSING_PARTITION, "dialect": "hive"}
    )
    ctx.require(missing.get("status") == "issues", f"missing partition: {missing}")
    partition = next(
        (item for item in missing.get("issues", []) if item.get("kind") == "partition"),
        None,
    )
    ctx.require(partition, f"no partition issue detail: {missing}")
    ctx.require(partition.get("table") == "shop.orders", partition)
    ctx.require(partition.get("field") == "dt", partition)
    ctx.require("缺少分区条件" in str(partition.get("message", "")), partition)
    ctx.require("MAX(dt)" in str(partition.get("suggestion", "")), partition)

    valid = ctx.mcp_call("validate_sql", {"sql": VALID_PARTITION, "dialect": "hive"})
    ctx.require(valid.get("status") == "pass", f"valid partition: {valid}")
    ctx.require(valid.get("issues") == [], f"passing SQL has issues: {valid}")
    return {"kind": partition["kind"], "field": partition["field"]}


def check_propose_query_invalid_input_is_readable(ctx):
    rejected = ctx.mcp_call(
        "propose_query", {"sql": MISSING_PARTITION}, allow_error=True
    )
    body = rejected.structuredContent or {}
    ctx.require(rejected.isError, f"propose_query unexpectedly succeeded: {body}")
    ctx.require(body.get("code") == "invalid_input", body)
    ctx.require("缺少分区条件" in str(body.get("message", "")), body)

    missing = ctx.mcp_call("propose_query", {}, allow_error=True)
    body = missing.structuredContent or {}
    ctx.require(missing.isError, f"missing sql unexpectedly succeeded: {body}")
    ctx.require(body.get("code") == "invalid_input", body)
    ctx.require("sql" in str(body.get("message", "")), body)
    return {"code": "invalid_input"}


def check_tool_events_store_input_and_issue_details(ctx):
    from semantica_dataagent.pi_runner import PiEventParser

    result = ctx.mcp_call("validate_sql", {"sql": MISSING_PARTITION}, raw=True)
    structured = getattr(result, "structuredContent", None)
    ctx.require(isinstance(structured, dict), f"unexpected MCP result: {result!r}")
    parser = PiEventParser()
    start = parser.feed(
        {
            "type": "tool_execution_start",
            "toolCallId": "call-issue-160",
            "toolName": "mcp__dataagent__validate_sql",
            "args": {"sql": MISSING_PARTITION, "dialect": "hive"},
        }
    )[0]
    end = parser.feed(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-issue-160",
            "toolName": "mcp__dataagent__validate_sql",
            "isError": False,
            "result": {
                "content": [{"type": "text", "text": json.dumps(structured)}],
                "structuredContent": structured,
            },
        }
    )[0]
    ctx.require(start.type == "tool_start" and end.type == "tool_end", (start, end))
    ctx.require(MISSING_PARTITION in str(start.data.get("input", "")), start.data)
    ctx.require("status=issues" in str(end.data.get("summary", "")), end.data)
    ctx.require("缺少分区条件" in str(end.data.get("summary", "")), end.data)

    owner_id = _owner_id(ctx)
    conversation_id, turn_id = _add_turn(ctx, owner_id)
    _record(ctx, turn_id, 0, start)
    _record(ctx, turn_id, 1, end)
    response = ctx.rest.get(f"/api/v1/conversations/{conversation_id}")
    ctx.require(response.status_code == 200, response.text[:500])
    events = response.json()["turns"][0]["events"]
    stored_start = next(item for item in events if item["type"] == "tool_start")
    stored_end = next(item for item in events if item["type"] == "tool_end")
    ctx.require(MISSING_PARTITION in str(stored_start["data"].get("input", "")), events)
    ctx.require("缺少分区条件" in str(stored_end["data"].get("summary", "")), events)
    return {"events": [item["type"] for item in events]}
