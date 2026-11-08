"""Issue #165 end-to-end checks: real runner summaries and snapshot guidance."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime

_TIMESTAMP = "%Y-%m-%d %H:%M:%S.%f"
MISSING_PARTITION = "SELECT id FROM shop.orders"


def _ends(events, tool: str):
    return [
        event
        for event in events
        if event.type == "tool_end" and event.data["name"].endswith(tool)
    ]


def _require_partition_summary(ctx, summary: str, label: str) -> None:
    ctx.require("status=issues" in summary, f"{label}: no status in {summary!r}")
    ctx.require("缺少分区条件" in summary, f"{label}: no issue detail in {summary!r}")
    ctx.require("MAX(dt)" in summary, f"{label}: no suggestion in {summary!r}")
    ctx.require("unknown" not in summary, f"{label}: fell back to unknown")


def _require_propose_summary(ctx, summary: str, label: str) -> None:
    ctx.require("invalid_input" in summary, f"{label}: no error code in {summary!r}")
    ctx.require(
        "SQL validation did not pass" in summary,
        f"{label}: no message in {summary!r}",
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
                "issue 165 工具事件",
                "pi",
                "provider-issue-165",
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
            (conversation_id, "issue 165", now, now),
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


def check_skill_distinguishes_snapshot_and_incremental(ctx):
    response = ctx.rest.get("/api/v1/skill")
    ctx.require(response.status_code == 200, response.text[:500])
    text = response.text
    normalized = " ".join(text.split())
    ctx.require(
        "INSERT OVERWRITE" in text, "skill does not mention full overwrite tasks"
    )
    for phrase in (
        "treat the table as a snapshot",
        "never the partition column of a snapshot table",
        "business time column",
    ):
        ctx.require(phrase in normalized, f"skill is missing: {phrase}")
    return {"skill_bytes": len(text.encode())}


def check_partition_suggestion_covers_snapshot_and_business_time(ctx):
    result = ctx.mcp_call("validate_sql", {"sql": MISSING_PARTITION, "dialect": "hive"})
    ctx.require(result.get("status") == "issues", f"validate_sql: {result}")
    partition = next(
        (item for item in result.get("issues", []) if item.get("kind") == "partition"),
        None,
    )
    ctx.require(partition, f"no partition issue: {result}")
    suggestion = str(partition.get("suggestion", ""))
    ctx.require("不确定时按快照" in suggestion, suggestion)
    ctx.require("业务时间" in suggestion, suggestion)
    return {"suggestion": suggestion}


def check_real_runner_tool_summaries_keep_details(ctx):
    from semantica_dataagent.claude_runner import ClaudeEventParser
    from semantica_dataagent.codex_runner import CodexEventParser
    from semantica_dataagent.pi_runner import PiEventParser

    validation = ctx.mcp_call(
        "validate_sql", {"sql": MISSING_PARTITION, "dialect": "hive"}
    )
    ctx.require(validation.get("status") == "issues", f"validate_sql: {validation}")
    text = json.dumps(validation, ensure_ascii=False)

    pi_parser = PiEventParser()
    pi_events = pi_parser.feed(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-issue-165",
            "toolName": "mcp__dataagent__validate_sql",
            "isError": False,
            "result": {
                "content": [{"type": "text", "text": text}],
                "details": {"server": "dataagent", "tool": "validate_sql"},
                "structuredContent": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": validation,
                    "isError": False,
                },
            },
        }
    )
    pi_end = _ends(pi_events, "validate_sql")[0]
    _require_partition_summary(ctx, pi_end.data["summary"], "pi")

    claude_parser = ClaudeEventParser()
    claude_parser.feed(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-issue-165",
                        "name": "mcp__dataagent__validate_sql",
                        "input": {"sql": MISSING_PARTITION, "dialect": "hive"},
                    }
                ]
            },
        }
    )
    claude_events = claude_parser.feed(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-issue-165",
                        "content": [{"type": "text", "text": text}],
                        "is_error": False,
                    }
                ]
            },
        }
    )
    claude_end = _ends(claude_events, "validate_sql")[0]
    _require_partition_summary(ctx, claude_end.data["summary"], "claude")

    codex_parser = CodexEventParser()
    codex_events = codex_parser.feed(
        {
            "type": "item.completed",
            "item": {
                "id": "item-issue-165",
                "type": "mcp_tool_call",
                "server": "dataagent",
                "tool": "validate_sql",
                "arguments": {"sql": MISSING_PARTITION, "dialect": "hive"},
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structured_content": validation,
                },
                "error": None,
                "status": "completed",
            },
        }
    )
    codex_end = _ends(codex_events, "validate_sql")[0]
    _require_partition_summary(ctx, codex_end.data["summary"], "codex")

    rejected = ctx.mcp_call(
        "propose_query", {"sql": MISSING_PARTITION}, allow_error=True
    )
    ctx.require(rejected.isError, f"propose_query unexpectedly passed: {rejected}")
    error_body = rejected.structuredContent or {}
    ctx.require(error_body.get("code") == "invalid_input", error_body)
    error_text = json.dumps(error_body, ensure_ascii=False)

    pi_error = PiEventParser().feed(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-issue-165-error",
            "toolName": "mcp__dataagent__propose_query",
            "isError": True,
            "result": {
                "content": [{"type": "text", "text": error_text}],
                "details": {"server": "dataagent", "tool": "propose_query"},
                "structuredContent": {
                    "content": [{"type": "text", "text": error_text}],
                    "structuredContent": error_body,
                    "isError": True,
                },
            },
        }
    )
    _require_propose_summary(
        ctx, _ends(pi_error, "propose_query")[0].data["summary"], "pi propose"
    )

    claude_parser = ClaudeEventParser()
    claude_parser.feed(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-issue-165-error",
                        "name": "mcp__dataagent__propose_query",
                        "input": {"sql": MISSING_PARTITION},
                    }
                ]
            },
        }
    )
    claude_error = claude_parser.feed(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-issue-165-error",
                        "content": error_text,
                        "is_error": True,
                    }
                ]
            },
        }
    )
    _require_propose_summary(
        ctx, _ends(claude_error, "propose_query")[0].data["summary"], "claude propose"
    )

    codex_error = CodexEventParser().feed(
        {
            "type": "item.completed",
            "item": {
                "id": "item-issue-165-error",
                "type": "mcp_tool_call",
                "server": "dataagent",
                "tool": "propose_query",
                "arguments": {"sql": MISSING_PARTITION},
                "result": {
                    "content": [{"type": "text", "text": error_text}],
                    "structured_content": error_body,
                },
                "error": None,
                "status": "failed",
            },
        }
    )
    _require_propose_summary(
        ctx, _ends(codex_error, "propose_query")[0].data["summary"], "codex propose"
    )

    owner_id = _owner_id(ctx)
    conversation_id, turn_id = _add_turn(ctx, owner_id)
    _record(ctx, turn_id, 0, pi_end)
    response = ctx.rest.get(f"/api/v1/conversations/{conversation_id}")
    ctx.require(response.status_code == 200, response.text[:500])
    stored = response.json()["turns"][0]["events"]
    stored_end = next(item for item in stored if item["type"] == "tool_end")
    _require_partition_summary(
        ctx, str(stored_end["data"].get("summary", "")), "stored pi"
    )
    return {"conversation_id": conversation_id}


def check_unparseable_tool_result_degrades_to_raw_text(ctx):
    from semantica_dataagent.agent_plugin import EVENT_DETAIL_LIMIT
    from semantica_dataagent.pi_runner import PiEventParser

    fragment = (
        '{"status": "issues", "issues": [{"kind": "partition", "message": "缺少分区条件'
    )
    events = PiEventParser().feed(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-issue-165-fragment",
            "toolName": "mcp__dataagent__validate_sql",
            "isError": False,
            "result": {
                "content": [{"type": "text", "text": fragment}],
                "details": {"server": "dataagent", "tool": "validate_sql"},
                "structuredContent": {},
            },
        }
    )
    summary = _ends(events, "validate_sql")[0].data["summary"]
    ctx.require("unknown" not in summary, f"fell back to unknown: {summary!r}")
    ctx.require("缺少分区条件" in summary, f"raw text missing: {summary!r}")
    ctx.require(len(summary) <= EVENT_DETAIL_LIMIT, f"summary too long: {len(summary)}")
    return {"summary_length": len(summary)}


def check_field_level_ambiguity_uses_retrieved_field_refs(ctx):
    response = ctx.rest.get("/api/v1/skill")
    ctx.require(response.status_code == 200, response.text[:500])
    normalized = " ".join(response.text.split())
    for phrase in (
        "Field-level ambiguity",
        "two or more fields",
        "field:db.table.column",
        "one option per candidate field",
        "at most one ambiguous word per turn",
        "do not write SQL or call `propose_query` in that turn",
        "different representations of the same field",
        "Do not clarify when",
    ):
        ctx.require(phrase in normalized, f"skill is missing: {phrase}")

    hits = ctx.mcp_call(
        "search_knowledge",
        {"query": "操作系统", "scope": "fields", "top_k": 10},
    )
    refs = {hit.get("ref") for hit in hits}
    expected = {
        "field:shop.channels.os_type",
        "field:shop.channels.os_name",
    }
    ctx.require(expected <= refs, f"field refs missing from retrieval: {sorted(refs)}")

    result = ctx.mcp_call(
        "request_clarification",
        {
            "questions": [
                {
                    "question": "“操作系统”指哪个字段？",
                    "options": [
                        {
                            "label": "os_type（操作系统代码）",
                            "ref": "field:shop.channels.os_type",
                        },
                        {
                            "label": "os_name（操作系统名称）",
                            "ref": "field:shop.channels.os_name",
                        },
                    ],
                }
            ]
        },
    )
    ctx.require(result.get("status") == "pending", result)
    selected = [option["ref"] for option in result["questions"][0]["options"]]
    ctx.require(
        selected == ["field:shop.channels.os_type", "field:shop.channels.os_name"],
        f"field refs were not preserved: {selected}",
    )

    rejected = ctx.mcp_call(
        "request_clarification",
        {
            "questions": [
                {
                    "question": "“操作系统”指哪个字段？",
                    "options": [
                        {
                            "label": "os_type（操作系统代码）",
                            "ref": "field:shop.channels.os_type",
                        },
                        {
                            "label": "不存在的字段",
                            "ref": "field:shop.channels.os_missing",
                        },
                    ],
                }
            ]
        },
        allow_error=True,
    )
    ctx.require(rejected.isError, f"fabricated field ref was accepted: {rejected}")
    body = rejected.structuredContent or {}
    ctx.require(body.get("code") == "invalid_input", body)
    return {"field_refs": sorted(refs & expected)}
