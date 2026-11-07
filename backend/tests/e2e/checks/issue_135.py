"""Check signed downloads and conversation deletion over REST and MCP."""

from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx


def check_signed_downloads(ctx):
    spec = importlib.util.spec_from_file_location(
        "issue_82_checks", Path(__file__).with_name("issue_82.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_local_mcp_query(ctx)


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


def _add_conversation(ctx, owner_id: str, title: str, *, running: bool = False) -> str:
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
                "provider-issue-135",
                "test-model",
                now,
                now,
            ),
        )
        if running:
            db.execute(
                "INSERT INTO turns "
                "(conversation_id, sequence, question, status, answer, sql, "
                "citations, error, started_at, finished_at) "
                "VALUES (?, 1, ?, 'running', '', '[]', '[]', NULL, ?, NULL)",
                (conversation_id, title, now),
            )
    return conversation_id


def _remove_turn(ctx, conversation_id: str) -> None:
    with sqlite3.connect(ctx.runtime.db_path) as db:
        db.execute("DELETE FROM turns WHERE conversation_id = ?", (conversation_id,))


def _listed_ids(ctx) -> set[str]:
    response = ctx.rest.get("/api/v1/conversations")
    ctx.require(response.status_code == 200, response.text)
    return {row["id"] for row in response.json()}


def check_delete_conversation(ctx):
    owner_id = _owner_id(ctx)
    conversation_id = _add_conversation(ctx, owner_id, "issue 135 删除验证")
    try:
        ctx.require(
            conversation_id in _listed_ids(ctx),
            "newly inserted conversation is missing from the list",
        )
        response = ctx.rest.delete(f"/api/v1/conversations/{conversation_id}")
        ctx.require(
            response.status_code == 204,
            f"owner delete returned {response.status_code}: {response.text}",
        )
        ctx.require(
            conversation_id not in _listed_ids(ctx),
            "deleted conversation still appears in the list",
        )
        again = ctx.rest.delete(f"/api/v1/conversations/{conversation_id}")
        ctx.require(
            again.status_code == 404,
            f"second delete returned {again.status_code}: {again.text}",
        )
    finally:
        ctx.rest.delete(f"/api/v1/conversations/{conversation_id}")
    return {"deleted": conversation_id}


def check_delete_other_owner(ctx):
    owner_id = _owner_id(ctx)
    conversation_id = _add_conversation(ctx, owner_id, "issue 135 他人会话")
    issued = ctx.rest.post(
        "/api/v1/tokens", json={"name": "issue-135-other", "role": "user"}
    )
    ctx.require(issued.status_code == 201, issued.text)
    other = httpx.Client(
        base_url=str(ctx.rest.base_url),
        headers={"Authorization": f"Bearer {issued.json()['token']}"},
        timeout=60,
        trust_env=False,
    )
    try:
        response = other.delete(f"/api/v1/conversations/{conversation_id}")
        ctx.require(
            response.status_code == 404,
            f"other owner delete returned {response.status_code}: {response.text}",
        )
        ctx.require(
            conversation_id in _listed_ids(ctx),
            "another owner's delete removed the conversation",
        )
    finally:
        other.close()
        ctx.rest.delete(f"/api/v1/conversations/{conversation_id}")
    return {"conversation": conversation_id}


def check_delete_running_conversation(ctx):
    owner_id = _owner_id(ctx)
    conversation_id = _add_conversation(
        ctx, owner_id, "issue 135 运行中会话", running=True
    )
    try:
        response = ctx.rest.delete(f"/api/v1/conversations/{conversation_id}")
        ctx.require(
            response.status_code == 409,
            f"running conversation delete returned {response.status_code}: "
            f"{response.text}",
        )
        ctx.require(
            conversation_id in _listed_ids(ctx),
            "running conversation was removed despite the 409",
        )
    finally:
        _remove_turn(ctx, conversation_id)
        ctx.rest.delete(f"/api/v1/conversations/{conversation_id}")
    return {"conversation": conversation_id}
